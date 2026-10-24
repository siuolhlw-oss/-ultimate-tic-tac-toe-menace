"""保留原始試驗，以 SQLite 備份副本追加固定局數；不分析策略或勝率。"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uttt.engine import replay, state_to_dict
from uttt.pilot import game_seed, runner_lock, check_consistency
from uttt.service import ResearchService, dumps, now, runtime_info
from uttt import stream_export


SCHEMA = """
CREATE TABLE IF NOT EXISTS continuation_experiments (
 id INTEGER PRIMARY KEY CHECK(id=1), format_version INTEGER NOT NULL,
 source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL, base_snapshot_sha256 TEXT NOT NULL,
 source_pilot_json TEXT NOT NULL, base_version INTEGER NOT NULL,
 additional_games INTEGER NOT NULL, target_version INTEGER NOT NULL, batch_size INTEGER NOT NULL,
 seed TEXT NOT NULL, model_a TEXT NOT NULL REFERENCES models(model_id),
 model_b TEXT NOT NULL REFERENCES models(model_id),
 initial_checkpoint_a TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
 initial_checkpoint_b TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
 initial_revision_a INTEGER NOT NULL, initial_revision_b INTEGER NOT NULL,
 script_sha256 TEXT NOT NULL, runtime_json TEXT NOT NULL,
 budget_bytes INTEGER NOT NULL, min_free_bytes INTEGER NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS continuation_batches (
 batch_index INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 start_index INTEGER NOT NULL, requested_games INTEGER NOT NULL,
 status TEXT NOT NULL, checkpoint_a TEXT REFERENCES checkpoints(checkpoint_id),
 checkpoint_b TEXT REFERENCES checkpoints(checkpoint_id), completed_at TEXT
);
"""
EXTRA_TABLES = ('continuation_experiments', 'continuation_batches')
RESERVE_GAME = 64 * 1024 ** 2


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def script_hash():
    return file_hash(__file__)


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class StorageBudget:
    """計入資料夾內所有檔案，並在預計寫入之外保留最低磁碟空間。"""
    def __init__(self, directory, budget_bytes=50_000_000_000, min_free_bytes=50_000_000_000):
        self.directory = Path(directory).resolve()
        self.budget_bytes = int(budget_bytes)
        self.min_free_bytes = int(min_free_bytes)
        if self.budget_bytes <= 0 or self.min_free_bytes < 0:
            raise ValueError('儲存預算須為正值，最低磁碟空間不可為負值。')

    def used(self):
        return sum(p.stat().st_size for p in self.directory.rglob('*') if p.is_file())

    def check(self, reserve_bytes=0):
        used = self.used()
        free = shutil.disk_usage(self.directory).free
        if used + reserve_bytes > self.budget_bytes:
            raise OSError(f'資料夾容量預算不足：已用 {used} bytes，下一步預留 {reserve_bytes} bytes；進度已保留。')
        if free - reserve_bytes < self.min_free_bytes:
            raise OSError(f'磁碟空間不足：剩餘 {free} bytes，最低保留 {self.min_free_bytes} bytes；進度已保留。')
        return {'used_bytes': used, 'free_bytes': free}


def validate_runtime(original):
    current = runtime_info()
    for key in ('source_sha256', 'python', 'sqlite', 'schema_version', 'rule_version',
                'algorithm_version', 'rng', 'epsilon_train', 'initial_weight', 'reward'):
        if original.get(key) != current.get(key):
            raise ValueError(f'程式或環境已改變（{key}），不能直接接續原試驗。')


def _initialize_copy(service, source_path, source_sha, snapshot_sha, additional, batch_size, guard):
    pilot = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    if not pilot or pilot['status'] != 'completed':
        raise ValueError('來源必須是已完成的初始自我對弈試驗。')
    validate_runtime(json.loads(pilot['runtime_json']))
    if batch_size != pilot['batch_size']:
        raise ValueError('本次續訓須保留原試驗的檢查點批次大小。')
    if check_consistency(service, pilot) != pilot['total_games']:
        raise ValueError('來源試驗的完成局數不符。')
    final_batch = service.repo.one('SELECT * FROM pilot_batches ORDER BY batch_index DESC LIMIT 1')
    a, b = pilot['model_a'], pilot['model_b']
    # 沿用副本內的相同模型與 RNG，不另建或還原成新模型。
    fields = {
        'id': 1, 'format_version': 1, 'source_path': str(source_path), 'source_sha256': source_sha,
        'base_snapshot_sha256': snapshot_sha, 'source_pilot_json': dumps(pilot),
        'base_version': pilot['total_games'], 'additional_games': additional,
        'target_version': pilot['total_games'] + additional, 'batch_size': batch_size,
        'seed': pilot['seed'], 'model_a': a, 'model_b': b,
        'initial_checkpoint_a': final_batch['checkpoint_a'],
        'initial_checkpoint_b': final_batch['checkpoint_b'],
        'initial_revision_a': service._model(a)['revision'],
        'initial_revision_b': service._model(b)['revision'],
        'script_sha256': script_hash(), 'runtime_json': dumps(runtime_info()),
        'budget_bytes': guard.budget_bytes, 'min_free_bytes': guard.min_free_bytes,
        'status': 'active', 'created_at': now(), 'completed_at': None,
    }
    service.conn.executescript(SCHEMA)
    with service.repo.transaction():
        service.conn.execute('INSERT INTO continuation_experiments (' + ','.join(fields)
                             + ') VALUES (' + ','.join(':' + k for k in fields) + ')', fields)


def prepare_branch(source_path, directory, additional=5000, batch_size=100, guard=None):
    """來源唯讀；備份與來源資訊完成後才發布新資料庫。呼叫端須持有 runner_lock。"""
    if type(additional) is not int or additional < 2 or additional % 2:
        raise ValueError('追加局數須為正偶數。')
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('批次大小須為正整數。')
    source_path, directory = Path(source_path).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / 'training.sqlite3'
    if source_path == destination or source_path.parent == directory:
        raise ValueError('續訓資料夾必須與來源分開。')
    guard = guard or StorageBudget(directory)
    if destination.exists():
        # 只接受本腳本已原子發布的續訓資料，不把其他資料庫當成未初始化副本。
        with sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='continuation_experiments'").fetchone():
                raise ValueError('目的資料庫已有其他內容，拒絕覆寫或重新初始化。')
        return destination
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    partial = directory / ('base-' + uuid.uuid4().hex + '.partial.sqlite3')
    try:
        source_sha = file_hash(source_path)
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
            source.execute('BEGIN')
            if source.execute("SELECT 1 FROM sqlite_master WHERE name='continuation_experiments'").fetchone():
                raise ValueError('此版本僅接受原始 pilot 作為來源。')
            page_bytes = source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0]
            guard.check(page_bytes + RESERVE_GAME)
            with closing(sqlite3.connect(partial)) as target:
                source.backup(target, pages=1024,
                              progress=lambda status, remaining, total: guard.check(remaining * 4096 + RESERVE_GAME))
            source.rollback()
        if file_hash(source_path) != source_sha:
            raise ValueError('備份期間來源主檔有變更，請先停止來源寫入再重試。')
        snapshot_sha = file_hash(partial)
        with ResearchService(partial) as service:
            _initialize_copy(service, source_path, source_sha, snapshot_sha, additional, batch_size, guard)
        stream_export._publish_without_overwrite(partial, destination)
        return destination
    finally:
        # 只移除本次 UUID 暫存檔；既有來源、目的及中斷訓練檔永不移除。
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(partial) + suffix)
            if candidate.exists():
                candidate.unlink()


def validate_configuration(service, source_path, additional, batch_size, guard):
    item = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
    if not item:
        raise ValueError('缺少續訓來源資訊。')
    expected = (str(Path(source_path).resolve()), additional, batch_size, guard.budget_bytes, guard.min_free_bytes)
    actual = tuple(item[k] for k in ('source_path', 'additional_games', 'batch_size', 'budget_bytes', 'min_free_bytes'))
    if actual != expected or item['script_sha256'] != script_hash():
        raise ValueError('續跑須使用相同來源、局數、批次、容量設定及續訓腳本。')
    validate_runtime(json.loads(item['runtime_json']))
    pilot = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    if dumps(pilot) != item['source_pilot_json']:
        raise ValueError('原始 pilot metadata 有變更。')
    validate_runtime(json.loads(pilot['runtime_json']))
    return item


def check_branch(service, item):
    a, b = item['model_a'], item['model_b']
    active = []
    additional_done = 0
    for table, offset, target, arm in (
        ('pilot_batches', 0, item['base_version'], 'self_play_pilot'),
        ('continuation_batches', item['base_version'], item['additional_games'], 'self_play_continuation'),
    ):
        batches = service.repo.all('SELECT * FROM ' + table + ' ORDER BY batch_index')
        finished_total = 0
        for i, batch in enumerate(batches):
            start = offset + i * item['batch_size']
            requested = min(item['batch_size'], offset + target - start)
            if (requested < 1 or batch['batch_index'] != i or batch['start_index'] != start
                    or batch['requested_games'] != requested or batch['status'] not in ('active', 'completed')):
                raise ValueError('批次索引、局數或狀態不一致。')
            run = service.repo.one('SELECT * FROM runs WHERE run_id=?', (batch['run_id'],))
            if not run or run['mode'] != 'train' or run['arm'] != arm:
                raise ValueError('批次實驗對應不一致。')
            games = service.repo.all('SELECT * FROM games WHERE run_id=? ORDER BY x_version', (batch['run_id'],))
            finished = [g for g in games if g['status'] == 'finished']
            unfinished = [g for g in games if g['status'] == 'active']
            if (len(finished) != run['completed_games'] or len(games) > requested
                    or len(unfinished) > 1 or len(games) != len(finished) + len(unfinished)):
                raise ValueError('完成局數、未完局或中止紀錄不一致。')
            for j, game in enumerate(games):
                index = start + j
                players = (a, b) if index % 2 == 0 else (b, a)
                if ((game['x_player'], game['o_player']) != players or game['x_version'] != index
                        or game['o_version'] != index or game['mode'] != 'train' or game['is_example']
                        or game['seed'] != str(game_seed(int(item['seed']), index))
                        or bool(game['learning_applied']) != (game['status'] == 'finished')):
                    raise ValueError('棋譜全域局次、種子、行動方或學習標記不一致。')
            if unfinished and games[-1]['status'] != 'active':
                raise ValueError('未完成局後出現其他對局。')
            if batch['status'] == 'completed':
                if len(finished) != requested or run['status'] != 'completed':
                    raise ValueError('完成批次的局數不符。')
                for model, field in ((a, 'checkpoint_a'), (b, 'checkpoint_b')):
                    cp = service.repo.one('SELECT model_id,version FROM checkpoints WHERE checkpoint_id=?', (batch[field],))
                    if not cp or cp['model_id'] != model or cp['version'] != start + requested:
                        raise ValueError('批次檢查點不符。')
            elif i != len(batches) - 1 or table == 'pilot_batches':
                raise ValueError('已完成的先前批次被改變。')
            finished_total += len(finished)
            active.extend(unfinished)
        if table == 'pilot_batches' and finished_total != target:
            raise ValueError('原始 pilot 的已完成局數有變更。')
        if table == 'continuation_batches':
            additional_done = finished_total
    if service.repo.one("SELECT 1 FROM games WHERE mode='train' AND run_id NOT IN "
                        '(SELECT run_id FROM pilot_batches UNION SELECT run_id FROM continuation_batches)'):
        raise ValueError('專用資料庫混入其他訓練。')
    if len(active) > 1 or additional_done > item['additional_games']:
        raise ValueError('續訓超額或未完局不唯一。')
    complete = item['base_version'] + additional_done
    for model_id in (a, b):
        model = service._model(model_id)
        if model['version'] != complete or model['trained_games'] != complete:
            raise ValueError('模型版本與全域完成局数不符。')
        lock = service.repo.one('SELECT game_id FROM model_locks WHERE model_id=?', (model_id,))
        if (lock['game_id'] if lock else None) != (active[0]['game_id'] if active else None):
            raise ValueError('模型占用與未完局不一致。')
    if item['status'] == 'completed' and (additional_done != item['additional_games'] or active):
        raise ValueError('續訓完成標記不符。')
    return additional_done


def checkpoint_headroom(service):
    # state_key JSON 轉義、每筆權重及盒子標記皆採保守上界；同時計入 DB、WAL 及頁面餘裕。
    states = service.conn.execute('SELECT COALESCE(SUM(LENGTH(state_key)),0),COUNT(*) FROM model_states').fetchone()
    weights = service.conn.execute('SELECT COUNT(*) FROM weights').fetchone()[0]
    payload_bound = 2 * states[0] + 256 * states[1] + 160 * weights + RESERVE_GAME
    return 3 * payload_bound + RESERVE_GAME


def train_continuation(service, source_path, additional=5000, batch_size=100, guard=None, progress=None, event=None):
    guard = guard or StorageBudget(Path(service.repo.path).resolve().parent)
    item = validate_configuration(service, source_path, additional, batch_size, guard)
    done = check_branch(service, item)
    a, b = item['model_a'], item['model_b']
    if progress:
        progress(done, additional)
    for i, relative_start in enumerate(range(0, additional, batch_size)):
        start = item['base_version'] + relative_start
        requested = min(batch_size, additional - relative_start)
        batch = service.repo.one('SELECT * FROM continuation_batches WHERE batch_index=?', (i,))
        if batch and batch['status'] == 'completed':
            continue
        if batch is None:
            guard.check(RESERVE_GAME)
            with service.repo.transaction():
                run_id = service._new_run('train', 'self_play_continuation', game_seed(int(item['seed']), start),
                    {'model_a': a, 'model_b': b, 'requested_games': requested, 'starting_versions': [start, start],
                     'continuation_batch_index': i, 'global_start_index': start,
                     'base_version': item['base_version'], 'continuation_script_sha256': item['script_sha256']})
                service.conn.execute('INSERT INTO continuation_batches VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                                     (i, run_id, start, requested, 'active'))
            batch = service.repo.one('SELECT * FROM continuation_batches WHERE batch_index=?', (i,))
        run_id = batch['run_id']
        while True:
            count = service.repo.one('SELECT completed_games FROM runs WHERE run_id=?', (run_id,))['completed_games']
            if count == requested:
                break
            guard.check(RESERVE_GAME)
            index = start + count
            game = service.repo.one("SELECT * FROM games WHERE run_id=? AND status='active'", (run_id,))
            if game is None:
                players = (a, b) if index % 2 == 0 else (b, a)
                game = service.get_game(service.new_game(*players, mode='train', run_id=run_id,
                                                        seed=game_seed(int(item['seed']), index)))
            while game['status'] == 'active':
                game = service.step(game['game_id'])
                if event:
                    event('after_step', game)
            if event:
                event('after_game', game)
            guard.check()
            if progress:
                progress(index + 1 - item['base_version'], additional)
        if event:
            event('before_checkpoint', {'batch_index': i})
        guard.check(checkpoint_headroom(service))
        with service.repo.transaction():
            cp_a, cp_b = service._save_checkpoint(a), service._save_checkpoint(b)
            service.conn.execute("UPDATE continuation_batches SET status='completed',checkpoint_a=?,checkpoint_b=?,completed_at=? WHERE batch_index=?",
                                 (cp_a, cp_b, now(), i))
            service.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
            if relative_start + requested == additional:
                service.conn.execute("UPDATE continuation_experiments SET status='completed',completed_at=? WHERE id=1", (now(),))
        guard.check()
    item = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
    if check_branch(service, item) != additional or item['status'] != 'completed':
        raise ValueError('追加訓練尚未完整結束。')
    return {'base_games': item['base_version'], 'additional_games': additional, 'total_games': item['target_version'],
            'model_a': a, 'model_b': b, 'batch_size': batch_size, 'seed': int(item['seed']),
            'purpose': '固定局數續訓；未分析策略相似、收斂或勝率，未執行真人PK'}


def verify_continuation(service):
    item = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
    if not item or item['status'] != 'completed' or check_branch(service, item) != item['additional_games']:
        raise ValueError('續訓未完成。')
    total_moves = games = 0
    first = {item['model_a']: 0, item['model_b']: 0}
    for row in service.conn.execute('SELECT g.* FROM games g JOIN continuation_batches b USING(run_id) ORDER BY g.x_version'):
        game = dict(row)
        rows = service.repo.all('SELECT ply,move_code,actor,model_id FROM moves WHERE game_id=? ORDER BY ply', (game['game_id'],))
        if [r['ply'] for r in rows] != list(range(1, len(rows) + 1)):
            raise ValueError('棋譜手數不連續。')
        state = replay([r['move_code'] for r in rows])[-1]
        if state.status != game['result'] or state_to_dict(state) != json.loads(game['state_json']):
            raise ValueError('追加棋譜重播與保存結局不符。')
        updates = service.repo.all('SELECT * FROM learning_updates WHERE game_id=? ORDER BY ply', (game['game_id'],))
        if len(updates) != len(rows):
            raise ValueError('棋步與學習紀錄數不符。')
        for move, update in zip(rows, updates):
            delta = 1 if game['result'] == 'draw' else (3 if game['result'] == move['actor'] else -1)
            after = max(0, update['weight_before_update'] + delta)
            if (move['ply'] != update['ply'] or move['model_id'] != update['model_id']
                    or update['reward_delta'] != delta or update['weight_after_update'] != after
                    or update['applied_delta'] != after - update['weight_before_update']
                    or update['model_version_after'] != game['x_version'] + 1):
                raise ValueError('逐手終局獎懲或學習版本不符。')
        first[game['x_player']] += 1
        total_moves += len(rows)
        games += 1
    if games != item['additional_games'] or list(first.values()) != [games // 2, games // 2]:
        raise ValueError('追加局數或先後手不平衡。')
    for model_id in (item['model_a'], item['model_b']):
        model = service._model(model_id)
        events = service.conn.execute('SELECT COUNT(*) FROM model_events WHERE model_id=?', (model_id,)).fetchone()[0]
        updates = service.conn.execute('SELECT COUNT(*) FROM learning_updates WHERE model_id=?', (model_id,)).fetchone()[0]
        if model['revision'] != events or model['update_count'] != updates:
            raise ValueError('模型 revision 或更新次數與事件數不符。')
    if service.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or service.conn.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('SQLite 完整性檢查失敗。')
    return {'new_games_replayed': games, 'new_moves_verified': total_moves, 'first_player_counts': first,
            'sqlite_integrity': 'ok', 'learning_records_verified': True,
            'original_pilot_metadata_preserved': True, 'total_training_games': item['target_version']}


class _BudgetWriter:
    def __init__(self, output, guard):
        self.output, self.guard = output, guard
        self.pending = 0

    def __getattr__(self, name):
        return getattr(self.output, name)

    def write(self, data):
        self.pending += len(data)
        if self.pending >= 4 * 1024 ** 2:
            self.output.flush()
            self.guard.check(RESERVE_GAME + len(data))
            self.pending = 0
        return self.output.write(data)


def export_continuation(service, destination, guard):
    """同一讀取快照匯出所有原始表與續訓 metadata，不修改核心匯出器。"""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    partial = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    guard.check(RESERVE_GAME)
    try:
        with partial.open('xb') as output:
            with service.repo.snapshot():
                tables = stream_export.TABLES + stream_export.OPTIONAL_TABLES + EXTRA_TABLES
                counts = {table: service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in tables}
                manifest = {'exported_at': now(), 'format_version': 1, 'selected_run': None,
                            'runtime': runtime_info(), 'continuation_script_sha256': script_hash(),
                            'note': '原始pilot與追加訓練的完整資料、來源及批次對應；同一讀取快照逐列匯出。'}
                with zipfile.ZipFile(_BudgetWriter(output, guard), 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    stream_export._write_json(archive, service.conn, manifest, tables)
                    for table in tables:
                        if counts[table]:
                            stream_export._write_csv(archive, service.conn, table)
            output.flush()
            os.fsync(output.fileno())
        guard.check()
        digest, size = file_hash(partial), partial.stat().st_size
        stream_export._publish_without_overwrite(partial, destination)
        return {'path': str(destination.resolve()), 'bytes': size, 'sha256': digest, 'rows': counts,
                'exported_at': manifest['exported_at'], 'format_version': 1}
    finally:
        if partial.exists():
            partial.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('data/selfplay-1000/training.sqlite3'))
    parser.add_argument('--directory', type=Path, default=Path('data/selfplay-6000'))
    parser.add_argument('--additional-games', type=int, default=5000)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--budget-gb', type=float, default=50, help='十進位GB；包含本資料夾DB、WAL、匯出及暫存')
    parser.add_argument('--min-free-gb', type=float, default=50, help='每一步預留寫入後，磁碟仍須保留的十進位GB')
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    guard = StorageBudget(directory, int(args.budget_gb * 10 ** 9), int(args.min_free_gb * 10 ** 9))
    started = time.perf_counter()
    try:
        with runner_lock(directory / 'runner.lock'):
            db = prepare_branch(args.source, directory, args.additional_games, args.batch_size, guard)
            report_path = directory / 'completion.json'
            with ResearchService(db) as service:
                def progress(done, total):
                    if done % 25 == 0 or done == total:
                        print(f'已追加 {done}/{total} 局；本次執行 {time.perf_counter() - started:.1f} 秒', flush=True)

                trained = train_continuation(service, args.source, args.additional_games, args.batch_size,
                                             guard, progress=progress)
                item = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
                if report_path.exists():
                    previous = json.loads(report_path.read_text(encoding='utf-8'))
                    archive = previous['export']
                    if (previous['training'] != trained or previous['continuation_script_sha256'] != script_hash()
                            or previous['source']['base_snapshot_sha256'] != item['base_snapshot_sha256']
                            or not Path(archive['path']).is_file() or file_hash(archive['path']) != archive['sha256']):
                        raise ValueError('既有完成報告或匯出內容不一致；不覆寫，請保留資料檢查。')
                    print(f'追加訓練已完成，未重複學習。報告：{report_path}', flush=True)
                    return 0
                print('正在逐局重播並核對追加棋譜與獎懲紀錄…', flush=True)
                verified = verify_continuation(service)
                print('正在匯出完整原始資料與續訓來源紀錄…', flush=True)
                archive = export_continuation(service, directory / f'continuation-records-{datetime.now():%Y%m%d-%H%M%S-%f}.zip', guard)
                report = {'created_at': now(), 'runtime': runtime_info(), 'continuation_script_sha256': script_hash(),
                          'training': trained, 'verification': verified, 'export': archive,
                          'source': {k: item[k] for k in ('source_path', 'source_sha256', 'base_snapshot_sha256',
                                     'base_version', 'initial_checkpoint_a', 'initial_checkpoint_b',
                                     'initial_revision_a', 'initial_revision_b')},
                          'storage_budget_bytes': guard.budget_bytes, 'minimum_free_bytes': guard.min_free_bytes,
                          'analysis': '未分析策略相似、收斂或勝率，未執行真人PK',
                          'invocation_elapsed_seconds': round(time.perf_counter() - started, 2)}
            report['database_bytes'] = db.stat().st_size
            report['database_sha256'] = file_hash(db)
            report['storage'] = guard.check(RESERVE_GAME)
            save_json(report_path, report)
            print(json.dumps({'report': str(report_path), 'training': trained, 'verification': verified,
                              'database_bytes': report['database_bytes'], 'archive_bytes': archive['bytes']},
                             ensure_ascii=False, indent=2), flush=True)
        return 0
    except KeyboardInterrupt:
        print('已保留已提交的每一步，請以相同命令續跑。', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'執行未完成：{exc}\n資料保留於 {directory}，不會重建或清除已發布的資料庫。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
