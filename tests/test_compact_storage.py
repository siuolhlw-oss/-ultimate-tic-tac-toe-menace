"""隔離資料驗證無損遷移、相同續訓、事件還原、凍結與回滾。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
from random import Random
import sqlite3
import tempfile
import unittest

from storage_v3.migration import migrate, table_digest, validate_source
from storage_v3.repository import CompactResearchService
from storage_v3.verification import ReadOnlyService, compare_histories, verify_replays
from uttt.engine import InvalidMove
from uttt.pilot import train_pilot
from uttt.service import ResearchService, dumps
from tools.continue_selfplay import StorageBudget, prepare_branch, train_continuation, verify_continuation


def finish(service, game):
    while service.get_game(game)['status'] == 'active':
        service.step(game)


class CompactStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-v3-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite3'
        self.compact = self.root / 'compact.sqlite3'

    def fixture(self):
        with ResearchService(self.source) as service:
            a = service.create_model('A', 771)
            b = service.create_model('B', 994)
            service.train(a, b, games=2, seed=82)
            service.evaluate(a, games=2, seed=93)
            abandoned = service.new_game(a, 'random', seed=11)
            for _ in range(3):
                service.step(abandoned)
            service.abandon(abandoned)
            active = service.new_game(b, a, seed=12)
            for _ in range(7):
                service.step(active)
        return a, b, abandoned, active

    def test_all_logical_tables_round_trip_and_source_stays_unchanged(self):
        self.fixture()
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        report = migrate(self.source, self.compact)
        self.assertTrue(report['verification']['all_logical_tables_equal'])
        self.assertGreater(report['saved_fraction'], 0.5)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        with closing(sqlite3.connect(self.source)) as source, CompactResearchService(self.compact) as target:
            for table in validate_source(source):
                self.assertEqual(table_digest(source, table), table_digest(target.conn, table), table)
            self.assertEqual(target.conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertFalse(list(self.root.glob('.snapshot-*')))

    def test_midgame_resume_produces_identical_moves_updates_rng_and_weights(self):
        a, b, _, active = self.fixture()
        migrate(self.source, self.compact)
        with ResearchService(self.source) as old, CompactResearchService(self.compact) as new:
            for actor in ('X', 'O'):
                self.assertEqual(old.model_at_game_start(active, actor), new.model_at_game_start(active, actor))
            finish(old, active)
            finish(new, active)
            self.assertEqual(old.moves(active), new.moves(active))
            for table in ('models', 'weights', 'learning_updates', 'model_events'):
                self.assertEqual(table_digest(old.conn, table), table_digest(new.conn, table), table)
            for actor in ('X', 'O'):
                self.assertEqual(old.model_at_game_start(active, actor), new.model_at_game_start(active, actor))

    def test_frozen_evaluation_keeps_training_models_identical(self):
        with CompactResearchService(self.compact) as service:
            a = service.create_model('A', 813)
            b = service.create_model('B', 950)
            service.train(a, b, games=2, seed=17)
            before = {t: table_digest(service.conn, t) for t in ('models', 'weights', 'model_states', 'model_events')}
            service.evaluate(a, games=2, seed=40)
            self.assertEqual(before, {t: table_digest(service.conn, t) for t in before})

    def test_pool_changes_roll_back_with_failed_transaction_and_retry(self):
        with CompactResearchService(self.compact) as service:
            a = service.create_model('A', 73)
            original = service._model(a)['rng_state']
            before = service.conn.execute('SELECT COUNT(*) FROM _rng_states').fetchone()[0]
            state = dumps(Random(847).getstate())
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with service.repo.transaction():
                    service.conn.execute('UPDATE models SET rng_state=? WHERE model_id=?', (state, a))
                    raise RuntimeError('stop')
            self.assertEqual(service._model(a)['rng_state'], original)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM _rng_states').fetchone()[0], before)
            with service.repo.transaction():
                service.conn.execute('UPDATE models SET rng_state=? WHERE model_id=?', (state, a))
            self.assertEqual(service._model(a)['rng_state'], state)

    def test_rng_whitespace_and_gaussian_cache_are_preserved_exactly(self):
        with CompactResearchService(self.compact) as service:
            a = service.create_model('A', 3)
            rng = Random(5)
            rng.gauss(0, 1)
            for value in (dumps(rng.getstate()), json.dumps(rng.getstate(), indent=2)):
                with service.repo.transaction():
                    service.conn.execute('UPDATE models SET rng_state=? WHERE model_id=?', (value, a))
                self.assertEqual(service._model(a)['rng_state'], value)

    def test_invalid_move_cannot_change_pool_or_create_states(self):
        with CompactResearchService(self.compact) as service:
            game = service.new_game('human', 'random', seed=17, mode='demo')
            before = {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                      for t in ('_states', '_rng_states', '_rng_bodies', '_moves')}
            with self.assertRaises(InvalidMove):
                service.step(game, 10)
            self.assertEqual(before, {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in before})

    def test_corrupt_checkpoint_and_rng_body_are_rejected(self):
        with CompactResearchService(self.compact) as service:
            a = service.create_model('A', 73)
            cp = service.list_checkpoints(a)[0]['checkpoint_id']
            service.conn.execute("UPDATE _checkpoints SET payload_zlib=X'010203' WHERE checkpoint_id=?", (cp,))
            with self.assertRaises(sqlite3.OperationalError):
                service._checkpoint(cp)
            service.conn.execute("UPDATE _rng_bodies SET payload_zlib=X'010203'")
            with self.assertRaises(sqlite3.OperationalError):
                service._model(a)

    def test_failed_migration_leaves_source_and_existing_destination_untouched(self):
        self.fixture()
        original = self.source.read_bytes()

        def fail(stage, count):
            if stage == 'copied:checkpoints':
                raise RuntimeError('interrupted')

        with self.assertRaises(RuntimeError):
            migrate(self.source, self.compact, progress=fail)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.compact.exists())
        self.assertFalse(list(self.root.glob('.*')))
        self.compact.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):
            migrate(self.source, self.compact)
        self.assertEqual(self.compact.read_bytes(), b'existing')

    def test_compact_entry_refuses_v2_without_in_place_migration(self):
        self.fixture()
        original = self.source.read_bytes()
        with self.assertRaises(ValueError):
            CompactResearchService(self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_readonly_replay_and_history_verification_preserve_both_files(self):
        self.fixture()
        migrate(self.source, self.compact)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.source, self.compact)}
        replay_result = verify_replays(self.compact)
        self.assertEqual(replay_result['games_replayed'], 6)
        self.assertEqual(replay_result['finished_games'], 4)
        self.assertEqual(compare_histories(self.source, self.compact)['sampled_games'], 2)
        with ReadOnlyService(self.compact) as service:
            with self.assertRaises(sqlite3.OperationalError):
                service.conn.execute("DELETE FROM _moves")
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_storage_budget_failure_preserves_source_and_publishes_nothing(self):
        self.fixture()
        before = self.source.read_bytes()
        with self.assertRaises(OSError):
            migrate(self.source, self.compact, guard=StorageBudget(self.root, 1, 0))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.compact.exists())

    def test_partial_continuation_metadata_resumes_same_target(self):
        with ResearchService(self.source) as service:
            train_pilot(service, total_games=2, batch_size=2, seed=541)
        branch = self.root / 'old-branch'
        guard = StorageBudget(branch, 2_000_000_000, 0)
        raw = prepare_branch(self.source, branch, additional=4, batch_size=2, guard=guard)

        def stop(event, data):
            if event == 'after_step' and data['ply'] == 7:
                raise RuntimeError('stop')

        with ResearchService(raw) as service:
            with self.assertRaises(RuntimeError):
                train_continuation(service, self.source, 4, 2, guard, event=stop)
        migrate(raw, self.compact)
        compact_guard = StorageBudget(self.root, 2_000_000_000, 0)
        with ResearchService(raw) as old, CompactResearchService(self.compact) as new:
            train_continuation(old, self.source, 4, 2, guard)
            train_continuation(new, self.source, 4, 2, compact_guard)
            self.assertEqual(verify_continuation(new)['total_training_games'], 6)
            ids_old = [g['game_id'] for g in old.repo.all('SELECT game_id FROM games ORDER BY x_version')]
            ids_new = [g['game_id'] for g in new.repo.all('SELECT game_id FROM games ORDER BY x_version')]
            # 快照中的前 3 局沿用 ID，後續新局 UUID 各自生成；比較棋步與持久亂數。
            for left, right in zip(ids_old, ids_new):
                columns = 'ply,actor,move_code,branch,rng_before,rng_after,weight_before_choice,probability'
                self.assertEqual(old.repo.all('SELECT ' + columns + ' FROM moves WHERE game_id=? ORDER BY ply', (left,)),
                                 new.repo.all('SELECT ' + columns + ' FROM moves WHERE game_id=? ORDER BY ply', (right,)))
            self.assertEqual(table_digest(old.conn, 'models'), table_digest(new.conn, 'models'))
            self.assertEqual(table_digest(old.conn, 'weights'), table_digest(new.conn, 'weights'))


if __name__ == '__main__':
    unittest.main()
