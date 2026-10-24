"""以單一 SQLite 快照逐列匯出，避免把整個研究資料庫載入記憶體。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import uuid
import zipfile

from .service import now, runtime_info


TABLES = (
    'runs', 'models', 'states', 'state_edges', 'model_states', 'weights',
    'checkpoints', 'games', 'moves', 'learning_updates', 'model_events',
)
OPTIONAL_TABLES = ('pilot_experiments', 'pilot_batches')


def _write_json(archive, connection, manifest, tables):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    with archive.open('research.json', 'w', force_zip64=True) as raw:
        with io.TextIOWrapper(raw, encoding='utf-8', newline='') as output:
            output.write('{"manifest":')
            for chunk in encoder.iterencode(manifest):
                output.write(chunk)
            for table in tables:
                output.write(',"' + table + '":[')
                cursor = connection.execute('SELECT * FROM ' + table)
                try:
                    first = True
                    for row in cursor:
                        if not first:
                            output.write(',')
                        first = False
                        # 只保留一列；單一檢查點本身仍可能是一筆較大的 JSON 字串。
                        for chunk in encoder.iterencode(dict(row)):
                            output.write(chunk)
                finally:
                    cursor.close()
                output.write(']')
            output.write(',"dependencies":{}}')


def _write_csv(archive, connection, table):
    columns = [row[1] for row in connection.execute('PRAGMA table_info(' + table + ')')]
    # 完整檢查點已保存在 research.json，CSV 查詢也不讀取這個大型欄位。
    if table == 'checkpoints':
        columns.remove('payload_json')
    cursor = connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + table)
    try:
        with archive.open(table + '.csv', 'w', force_zip64=True) as raw:
            with io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as output:
                writer = csv.writer(output)
                writer.writerow(columns)
                for row in cursor:
                    writer.writerow([
                        "'" + value if isinstance(value, str) and value.startswith(
                            ('=', '+', '-', '@', '\t', '\r')) else value
                        for value in row
                    ])
    finally:
        cursor.close()


def _publish_without_overwrite(partial, destination):
    if os.name == 'nt':
        # Windows rename 在目的檔已存在時失敗，不會覆蓋其他程序的產物。
        partial.rename(destination)
    else:
        # POSIX rename 會覆蓋；同目錄 hard link 可原子發佈且拒絕同名檔。
        os.link(partial, destination)
        partial.unlink()


def export_archive(service, destination: Path) -> dict:
    """匯出完整資料並回傳路徑、位元組數、SHA-256 及各表列數。

    與 ``ResearchService.export_data()`` 未篩選實驗時的 JSON/CSV 契約相同。
    既有目的檔永不覆寫；失敗只清除本次建立的暫存檔。記憶體用量主要
    取決於單列大小，尤其是既有 schema 的單筆 checkpoint payload。
    """
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('匯出檔已存在，請使用新檔名。')
    if service.conn.in_transaction:
        raise ValueError('請先完成目前交易，再建立匯出快照。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    created = False
    try:
        with partial.open('xb') as output:
            created = True
            with service.repo.snapshot():
                # 第一個查詢即固定讀取快照；JSON、CSV 和列數來自同一時點。
                existing = {row[0] for row in service.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                tables = TABLES + tuple(table for table in OPTIONAL_TABLES if table in existing)
                counts = {
                    table: service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                    for table in tables
                }
                manifest = {
                    'exported_at': now(), 'format_version': 1, 'selected_run': None,
                    'runtime': runtime_info(),
                    'note': '完整資料逐列匯出；CSV文字公式加單引號，原值見JSON。',
                }
                with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    _write_json(archive, service.conn, manifest, tables)
                    for table in tables:
                        if counts[table]:
                            _write_csv(archive, service.conn, table)
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256()
        with partial.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        size = partial.stat().st_size
        _publish_without_overwrite(partial, destination)
        return {
            'path': str(destination.resolve()), 'bytes': size, 'sha256': digest.hexdigest(),
            'rows': counts, 'format_version': 1, 'exported_at': manifest['exported_at'],
        }
    finally:
        if created and partial.exists():
            partial.unlink()
