"""用 SQLite 備份快照建立新檔，逐表驗證所有 v2 原值。"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from uttt.stream_export import _publish_without_overwrite
from .repository import CompactResearchService, STRUCTURE, STORAGE_VERSION

OPTIONAL_TABLES = ('pilot_experiments', 'pilot_batches', 'continuation_experiments', 'continuation_batches')


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def ordered_query(connection, table):
    columns = [tuple(r) for r in connection.execute('PRAGMA table_info(' + table + ')')]
    keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
    # 視圖的 PRAGMA 不列主鍵；沿用 v2 契約的順序。
    if table in STRUCTURE:
        keys = [r[1] for r in sorted(STRUCTURE[table], key=lambda r: r[5]) if r[5]]
    return 'SELECT * FROM ' + table + (' ORDER BY ' + ','.join(keys) if keys else '')


def hash_row(digest, row):
    # 陣列 JSON 與換行有明確型別／列界線，不把不同欄位串接成相同字串。
    digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False).encode('utf-8'))
    digest.update(b'\n')


def table_digest(connection, table):
    digest, count = hashlib.sha256(), 0
    for row in connection.execute(ordered_query(connection, table)):
        hash_row(digest, row)
        count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def validate_source(connection):
    if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise ValueError('遷移來源須為本專案 v2 資料庫。')
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    allowed = set(STRUCTURE) | set(OPTIONAL_TABLES)
    if set(STRUCTURE) - tables or tables - allowed:
        raise ValueError('來源資料表不符；拒絕漏存未知資料表。')
    for table, expected in STRUCTURE.items():
        actual = [tuple(r) for r in connection.execute('PRAGMA table_info(' + table + ')')]
        if actual != expected:
            raise ValueError('來源欄位結構不相容：' + table)
    return list(STRUCTURE) + [t for t in OPTIONAL_TABLES if t in tables]


def migrate(source_path, destination, progress=None, guard=None):
    """原子發佈已驗證的副本；從不覆寫來源、既有目的或報告。

    備份 API 將來源的已提交資料（包含 WAL）固定成自足快照。
    只刪除本次 UUID 暫存檔；報告保留快照雜湊與逐表原值雜湊。
    """
    source_path, destination = Path(source_path).resolve(), Path(destination).resolve()
    report_path = destination.with_suffix(destination.suffix + '.migration.json')
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path == destination or destination.exists() or report_path.exists():
        raise FileExistsError('來源／目的不可相同，目的及報告不能覆寫。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    snapshot = destination.parent / ('.snapshot-' + token + '.sqlite3')
    partial = destination.parent / ('.compact-' + token + '.sqlite3')
    partial_report = destination.parent / ('.report-' + token + '.json')
    started = time.perf_counter()
    published = False
    try:
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
            source.execute('BEGIN')
            validate_source(source)
            snapshot_bound = source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0]
            if guard:
                guard.check(snapshot_bound + 64 * 1024 ** 2)
            if progress:
                progress('backup', None)
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target, pages=1024,
                              progress=(lambda status, remaining, total: guard.check(remaining * 4096 + 64 * 1024 ** 2)) if guard else None)
            source.rollback()
        snapshot_sha = file_hash(snapshot)
        with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as source:
            tables = validate_source(source)
            observed = {}
            with CompactResearchService(partial) as service:
                for table in tables:
                    if table not in STRUCTURE:
                        sql = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                        service.conn.executescript(sql + ';')
                # 一個交易：池、索引、所有資料與來源資訊同時完成。
                with service.repo.transaction():
                    for table in tables:
                        cursor = source.execute(ordered_query(source, table))
                        columns = [d[0] for d in cursor.description]
                        sql = 'INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')'
                        digest, count = hashlib.sha256(), 0
                        # 快照單筆可能很大，採逐筆；其他資料只保留一個小批次。
                        batch_size = 1 if table == 'checkpoints' else 256
                        while rows := cursor.fetchmany(batch_size):
                            if guard:
                                guard.check(64 * 1024 ** 2)
                            for row in rows:
                                hash_row(digest, row)
                                if table == 'checkpoints':
                                    values = dict(zip(columns, row))
                                    if hashlib.sha256(values['payload_json'].encode('utf-8')).hexdigest() != values['sha256']:
                                        raise ValueError('來源檢查點校驗失敗。')
                            service.conn.executemany(sql, rows)
                            count += len(rows)
                        observed[table] = {'rows': count, 'sha256': digest.hexdigest()}
                        if progress:
                            progress('copied:' + table, count)
                    lineage = {'source_path': str(source_path), 'snapshot_sha256': snapshot_sha,
                               'snapshot_bytes': snapshot.stat().st_size,
                               'source_logical_digests': observed}
                    service.conn.execute('INSERT INTO storage_metadata VALUES(?,?)',
                                         ('migration', json.dumps(lineage, ensure_ascii=False, sort_keys=True)))
                for table in tables:
                    actual = table_digest(service.conn, table)
                    if actual != observed[table]:
                        raise ValueError('遷移後原值核對不符：' + table)
                    if progress:
                        progress('verified:' + table, actual['rows'])
                if service.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('新資料庫完整性檢查失敗。')
                if service.conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('新資料庫外鍵檢查失敗。')
                models = [dict(r) for r in service.conn.execute('SELECT model_id,name,version,revision,trained_games FROM models')]
                pooled = {table: service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                          for table in ('_states', '_rng_states', '_rng_bodies')}
            compact_bytes = partial.stat().st_size
            if guard:
                guard.check()
            report = {'created_at': datetime.now(timezone.utc).isoformat(), 'storage_version': STORAGE_VERSION,
                      'destination': str(destination), 'source': lineage, 'models': models,
                      'verification': {'all_logical_tables_equal': True, 'sqlite_integrity': 'ok',
                                       'foreign_keys': 'ok', 'tables': observed}, 'pool_rows': pooled,
                      'compact_bytes': compact_bytes, 'compact_sha256': file_hash(partial),
                      'saved_bytes': snapshot.stat().st_size - compact_bytes,
                      'saved_fraction': 1 - compact_bytes / snapshot.stat().st_size,
                      'elapsed_seconds': round(time.perf_counter() - started, 2),
                      'scope': '同一備份快照的全表原值核對；不表示訓練收斂或新增獨立實驗。'}
        partial_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        _publish_without_overwrite(partial, destination)
        published = True
        _publish_without_overwrite(partial_report, report_path)
        return report
    finally:
        for base in (snapshot, partial, partial_report):
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(base) + suffix)
                # 僅本次由目的資料夾產生的 UUID 檔案，沒有遞迴刪除。
                if candidate.parent != destination.parent:
                    raise RuntimeError('暫存清理路徑越界。')
                if candidate.exists():
                    candidate.unlink()
        if published and not report_path.exists() and progress:
            progress('report-publication-failed:database-preserved', None)
