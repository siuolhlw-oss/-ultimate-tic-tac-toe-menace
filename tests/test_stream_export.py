"""逐列匯出的契約、快照與不可覆寫測試；不接觸正式研究資料。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from uttt.service import ResearchService
from uttt import stream_export


class StreamExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='uttt-export-tests-')
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / 'test.sqlite3'
        self.service = ResearchService(self.db)
        self.addCleanup(self.service.close)
        self.destination = Path(self.directory.name) / 'records.zip'

    def test_round_trip_matches_existing_export_contract(self):
        a = self.service.create_model('=測試A', 101)
        b = self.service.create_model('測試B', 102)
        self.service.train(a, b, 2, seed=103)
        reference = self.service.export_data()
        result = stream_export.export_archive(self.service, self.destination)
        self.assertEqual(result['bytes'], self.destination.stat().st_size)
        self.assertEqual(result['sha256'], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        with zipfile.ZipFile(io.BytesIO(reference)) as old, zipfile.ZipFile(self.destination) as new:
            self.assertEqual(set(old.namelist()), set(new.namelist()))
            expected = json.loads(old.read('research.json'))
            actual = json.loads(new.read('research.json'))
            self.assertEqual(actual['dependencies'], {})
            self.assertEqual(actual['manifest']['format_version'], 1)
            for table in stream_export.TABLES:
                self.assertEqual(actual[table], expected[table])
                self.assertEqual(result['rows'][table], len(actual[table]))
                if actual[table]:
                    self.assertTrue(new.read(table + '.csv').startswith(b'\xef\xbb\xbf'))
                    rows = lambda archive: list(csv.DictReader(io.StringIO(
                        archive.read(table + '.csv').decode('utf-8-sig'))))
                    self.assertEqual(rows(new), rows(old))
            models = list(csv.DictReader(io.StringIO(new.read('models.csv').decode('utf-8-sig'))))
            self.assertEqual(models[0]['name'], "'=測試A")
            checkpoints = list(csv.DictReader(io.StringIO(new.read('checkpoints.csv').decode('utf-8-sig'))))
            self.assertNotIn('payload_json', checkpoints[0])
            self.assertIn('payload_json', actual['checkpoints'][0])

    def test_export_uses_one_snapshot_even_with_concurrent_writer(self):
        self.service.create_model('原模型', 1)
        original_write_json = stream_export._write_json

        def concurrent_write(*args):
            with ResearchService(self.db) as other:
                other.create_model('快照後新增', 2)
            original_write_json(*args)

        with patch.object(stream_export, '_write_json', side_effect=concurrent_write):
            result = stream_export.export_archive(self.service, self.destination)
        self.assertEqual(len(self.service.list_models()), 2)
        self.assertEqual(result['rows']['models'], 1)
        with zipfile.ZipFile(self.destination) as archive:
            data = json.loads(archive.read('research.json'))
            rows = list(csv.DictReader(io.StringIO(archive.read('models.csv').decode('utf-8-sig'))))
            self.assertEqual([r['name'] for r in data['models']], ['原模型'])
            self.assertEqual([r['name'] for r in rows], ['原模型'])

    def test_refuses_existing_destination_and_racing_destination(self):
        self.destination.write_bytes('原有內容'.encode())
        with self.assertRaises(FileExistsError):
            stream_export.export_archive(self.service, self.destination)
        self.assertEqual(self.destination.read_bytes(), '原有內容'.encode())
        self.destination.unlink()
        original_publish = stream_export._publish_without_overwrite

        def race(partial, destination):
            destination.write_bytes('其他程序建立'.encode())
            original_publish(partial, destination)

        with patch.object(stream_export, '_publish_without_overwrite', side_effect=race):
            with self.assertRaises(FileExistsError):
                stream_export.export_archive(self.service, self.destination)
        self.assertEqual(self.destination.read_bytes(), '其他程序建立'.encode())
        self.assertEqual(list(self.destination.parent.glob('*.partial')), [])

    def test_optional_pilot_metadata_is_preserved(self):
        self.service.conn.execute('CREATE TABLE pilot_experiments (experiment_id TEXT, config_json TEXT)')
        self.service.conn.execute('CREATE TABLE pilot_batches (experiment_id TEXT, run_id TEXT)')
        self.service.conn.execute('INSERT INTO pilot_experiments VALUES (?,?)', ('example', '{"seed":123}'))
        self.service.conn.execute('INSERT INTO pilot_batches VALUES (?,?)', ('example', 'run_example'))
        result = stream_export.export_archive(self.service, self.destination)
        with zipfile.ZipFile(self.destination) as archive:
            data = json.loads(archive.read('research.json'))
            self.assertEqual(data['pilot_experiments'][0]['config_json'], '{"seed":123}')
            self.assertEqual(data['pilot_batches'][0]['run_id'], 'run_example')
            for table in stream_export.OPTIONAL_TABLES:
                self.assertIn(table + '.csv', archive.namelist())
                self.assertEqual(result['rows'][table], 1)

    def test_no_whole_table_helper_and_failure_rolls_back_snapshot(self):
        with patch.object(self.service.repo, 'all', side_effect=AssertionError('不可全表載入')):
            stream_export.export_archive(self.service, self.destination)
        failing_destination = self.destination.with_name('failed.zip')
        with patch.object(stream_export, '_write_json', side_effect=RuntimeError('模擬寫入失敗')):
            with self.assertRaisesRegex(RuntimeError, '模擬寫入失敗'):
                stream_export.export_archive(self.service, failing_destination)
        self.assertFalse(self.service.conn.in_transaction)
        self.assertFalse(failing_destination.exists())
        self.assertEqual(list(self.destination.parent.glob('*.partial')), [])


if __name__ == '__main__':
    unittest.main()
