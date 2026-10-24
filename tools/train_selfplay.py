"""從零互弈 1,000 局並保存棋譜；相同命令可續跑，不自動評估勝率。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uttt.pilot import runner_lock, train_pilot, verify_records
from uttt.service import ResearchService, runtime_info, now
from uttt.stream_export import export_archive


def file_hash(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def save_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/selfplay-1000'), help='實驗專用資料夾；續跑須用相同路徑')
    parser.add_argument('--games', type=int, default=1000, help='總完成局數，須為正偶數')
    parser.add_argument('--batch-size', type=int, default=100, help='每批完成後保存兩個模型')
    parser.add_argument('--seed', type=int, default=20261013, help='A 的初始種子，B 使用此值加一')
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        with runner_lock(directory / 'runner.lock'):
            report_path = directory / 'completion.json'
            with ResearchService(directory / 'training.sqlite3') as service:
                def progress(done, total):
                    if done % min(25, args.batch_size) == 0 or done == total:
                        print(f'已完成 {done}/{total} 局；本次執行 {time.perf_counter() - started:.1f} 秒', flush=True)
                    if done % args.batch_size == 0 and done < total:
                        if shutil.disk_usage(directory).free < 1024 ** 3:
                            raise OSError('剩餘磁碟不足 1 GiB，已保留進度；清出空間後用相同命令續跑。')
                trained = train_pilot(service, args.games, args.batch_size, args.seed, progress=progress)
                if report_path.exists():
                    old = json.loads(report_path.read_text(encoding='utf-8'))
                    archive_path = Path(old['export']['path'])
                    if old['training']['model_a'] != trained['model_a'] or old['training']['total_games'] != args.games:
                        raise ValueError('完成報告與實驗不符，請保留檔案並檢查。')
                    if not archive_path.is_file() or file_hash(archive_path) != old['export']['sha256']:
                        raise ValueError('既有匯出遺失或校驗失敗，請保留資料庫與報告進行檢查。')
                    print(f'此實驗已完成，未重複訓練。報告：{report_path}', flush=True)
                    return 0
                print('正在逐局重播核對紀錄（不進行棋力分析）…', flush=True)
                verified = verify_records(service)
                print('正在逐列匯出完整棋譜、珠數及檢查點…', flush=True)
                destination = directory / f'training-records-{datetime.now():%Y%m%d-%H%M%S-%f}.zip'
                archive = export_archive(service, destination)
                report = {'created_at': now(), 'runtime': runtime_info(), 'training': trained,
                          'verification': verified, 'export': archive,
                          'human_pk': '尚未執行；請使用凍結模型的 PK 評估模式',
                          'analysis': '依使用者要求省略策略相似與自動勝率分析',
                          'invocation_elapsed_seconds': round(time.perf_counter() - started, 2)}
            report['database_bytes'] = (directory / 'training.sqlite3').stat().st_size
            save_json(report_path, report)
            print(json.dumps({'report': str(report_path), 'games': verified['games_replayed'],
                              'moves': verified['moves_verified'], 'archive': archive['path'],
                              'database_bytes': report['database_bytes']}, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print('已中止執行，已提交的每一手仍保存。請用相同命令接續。', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'執行未完成：{exc}\n資料保留於 {directory}，不會自動重建或清除。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
