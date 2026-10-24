"""匯出限於指定實驗，拒絕破損棋譜，保留固定全局 D4 歷史語意。"""
import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.export_explorer_data import (canonical_history, export_experiment,
                                        javascript_data, read_experiment)
from uttt.engine import State, legal_moves
from uttt.pilot import train_pilot
from uttt.service import ResearchService
from uttt.symmetry import transform_move


class ExplorerDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'training.sqlite3'
        with ResearchService(self.db) as service:
            train_pilot(service, total_games=2, batch_size=1, seed=349)
            # 同庫後續真人 PK / 示範不得混入原始訓練批次。
            game = service.new_game('random', 'random', mode='demo', seed=7)
            while service.get_game(game)['status'] == 'active':
                service.step(game)

    def tearDown(self):
        self.temp.cleanup()

    def test_readonly_selection_and_full_oracle(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        oracle = io.StringIO()
        payload = read_experiment(self.db, expected_games=2, oracle=oracle)
        self.assertEqual(payload['summary']['games'], 2)
        self.assertEqual([g['index'] for g in payload['games']], [1, 2])
        self.assertEqual(payload['games'][0]['xModel'], '自我對弈 AI-A')
        self.assertEqual(payload['games'][1]['xModel'], '自我對弈 AI-B')
        frames = [json.loads(line) for line in oracle.getvalue().splitlines()]
        self.assertEqual(len(frames), payload['summary']['moves'] + 2)
        self.assertEqual(frames[0]['legal'], list(legal_moves(State())))
        self.assertEqual(frames[-1]['legal'], [])
        self.assertIsNone(payload['summary']['fullGameTotal'])
        self.assertEqual(payload['summary']['firstMoveD4'], 15)
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(payload['source']['db'], 'training.sqlite3')

    def test_export_preserves_other_files_and_js_roundtrip(self):
        destination = self.root / 'exports'
        destination.mkdir()
        marker = destination / 'index.html'
        marker.write_text('keep', encoding='utf-8')
        payload = export_experiment(self.db, destination, 2, include_oracle=True)
        js = (destination / 'data.js').read_text(encoding='utf-8')
        self.assertEqual(json.loads(js.removeprefix('window.UTTT_DATA = ').removesuffix(';\n')), payload)
        self.assertEqual(json.loads((destination / 'data.json').read_text(encoding='utf-8')), payload)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        self.assertTrue((destination / 'validation/rules-oracle.jsonl').is_file())

    def test_corruption_and_wrong_size_are_rejected(self):
        with self.assertRaises(ValueError):
            read_experiment(self.db, expected_games=1000)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE moves SET actor='O' WHERE game_id=(SELECT game_id FROM games WHERE mode='train' AND x_version=0) AND ply=1")
            conn.commit()
        with self.assertRaisesRegex(ValueError, '原始角色'):
            export_experiment(self.db, self.root / 'bad', 2, include_oracle=True)
        self.assertFalse((self.root / 'bad/data.json').exists())
        self.assertFalse((self.root / 'bad/validation/rules-oracle.jsonl.partial').exists())

    def test_history_uses_one_transform_and_script_chars_are_escaped(self):
        sequence = (14, 41, 15)
        for transform in range(8):
            other = tuple(transform_move(move, transform) for move in sequence)
            self.assertEqual(canonical_history(sequence), canonical_history(other))
        # 順序相異的落子歷史不可只因末盤相同就合併。
        self.assertNotEqual(canonical_history((11, 12, 13)), canonical_history((13, 12, 11)))
        data = {'text': '</script>&\u2028\u2029測試'}
        js = javascript_data(data)
        self.assertNotIn('</script>', js)
        self.assertNotIn('\u2028', js)
        self.assertEqual(json.loads(js.removeprefix('window.UTTT_DATA = ').removesuffix(';\n')), data)


if __name__ == '__main__':
    unittest.main()
