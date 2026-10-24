"""小樣本驗證固定局數訓練的中斷續跑與交易，不分析棋力。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from uttt.pilot import initialize, runner_lock, train_pilot, verify_records
from uttt.service import ResearchService


SETTINGS = {'total_games': 4, 'batch_size': 2, 'seed': 7452}


class SimulatedStop(RuntimeError):
    pass


def normalized_training(service):
    """排除 UUID／建立時間；比較原始棋譜、所有珠數與持久亂數。"""
    experiment = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    model_ids = (experiment['model_a'], experiment['model_b'])
    labels = dict(zip(model_ids, ('A', 'B')))
    model_fields = ('seed', 'rng_state', 'version', 'trained_games', 'update_count', 'revision')
    models = []
    for model_id in model_ids:
        model = service._model(model_id)
        weights = service.repo.all(
            'SELECT state_key,group_rep,bead_weight,update_count FROM weights '
            'WHERE model_id=? ORDER BY state_key,group_rep', (model_id,))
        models.append(({key: model[key] for key in model_fields}, weights))
    games = []
    for game in service.repo.all('SELECT * FROM games ORDER BY x_version'):
        moves = service.moves(game['game_id'])
        for move in moves:
            del move['game_id']
            move['model_id'] = labels[move['model_id']]
        games.append({
            'players': (labels[game['x_player']], labels[game['o_player']]),
            'seed': game['seed'], 'state': game['state_json'], 'result': game['result'],
            'versions': (game['x_version'], game['o_version']),
            'revisions': (game['x_revision'], game['o_revision']), 'moves': moves,
        })
    return {'models': models, 'games': games}


class PilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline_directory = tempfile.TemporaryDirectory(prefix='uttt-pilot-baseline-')
        with ResearchService(Path(cls.baseline_directory.name) / 'baseline.sqlite3') as service:
            train_pilot(service, **SETTINGS)
            cls.baseline = normalized_training(service)

    @classmethod
    def tearDownClass(cls):
        cls.baseline_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='uttt-pilot-tests-')
        self.db = Path(self.directory.name) / 'pilot.sqlite3'

    def tearDown(self):
        self.directory.cleanup()

    def assert_same_as_baseline(self, service):
        self.assertEqual(normalized_training(service), self.baseline)
        verified = verify_records(service)
        self.assertEqual(verified['games_replayed'], 4)
        self.assertEqual(sorted(verified['first_player_counts'].values()), [2, 2])
        self.assertTrue(verified['learning_records_verified'])

    def test_resume_after_midgame_completed_game_and_before_checkpoint(self):
        for stop_at in ('midgame', 'after_game', 'before_checkpoint'):
            with self.subTest(stop_at=stop_at):
                db = Path(self.directory.name) / (stop_at + '.sqlite3')

                def interrupt(event, data):
                    selected = (event == 'after_step' and data['ply'] == 7
                                and data['status'] == 'active') if stop_at == 'midgame' else event == stop_at
                    if selected:
                        raise SimulatedStop(stop_at)

                with ResearchService(db) as service:
                    with self.assertRaises(SimulatedStop):
                        train_pilot(service, **SETTINGS, event=interrupt)
                # 重新建立連線，確保依據持久資料恢復，而非沿用程序記憶體。
                with ResearchService(db) as service:
                    train_pilot(service, **SETTINGS)
                    self.assert_same_as_baseline(service)

    def test_completed_rerun_does_not_add_games_learning_or_checkpoints(self):
        with ResearchService(self.db) as service:
            train_pilot(service, **SETTINGS)
            before = normalized_training(service)
            tables = ('runs', 'games', 'moves', 'learning_updates', 'model_events',
                      'checkpoints', 'pilot_batches')
            counts = {table: service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                      for table in tables}
            train_pilot(service, **SETTINGS)
            self.assertEqual(normalized_training(service), before)
            self.assertEqual(counts, {
                table: service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in tables})
            self.assert_same_as_baseline(service)

    def test_resume_rejects_changed_configuration(self):
        with ResearchService(self.db) as service:
            initialize(service, **SETTINGS)
            for changed in ({'seed': 1}, {'batch_size': 1}, {'total_games': 6}):
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, '續跑'):
                    train_pilot(service, **{**SETTINGS, **changed})
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0], 0)

    def test_resume_rejects_abandoned_game(self):
        def interrupt(event, data):
            if event == 'after_step':
                raise SimulatedStop()

        with ResearchService(self.db) as service:
            with self.assertRaises(SimulatedStop):
                train_pilot(service, **SETTINGS, event=interrupt)
            game = service.repo.one("SELECT game_id FROM games WHERE status='active'")
            service.abandon(game['game_id'])
        with ResearchService(self.db) as service:
            with self.assertRaisesRegex(ValueError, '中止紀錄'):
                train_pilot(service, **SETTINGS)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM games').fetchone()[0], 1)

    def test_runner_lock_excludes_second_handle_and_releases_after_error(self):
        path = Path(self.directory.name) / 'runner.lock'
        with self.assertRaises(SimulatedStop):
            with runner_lock(path):
                with self.assertRaisesRegex(RuntimeError, '另一個程序'):
                    with runner_lock(path):
                        self.fail('第二次占用不應成功')
                raise SimulatedStop()
        with runner_lock(path):
            self.assertTrue(path.exists())

    def test_initialization_rolls_back_both_models_if_second_checkpoint_fails(self):
        with ResearchService(self.db) as service:
            original = service._save_checkpoint
            calls = 0

            def fail_second(model_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SimulatedStop('second checkpoint')
                return original(model_id)

            with patch.object(service, '_save_checkpoint', side_effect=fail_second):
                with self.assertRaises(SimulatedStop):
                    initialize(service, **SETTINGS)
            for table in ('models', 'checkpoints', 'pilot_experiments'):
                self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertFalse(service.conn.in_transaction)
        with ResearchService(self.db) as service:
            train_pilot(service, **SETTINGS)
            self.assert_same_as_baseline(service)

    def test_batch_checkpoint_failure_does_not_partially_mark_batch_complete(self):
        def stop_before_checkpoint(event, data):
            if event == 'before_checkpoint':
                raise SimulatedStop()

        with ResearchService(self.db) as service:
            with self.assertRaises(SimulatedStop):
                train_pilot(service, **SETTINGS, event=stop_before_checkpoint)
            original = service._save_checkpoint
            calls = 0

            def fail_second(model_id):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SimulatedStop('second batch checkpoint')
                return original(model_id)

            with patch.object(service, '_save_checkpoint', side_effect=fail_second):
                with self.assertRaises(SimulatedStop):
                    train_pilot(service, **SETTINGS)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 2)
            batch = service.repo.one('SELECT * FROM pilot_batches WHERE batch_index=0')
            self.assertEqual(batch['status'], 'active')
            self.assertIsNone(batch['checkpoint_a'])
            self.assertIsNone(batch['checkpoint_b'])
            self.assertEqual(service.repo.one('SELECT * FROM runs')['completed_games'], 2)
        with ResearchService(self.db) as service:
            train_pilot(service, **SETTINGS)
            self.assert_same_as_baseline(service)


if __name__ == '__main__':
    unittest.main()
