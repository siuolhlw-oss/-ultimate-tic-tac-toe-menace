"""以 2＋4 局驗證續訓，所有資料均使用隔離暫存資料夾。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.continue_selfplay import (
    RESERVE_GAME, StorageBudget, check_branch, export_continuation, file_hash,
    prepare_branch, train_continuation, verify_continuation,
)
from uttt.pilot import train_pilot
from uttt.service import ResearchService


class SimulatedStop(RuntimeError):
    pass


def normalized(service):
    pilot = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    ids = (pilot['model_a'], pilot['model_b'])
    fields = ('rng_state', 'version', 'revision', 'trained_games', 'update_count', 'seed')
    models = []
    for model_id in ids:
        model = service._model(model_id)
        models.append((tuple(model[k] for k in fields), service.repo.all(
            'SELECT state_key,group_rep,bead_weight,update_count FROM weights '
            'WHERE model_id=? ORDER BY state_key,group_rep', (model_id,))))
    games = []
    for game in service.repo.all('SELECT * FROM games ORDER BY x_version'):
        moves = service.repo.all('SELECT ply,move_code,actor,branch,probability,weight_before_choice,rng_before,rng_after '
                                 'FROM moves WHERE game_id=? ORDER BY ply', (game['game_id'],))
        games.append((game['x_version'], game['seed'], game['state_json'], game['result'], moves))
    return models, games


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix='uttt-continuation-source-')
        cls.source = Path(cls.fixtures.name) / 'source.sqlite3'
        with ResearchService(cls.source) as service:
            train_pilot(service, total_games=2, batch_size=2, seed=7452)
            cls.pilot = service.repo.one('SELECT * FROM pilot_experiments')
        cls.source_hash = file_hash(cls.source)
        with ResearchService(Path(cls.fixtures.name) / 'baseline.sqlite3') as service:
            train_pilot(service, total_games=6, batch_size=2, seed=7452)
            cls.baseline = normalized(service)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-continuation-tests-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.guard = StorageBudget(self.directory, 2_000_000_000, 0)

    def branch(self, directory=None):
        directory = directory or self.directory
        directory.mkdir(parents=True, exist_ok=True)
        guard = self.guard if directory == self.directory else StorageBudget(directory, 2_000_000_000, 0)
        return prepare_branch(self.source, directory, 4, 2, guard), guard

    def train(self, service, guard=None, event=None):
        return train_continuation(service, self.source, 4, 2, guard or self.guard, event=event)

    def assert_finished(self, service):
        self.assertEqual(normalized(service), self.baseline)
        self.assertEqual(service.repo.one('SELECT * FROM pilot_experiments'), self.pilot)
        verified = verify_continuation(service)
        self.assertEqual(verified['new_games_replayed'], 4)
        self.assertEqual(verified['total_training_games'], 6)
        self.assertEqual(sorted(verified['first_player_counts'].values()), [2, 2])
        self.assertEqual(file_hash(self.source), self.source_hash)

    def test_copy_continuation_matches_uninterrupted_six_games_and_preserves_source(self):
        db, guard = self.branch()
        with ResearchService(db) as service:
            result = self.train(service, guard)
            self.assertEqual(result['base_games'], 2)
            self.assertEqual(result['additional_games'], 4)
            self.assert_finished(service)
            self.assertEqual(service.repo.one('SELECT source_sha256 FROM continuation_experiments')['source_sha256'], self.source_hash)

    def test_interruptions_resume_from_persisted_board_rng_and_completed_counts(self):
        for point in ('midgame', 'after_game', 'before_checkpoint'):
            with self.subTest(point=point):
                db, guard = self.branch(self.directory / point)

                def interrupt(event, data):
                    stop = event == point
                    if point == 'midgame':
                        stop = event == 'after_step' and data['ply'] == 7 and data['status'] == 'active'
                    if stop:
                        raise SimulatedStop()

                with ResearchService(db) as service:
                    with self.assertRaises(SimulatedStop):
                        self.train(service, guard, interrupt)
                with ResearchService(db) as service:
                    self.train(service, guard)
                    self.assert_finished(service)

    def test_completed_rerun_and_existing_branch_do_not_copy_or_train_again(self):
        db, guard = self.branch()
        with ResearchService(db) as service:
            self.train(service, guard)
            before = normalized(service)
            tables = ('games', 'checkpoints', 'runs', 'learning_updates', 'model_events')
            counts = {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in tables}
        self.assertEqual(prepare_branch(self.source, self.directory, 4, 2, guard), db)
        with ResearchService(db) as service:
            self.train(service, guard)
            self.assertEqual(normalized(service), before)
            self.assertEqual(counts, {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in tables})

    def test_refuses_changed_configuration_script_and_abandoned_game(self):
        db, guard = self.branch()
        with ResearchService(db) as service:
            with self.assertRaisesRegex(ValueError, '相同來源'):
                train_continuation(service, self.source, 6, 2, guard)
            with patch('tools.continue_selfplay.script_hash', return_value='changed'):
                with self.assertRaisesRegex(ValueError, '相同來源'):
                    self.train(service, guard)

            def interrupt(event, data):
                if event == 'after_step':
                    raise SimulatedStop()

            with self.assertRaises(SimulatedStop):
                self.train(service, guard, interrupt)
            game = service.repo.one("SELECT game_id FROM games WHERE status='active'")
            service.abandon(game['game_id'])
            with self.assertRaisesRegex(ValueError, '中止紀錄'):
                self.train(service, guard)

    def test_batch_second_checkpoint_failure_rolls_back_then_resumes(self):
        db, guard = self.branch()
        with ResearchService(db) as service:
            original = service._save_checkpoint
            calls = 0

            def fail_second(model):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SimulatedStop()
                return original(model)

            with patch.object(service, '_save_checkpoint', side_effect=fail_second):
                with self.assertRaises(SimulatedStop):
                    self.train(service, guard)
            batch = service.repo.one('SELECT * FROM continuation_batches')
            self.assertEqual(batch['status'], 'active')
            self.assertIsNone(batch['checkpoint_a'])
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 4)
        with ResearchService(db) as service:
            self.train(service, guard)
            self.assert_finished(service)

    def test_capacity_stop_before_checkpoint_retains_finished_games(self):
        db, guard = self.branch()
        original = guard.check

        def stop_checkpoint(reserve=0):
            if reserve > RESERVE_GAME:
                raise OSError('checkpoint headroom')
            return original(reserve)

        with ResearchService(db) as service:
            with patch.object(guard, 'check', side_effect=stop_checkpoint):
                with self.assertRaisesRegex(OSError, 'checkpoint headroom'):
                    self.train(service, guard)
            self.assertEqual(service._model(self.pilot['model_a'])['version'], 4)
            self.assertEqual(service.repo.one('SELECT status FROM continuation_batches')['status'], 'active')
        with ResearchService(db) as service:
            self.train(service, guard)
            self.assert_finished(service)

    def test_budget_counts_all_files_and_reserves_disk_free_before_writing(self):
        (self.directory / 'example-wal').write_bytes(b'x' * 100)
        (self.directory / 'example.zip').write_bytes(b'x' * 100)
        guard = StorageBudget(self.directory, 250, 1000)
        with patch('tools.continue_selfplay.shutil.disk_usage') as usage:
            usage.return_value.free = 2000
            with self.assertRaisesRegex(OSError, '容量預算'):
                guard.check(51)
            self.assertEqual(guard.check(50)['used_bytes'], 200)
            usage.return_value.free = 1049
            with self.assertRaisesRegex(OSError, '磁碟空間'):
                guard.check(50)

    def test_failed_initialization_does_not_publish_or_alter_source(self):
        with patch('tools.continue_selfplay._initialize_copy', side_effect=SimulatedStop()):
            with self.assertRaises(SimulatedStop):
                self.branch()
        self.assertFalse((self.directory / 'training.sqlite3').exists())
        self.assertEqual(list(self.directory.glob('*.partial.sqlite3*')), [])
        self.assertEqual(file_hash(self.source), self.source_hash)

    def test_complete_export_contains_source_and_continuation_mapping(self):
        db, guard = self.branch()
        destination = self.directory / 'records.zip'
        with ResearchService(db) as service:
            self.train(service, guard)
            exported = export_continuation(service, destination, guard)
            with zipfile.ZipFile(destination) as archive:
                data = json.loads(archive.read('research.json'))
                self.assertEqual(data['pilot_experiments'][0]['total_games'], 2)
                self.assertEqual(data['continuation_experiments'][0]['target_version'], 6)
                self.assertEqual(len(data['continuation_batches']), 2)
                self.assertEqual(len(data['games']), 6)
                self.assertIn('continuation_experiments.csv', archive.namelist())
                self.assertIn('continuation_batches.csv', archive.namelist())
            self.assertEqual(exported['sha256'], hashlib.sha256(destination.read_bytes()).hexdigest())
            self.assertEqual(exported['rows']['continuation_batches'], 2)
            with self.assertRaises(FileExistsError):
                export_continuation(service, destination, guard)


if __name__ == '__main__':
    unittest.main()
