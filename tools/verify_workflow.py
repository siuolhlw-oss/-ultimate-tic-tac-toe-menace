"""獨立資料庫的小規模功能驗證；不是正式收斂實驗。"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uttt.engine import replay, state_to_dict
from uttt.service import ResearchService, runtime_info


def frozen_tables(service):
    names = ('models', 'weights', 'model_states', 'learning_updates', 'model_events')
    return {name: service.repo.all('SELECT * FROM ' + name) for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data') / ('verification-' + datetime.now().strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    db = args.output / 'verification.sqlite3'
    with ResearchService(db) as service:
        a = service.create_model('功能驗證 A（非正式實驗）', seed=20261006)
        b = service.create_model('功能驗證 B（非正式實驗）', seed=20261007)
        trained = service.train(a, b, games=20, seed=20261008)
        before = frozen_tables(service)
        evaluated = service.evaluate(a, games=20, seed=20261009)
        assert before == frozen_tables(service), '評估污染訓練模型'
        archive = service.export_data()
        (args.output / 'records.zip').write_bytes(archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            exported = json.loads(zipped.read('research.json'))
            csv_moves = list(csv.DictReader(io.StringIO(zipped.read('moves.csv').decode('utf-8-sig'))))
        assert len(csv_moves) == len(exported['moves'])
        for game in exported['games']:
            rows = sorted((m for m in exported['moves'] if m['game_id'] == game['game_id']), key=lambda m: m['ply'])
            sequence = [m['move_code'] for m in rows]
            csv_sequence = [int(m['move_code']) for m in sorted((m for m in csv_moves if m['game_id'] == game['game_id']), key=lambda m: int(m['ply']))]
            assert sequence == csv_sequence
            state = replay(sequence)[-1]
            assert state_to_dict(state) == json.loads(game['state_json'])
            assert state.status == game['result']
            for actor, player in (('X', game['x_player']), ('O', game['o_player'])):
                if player not in ('human', 'random'):
                    restored = service.model_at_game_start(game['game_id'], actor)
                    assert restored['model']['version'] == game[actor.lower() + '_version']
        assert service.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not service.conn.execute('PRAGMA foreign_key_check').fetchall()
        report = {'purpose': '程式功能驗證；不可當作正式收斂成果',
                  'created_at': datetime.now(timezone.utc).isoformat(), 'runtime': runtime_info(),
                  'training': trained, 'evaluation': evaluated,
                  'game_count': len(exported['games']), 'move_count': len(exported['moves']),
                  'model_box_counts': {m: service.conn.execute('SELECT COUNT(*) FROM model_states WHERE model_id=?', (m,)).fetchone()[0] for m in (a, b)},
                  'evaluation_keeps_training_unchanged': True,
                  'all_exported_games_replayed': True, 'csv_json_sequences_equal': True,
                  'all_game_start_models_reconstructed': True,
                  'sqlite_integrity': 'ok', 'zip_sha256': hashlib.sha256(archive).hexdigest(),
                  'zip_bytes': len(archive), 'elapsed_seconds': round(time.perf_counter() - start, 3)}
    report['database_bytes'] = db.stat().st_size
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(args.output.resolve()), **report}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
