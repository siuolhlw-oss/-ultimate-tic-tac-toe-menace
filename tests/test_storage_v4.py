"""隔離測試 v4 的無損最新狀態、行為相等與可靠恢復。"""
from contextlib import closing
import json
from pathlib import Path
from random import Random
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from storage_v3.repository import CompactResearchService
from storage_v4 import codec
from storage_v4.artifacts import backup_latest, archive_milestone, restore_archive
from storage_v4.migration import migrate, code_contract
from storage_v4.repository import SlimService
from tools.continue_selfplay import StorageBudget, file_hash, save_json
from tools.train_million import run, validate_contract
from tools.verify_million import compare_game, verify
from uttt.engine import State, legal_moves, play
from uttt.menace import ModelConsistencyError, choose
from uttt.service import dumps
from uttt.pilot import runner_lock
from uttt.symmetry import canonicalize, action_groups


class StorageV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-v4-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source.sqlite3'
        self.directory = self.root / 'slim'
        self.directory.mkdir()
        self.target = self.directory / 'training.sqlite3'
        self.budget = StorageBudget(self.directory, 1_000_000_000, 0)
        with CompactResearchService(self.source) as service:
            a = service.create_model('A', 771)
            b = service.create_model('B', 994)
            service.train(a, b, games=2, seed=82)

    def migrate(self, target=20):
        return migrate(self.source, self.target, expected_base=2, target=target, eval_games=4,
                       require_extension=False, budget=self.budget)

    def test_codec_roundtrip_keys_forced_and_terminal(self):
        rng = Random(101)
        state = State()
        while True:
            canonical, _ = canonicalize(state)
            packed = codec.pack(canonical)
            self.assertEqual(len(packed), 27)
            self.assertEqual(codec.unpack(packed), canonical)
            self.assertEqual(codec.from_legacy(codec.legacy_key(packed)), packed)
            if state.status != 'active':
                break
            state = play(state, rng.choice(legal_moves(state)))
        a, b = State(turn='X'), State(turn='O')
        self.assertNotEqual(codec.pack(a), codec.pack(b))
        self.assertNotEqual(codec.pack(State(forced=1)), codec.pack(State(forced=2)))
        for invalid in [b'', b'\xff' * 27, codec.pack(a)[:-1] + b'\x02']:
            with self.assertRaises(ValueError):
                codec.unpack(invalid)

    def test_migration_and_identical_continuation(self):
        before = file_hash(self.source)
        report = self.migrate()
        self.assertTrue(report['models_equal'])
        self.assertEqual(file_hash(self.source), before)
        with CompactResearchService(self.source) as old, SlimService(self.target) as new:
            self.assertEqual(new.check_head(), 2)
            self.assertLess(new.conn.execute('SELECT COUNT(*) FROM weights').fetchone()[0],
                            old.conn.execute('SELECT COUNT(*) FROM _weights').fetchone()[0])
            for _ in range(8):
                compare_game(old, new)
            self.assertEqual(new.head(), 10)
            self.assertEqual(new.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_failure_midgame_rolls_back_models_boxes_and_retries_same(self):
        self.migrate()
        with SlimService(self.target) as new, CompactResearchService(self.source) as old:
            before = new.models()
            counts = [new.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ('states', 'boxes', 'weights', 'summaries')]
            def fail(state):
                if state.ply == 17:
                    raise RuntimeError('simulated interruption')
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                new.train_one(on_move=fail)
            self.assertEqual(before, new.models())
            self.assertEqual(counts, [new.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ('states', 'boxes', 'weights', 'summaries')])
            compare_game(old, new)

    def test_frozen_evaluation_is_repeatable_and_does_not_write(self):
        self.migrate()
        with SlimService(self.target) as service:
            before = '\n'.join(service.conn.iterdump())
            result = service.evaluate(1, games=8, seed=998)
            self.assertEqual(result, service.evaluate(1, games=8, seed=998))
            self.assertEqual(sum(result[k] for k in ('wins', 'draws', 'losses')), 8)
            self.assertEqual(sum(result['as_x'].values()), 4)
            self.assertEqual(sum(result['as_o'].values()), 4)
            self.assertEqual(before, '\n'.join(service.conn.iterdump()))
            service.save_evaluation(result)
            self.assertEqual(service.head(), 2)

    def test_process_death_recovers_last_whole_game(self):
        self.migrate()
        with SlimService(self.target) as service:
            before = '\n'.join(service.conn.iterdump())
        script = ('import os,sys\nfrom storage_v4.repository import SlimService\n'
                  'with SlimService(sys.argv[1]) as service:\n'
                  ' service.train_one(on_move=lambda state: os._exit(23) if state.ply == 17 else None)\n')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.target)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors='replace'))
        with SlimService(self.target) as new, CompactResearchService(self.source) as old:
            self.assertEqual('\n'.join(new.conn.iterdump()), before)
            compare_game(old, new)

    def test_missing_source_default_is_not_silently_invented(self):
        with CompactResearchService(self.source) as old:
            row = old.conn.execute('SELECT model_id,state_id,group_rep FROM _weights '
                                   'WHERE bead_weight=3 AND update_count=0 LIMIT 1').fetchone()
            old.conn.execute('DELETE FROM _weights WHERE model_id=? AND state_id=? AND group_rep=?', tuple(row))
            old.conn.commit()
        with self.assertRaisesRegex(ValueError, '缺少合法'):
            self.migrate()
        self.assertFalse(self.target.exists())

    def test_duplicate_runner_cannot_clear_stop_and_unverified_cannot_train(self):
        self.migrate()
        with self.assertRaisesRegex(ValueError, '驗證報告'):
            run(self.directory)
        request = self.directory / 'stop-request.json'
        save_json(request, {'requested': True})
        with runner_lock(self.directory / 'runner.lock'):
            with self.assertRaises((RuntimeError, OSError)):
                run(self.directory, resume=True)
        self.assertTrue(request.is_file())
        with SlimService(self.target, read_only=True) as service:
            self.assertEqual(service.head(), 2)

    def test_known_unseen_zero_and_returned_default_remain_distinct(self):
        self.migrate()
        with SlimService(self.target) as service:
            state = canonicalize(play(State(), 55))[0]
            groups = action_groups(state)
            with service.transaction():
                sid, _, _ = service._box_weights(1, state, groups, game=3, ply=2)
                service.conn.execute('DELETE FROM weights WHERE model=1 AND state=?', (sid,))
                for rep in groups:
                    service.conn.execute('INSERT INTO weights VALUES(?,?,?,?,?)', (1, sid, rep, 0, 3))
                _, weights, _ = service._box_weights(1, state, groups)
                self.assertTrue(all(v == 0 for v in weights.values()))
                self.assertEqual(choose(state, weights, Random(1)).branch, 'zero_uniform')
                rep = next(iter(groups))
                service.conn.execute('UPDATE weights SET weight=3,updates=4 WHERE model=1 AND state=? AND action=?', (sid, rep))
                self.assertEqual(tuple(service.conn.execute('SELECT weight,updates FROM weights WHERE model=1 AND state=? AND action=?', (sid, rep)).fetchone()), (3, 4))
            with self.assertRaises(ModelConsistencyError):
                service._box_weights(1, State('XOXXOOOXX' * 9), {})

    def test_illegal_saved_action_is_error(self):
        self.migrate()
        with SlimService(self.target) as service:
            state = canonicalize(play(State(), 55))[0]
            groups = action_groups(state)
            with service.transaction():
                sid, _, _ = service._box_weights(1, state, groups, game=3, ply=2)
                service.conn.execute('INSERT INTO weights VALUES(?,?,?,?,?)', (1, sid, 11, 3, 1))
            with self.assertRaises(ModelConsistencyError):
                service._box_weights(1, state, groups)

    def test_backup_archive_restore_and_corruption(self):
        self.migrate()
        with SlimService(self.target) as service:
            service.train_one()
            expected = service.models()
            record = backup_latest(service, self.directory, self.budget)
            self.assertEqual(record['version'], 3)
            archive_milestone(self.directory / 'latest-backup.sqlite3', self.directory, 3, self.budget)
        archive = self.directory / 'model-0000003.sqlite3.gz'
        restored = self.directory / 'restored.sqlite3'
        restore_archive(archive, restored, self.budget)
        with SlimService(restored, read_only=True) as service:
            self.assertEqual(service.models(), expected)
        with self.assertRaises(FileExistsError):
            restore_archive(archive, restored, self.budget)
        with archive.open('ab') as output:
            output.write(b'corruption')
        with self.assertRaises(ValueError):
            restore_archive(archive, self.directory / 'bad.sqlite3', self.budget)
        self.assertFalse((self.directory / 'bad.sqlite3').exists())

    def test_budget_and_existing_destination_leave_source_intact(self):
        before = file_hash(self.source)
        with self.assertRaises(OSError):
            migrate(self.source, self.target, expected_base=2, target=20, require_extension=False,
                    budget=StorageBudget(self.directory, 1, 0))
        self.assertFalse(self.target.exists())
        self.assertEqual(file_hash(self.source), before)
        self.migrate()
        with self.assertRaises(FileExistsError):
            self.migrate()

    def test_contract_validation_and_readonly_write_rejected(self):
        self.migrate()
        with SlimService(self.target) as service:
            validate_contract(service)
            service.set_meta('code_contract', {})
            with self.assertRaises(ValueError):
                validate_contract(service)
        with SlimService(self.target, read_only=True) as service:
            with self.assertRaises(sqlite3.OperationalError):
                service.train_one()

    def test_acceptance_runner_maintenance_resume_and_target(self):
        self.migrate(target=6)
        result = verify(self.directory, games=2)
        self.assertTrue(result['passed'])
        with SlimService(self.target) as service:
            config = service.meta('config')
            config.update(eval_every=2, milestones=[4, 6])
            service.set_meta('config', config)
        run(self.directory, stop_after=2)
        with SlimService(self.target, read_only=True) as service:
            self.assertEqual(service.head(), 4)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0], 4)
        run(self.directory)
        with SlimService(self.target, read_only=True) as service:
            self.assertEqual(service.head(), 6)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0], 6)
        with SlimService(self.target) as service:
            with self.assertRaisesRegex(ValueError, '已達'):
                service.train_one()
        self.assertEqual(run(self.directory)['phase'], 'completed')


if __name__ == '__main__':
    unittest.main()
