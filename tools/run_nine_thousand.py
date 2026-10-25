"""隔離續訓 3,000 局，完成驗證後自動進行四模型凍結交叉賽。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import extend_compact_selfplay as extension
from tools.cross_match import prepare_match, run_match, code_hashes
from tools.continue_selfplay import StorageBudget, save_json
from uttt.pilot import runner_lock
from uttt.service import now


def main(argv=None):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.SetPriorityClass.restype = wintypes.BOOL
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise ctypes.WinError()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--old-source', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--additional', type=int, default=3000)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--games-per-pair', type=int, default=200)
    parser.add_argument('--seed', type=int, default=20261025)
    parser.add_argument('--budget-gb', type=float, default=50)
    parser.add_argument('--min-free-gb', type=float, default=50)
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if directory in (args.source.resolve().parent, args.old_source.resolve().parent):
        parser.error('必須使用獨立的新組資料夾。')
    directory.mkdir(parents=True, exist_ok=True)
    state = dict(pid=os.getpid(), phase='preparing', started_at=now())
    def status(phase, **values):
        state.update(phase=phase, updated_at=now(), **values)
        save_json(directory / 'workflow-status.json', state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
    acquired = False
    try:
        with runner_lock(directory / 'workflow.lock'):
            acquired = True
            config = dict(source=str(args.source.resolve()), old_source=str(args.old_source.resolve()),
                          additional=args.additional, batch_size=args.batch_size, games_per_pair=args.games_per_pair,
                          seed=args.seed, budget_gb=args.budget_gb, min_free_gb=args.min_free_gb,
                          code_hashes={**extension.dependency_hashes(), **code_hashes()},
                          aliases={'C': 'extension.model_a', 'D': 'extension.model_b'})
            config_path = directory / 'workflow-config.json'
            if config_path.exists():
                if json.loads(config_path.read_text(encoding='utf-8')) != config:
                    raise ValueError('續跑參數或程式已改變。')
            else:
                save_json(config_path, config)
            status('training')
            result = extension.main(['--source', str(args.source.resolve()), '--directory', str(directory),
                '--additional', str(args.additional), '--batch-size', str(args.batch_size),
                '--budget-gb', str(args.budget_gb), '--min-free-gb', str(args.min_free_gb), '--export', 'none'])
            if result:
                raise RuntimeError('續訓或驗證未完成，保留進度，尚未開始比賽。')
            report = json.loads((directory / 'completion.json').read_text(encoding='utf-8'))
            guard = StorageBudget(directory, int(args.budget_gb*10**9), int(args.min_free_gb*10**9))
            status('preparing_match', training_games=report['training']['total_games'])
            tick = time.perf_counter()
            db = prepare_match(directory / 'matches', directory / 'training.sqlite3', args.old_source,
                               args.games_per_pair, args.seed, report['training']['total_games'], guard)
            def progress(done, total):
                if done % 10 == 0 or done == total:
                    status('matching', completed_matches=done, match_target=total)
            result = run_match(db, guard, progress)
            status('ready', completed_matches=result['games_replayed'], match_target=4*args.games_per_pair,
                   evaluation_seconds=time.perf_counter()-tick, results=str(db.parent / 'results.json'))
        return 0
    except KeyboardInterrupt:
        if acquired:
            status('interrupted')
        return 130
    except Exception as exc:
        if acquired:
            status('failed', error=str(exc))
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
