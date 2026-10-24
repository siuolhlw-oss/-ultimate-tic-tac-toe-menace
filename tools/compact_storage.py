"""獨立 v3 副本的遷移、進度與原目標續跑入口。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_v3.migration import migrate
from storage_v3.repository import CompactResearchService
from uttt.pilot import runner_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    migration = commands.add_parser('migrate')
    migration.add_argument('--source', type=Path, required=True)
    migration.add_argument('--destination', type=Path, required=True)
    migration.add_argument('--budget-gb', type=float, default=50)
    migration.add_argument('--min-free-gb', type=float, default=50)
    for command in ('status', 'continue'):
        commands.add_parser(command).add_argument('--database', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'migrate':
        from tools.continue_selfplay import StorageBudget
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        guard = StorageBudget(args.destination.parent, int(args.budget_gb * 10**9), int(args.min_free_gb * 10**9))
        report = migrate(args.source, args.destination,
                         progress=lambda stage, count: print(stage + ('' if count is None else ': ' + str(count)), flush=True), guard=guard)
        print(json.dumps({k: report[k] for k in ('destination', 'compact_bytes', 'saved_fraction', 'models')}, ensure_ascii=False, indent=2))
        return
    if not args.database.is_file():
        raise FileNotFoundError(args.database)
    if args.command == 'status':
        with CompactResearchService(args.database, read_only=True) as service:
            print(json.dumps({'models': service.repo.all('SELECT model_id,name,version,revision FROM models'),
                              'games': service.repo.all('SELECT mode,status,COUNT(*) AS n FROM games GROUP BY mode,status'),
                              'storage_version': 3}, ensure_ascii=False, indent=2))
        return
    # 只接回被遷移的原續訓目標，不重新建立模型或擅自多加局數。
    from tools.continue_selfplay import StorageBudget, train_continuation, verify_continuation, export_continuation
    with runner_lock(args.database.with_suffix('.runner.lock')):
        with CompactResearchService(args.database) as service:
            item = service.repo.one('SELECT * FROM continuation_experiments WHERE id=1')
            if not item:
                raise ValueError('此副本沒有原追加訓練設定；請使用介面或另外固定新實驗設定。')
            guard = StorageBudget(args.database.parent, item['budget_bytes'], item['min_free_bytes'])
            result = train_continuation(service, item['source_path'], item['additional_games'], item['batch_size'], guard,
                progress=lambda done, total: print(f'原追加目標：{done}/{total}', flush=True) if done % 25 == 0 else None)
            verified = verify_continuation(service)
            archive = args.database.with_suffix('.records.zip')
            if archive.exists():
                raise FileExistsError('訓練已核對完成；既有匯出不覆寫。')
            exported = export_continuation(service, archive, guard)
            print(json.dumps({'training': result, 'verification': verified, 'export': exported}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
