"""SQLite 結構與交易。所有研究資料留在呼叫者指定的檔案。"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, arm TEXT NOT NULL,
 seed TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
 config_json TEXT NOT NULL, completed_games INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS models (
 model_id TEXT PRIMARY KEY, name TEXT NOT NULL, seed TEXT NOT NULL,
 rng_state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 trained_games INTEGER NOT NULL DEFAULT 0, update_count INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, parent_checkpoint_id TEXT, rule_version TEXT NOT NULL,
 algorithm_version TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS states (
 state_key TEXT PRIMARY KEY, ply INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_edges (
 from_state_key TEXT NOT NULL REFERENCES states(state_key),
 group_rep INTEGER NOT NULL, members_json TEXT NOT NULL, group_size INTEGER NOT NULL,
 to_state_key TEXT REFERENCES states(state_key),
 PRIMARY KEY(from_state_key, group_rep)
);
CREATE TABLE IF NOT EXISTS checkpoints (
 checkpoint_id TEXT PRIMARY KEY, model_id TEXT NOT NULL REFERENCES models(model_id),
 version INTEGER NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
 game_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
 mode TEXT NOT NULL, x_player TEXT NOT NULL, o_player TEXT NOT NULL,
 x_version INTEGER, o_version INTEGER, x_checkpoint_id TEXT, o_checkpoint_id TEXT,
 rule_version TEXT NOT NULL, algorithm_version TEXT NOT NULL,
 seed TEXT NOT NULL, rng_state TEXT NOT NULL, state_json TEXT NOT NULL,
 ply INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
 result TEXT NOT NULL DEFAULT 'active', end_reason TEXT,
 learning_applied INTEGER NOT NULL DEFAULT 0, is_example INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, ended_at TEXT, x_revision INTEGER, o_revision INTEGER
);
CREATE TABLE IF NOT EXISTS model_locks (
 model_id TEXT PRIMARY KEY REFERENCES models(model_id),
 game_id TEXT NOT NULL REFERENCES games(game_id)
);
CREATE TABLE IF NOT EXISTS model_states (
 model_id TEXT NOT NULL REFERENCES models(model_id),
 state_key TEXT NOT NULL REFERENCES states(state_key),
 created_game_id TEXT REFERENCES games(game_id), created_ply INTEGER,
 PRIMARY KEY(model_id, state_key)
);
CREATE TABLE IF NOT EXISTS weights (
 model_id TEXT NOT NULL, state_key TEXT NOT NULL, group_rep INTEGER NOT NULL,
 bead_weight INTEGER NOT NULL CHECK(bead_weight >= 0),
 update_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(model_id, state_key, group_rep),
 FOREIGN KEY(model_id, state_key) REFERENCES model_states(model_id, state_key),
 FOREIGN KEY(state_key, group_rep) REFERENCES state_edges(from_state_key, group_rep)
);
CREATE TABLE IF NOT EXISTS moves (
 game_id TEXT NOT NULL REFERENCES games(game_id), ply INTEGER NOT NULL,
 actor TEXT NOT NULL, model_id TEXT REFERENCES models(model_id),
 move_code INTEGER NOT NULL CHECK(move_code / 10 BETWEEN 1 AND 9 AND move_code % 10 BETWEEN 1 AND 9),
 forced_before INTEGER NOT NULL, forced_after INTEGER NOT NULL,
 state_key TEXT NOT NULL REFERENCES states(state_key),
 next_state_key TEXT NOT NULL REFERENCES states(state_key),
 transform INTEGER NOT NULL, canonical_move INTEGER NOT NULL, group_rep INTEGER NOT NULL,
 branch TEXT NOT NULL, probability REAL, weight_before_choice INTEGER,
 new_box INTEGER NOT NULL DEFAULT 0, rng_before TEXT, rng_after TEXT,
 PRIMARY KEY(game_id, ply)
);
CREATE TABLE IF NOT EXISTS learning_updates (
 game_id TEXT NOT NULL REFERENCES games(game_id), model_id TEXT NOT NULL REFERENCES models(model_id),
 ply INTEGER NOT NULL, state_key TEXT NOT NULL, group_rep INTEGER NOT NULL,
 weight_before_update INTEGER NOT NULL, reward_delta INTEGER NOT NULL,
 applied_delta INTEGER NOT NULL, weight_after_update INTEGER NOT NULL,
 model_version_after INTEGER NOT NULL,
 PRIMARY KEY(game_id, model_id, ply),
 FOREIGN KEY(game_id, ply) REFERENCES moves(game_id, ply)
);
CREATE TABLE IF NOT EXISTS model_events (
 model_id TEXT NOT NULL REFERENCES models(model_id), revision INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('move','finish')),
 game_id TEXT NOT NULL REFERENCES games(game_id), ply INTEGER,
 PRIMARY KEY(model_id, revision)
);
CREATE INDEX IF NOT EXISTS games_run ON games(run_id);
CREATE INDEX IF NOT EXISTS moves_state ON moves(state_key, group_rep, model_id);
CREATE INDEX IF NOT EXISTS states_depth ON states(ply);
CREATE INDEX IF NOT EXISTS checkpoints_model ON checkpoints(model_id, created_at);
"""


class Repository:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.conn.close()
            raise ValueError(f'不支援的資料庫版本：{version}；請使用相符程式。')
        self.conn.executescript(SCHEMA)
        self.conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    @contextmanager
    def transaction(self):
        """鎖定一次落子的寫入；錯誤時連同亂數與初始化一起還原。"""
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params)]

    @contextmanager
    def snapshot(self):
        """多次查詢使用同一唯讀快照，避免版本和珠數來自不同時點。"""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute('BEGIN')
        try:
            yield
        finally:
            self.conn.rollback()

    def one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def close(self):
        self.conn.close()
