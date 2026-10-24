"""整數局面索引、共用 MT19937 主體、壓縮快照與相容 SQL 視圖。

公開視圖保持 v2 欄位與文字原值；物理格式為 v3。只改表示方式，
ResearchService 的合法性、抽選、事件與學習流程直接沿用原核心。
"""
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import struct
import zlib

from uttt.repository import Repository, SCHEMA
from uttt.service import ResearchService, dumps

STORAGE_VERSION = 3
STATE_COLUMNS = {
    'state_edges': {'from_state_key': 'from_state_id', 'to_state_key': 'to_state_id'},
    'model_states': {'state_key': 'state_id'},
    'weights': {'state_key': 'state_id'},
    'moves': {'state_key': 'state_id', 'next_state_key': 'next_state_id'},
    'learning_updates': {'state_key': 'state_id'},
}
RNG_COLUMNS = {
    'models': {'rng_state': 'rng_id'}, 'games': {'rng_state': 'rng_id'},
    'moves': {'rng_before': 'rng_before_id', 'rng_after': 'rng_after_id'},
}
TRANSFORMED = {'states', 'checkpoints', *STATE_COLUMNS, *RNG_COLUMNS}

POOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_metadata(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _states(
 state_id INTEGER PRIMARY KEY,state_key TEXT NOT NULL UNIQUE,
 ply INTEGER NOT NULL,status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _rng_bodies(
 body_id INTEGER PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,payload_zlib BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS _rng_states(
 rng_id INTEGER PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,
 body_id INTEGER REFERENCES _rng_bodies(body_id),version INTEGER,position INTEGER,
 gaussian_json TEXT,raw_zlib BLOB,
 CHECK((body_id IS NOT NULL AND raw_zlib IS NULL AND position BETWEEN 0 AND 624)
       OR (body_id IS NULL AND raw_zlib IS NOT NULL))
);
"""


def physical_table(name):
    return '_' + name if name in TRANSFORMED else name


def physical_references(sql):
    for table in TRANSFORMED:
        sql = re.sub(r'\bREFERENCES\s+' + table + r'\b', 'REFERENCES _' + table, sql)
    return sql


def legacy_structure():
    with sqlite3.connect(':memory:') as connection:
        connection.executescript(SCHEMA)
        return {r[0]: [tuple(c) for c in connection.execute('PRAGMA table_info(' + r[0] + ')')]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


STRUCTURE = legacy_structure()


def transformed_column(table, column):
    if column in STATE_COLUMNS.get(table, {}):
        return STATE_COLUMNS[table][column]
    if column in RNG_COLUMNS.get(table, {}):
        return RNG_COLUMNS[table][column]
    if table == 'checkpoints' and column == 'payload_json':
        return 'payload_zlib'
    return column


def write_expression(table, column, prefix, default=None):
    value = prefix + '.' + column
    if default is not None:
        value = 'COALESCE(' + value + ',' + default + ')'
    if column in STATE_COLUMNS.get(table, {}):
        return '(SELECT state_id FROM _states WHERE state_key=' + value + ')'
    if column in RNG_COLUMNS.get(table, {}):
        return 'store_rng(' + value + ')'
    if table == 'checkpoints' and column == 'payload_json':
        return 'pack_text(' + value + ')'
    return value


def schema_sql():
    statements = [POOL_SCHEMA]
    for sql in SCHEMA.split(';'):
        sql = sql.strip()
        if not sql:
            continue
        match = re.match(r'CREATE TABLE IF NOT EXISTS (\w+)', sql)
        if match:
            table = match[1]
            if table == 'states':
                continue
            sql = sql.replace('CREATE TABLE IF NOT EXISTS ' + table,
                              'CREATE TABLE IF NOT EXISTS ' + physical_table(table), 1)
            for old, new in STATE_COLUMNS.get(table, {}).items():
                sql = re.sub(r'\b' + old + r'\b', new, sql)
                sql = re.sub(r'\b' + new + r'\s+TEXT', new + ' INTEGER', sql)
            for old, new in RNG_COLUMNS.get(table, {}).items():
                sql = re.sub(r'\b' + old + r'\s+TEXT(?:\s+NOT NULL)?',
                             new + ' INTEGER' + (' NOT NULL' if table != 'moves' else '')
                             + ' REFERENCES _rng_states(rng_id)', sql)
            if table == 'checkpoints':
                sql = sql.replace('payload_json TEXT', 'payload_zlib BLOB')
            sql = physical_references(sql)
            # 指向局面表的外鍵也改成整數主鍵。
            sql = sql.replace('REFERENCES _states(state_key)', 'REFERENCES _states(state_id)')
            sql = sql.replace('REFERENCES _state_edges(from_state_key,',
                              'REFERENCES _state_edges(from_state_id,')
        else:
            for table in TRANSFORMED:
                sql = re.sub(r'\bON\s+' + table + r'\b', 'ON _' + table, sql)
            # 舊顯式索引只有 moves_state 使用局面文字。
            sql = re.sub(r'\bstate_key\b', 'state_id', sql)
        statements.append(sql + ';')

    for table in sorted(TRANSFORMED):
        columns, joins = [], []
        for _, column, _, _, _, _ in STRUCTURE[table]:
            if column in STATE_COLUMNS.get(table, {}):
                alias = 's_' + column
                joins.append('LEFT JOIN _states ' + alias + ' ON ' + alias
                             + '.state_id=t.' + transformed_column(table, column))
                expression = alias + '.state_key'
            elif column in RNG_COLUMNS.get(table, {}):
                alias = 'r_' + column
                body = 'b_' + column
                joins.append('LEFT JOIN _rng_states ' + alias + ' ON ' + alias
                             + '.rng_id=t.' + transformed_column(table, column))
                joins.append('LEFT JOIN _rng_bodies ' + body + ' ON ' + body
                             + '.body_id=' + alias + '.body_id')
                expression = ('restore_rng(' + body + '.payload_zlib,' + alias + '.version,'
                              + alias + '.position,' + alias + '.gaussian_json,'
                              + alias + '.raw_zlib,' + alias + '.sha256)')
            elif table == 'checkpoints' and column == 'payload_json':
                expression = 'unpack_text(t.payload_zlib)'
            else:
                expression = 't.' + column
            columns.append(expression + ' AS ' + column)
        statements.append('CREATE VIEW IF NOT EXISTS ' + table + ' AS SELECT '
                          + ','.join(columns) + ' FROM ' + physical_table(table)
                          + ' t ' + ' '.join(joins) + ';')
        names = [c[1] for c in STRUCTURE[table]]
        insert_values = [write_expression(table, c[1], 'NEW', c[4]) for c in STRUCTURE[table]]
        physical_names = [transformed_column(table, c) for c in names]
        predicate = ' AND '.join(transformed_column(table, c[1]) + ' IS '
                                 + write_expression(table, c[1], 'OLD')
                                 for c in STRUCTURE[table] if c[5])
        statements.append('CREATE TRIGGER IF NOT EXISTS ' + table + '_insert INSTEAD OF INSERT ON '
                          + table + ' BEGIN INSERT INTO ' + physical_table(table) + '('
                          + ','.join(physical_names) + ') VALUES(' + ','.join(insert_values) + '); END;')
        statements.append('CREATE TRIGGER IF NOT EXISTS ' + table + '_update INSTEAD OF UPDATE ON '
                          + table + ' BEGIN UPDATE ' + physical_table(table) + ' SET '
                          + ','.join(transformed_column(table, c) + '=' + write_expression(table, c, 'NEW')
                                     for c in names) + ' WHERE ' + predicate + '; END;')
        statements.append('CREATE TRIGGER IF NOT EXISTS ' + table + '_delete INSTEAD OF DELETE ON '
                          + table + ' BEGIN DELETE FROM ' + physical_table(table)
                          + ' WHERE ' + predicate + '; END;')
    return '\n'.join(statements)


COMPACT_SCHEMA = schema_sql()


@lru_cache(maxsize=256)
def body_text(payload):
    raw = zlib.decompress(payload)
    if len(raw) != 2496:
        raise ValueError('亂數主體長度不符。')
    return ','.join(map(str, struct.unpack('<624I', raw)))


def restore_rng(body, version, position, gaussian, raw, sha256):
    if sha256 is None:
        return None
    if raw is not None:
        text = zlib.decompress(raw).decode('utf-8')
    else:
        text = '[' + str(version) + ',[' + body_text(body) + ',' + str(position) + '],' + gaussian + ']'
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != sha256:
        raise ValueError('亂數狀態校驗失敗。')
    return text


class CompactConnection(sqlite3.Connection):
    def executescript(self, sql):
        # 核心的 pilot/continuation 建表語句仍指向實體外鍵，避免引用視圖。
        return super().executescript(physical_references(sql))


def register_functions(connection):
    def store_rng(text):
        if text is None:
            return None
        raw = text.encode('utf-8')
        digest = hashlib.sha256(raw).hexdigest()
        row = connection.execute('SELECT rng_id FROM _rng_states WHERE sha256=?', (digest,)).fetchone()
        if row:
            return row[0]
        state = json.loads(text)
        canonical = dumps(state) == text
        supported = (canonical and isinstance(state, list) and len(state) == 3
                     and type(state[0]) is int and state[0] == 3
                     and isinstance(state[1], list) and len(state[1]) == 625
                     and all(type(n) is int and 0 <= n <= 0xffffffff for n in state[1][:-1])
                     and type(state[1][-1]) is int and 0 <= state[1][-1] <= 624)
        if supported:
            body = struct.pack('<624I', *state[1][:-1])
            body_hash = hashlib.sha256(body).hexdigest()
            connection.execute('INSERT OR IGNORE INTO _rng_bodies(sha256,payload_zlib) VALUES(?,?)',
                               (body_hash, zlib.compress(body)))
            body_id = connection.execute('SELECT body_id FROM _rng_bodies WHERE sha256=?', (body_hash,)).fetchone()[0]
            connection.execute('INSERT INTO _rng_states(sha256,body_id,version,position,gaussian_json) VALUES(?,?,?,?,?)',
                               (digest, body_id, state[0], state[1][-1], dumps(state[2])))
        else:
            # 其他亂數表示及不同空白格式保留完整原文，不做有損正規化。
            connection.execute('INSERT INTO _rng_states(sha256,raw_zlib) VALUES(?,?)', (digest, zlib.compress(raw)))
        return connection.execute('SELECT rng_id FROM _rng_states WHERE sha256=?', (digest,)).fetchone()[0]

    connection.create_function('store_rng', 1, store_rng)
    connection.create_function('restore_rng', 6, restore_rng, deterministic=True)
    connection.create_function('pack_text', 1, lambda s: zlib.compress(s.encode('utf-8')), deterministic=True)
    connection.create_function('unpack_text', 1, lambda b: zlib.decompress(b).decode('utf-8'), deterministic=True)


class CompactRepository(Repository):
    def __init__(self, path, read_only=False):
        self.path = str(path)
        if read_only:
            uri = Path(path).resolve().as_uri() + '?mode=ro'
            self.conn = sqlite3.connect(uri, uri=True, timeout=20, isolation_level=None, factory=CompactConnection)
        else:
            if self.path != ':memory:':
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=20, isolation_level=None, factory=CompactConnection)
        self.conn.row_factory = sqlite3.Row
        register_functions(self.conn)
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in ((STORAGE_VERSION,) if read_only else (0, STORAGE_VERSION)):
            self.conn.close()
            raise ValueError('此入口只接受 v3 資料庫；v2 請先用遷移工具建立副本。')
        if read_only:
            self.conn.execute('PRAGMA query_only=ON')
            return
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript(COMPACT_SCHEMA)
        self.conn.execute('PRAGMA user_version=3')
        self.conn.execute('INSERT OR IGNORE INTO storage_metadata VALUES(?,?)',
                          ('format', dumps({'storage_version': 3, 'logical_schema_version': 2,
                                            'codec': 'state-id+mt-body+zlib-v1'})))


class CompactResearchService(ResearchService):
    def __init__(self, db_path, read_only=False):
        self.repo = CompactRepository(db_path, read_only=read_only)
        self.conn = self.repo.conn
        self._checkpoint_cache = {}
