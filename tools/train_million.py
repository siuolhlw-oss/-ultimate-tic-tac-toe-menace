"""精簡模式：先遷移與驗證，再持久續訓；停止請使用 stop 子命令。"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage_v4.artifacts import archive_milestone, backup_latest, restore_archive
from storage_v4.migration import migrate, code_contract
from storage_v4.repository import SlimService
from tools.continue_selfplay import StorageBudget, RESERVE_GAME, save_json
from uttt.pilot import runner_lock
from uttt.service import now


def progress(directory, phase, **data):
    record = {'updated_at': now(), 'pid': os.getpid(), 'phase': phase, **data}
    save_json(Path(directory) / 'status.json', record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def validate_contract(service):
    if service.meta('code_contract') != code_contract():
        raise ValueError('程式或環境已改變；需重新驗證並記錄轉換，拒絕悄悄混用續訓。')


def maintenance(service, directory, budget):
    config, version = service.meta('config'), service.head()
    for model in (1, 2):
        row = service.conn.execute('SELECT seed,games FROM evaluations WHERE version=? AND model=?', (version, model)).fetchone()
        if row:
            if tuple(row) != (str(config['eval_seed']), config['eval_games']):
                raise ValueError('已存評估設定不符。')
        else:
            progress(directory, 'evaluating', completed_games=version, target_games=config['target'], model=model)
            budget.check(RESERVE_GAME)
            result = service.evaluate(model, games=config['eval_games'], seed=config['eval_seed'])
            service.save_evaluation(result)
            save_json(Path(directory) / f'evaluation-{version:07d}-{model}.json', result)
    if version > config['base']:
        progress(directory, 'backing_up', completed_games=version, target_games=config['target'])
        service.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        record = backup_latest(service, directory, budget)
        if version in config['milestones']:
            progress(directory, 'archiving', completed_games=version, target_games=config['target'])
            archive_milestone(Path(directory) / 'latest-backup.sqlite3', directory, version, budget)
    else:
        record = None
    page_bytes = service.conn.execute('PRAGMA page_count').fetchone()[0] * service.conn.execute('PRAGMA page_size').fetchone()[0]
    projected_database = round(page_bytes * config['target'] / version)
    archives = [json.loads(p.read_text(encoding='utf-8')) for p in Path(directory).glob('model-*.sqlite3.gz.json')]
    # 尚無大里程碑實測時，不先假設壓縮率；保守以未壓縮大小計算。
    ratio = max((a['compressed_bytes'] / a['uncompressed_bytes'] for a in archives), default=1.0)
    projected_archives = round(sum(page_bytes * v / version * ratio for v in config['milestones'] if v <= config['target']))
    capacity = {'version': version, 'database_bytes': page_bytes, 'storage': budget.check(RESERVE_GAME),
                'projected_latest_database_bytes': projected_database,
                'projected_milestone_archives_bytes': projected_archives,
                'projected_steady_bytes': 2 * projected_database + projected_archives,
                'projected_backup_peak_bytes': 3 * projected_database + projected_archives,
                'archive_ratio_assumption': ratio,
                'projection_note': '線性外推非保證；常態含最新模型、備份、里程碑，備份尖峰另加一份模型。'
                                   '未含少量 JSON／日誌；舊 v3 資料在預算資料夾之外，仍計入磁碟剩餘空間。',
                'models': [{k: r[k] for k in ('alias', 'version', 'box_count')} for r in service.models()]}
    save_json(Path(directory) / f'capacity-{version:07d}.json', capacity)
    with service.transaction():
        service.set_meta('maintenance_completed', version)
    return record


def run(directory, *, stop_after=None, resume=False):
    directory = Path(directory).resolve()
    database = directory / 'training.sqlite3'
    with runner_lock(directory / 'runner.lock'):
        # 先取得唯一執行鎖，避免第二個續跑指令清掉另一程序的停止要求。
        if resume:
            request = directory / 'stop-request.json'
            if request.exists():
                request.unlink()
        started = time.perf_counter()
        with SlimService(database) as service:
            validate_contract(service)
            config = service.meta('config')
            acceptance_path = directory / 'acceptance.json'
            if not acceptance_path.is_file():
                raise ValueError('尚無隔離驗證報告，拒絕啟動正式續訓。')
            acceptance = json.loads(acceptance_path.read_text(encoding='utf-8'))
            provenance = service.meta('provenance')
            if (not acceptance.get('passed') or acceptance.get('code_contract') != code_contract()
                    or acceptance.get('source_snapshot_sha256') != provenance['snapshot_sha256']
                    or acceptance.get('base') != config['base']):
                raise ValueError('驗證報告與來源／程式／起點不符。')
            initial = service.check_head()
            budget = StorageBudget(directory, config['budget_bytes'], config['min_free_bytes'])
            try:
                while True:
                    version = service.head()
                    status = {'base_games': config['base'], 'completed_games': version,
                              'target_games': config['target'], 'additional_completed': version - config['base'],
                              'elapsed_seconds': round(time.perf_counter() - started, 2)}
                    if (directory / 'stop-request.json').exists():
                        return progress(directory, 'paused', **status)
                    due = version == config['base'] or version % config['eval_every'] == 0 or version == config['target']
                    if due and service.meta('maintenance_completed') != version:
                        maintenance(service, directory, budget)
                    if version == config['target']:
                        record = progress(directory, 'completed', **status)
                        save_json(directory / 'completion.json', record)
                        return record
                    if stop_after is not None and version - initial >= stop_after:
                        return progress(directory, 'paused', **status)
                    if version % 50 == 0 or version == initial:
                        storage = budget.check(RESERVE_GAME)
                        page_bytes = service.conn.execute('PRAGMA page_count').fetchone()[0] * service.conn.execute('PRAGMA page_size').fetchone()[0]
                        progress(directory, 'training', **status, database_bytes=page_bytes, storage=storage,
                                 models=[{k: r[k] for k in ('alias', 'version', 'box_count')} for r in service.models()])
                    service.train_one()
            except KeyboardInterrupt:
                return progress(directory, 'paused', completed_games=service.head(), target_games=config['target'])
            except BaseException as exc:
                progress(directory, 'failed', completed_games=service.head(), target_games=config['target'], error=str(exc))
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--source', required=True)
    prepare.add_argument('--directory', required=True)
    prepare.add_argument('--target', type=int, default=1000000)
    prepare.add_argument('--base', type=int, default=9000)
    prepare.add_argument('--budget-gb', type=float, default=50)
    prepare.add_argument('--min-free-gb', type=float, default=50)
    for name in ('run', 'status', 'stop', 'resume'):
        command = sub.add_parser(name)
        command.add_argument('--directory', required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--archive', required=True)
    restore.add_argument('--destination', required=True)
    args = parser.parse_args()
    if args.command == 'restore':
        destination = Path(args.destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps(restore_archive(args.archive, destination, StorageBudget(destination.parent)), ensure_ascii=False))
        return
    directory = Path(args.directory).resolve()
    if args.command == 'prepare':
        directory.mkdir(parents=True, exist_ok=True)
        with runner_lock(directory / 'runner.lock'):
            budget = StorageBudget(directory, int(args.budget_gb * 1e9), int(args.min_free_gb * 1e9))
            report = migrate(args.source, directory / 'training.sqlite3', target=args.target,
                             expected_base=args.base, budget=budget,
                             progress=lambda table, count: progress(directory, 'migrating', table=table, processed=count))
            progress(directory, 'awaiting_validation', report=report)
    elif args.command == 'status':
        with SlimService(directory / 'training.sqlite3', read_only=True) as service:
            print(json.dumps({'completed_games': service.head(), 'config': service.meta('config'),
                              'models': [{k: r[k] for k in ('alias', 'version', 'box_count')} for r in service.models()],
                              'status': json.loads((directory / 'status.json').read_text(encoding='utf-8'))
                              if (directory / 'status.json').exists() else None}, ensure_ascii=False, indent=2))
    elif args.command == 'stop':
        if not (directory / 'training.sqlite3').is_file():
            raise FileNotFoundError(directory / 'training.sqlite3')
        save_json(directory / 'stop-request.json', {'requested_at': now(), 'reason': 'user_requested'})
        print('已要求在目前整局或正在執行的評估／備份階段完成後停止。')
    else:
        run(directory, resume=args.command == 'resume')


if __name__ == '__main__':
    main()
