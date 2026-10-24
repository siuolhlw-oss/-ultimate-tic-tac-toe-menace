"""從已完成的 v3 續訓副本再追加固定局數；保留歷代來源與中斷續跑。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_v3.repository import CompactResearchService
from uttt.engine import replay, state_to_dict
from uttt.pilot import game_seed, runner_lock
from uttt.service import dumps, now, runtime_info
from uttt import stream_export
from tools import continue_selfplay as previous
from tools.continue_selfplay import StorageBudget, RESERVE_GAME, save_json, file_hash, validate_runtime, checkpoint_headroom, _BudgetWriter
from tools.novelty_statistics import publish as publish_novelty

SCHEMA = previous.SCHEMA.replace('continuation_', 'extension_') + '''
CREATE TABLE IF NOT EXISTS extension_provenance(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
'''
EXTRA_TABLES = previous.EXTRA_TABLES + ('extension_experiments', 'extension_batches', 'extension_provenance')
PARENT_TABLES = ('pilot_experiments', 'pilot_batches', *previous.EXTRA_TABLES)


def script_hash():
    return file_hash(__file__)


def dependency_hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: file_hash(root / name) for name in (
        'tools/extend_compact_selfplay.py', 'tools/continue_selfplay.py', 'tools/novelty_statistics.py',
        'storage_v3/repository.py', 'uttt/pilot.py', 'uttt/stream_export.py')}


def lineage(service):
    return {table: service.repo.all('SELECT * FROM ' + table + ' ORDER BY 1') for table in PARENT_TABLES}


def prepare_branch(source_path, directory, additional=10000, batch_size=100, guard=None):
    if type(additional) is not int or additional < 2 or additional % 2:
        raise ValueError('追加局數須為正偶數。')
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError('批次大小須為正整數。')
    source_path, directory = Path(source_path).resolve(), Path(directory).resolve()
    if source_path.parent == directory:
        raise ValueError('本次續訓須使用獨立資料夾。')
    directory.mkdir(parents=True, exist_ok=True)
    guard = guard or StorageBudget(directory)
    destination = directory / 'training.sqlite3'
    if destination.exists():
        with CompactResearchService(destination, read_only=True) as service:
            validate_configuration(service, source_path, additional, batch_size, guard)
        return destination
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    partial = directory / ('base-' + uuid.uuid4().hex + '.partial.sqlite3')
    try:
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
            source.execute('BEGIN')
            if source.execute('PRAGMA user_version').fetchone()[0] != 3:
                raise ValueError('來源必須是 v3。')
            size = source.execute('PRAGMA page_count').fetchone()[0] * source.execute('PRAGMA page_size').fetchone()[0]
            guard.check(size + RESERVE_GAME)
            with closing(sqlite3.connect(partial)) as target:
                source.backup(target, pages=2048, progress=lambda status, remaining, total: guard.check(remaining * 4096 + RESERVE_GAME))
            source.rollback()
        snapshot_sha = file_hash(partial)
        with CompactResearchService(partial) as service:
            if service.repo.one("SELECT 1 FROM sqlite_master WHERE name='extension_experiments'"):
                raise ValueError('來源已有本代續訓設定，不可重複建支線。')
            parent = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
            if not parent or parent['status'] != 'completed':
                raise ValueError('來源必須已完成前一代續訓。')
            previous.validate_configuration(service, parent['source_path'], parent['additional_games'], parent['batch_size'],
                StorageBudget(directory, parent['budget_bytes'], parent['min_free_bytes']))
            previous.check_branch(service, parent)
            if batch_size != parent['batch_size']:
                raise ValueError('須沿用既有檢查點間隔。')
            inherited = lineage(service)
            a, b = parent['model_a'], parent['model_b']
            service.conn.executescript(SCHEMA)
            with service.repo.transaction():
                cp_a, cp_b = service._save_checkpoint(a), service._save_checkpoint(b)
                fields = dict(id=1, format_version=1, source_path=str(source_path), source_sha256=snapshot_sha,
                    base_snapshot_sha256=snapshot_sha, source_pilot_json=dumps(inherited['pilot_experiments'][0]),
                    base_version=parent['target_version'], additional_games=additional,
                    target_version=parent['target_version'] + additional, batch_size=batch_size, seed=parent['seed'],
                    model_a=a, model_b=b, initial_checkpoint_a=cp_a, initial_checkpoint_b=cp_b,
                    initial_revision_a=service._model(a)['revision'], initial_revision_b=service._model(b)['revision'],
                    script_sha256=script_hash(), runtime_json=dumps(runtime_info()), budget_bytes=guard.budget_bytes,
                    min_free_bytes=guard.min_free_bytes, status='active', created_at=now(), completed_at=None)
                service.conn.execute('INSERT INTO extension_experiments (' + ','.join(fields) + ') VALUES (' +
                                     ','.join(':' + k for k in fields) + ')', fields)
                for key, value in {'parent_metadata': inherited, 'code_hashes': dependency_hashes(),
                                   'source_hash_scope': 'SQLite backup API 的一致快照 SHA-256，包含已提交 WAL；不是未固定時點的主檔雜湊。'}.items():
                    service.conn.execute('INSERT INTO extension_provenance VALUES(?,?)', (key, dumps(value)))
            guard.check()
        stream_export._publish_without_overwrite(partial, destination)
        return destination
    finally:
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(partial) + suffix)
            if candidate.exists():
                candidate.unlink()


def validate_configuration(service, source_path, additional, batch_size, guard):
    item = service.repo.one('SELECT * FROM extension_experiments WHERE id=1')
    if not item:
        raise ValueError('缺少新一代續訓來源設定。')
    actual = tuple(item[k] for k in ('source_path', 'additional_games', 'batch_size', 'budget_bytes', 'min_free_bytes'))
    if actual != (str(Path(source_path).resolve()), additional, batch_size, guard.budget_bytes, guard.min_free_bytes):
        raise ValueError('續跑須使用相同來源、局數、批次及容量設定。')
    recorded = {r['key']: json.loads(r['value_json']) for r in service.repo.all('SELECT * FROM extension_provenance')}
    if recorded['code_hashes'] != dependency_hashes() or item['script_sha256'] != script_hash():
        raise ValueError('續訓程式已改變，拒絕混用。')
    if recorded['parent_metadata'] != lineage(service):
        raise ValueError('先前試驗來源或批次記錄已改變。')
    validate_runtime(json.loads(item['runtime_json']))
    return item


def check_branch(service, item):
    a, b = item['model_a'], item['model_b']
    inherited_runs = service.repo.all('SELECT run_id FROM pilot_batches UNION SELECT run_id FROM continuation_batches')
    original_count = service.conn.execute("SELECT COUNT(*) FROM games WHERE mode='train' AND status='finished' AND "
        'run_id IN (SELECT run_id FROM pilot_batches UNION SELECT run_id FROM continuation_batches)').fetchone()[0]
    if original_count != item['base_version']:
        raise ValueError('原訓練局數已改變。')
    valid_runs = {r['run_id'] for r in inherited_runs}
    batches = service.repo.all('SELECT * FROM extension_batches ORDER BY batch_index')
    done, active = 0, []
    for i, batch in enumerate(batches):
        start = item['base_version'] + i * item['batch_size']
        requested = min(item['batch_size'], item['target_version'] - start)
        if (requested <= 0 or batch['batch_index'] != i or batch['start_index'] != start
                or batch['requested_games'] != requested or batch['status'] not in ('active', 'completed')):
            raise ValueError('新批次索引或局數不符。')
        valid_runs.add(batch['run_id'])
        run = service.repo.one('SELECT * FROM runs WHERE run_id=?', (batch['run_id'],))
        if not run or run['mode'] != 'train' or run['arm'] != 'self_play_extension':
            raise ValueError('批次實驗對應不符。')
        games = service.repo.all('SELECT game_id,x_player,o_player,x_version,o_version,mode,is_example,seed,status,learning_applied '
                                 'FROM games WHERE run_id=? ORDER BY x_version', (batch['run_id'],))
        finished = [g for g in games if g['status'] == 'finished']
        unfinished = [g for g in games if g['status'] == 'active']
        if (len(games) != len(finished) + len(unfinished) or len(games) > requested or len(unfinished) > 1
                or len(finished) != run['completed_games']):
            raise ValueError('完成局數、未完局或中止紀錄不符。')
        for j, g in enumerate(games):
            index = start + j
            if ((g['x_player'], g['o_player']) != ((a, b) if index % 2 == 0 else (b, a))
                    or g['x_version'] != index or g['o_version'] != index or g['mode'] != 'train'
                    or g['is_example'] or g['seed'] != str(game_seed(int(item['seed']), index))
                    or bool(g['learning_applied']) != (g['status'] == 'finished')):
                raise ValueError('棋譜順序、種子、先後手或學習標記不符。')
        if unfinished and games[-1]['status'] != 'active':
            raise ValueError('未完成局後出現其他對局。')
        if batch['status'] == 'completed':
            if len(finished) != requested or run['status'] != 'completed':
                raise ValueError('完成批次局數不符。')
            for model, field in ((a, 'checkpoint_a'), (b, 'checkpoint_b')):
                cp = service.repo.one('SELECT model_id,version FROM checkpoints WHERE checkpoint_id=?', (batch[field],))
                if not cp or cp['model_id'] != model or cp['version'] != start + requested:
                    raise ValueError('檢查點版本不符。')
        elif i != len(batches) - 1:
            raise ValueError('非最後批次尚未完成。')
        done += len(finished)
        active.extend(unfinished)
    actual_runs = {r['run_id'] for r in service.repo.all("SELECT DISTINCT run_id FROM games WHERE mode='train'")}
    if actual_runs - valid_runs or len(active) > 1 or done > item['additional_games']:
        raise ValueError('混入其他訓練、超額或多筆未完局。')
    for model_id in (a, b):
        model = service._model(model_id)
        if model['version'] != item['base_version'] + done or model['trained_games'] != model['version']:
            raise ValueError('模型與訓練版本不符。')
        lock = service.repo.one('SELECT game_id FROM model_locks WHERE model_id=?', (model_id,))
        if (lock['game_id'] if lock else None) != (active[0]['game_id'] if active else None):
            raise ValueError('模型占用與未完局不符。')
    if item['status'] == 'completed' and (done != item['additional_games'] or active):
        raise ValueError('完成標記不符。')
    return done


def train_extension(service, source_path, additional=10000, batch_size=100, guard=None, progress=None, event=None):
    guard = guard or StorageBudget(Path(service.repo.path).resolve().parent)
    item = validate_configuration(service, source_path, additional, batch_size, guard)
    done = check_branch(service, item)
    a, b = item['model_a'], item['model_b']
    if progress:
        progress(done, additional)
    for i, relative_start in enumerate(range(0, additional, batch_size)):
        start = item['base_version'] + relative_start
        requested = min(batch_size, additional - relative_start)
        batch = service.repo.one('SELECT * FROM extension_batches WHERE batch_index=?', (i,))
        if batch and batch['status'] == 'completed':
            continue
        if batch is None:
            guard.check(RESERVE_GAME)
            with service.repo.transaction():
                run_id = service._new_run('train', 'self_play_extension', game_seed(int(item['seed']), start),
                    {'model_a': a, 'model_b': b, 'requested_games': requested, 'starting_versions': [start, start],
                     'extension_batch_index': i, 'global_start_index': start,
                     'base_version': item['base_version'], 'extension_script_sha256': item['script_sha256']})
                service.conn.execute('INSERT INTO extension_batches VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                                     (i, run_id, start, requested, 'active'))
            batch = service.repo.one('SELECT * FROM extension_batches WHERE batch_index=?', (i,))
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
            service.conn.execute("UPDATE extension_batches SET status='completed',checkpoint_a=?,checkpoint_b=?,completed_at=? WHERE batch_index=?",
                                 (cp_a, cp_b, now(), i))
            service.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
            if relative_start + requested == additional:
                service.conn.execute("UPDATE extension_experiments SET status='completed',completed_at=? WHERE id=1", (now(),))
        guard.check()
    item = service.repo.one('SELECT * FROM extension_experiments WHERE id=1')
    if check_branch(service, item) != additional or item['status'] != 'completed':
        raise ValueError('追加訓練尚未完整結束。')
    return {'base_games': item['base_version'], 'additional_games': additional, 'total_games': item['target_version'],
            'model_a': a, 'model_b': b, 'batch_size': batch_size, 'seed': int(item['seed']),
            'purpose': '固定局數續訓與新穎率統計；未判定棋力或收斂'}


def verify_extension(service):
    item = service.repo.one('SELECT * FROM extension_experiments WHERE id=1')
    if not item or item['status'] != 'completed' or check_branch(service, item) != item['additional_games']:
        raise ValueError('續訓未完成。')
    total_moves = games = 0
    first = {item['model_a']: 0, item['model_b']: 0}
    for row in service.conn.execute('SELECT g.* FROM games g JOIN extension_batches b USING(run_id) ORDER BY g.x_version'):
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


def export_extension(service, destination, guard):
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
                            'runtime': runtime_info(), 'extension_script_sha256': script_hash(),
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('data/storage-v3/training-6000.sqlite3'))
    parser.add_argument('--directory', type=Path, default=Path('data/selfplay-16000'))
    parser.add_argument('--additional', type=int, default=10000)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--budget-gb', type=float, default=50)
    parser.add_argument('--min-free-gb', type=float, default=50)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    guard = StorageBudget(directory, int(args.budget_gb * 10**9), int(args.min_free_gb * 10**9))
    started = time.perf_counter()
    state = {'phase': 'preparing', 'pid': os.getpid(), 'additional_target': args.additional}

    def status(phase, **values):
        state.update(phase=phase, updated_at=now(), **values)
        save_json(directory / 'status.json', state)
        print(json.dumps(state, ensure_ascii=False), flush=True)

    acquired_lock = False
    try:
        with runner_lock(directory / 'runner.lock'):
            acquired_lock = True
            status('preparing')
            db = prepare_branch(args.source, directory, args.additional, args.batch_size, guard)
            with CompactResearchService(db) as service:
                item = validate_configuration(service, args.source, args.additional, args.batch_size, guard)
                state.update(base_games=item['base_version'], total_target=item['target_version'])
                complete_path = directory / 'completion.json'
                if complete_path.exists():
                    report = json.loads(complete_path.read_text(encoding='utf-8'))
                    if (check_branch(service, item) != args.additional or item['status'] != 'completed'
                            or report['extension_script_sha256'] != script_hash()
                            or report['source']['base_snapshot_sha256'] != item['base_snapshot_sha256']
                            or file_hash(report['export']['path']) != report['export']['sha256']):
                        raise ValueError('既有完成報告與資料不一致。')
                    status('ready', completed_games=item['target_version'], report=str(complete_path))
                    return 0
                initial = publish_novelty(db, directory, baseline_games=item['base_version'])
                status('training', completed_games=initial['completed_games'])
                last_statistics = initial['completed_games'] // 1000

                def progress(done, total):
                    nonlocal last_statistics
                    count = item['base_version'] + done
                    if count // 1000 > last_statistics:
                        publish_novelty(db, directory, baseline_games=item['base_version'])
                        last_statistics = count // 1000
                    if done % 25 == 0 or done == total:
                        status('training', completed_games=count, additional_completed=done,
                               elapsed_seconds=round(time.perf_counter() - started, 1),
                               database_bytes=db.stat().st_size)

                trained = train_extension(service, args.source, args.additional, args.batch_size, guard, progress=progress)
                status('verifying', completed_games=item['target_version'])
                verified = verify_extension(service)
                novelty = publish_novelty(db, directory, baseline_games=item['base_version'])
                status('exporting')
                archive = export_extension(service, directory / ('extension-records-' + uuid.uuid4().hex + '.zip'), guard)
                report = dict(created_at=now(), runtime=runtime_info(), extension_script_sha256=script_hash(),
                    code_hashes=dependency_hashes(), training=trained, verification=verified, export=archive,
                    novelty_summary='novelty-summary.json', novelty_comparison=novelty['comparison_to_last_baseline_block'],
                    source={k: item[k] for k in ('source_path', 'source_sha256', 'base_snapshot_sha256', 'base_version',
                                                'initial_checkpoint_a', 'initial_checkpoint_b', 'initial_revision_a', 'initial_revision_b')},
                    storage_budget_bytes=guard.budget_bytes, minimum_free_bytes=guard.min_free_bytes,
                    invocation_elapsed_seconds=round(time.perf_counter() - started, 2))
            report.update(database_bytes=db.stat().st_size, database_sha256=file_hash(db), storage=guard.check())
            save_json(complete_path, report)
            status('ready', completed_games=item['target_version'], report=str(complete_path))
        return 0
    except KeyboardInterrupt:
        status('interrupted', error='已提交的棋步與亂數保留；可用相同指令續跑。')
        return 130
    except Exception as exc:
        if acquired_lock:
            status('failed', error=str(exc))
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

