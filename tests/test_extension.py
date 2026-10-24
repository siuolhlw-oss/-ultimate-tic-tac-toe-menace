"""下一代 v3 續訓與新穎率的隔離驗證。"""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from storage_v3.migration import migrate
from storage_v3.repository import CompactResearchService
from tools import continue_selfplay as previous
from tools import extend_compact_selfplay as extension
from tools.novelty_statistics import collect, publish, sequence_key
from uttt.pilot import train_pilot
from uttt.service import ResearchService
from uttt.symmetry import transform_move


def normalized(service):
    pilot = service.repo.one('SELECT * FROM pilot_experiments')
    models = []
    for model_id in (pilot['model_a'], pilot['model_b']):
        model = service._model(model_id)
        models.append(([model[k] for k in ('rng_state', 'version', 'revision', 'trained_games', 'update_count', 'seed')],
            service.repo.all('SELECT state_key,group_rep,bead_weight,update_count FROM weights WHERE model_id=? ORDER BY state_key,group_rep', (model_id,))))
    games = []
    for game in service.repo.all("SELECT game_id,x_version,seed,state_json,result FROM games WHERE mode='train' ORDER BY x_version"):
        rows = service.repo.all('SELECT ply,move_code,actor,branch,probability,weight_before_choice,rng_before,rng_after '
                                'FROM moves WHERE game_id=? ORDER BY ply', (game['game_id'],))
        games.append((game['x_version'], game['seed'], game['state_json'], game['result'], rows))
    return models, games


class ExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix='uttt-extension-source-')
        root = Path(cls.fixtures.name)
        initial = root / 'initial.sqlite3'
        with ResearchService(initial) as service:
            train_pilot(service, total_games=2, batch_size=2, seed=7452)
        guard = previous.StorageBudget(root / 'second', 2_000_000_000, 0)
        second = previous.prepare_branch(initial, root / 'second', 2, 2, guard)
        with ResearchService(second) as service:
            previous.train_continuation(service, initial, 2, 2, guard)
        cls.source = root / 'compact.sqlite3'
        migrate(second, cls.source)
        cls.source_sha = previous.file_hash(cls.source)
        with ResearchService(root / 'baseline.sqlite3') as service:
            train_pilot(service, total_games=8, batch_size=2, seed=7452)
            cls.baseline = normalized(service)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-extension-tests-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.guard = previous.StorageBudget(self.directory, 2_000_000_000, 0)

    def branch(self):
        return extension.prepare_branch(self.source, self.directory, 4, 2, self.guard)

    def train(self, service, event=None):
        return extension.train_extension(service, self.source, 4, 2, self.guard, event=event)

    def test_matches_uninterrupted_training_and_preserves_source_export_and_rerun(self):
        db = self.branch()
        with CompactResearchService(db) as service:
            parent = extension.lineage(service)
            self.train(service)
            self.assertEqual(normalized(service), self.baseline)
            self.assertEqual(extension.lineage(service), parent)
            self.assertEqual(extension.verify_extension(service)['new_games_replayed'], 4)
            counts = {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ('games', 'checkpoints', 'model_events')}
            self.train(service)
            self.assertEqual(counts, {t: service.conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in counts})
            archive = extension.export_extension(service, self.directory / 'records.zip', self.guard)
        self.assertEqual(previous.file_hash(self.source), self.source_sha)
        self.assertEqual(extension.prepare_branch(self.source, self.directory, 4, 2, self.guard), db)
        with zipfile.ZipFile(archive['path']) as z:
            data = json.loads(z.read('research.json'))
            for table in extension.EXTRA_TABLES:
                self.assertIn(table, data)
        stats = publish(db, self.directory, block_size=2, baseline_games=4)
        self.assertEqual(stats['completed_games'], 8)
        self.assertIsNotNone(stats['comparison_to_last_baseline_block'])

    def test_midgame_resume_matches_moves_weights_and_rng(self):
        db = self.branch()
        def interrupt(event, data):
            if event == 'after_step' and data['ply'] == 7:
                raise RuntimeError('simulated interruption')
        with CompactResearchService(db) as service:
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                self.train(service, interrupt)
        with CompactResearchService(db) as service:
            self.train(service)
            self.assertEqual(normalized(service), self.baseline)

    def test_checkpoint_failure_rolls_back_and_resumes(self):
        db = self.branch()
        with CompactResearchService(db) as service:
            original = service._save_checkpoint
            calls = 0
            def fail_second(model):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError('second checkpoint')
                return original(model)
            before = service.conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]
            with patch.object(service, '_save_checkpoint', side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, 'second checkpoint'):
                    self.train(service)
            self.assertEqual(service.conn.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], before)
            self.assertEqual(service.repo.one('SELECT status FROM extension_batches')['status'], 'active')
        with CompactResearchService(db) as service:
            self.train(service)
            self.assertEqual(normalized(service), self.baseline)

    def test_rejects_changed_configuration_code_and_parent_metadata(self):
        db = self.branch()
        with CompactResearchService(db) as service:
            with self.assertRaisesRegex(ValueError, '相同來源'):
                extension.train_extension(service, self.source, 6, 2, self.guard)
            with patch.object(extension, 'dependency_hashes', return_value={}):
                with self.assertRaisesRegex(ValueError, '程式已改變'):
                    self.train(service)
            service.conn.execute("UPDATE continuation_experiments SET status='active'")
            with self.assertRaisesRegex(ValueError, '先前試驗'):
                self.train(service)

    def test_cli_completes_verification_export_statistics_and_rerun(self):
        command = [sys.executable, '-X', 'utf8', str(Path(extension.__file__).resolve()),
                   '--source', str(self.source), '--directory', str(self.directory),
                   '--additional', '4', '--batch-size', '2', '--budget-gb', '2', '--min-free-gb', '0']
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((self.directory / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['phase'], 'ready')
            self.assertEqual(status['completed_games'], 8)
        report = json.loads((self.directory / 'completion.json').read_text(encoding='utf-8'))
        self.assertEqual(report['verification']['new_games_replayed'], 4)
        self.assertEqual(len(list(self.directory.glob('extension-records-*.zip'))), 1)


class NoveltyTests(unittest.TestCase):
    def test_fixed_global_transform_preserves_sequence_order(self):
        moves = [12, 23, 34, 41]
        self.assertEqual(sequence_key(moves), sequence_key([transform_move(m, 1) for m in moves]))
        self.assertNotEqual(sequence_key(moves), sequence_key([34, 41, 12, 23]))

    def test_new_states_and_whole_games_are_separate_and_evaluation_excluded(self):
        with tempfile.TemporaryDirectory(prefix='uttt-novelty-') as temporary:
            db = Path(temporary) / 'counts.sqlite3'
            with sqlite3.connect(db) as c:
                c.executescript('''PRAGMA user_version=3;
                    CREATE TABLE _games(game_id TEXT,x_version INT,o_version INT,ply INT,rule_version TEXT,mode TEXT,status TEXT,is_example INT);
                    CREATE TABLE _moves(game_id TEXT,ply INT,move_code INT,state_id INT,new_box INT,model_id TEXT);''')
                for index, mode, status in ((0, 'train', 'finished'), (1, 'train', 'finished'),
                                             (2, 'eval', 'finished'), (2, 'train', 'active')):
                    gid = str(index) + mode
                    c.execute('INSERT INTO _games VALUES(?,?,?,?,?,?,?,?)', (gid,index,index,2,'rule',mode,status,0))
                    codes = [12, 23] if index == 0 else [transform_move(m, 1) for m in (12, 23)]
                    for ply, code in enumerate(codes, 1):
                        c.execute('INSERT INTO _moves VALUES(?,?,?,?,?,?)', (gid,ply,code,ply,int(index == 0),'A'))
            c.close()
            result = collect(db, block_size=1)
            self.assertEqual(result['completed_games'], 2)
            self.assertEqual(result['totals']['unique_decision_states'], 2)
            self.assertEqual(result['totals']['unique_raw_games'], 2)
            self.assertEqual(result['totals']['unique_d4_games'], 1)
            self.assertEqual([b['new_state_pct'] for b in result['blocks']], [100, 0])
            self.assertEqual([b['new_d4_game_pct'] for b in result['blocks']], [100, 0])


if __name__ == '__main__':
    unittest.main()
