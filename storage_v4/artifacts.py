"""有界記憶體的 SQLite 備份、壓縮里程碑與還原。"""
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from tools.continue_selfplay import RESERVE_GAME, file_hash, save_json
from uttt.service import now
from .repository import SlimService


def validate_database(path, expected_version=None):
    with SlimService(path, read_only=True) as service:
        version = service.check_head()
        if expected_version is not None and version != expected_version:
            raise ValueError('備份模型版本不符。')
        if service.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('資料庫快速完整性檢查失敗。')
        return version


def backup_latest(service, directory, budget):
    if service.conn.in_transaction:
        raise ValueError('不能備份未提交的交易。')
    directory = Path(directory).resolve()
    target = directory / 'latest-backup.sqlite3'
    partial = directory / ('.backup-' + uuid.uuid4().hex + '.sqlite3')
    version = service.head()
    size = service.conn.execute('PRAGMA page_count').fetchone()[0] * service.conn.execute('PRAGMA page_size').fetchone()[0]
    budget.check(size + RESERVE_GAME)
    try:
        with closing(sqlite3.connect(partial)) as target_conn:
            service.conn.backup(target_conn, pages=4096,
                progress=lambda status, remaining, total: budget.check(remaining * 4096 + RESERVE_GAME))
        validate_database(partial, version)
        sha = file_hash(partial)
        record = {'version': version, 'created_at': now(), 'sha256': sha, 'bytes': partial.stat().st_size,
                  'method': 'sqlite-backup-api', 'verified': True}
        partial.replace(target)
        save_json(directory / 'latest-backup.json', record)
        return record
    finally:
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(partial) + suffix)
            if candidate.exists():
                candidate.unlink()


def archive_milestone(backup, directory, version, budget):
    directory, backup = Path(directory).resolve(), Path(backup).resolve()
    target = directory / f'model-{version:07d}.sqlite3.gz'
    manifest = target.with_suffix(target.suffix + '.json')
    if target.exists() or manifest.exists():
        if not target.is_file() or not manifest.is_file():
            raise ValueError('里程碑檔案與驗證紀錄不完整，保留現場供檢查。')
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if record['version'] != version or file_hash(target) != record['compressed_sha256']:
            raise ValueError('既有里程碑校驗失敗。')
        return record
    validate_database(backup, version)
    size = backup.stat().st_size
    budget.check(size + RESERVE_GAME)
    partial = directory / ('.archive-' + uuid.uuid4().hex + '.gz')
    digest, copied = hashlib.sha256(), 0
    try:
        with backup.open('rb') as source, partial.open('xb') as output:
            with gzip.GzipFile(filename='', mode='wb', compresslevel=6, fileobj=output, mtime=0) as compressed:
                while chunk := source.read(1024 * 1024):
                    compressed.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                    if copied % (16 * 1024 * 1024) == 0:
                        budget.check(max(0, size - copied) + RESERVE_GAME)
        with gzip.open(partial, 'rb') as verify:
            actual = hashlib.file_digest(verify, 'sha256').hexdigest()
        if actual != digest.hexdigest():
            raise ValueError('里程碑解壓校驗失敗。')
        record = {'version': version, 'created_at': now(), 'uncompressed_sha256': actual,
                  'compressed_sha256': file_hash(partial), 'uncompressed_bytes': copied,
                  'compressed_bytes': partial.stat().st_size, 'includes': 'both-models+summaries+evaluations',
                  'arbitrary_history': False}
        partial.rename(target)
        save_json(manifest, record)
        return record
    finally:
        if partial.exists():
            partial.unlink()


def restore_archive(archive, destination, budget):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    record = json.loads(archive.with_suffix(archive.suffix + '.json').read_text(encoding='utf-8'))
    if file_hash(archive) != record['compressed_sha256']:
        raise ValueError('壓縮檔校驗不符。')
    budget.check(record['uncompressed_bytes'] + RESERVE_GAME)
    partial = destination.parent / ('.restore-' + uuid.uuid4().hex + '.sqlite3')
    digest, copied = hashlib.sha256(), 0
    try:
        with gzip.open(archive, 'rb') as source, partial.open('xb') as target:
            while chunk := source.read(1024 * 1024):
                copied += len(chunk)
                if copied > record['uncompressed_bytes']:
                    raise ValueError('解壓內容超出已驗證大小。')
                target.write(chunk)
                digest.update(chunk)
                budget.check(record['uncompressed_bytes'] - copied + RESERVE_GAME)
        if copied != record['uncompressed_bytes'] or digest.hexdigest() != record['uncompressed_sha256']:
            raise ValueError('解壓內容校驗不符。')
        validate_database(partial, record['version'])
        partial.rename(destination)
        return record
    finally:
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(partial) + suffix)
            if candidate.exists():
                candidate.unlink()
