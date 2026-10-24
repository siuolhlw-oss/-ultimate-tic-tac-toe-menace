"""單次作業：等待既有 6,000 局交付完成，再建立並驗證正式 v3 副本。"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_v3.migration import migrate
from storage_v3.verification import verify_replays, compare_histories
from uttt.pilot import runner_lock
from uttt.service import now
from tools.continue_selfplay import StorageBudget, save_json, file_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-directory', type=Path, default=Path('data/selfplay-6000'))
    parser.add_argument('--directory', type=Path, default=Path('data/storage-v3'))
    args = parser.parse_args()
    output = args.directory.resolve()
    output.mkdir(parents=True, exist_ok=True)
    original = args.source_directory.resolve()
    source = original / 'training.sqlite3'
    completion = original / 'completion.json'
    destination = output / 'training-6000.sqlite3'
    status_path = output / 'finalization-status.json'
    validation_path = destination.with_suffix('.validation.json')
    guard = StorageBudget(output, 50_000_000_000, 50_000_000_000)

    def status(phase, **extra):
        save_json(status_path, {'phase': phase, 'updated_at': now(), 'source': str(source), 'destination': str(destination), **extra})
        print(phase, flush=True)

    with runner_lock(output / 'finalizer.runner.lock'):
        if validation_path.exists() and destination.exists():
            print('已完成且保留既有副本；不重複遷移。', flush=True)
            return
        try:
            if not source.is_file():
                raise FileNotFoundError(source)
            status('waiting_for_original_completion')
            while not completion.exists():
                time.sleep(30)
            report = json.loads(completion.read_text(encoding='utf-8'))
            if (report['training']['total_games'] != 6000
                    or report['verification']['total_training_games'] != 6000
                    or report['verification']['new_games_replayed'] != 5000):
                raise ValueError('來源完成報告不是已核對的累計 6,000 局。')
            status('checking_original_completion')
            if file_hash(source) != report['database_sha256']:
                raise ValueError('來源已在原完成報告之後改動，保留資料並停止。')
            if destination.exists():
                migration_report = json.loads(destination.with_suffix('.sqlite3.migration.json').read_text(encoding='utf-8'))
                if file_hash(destination) != migration_report['compact_sha256']:
                    raise ValueError('既有副本在遷移後已改動，保留資料並停止。')
            else:
                migration_report = migrate(source, destination, progress=lambda stage, count: status(stage, rows=count), guard=guard)
            status('replaying_all_games')
            replays = verify_replays(destination)
            if replays['games_replayed'] != 6000 or replays['finished_games'] != 6000:
                raise ValueError('新副本的實際完成局數不是 6,000。')
            status('checking_historical_models')
            histories = compare_histories(source, destination)
            save_json(validation_path, {'source_completion_sha256': file_hash(completion),
                                       'migration': migration_report, 'replays': replays, 'histories': histories,
                                       'scope': '原完成批次的無損副本；沒有重新訓練或新增實驗局數。'})
            status('ready', compact_bytes=migration_report['compact_bytes'],
                   saved_fraction=migration_report['saved_fraction'], validation=str(validation_path))
        except BaseException as error:
            status('failed', error=str(error))
            raise


if __name__ == '__main__':
    main()
