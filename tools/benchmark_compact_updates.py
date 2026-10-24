"""用已完成的 v3 研究副本，比較舊／新儲存層；不寫入來源或正式訓練。

指定基準版本 checkout、已完成資料庫及全新輸出目錄。單一程序依序執行，
Windows 採較低優先權；結果僅為工程驗證，不是獨立訓練實驗。
"""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import pickle
import platform
import shutil
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage_v3.repository import CompactResearchService


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def physical_digest(connection):
    """依主鍵串流比較全部資料列，包含二進位快照；不包含觸發器版本標記。"""
    result = {}
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        if table.startswith('sqlite_'):
            continue
        columns = list(connection.execute('PRAGMA table_info("' + table + '")'))
        keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
        order = ','.join('"' + key + '"' for key in keys) or 'rowid'
        h, count = hashlib.sha256(), 0
        where = " WHERE key != 'update_triggers'" if table == 'storage_metadata' else ''
        for row in connection.execute('SELECT * FROM "' + table + '"' + where + ' ORDER BY ' + order):
            raw = pickle.dumps(tuple(row), protocol=4)
            h.update(len(raw).to_bytes(8, 'big'))
            h.update(raw)
            count += 1
        result[table] = {'rows': count, 'sha256': h.hexdigest()}
    return result


def game_trial(service_class, path, models, index, stamp):
    ids = itertools.count()
    players = models if index % 2 == 0 else models[::-1]
    times = []
    reopen_seconds = 0.0
    started = time.perf_counter()
    with patch('uttt.service.identifier', side_effect=lambda prefix: f'{prefix}_issue1_{index}_{next(ids)}'), \
            patch('uttt.service.now', return_value=stamp):
        service = service_class(path)
        try:
            game = service.new_game(*players, seed=771000 + index)
            startup_seconds = time.perf_counter() - started
            while service.get_game(game)['status'] == 'active':
                start = time.perf_counter()
                service.step(game)
                times.append(time.perf_counter() - start)
                if index == 0 and len(times) == 11:
                    reopen_start = time.perf_counter()
                    service.close()
                    service = service_class(path)
                    reopen_seconds += time.perf_counter() - reopen_start
            elapsed = time.perf_counter() - started
            moves = service.moves(game)
            result = service.get_game(game)
            return {'game_id': game, 'moves': moves, 'game': result,
                    'wall_seconds': elapsed, 'steps': len(times),
                    'startup_seconds': startup_seconds, 'midgame_reopen_seconds': reopen_seconds,
                    'ordinary_steps_seconds': sum(times[:-1]), 'final_step_seconds': times[-1],
                    'step_seconds': times, 'step_median_seconds': statistics.median(times)}
        finally:
            service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--games', type=int, default=3)
    args = parser.parse_args()
    if args.games < 1:
        parser.error('--games must be positive')
    if sys.platform == 'win32':
        import ctypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        if not k.SetPriorityClass(k.GetCurrentProcess(), 0x4000):
            raise OSError(ctypes.get_last_error(), 'cannot set below-normal priority')
    source, baseline, output = args.source.resolve(), args.baseline_root.resolve(), args.output.resolve()
    if not source.is_file() or output.exists() or output == source.parent or baseline == ROOT:
        parser.error('require an existing source, distinct baseline checkout and new isolated output directory')
    # 限定比較只換儲存層，核心遊戲與學習程式必須相同。
    for path in (ROOT / 'uttt').glob('*.py'):
        if file_hash(path) != file_hash(baseline / 'uttt' / path.name):
            raise ValueError('baseline core differs: ' + path.name)
    spec = importlib.util.spec_from_file_location('issue1_baseline_repository', baseline / 'storage_v3/repository.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    output.mkdir(parents=True)
    source_hash = file_hash(source)
    snapshot = output / 'source-snapshot.sqlite3'
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute('PRAGMA user_version').fetchone()[0] != 3:
            raise ValueError('source must be v3')
        if conn.execute("SELECT COUNT(*) FROM _games WHERE status='active' AND mode='train'").fetchone()[0]:
            raise ValueError('source has unfinished training; choose a completed training snapshot')
        if conn.execute('SELECT COUNT(*) FROM model_locks').fetchone()[0]:
            raise ValueError('source models are locked')
        models = [r[0] for r in conn.execute('SELECT model_id FROM _models WHERE trained_games>0 ORDER BY trained_games DESC,model_id LIMIT 2')]
        if len(models) != 2:
            raise ValueError('need two trained models')
        initial_models = list(conn.execute('SELECT model_id,version,revision,trained_games FROM _models ORDER BY model_id'))
        preserved_active_nontraining = conn.execute("SELECT COUNT(*) FROM _games WHERE status='active' AND mode!='train'").fetchone()[0]
        weights = conn.execute('SELECT COUNT(*) FROM _weights').fetchone()[0]
        with closing(sqlite3.connect(snapshot)) as target:
            conn.backup(target, pages=1024, progress=lambda *unused: time.sleep(0.001))
    print(json.dumps({'stage': 'snapshot_ready', 'weights': weights}), flush=True)
    paths = {name: output / (name + '.sqlite3') for name in ('baseline', 'optimized')}
    for path in paths.values():
        shutil.copyfile(snapshot, path)  # snapshot 已關閉且無 WAL，來源研究資料一律使用上方備份 API。
    classes = {'baseline': old.CompactResearchService, 'optimized': CompactResearchService}
    report = {'purpose': 'isolated engineering validation, not an independent training replicate',
              'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
              'source_sha256': source_hash, 'snapshot_sha256': file_hash(snapshot),
              'baseline_code_sha256': file_hash(baseline / 'storage_v3/repository.py'),
              'optimized_code_sha256': file_hash(ROOT / 'storage_v3/repository.py'),
              'initial_models': initial_models, 'initial_weights': weights,
              'preserved_active_nontraining_games': preserved_active_nontraining, 'trials': []}
    for index in range(args.games):
        stamp = datetime.now(timezone.utc).isoformat()
        trial = {}
        order = ('baseline', 'optimized') if index % 2 == 0 else ('optimized', 'baseline')
        for name in order:
            trial[name] = game_trial(classes[name], paths[name], models, index, stamp)
            print(json.dumps({'stage': 'game_finished', 'index': index, 'variant': name,
                              'seconds': trial[name]['wall_seconds'], 'steps': trial[name]['steps']}), flush=True)
        assert trial['baseline']['moves'] == trial['optimized']['moves'], 'move/RNG/selection mismatch'
        assert trial['baseline']['game'] == trial['optimized']['game'], 'game result mismatch'
        report['trials'].append({name: {k: v for k, v in data.items() if k not in ('moves', 'game')}
                                 for name, data in trial.items()})
        (output / 'progress.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    digests = {}
    for name in ('baseline', 'optimized'):
        print(json.dumps({'stage': 'verify_all_tables', 'variant': name}), flush=True)
        with classes[name](paths[name], read_only=True) as service:
            assert service.conn.execute('PRAGMA foreign_key_check').fetchone() is None
            digests[name] = physical_digest(service.conn)
    assert digests['baseline'] == digests['optimized'], 'persisted table mismatch'
    report['all_data_tables_equal'] = True
    report['table_digests'] = digests['optimized']
    report['source_file_unchanged'] = file_hash(source) == source_hash
    assert report['source_file_unchanged']
    report['seconds'] = {name: sum(t[name]['wall_seconds'] for t in report['trials']) for name in classes}
    report['speedup'] = report['seconds']['baseline'] / report['seconds']['optimized']
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('all_data_tables_equal', 'source_file_unchanged', 'seconds', 'speedup')}), flush=True)


if __name__ == '__main__':
    main()
