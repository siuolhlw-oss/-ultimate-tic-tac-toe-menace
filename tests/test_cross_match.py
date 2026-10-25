"""相同來源 ID、凍結交叉賽、續跑與可選匯出的隔離驗證。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage_v3.migration import migrate
from storage_v3.repository import CompactResearchService
from tools import continue_selfplay as previous
from tools import extend_compact_selfplay as extension
from tools import cross_match as match
from tools.run_nine_thousand import main as workflow
from uttt.pilot import train_pilot
from uttt.service import ResearchService


class CrossMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='uttt-match-source-')
        root = Path(cls.fixture.name)
        initial = root / 'initial.sqlite3'
        with ResearchService(initial) as service:
            train_pilot(service, total_games=2, batch_size=2, seed=7452)
        guard = previous.StorageBudget(root / 'second', 2_000_000_000, 0)
        second = previous.prepare_branch(initial, root / 'second', 2, 2, guard)
        with ResearchService(second) as service:
            previous.train_continuation(service, initial, 2, 2, guard)
        cls.source = root / 'compact.sqlite3'
        migrate(second, cls.source)
        newdir = root / 'new'
        guard = previous.StorageBudget(newdir, 2_000_000_000, 0)
        cls.new = extension.prepare_branch(cls.source, newdir, 4, 2, guard)
        with CompactResearchService(cls.new) as service:
            extension.train_extension(service, cls.source, 4, 2, guard)
        cls.hashes = [previous.file_hash(p) for p in (cls.source, cls.new)]

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-match-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.guard = previous.StorageBudget(self.directory, 2_000_000_000, 0)

    def prepare(self, directory=None):
        return match.prepare_match(directory or self.directory, self.new, self.source, 2, 117, 8, self.guard)

    def test_duplicate_source_ids_are_distinct_frozen_players_and_balanced(self):
        db = self.prepare()
        result = match.run_match(db, self.guard)
        self.assertEqual(result['games_replayed'], 8)
        self.assertTrue(result['frozen_models_unchanged'])
        sources = result['sources']
        self.assertEqual(sources['A']['source_model_id'], sources['C']['source_model_id'])
        self.assertNotEqual(sources['A']['evaluation_model_id'], sources['C']['evaluation_model_id'])
        self.assertEqual(len({s['evaluation_model_id'] for s in sources.values()}), 4)
        for pair in result['pairs']:
            self.assertEqual(pair['first_player_counts'], {'X': 1, 'O': 1})
            self.assertEqual((pair['new_trained_games'], pair['old_trained_games']), (8, 4))
            self.assertEqual(pair['wins'] + pair['draws'] + pair['losses'], 2)
        self.assertEqual(self.hashes, [previous.file_hash(p) for p in (self.source, self.new)])
        self.assertEqual(match.run_match(db, self.guard)['pairs'], result['pairs'])
        with CompactResearchService(db, read_only=True) as service:
            for table in ('weights', 'model_states', 'learning_updates', 'model_events'):
                self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0], 8)

    def test_midgame_resume_matches_uninterrupted_evaluation(self):
        def interrupt(kind, game):
            if game['ply'] == 7:
                raise RuntimeError('interruption')
        db = self.prepare(self.directory / 'resumed')
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            match.run_match(db, self.guard, event=interrupt)
        self.prepare(self.directory / 'resumed')
        result = match.run_match(db, self.guard)
        baseline = self.prepare(self.directory / 'baseline')
        self.assertEqual(result['pairs'], match.run_match(baseline, self.guard)['pairs'])
        def moves(path):
            with CompactResearchService(path, read_only=True) as service:
                return [service.repo.all('SELECT ply,move_code,branch,probability,rng_before,rng_after FROM moves WHERE game_id=? ORDER BY ply', (g['game_id'],))
                        for g in service.repo.all('SELECT game_id FROM games ORDER BY created_at,game_id')]
        self.assertEqual(moves(db), moves(baseline))

    def test_resume_rejects_changed_configuration_and_frozen_model(self):
        db = self.prepare()
        with self.assertRaisesRegex(ValueError, '設定'):
            match.prepare_match(self.directory, self.new, self.source, 4, 117, 8, self.guard)
        with CompactResearchService(db) as service:
            service.conn.execute("UPDATE models SET version=version+1 WHERE model_id='evaluation_A'")
        with self.assertRaisesRegex(ValueError, '凍結模型'):
            match.run_match(db, self.guard)

    def test_invalid_target_and_corrupted_checkpoint_roll_back_import(self):
        with self.assertRaisesRegex(ValueError, '目標'):
            match.read_pair(self.new, 9)
        rows = match.read_pair(self.new, 8)
        rows[0]['sha256'] = 'invalid'
        with patch.object(match, 'read_pair', return_value=rows):
            with self.assertRaisesRegex(ValueError, '校驗'):
                self.prepare()
        with CompactResearchService(self.directory / 'evaluation.sqlite3', read_only=True) as service:
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM models').fetchone()[0], 0)

    def test_reads_completed_checkpoint_while_source_has_active_training(self):
        copy = self.directory / 'active.sqlite3'
        with CompactResearchService(self.new, read_only=True) as service:
            service.backup(copy)
        with CompactResearchService(copy) as service:
            item = service.repo.one('SELECT * FROM extension_experiments WHERE id=1')
            game = service.new_game(item['model_a'], item['model_b'], seed=391)
            service.step(game)
            before = service.list_models()
            rows = match.read_pair(copy, 8)
            self.assertEqual([r['version'] for r in rows], [8, 8])
            self.assertEqual(before, service.list_models())
            self.assertEqual(service.get_game(game)['ply'], 1)

    def test_default_no_export_resume_and_later_optional_export(self):
        args = ['--source', str(self.source), '--directory', str(self.directory), '--additional', '4',
                '--batch-size', '2', '--budget-gb', '2', '--min-free-gb', '0']
        with patch.object(extension, 'export_extension', side_effect=AssertionError('must not export')):
            self.assertEqual(extension.main(args), 0)
            self.assertEqual(extension.main(args), 0)
        report = json.loads((self.directory / 'completion.json').read_text(encoding='utf-8'))
        self.assertIsNone(report['export'])
        self.assertEqual(report['verification']['new_games_replayed'], 4)
        self.assertFalse(list(self.directory.glob('*.zip')))
        with CompactResearchService(self.directory / 'training.sqlite3', read_only=True) as service:
            before = [tuple(r) for r in service.conn.execute('SELECT model_id,version,rng_state,revision FROM models ORDER BY model_id')]
        self.assertEqual(extension.main(args + ['--export', 'full']), 0)
        self.assertEqual(extension.main(args + ['--export', 'full']), 0)
        self.assertEqual(len(list(self.directory.glob('*.zip'))), 1)
        with CompactResearchService(self.directory / 'training.sqlite3', read_only=True) as service:
            self.assertEqual(before, [tuple(r) for r in service.conn.execute('SELECT model_id,version,rng_state,revision FROM models ORDER BY model_id')])

    def test_workflow_trains_verifies_and_matches_then_reruns_without_duplicates(self):
        args = ['--source', str(self.source), '--old-source', str(self.source), '--directory', str(self.directory),
                '--additional', '4', '--batch-size', '2', '--games-per-pair', '2', '--budget-gb', '2', '--min-free-gb', '0']
        for _ in range(2):
            self.assertEqual(workflow(args), 0)
            result = json.loads((self.directory / 'workflow-status.json').read_text(encoding='utf-8'))
            self.assertEqual(result['phase'], 'ready')
            self.assertEqual(result['completed_matches'], 8)
        self.assertFalse(list(self.directory.rglob('*.zip')))


if __name__ == '__main__':
    unittest.main()
