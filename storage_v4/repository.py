"""單局原子提交；盒子存在性與稀疏珠數分開保存。

保留目前模型 RNG、version、revision、update_count。逐手決策只在記憶體中
留到終局獎懲，交易失敗時連同新盒與 RNG 一起回滾。
"""
from contextlib import contextmanager
import json
from pathlib import Path
from random import Random
import sqlite3
import time

from uttt.engine import State, legal_moves, play, RULE_VERSION
from uttt.menace import choose, ModelConsistencyError
from uttt.pilot import game_seed
from uttt.service import dumps, now, rng_from, ALGORITHM_VERSION
from uttt.symmetry import canonicalize, action_groups
from .codec import pack

VERSION = 4
CURRENT = ('rng_state', 'version', 'trained_games', 'update_count', 'revision')
SCHEMA = '''
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE states(id INTEGER PRIMARY KEY, key BLOB NOT NULL UNIQUE CHECK(length(key)=27));
CREATE TABLE models(
 id INTEGER PRIMARY KEY CHECK(id IN (1,2)), alias TEXT NOT NULL UNIQUE,
 original_id TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL,
 rng_state TEXT NOT NULL, version INTEGER NOT NULL, trained_games INTEGER NOT NULL,
 update_count INTEGER NOT NULL, revision INTEGER NOT NULL, box_count INTEGER NOT NULL);
CREATE TABLE boxes(
 model INTEGER NOT NULL REFERENCES models(id), state INTEGER NOT NULL REFERENCES states(id),
 created_game INTEGER, created_ply INTEGER,
 PRIMARY KEY(model,state)) WITHOUT ROWID;
CREATE TABLE weights(
 model INTEGER NOT NULL, state INTEGER NOT NULL, action INTEGER NOT NULL,
 weight INTEGER NOT NULL CHECK(weight>=0), updates INTEGER NOT NULL CHECK(updates>=0),
 PRIMARY KEY(model,state,action), FOREIGN KEY(model,state) REFERENCES boxes(model,state),
 CHECK(action/10 BETWEEN 1 AND 9 AND action%10 BETWEEN 1 AND 9),
 CHECK(NOT(weight=3 AND updates=0))) WITHOUT ROWID;
CREATE TABLE summaries(
 game INTEGER PRIMARY KEY, x_model INTEGER NOT NULL REFERENCES models(id),
 result TEXT NOT NULL CHECK(result IN ('X','O','draw')), plies INTEGER NOT NULL,
 new_c INTEGER NOT NULL, new_d INTEGER NOT NULL, seed INTEGER NOT NULL,
 elapsed_ms INTEGER, ended_at TEXT);
CREATE TABLE evaluations(
 version INTEGER NOT NULL, model INTEGER NOT NULL REFERENCES models(id),
 seed TEXT NOT NULL, games INTEGER NOT NULL, wins INTEGER NOT NULL, draws INTEGER NOT NULL,
 losses INTEGER NOT NULL, detail_json TEXT NOT NULL, PRIMARY KEY(version,model)) WITHOUT ROWID;
'''


class SlimService:
    def __init__(self, path, *, read_only=False, create=False):
        self.path = Path(path).resolve()
        if create:
            if self.path.exists():
                raise FileExistsError(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.conn = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if read_only else '?mode=rwc'),
                                    uri=True, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        if create:
            self.conn.executescript(SCHEMA)
            self.conn.execute('PRAGMA user_version=4')
        if self.conn.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            self.close()
            raise ValueError('此入口僅接受精簡 v4，不能直接開啟 v3。')
        if read_only:
            self.conn.execute('PRAGMA query_only=ON')
        else:
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.execute('PRAGMA cache_size=-32768')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def meta(self, key, default=None):
        row = self.conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.conn.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (key, dumps(value)))

    def models(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM models ORDER BY id')]

    def legacy_model(self, model):
        row = dict(self.conn.execute('SELECT * FROM models WHERE id=?', (model,)).fetchone())
        result = json.loads(row['metadata_json'])
        result.update({k: row[k] for k in CURRENT})
        return result

    def head(self):
        rows = self.models()
        if len(rows) != 2 or [r['id'] for r in rows] != [1, 2]:
            raise ValueError('需要固定的 C／D 兩個模型。')
        if rows[0]['version'] != rows[1]['version'] or any(r['version'] != r['trained_games'] for r in rows):
            raise ValueError('兩模型的完成局數不一致。')
        return rows[0]['version']

    def check_head(self):
        version = self.head()
        count, maximum = self.conn.execute('SELECT COUNT(*),COALESCE(MAX(game),0) FROM summaries').fetchone()
        if (count, maximum) != (version, version):
            raise ValueError('逐局摘要與模型版本不一致。')
        for model in self.models():
            original = self.legacy_model(model['id'])
            if original['rule_version'] != RULE_VERSION or original['algorithm_version'] != ALGORITHM_VERSION:
                raise ValueError('規則或演算法版本不符。')
            rng_from(model['rng_state'])
        return version

    def _box_weights(self, model, canonical, groups, *, game=None, ply=None):
        # 呼叫者已取得合法集合；此處再防禦終局誤建盒。
        if not legal_moves(canonical) or not groups:
            raise ModelConsistencyError('終局不可讀取行動盒子。')
        row = self.conn.execute('SELECT id FROM states WHERE key=?', (pack(canonical),)).fetchone()
        if row is None:
            if game is None:
                return None, None, False
            sid = self.conn.execute('INSERT INTO states(key) VALUES(?)', (pack(canonical),)).lastrowid
        else:
            sid = row[0]
        exists = self.conn.execute('SELECT 1 FROM boxes WHERE model=? AND state=?', (model, sid)).fetchone()
        created = False
        if not exists:
            if game is None:
                return sid, None, False
            self.conn.execute('INSERT INTO boxes VALUES(?,?,?,?)', (model, sid, game, ply))
            created = True
        weights = {rep: 3 for rep in groups}
        for r in self.conn.execute('SELECT action,weight,updates FROM weights WHERE model=? AND state=?', (model, sid)):
            if r['action'] not in groups or type(r['weight']) is not int or r['weight'] < 0 or r['updates'] < 0:
                raise ModelConsistencyError('稀疏權重包含非法動作或無效珠數。')
            weights[r['action']] = r['weight']
        return sid, weights, created

    def train_one(self, *, capture=False, on_move=None):
        started = time.perf_counter()
        with self.transaction():
            before = self.head()
            game = before + 1
            config = self.meta('config')
            if game > config['target']:
                raise ValueError('已達訓練目標。')
            model_rows = {r['id']: r for r in self.models()}
            rngs = {m: rng_from(r['rng_state']) for m, r in model_rows.items()}
            x_model = 1 if before % 2 == 0 else 2
            actors = {'X': x_model, 'O': 3 - x_model}
            decisions, trace = [], []
            new = {1: 0, 2: 0}
            played = {1: 0, 2: 0}
            state = State()
            while state.status == 'active':
                legal = legal_moves(state)
                if not legal:
                    raise ModelConsistencyError('進行中局面沒有合法動作。')
                canonical, _ = canonicalize(state)
                groups = action_groups(canonical)
                model = actors[state.turn]
                sid, weights, created = self._box_weights(model, canonical, groups, game=game, ply=state.ply + 1)
                choice = choose(state, weights, rngs[model], training=True)
                if choice.move not in legal:
                    raise ModelConsistencyError('模型轉回原方向後的動作不合法。')
                after = play(state, choice.move)
                if canonicalize(play(canonical, choice.group_rep))[0] != canonicalize(after)[0]:
                    raise ModelConsistencyError('同構動作轉移不一致。')
                decisions.append((model, sid, choice.group_rep))
                new[model] += int(created)
                played[model] += 1
                if capture:
                    trace.append({'move': choice.move, 'canonical_move': choice.canonical_move,
                                  'group_rep': choice.group_rep, 'branch': choice.branch,
                                  'weight': choice.weight_before_choice, 'probability': choice.probability})
                state = after
                if on_move:
                    on_move(state)
            for model, sid, rep in decisions:
                actor = 'X' if model == x_model else 'O'
                delta = 1 if state.status == 'draw' else (3 if state.status == actor else -1)
                row = self.conn.execute('SELECT weight,updates FROM weights WHERE model=? AND state=? AND action=?',
                                        (model, sid, rep)).fetchone()
                old, updates = (row[0], row[1]) if row else (3, 0)
                weight = max(0, old + delta)
                self.conn.execute('INSERT INTO weights VALUES(?,?,?,?,?) ON CONFLICT(model,state,action) '
                                  'DO UPDATE SET weight=excluded.weight,updates=excluded.updates',
                                  (model, sid, rep, weight, updates + 1))
            for model in (1, 2):
                self.conn.execute('UPDATE models SET rng_state=?,version=version+1,trained_games=trained_games+1,'
                                  'update_count=update_count+?,revision=revision+?,box_count=box_count+? WHERE id=?',
                                  (dumps(rngs[model].getstate()), played[model], played[model] + 1, new[model], model))
            seed = game_seed(config['training_seed'], before)
            self.conn.execute('INSERT INTO summaries VALUES(?,?,?,?,?,?,?,?,?)',
                              (game, x_model, state.status, state.ply, new[1], new[2], seed,
                               round((time.perf_counter() - started) * 1000), now()))
        return {'game': game, 'result': state.status, 'plies': state.ply, 'new_c': new[1], 'new_d': new[2],
                **({'moves': trace} if capture else {})}

    def evaluate(self, model, *, games=200, seed=20261025):
        if type(games) is not int or games < 2 or games % 2:
            raise ValueError('評估局數須為正偶數。')
        # 獨立唯讀連線固定整段評估快照，不建立任何盒子或消耗訓練 RNG。
        results = {'wins': 0, 'draws': 0, 'losses': 0, 'plies': 0, 'unseen_moves': 0,
                   'as_x': {'wins': 0, 'draws': 0, 'losses': 0},
                   'as_o': {'wins': 0, 'draws': 0, 'losses': 0}}
        with SlimService(self.path, read_only=True) as frozen:
            frozen.conn.execute('BEGIN')
            version = frozen.head()
            if not frozen.conn.execute('SELECT 1 FROM models WHERE id=?', (model,)).fetchone():
                raise ValueError('找不到評估模型。')
            for index in range(games):
                rng, state = Random(game_seed(seed, index)), State()
                actor = 'X' if index % 2 == 0 else 'O'
                while state.status == 'active':
                    legal = legal_moves(state)
                    if state.turn == actor:
                        canonical, _ = canonicalize(state)
                        _, weights, _ = frozen._box_weights(model, canonical, action_groups(canonical))
                        results['unseen_moves'] += int(weights is None)
                        move = choose(state, weights, rng, training=False).move
                    else:
                        move = rng.choice(legal)
                    state = play(state, move)
                result = 'draws' if state.status == 'draw' else ('wins' if state.status == actor else 'losses')
                results[result] += 1
                results['as_x' if actor == 'X' else 'as_o'][result] += 1
                results['plies'] += state.ply
            frozen.conn.rollback()
        results.update(version=version, model=model, games=games, seed=seed,
                       opponent='uniform-legal-v1', epsilon=0, weighted_selection=True,
                       mean_plies=results['plies'] / games)
        return results

    def save_evaluation(self, result):
        with self.transaction():
            if self.head() != result['version']:
                raise ValueError('模型版本已改變，拒絕將評估附到不同版本。')
            self.conn.execute('INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)',
                              (result['version'], result['model'], str(result['seed']), result['games'],
                               result['wins'], result['draws'], result['losses'], dumps(result)))
