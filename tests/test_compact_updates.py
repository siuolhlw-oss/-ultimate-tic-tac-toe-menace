"""更新範圍、舊 v3 觸發器升級與外鍵完整性；只使用隔離資料庫。"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from storage_v3.migration import table_digest
from storage_v3.repository import (
    CompactResearchService, STRUCTURE, TRANSFORMED, physical_table,
    refresh_update_triggers, transformed_column, write_expression,
)
from uttt.engine import State, legal_moves, play
from uttt.symmetry import canonicalize


def install_legacy_updates(connection):
    """重建首版的全欄更新方式，用於升級及前後比較。"""
    for table in sorted(TRANSFORMED):
        connection.execute('DROP TRIGGER IF EXISTS ' + table + '_update')
        connection.execute('DROP TRIGGER IF EXISTS ' + table + '_update_key')
        names = [c[1] for c in STRUCTURE[table]]
        predicate = ' AND '.join(transformed_column(table, c[1]) + ' IS '
                                 + write_expression(table, c[1], 'OLD')
                                 for c in STRUCTURE[table] if c[5])
        assignments = ','.join(transformed_column(table, c) + '='
                               + write_expression(table, c, 'NEW') for c in names)
        connection.execute('CREATE TRIGGER ' + table + '_update INSTEAD OF UPDATE ON '
                           + table + ' BEGIN UPDATE ' + physical_table(table) + ' SET '
                           + assignments + ' WHERE ' + predicate + '; END')
    connection.execute("DELETE FROM storage_metadata WHERE key='update_triggers'")


def trigger_snapshot(connection):
    return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))


class CompactUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uttt-update-tests-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'isolated.sqlite3'
        self.service = CompactResearchService(self.path)
        self.addCleanup(lambda: self.service.close())
        self.model = self.service.create_model('test', seed=17)
        self.game = self.service.new_game(self.model, 'random', seed=21)
        self.service.step(self.game)
        self.key = self.service.conn.execute('SELECT state_key FROM states WHERE ply=0').fetchone()[0]

    def test_target_update_and_explicit_unchanged_keys_preserve_references(self):
        c = self.service.conn
        original = table_digest(c, 'weights')
        destination = c.execute('SELECT state_key FROM states WHERE ply=1').fetchone()[0]
        for sql in ('UPDATE state_edges SET to_state_key=? WHERE from_state_key=? AND group_rep=11',
                    'UPDATE state_edges SET from_state_key=from_state_key,group_rep=group_rep,'
                    'to_state_key=? WHERE from_state_key=? AND group_rep=11'):
            with self.service.repo.transaction():
                c.execute(sql, (destination, self.key))
            self.assertEqual(c.execute('SELECT to_state_key FROM state_edges WHERE from_state_key=? '
                                       'AND group_rep=11', (self.key,)).fetchone()[0], destination)
            self.assertEqual(table_digest(c, 'weights'), original)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def test_referenced_key_change_and_missing_parent_still_fail(self):
        c = self.service.conn
        before = table_digest(c, 'state_edges')
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute('UPDATE state_edges SET group_rep=98 WHERE from_state_key=? AND group_rep=11', (self.key,))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute('UPDATE models SET model_id=? WHERE model_id=?', ('renamed', self.model))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute('UPDATE _state_edges SET to_state_id=-1 WHERE from_state_id=1 AND group_rep=11')
        self.assertEqual(table_digest(c, 'state_edges'), before)
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_unreferenced_composite_key_and_target_can_change_atomically(self):
        s = self.service
        c = s.conn
        first = canonicalize(play(play(State(), 11), 12))[0]
        second = canonicalize(play(first, legal_moves(first)[0]))[0]
        old_key, new_key = s._ensure_state(first), s._ensure_state(second)
        c.execute('INSERT INTO state_edges VALUES(?,?,?,?,?)', (old_key, 11, '[11]', 1, None))
        c.execute('UPDATE state_edges SET from_state_key=?,group_rep=12,to_state_key=? '
                  'WHERE from_state_key=? AND group_rep=11', (new_key, self.key, old_key))
        self.assertIsNone(c.execute('SELECT 1 FROM state_edges WHERE from_state_key=? AND group_rep=11', (old_key,)).fetchone())
        self.assertEqual(c.execute('SELECT to_state_key FROM state_edges WHERE from_state_key=? '
                                   'AND group_rep=12', (new_key,)).fetchone()[0], self.key)
        self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_existing_v3_readonly_then_writable_upgrade_is_lossless_and_idempotent(self):
        s = self.service
        install_legacy_updates(s.conn)
        before = {table: table_digest(s.conn, table) for table in STRUCTURE}
        legacy = trigger_snapshot(s.conn)
        s.close()
        with CompactResearchService(self.path, read_only=True) as readonly:
            self.assertEqual(trigger_snapshot(readonly.conn), legacy)
            self.assertEqual(before, {t: table_digest(readonly.conn, t) for t in STRUCTURE})
        self.service = CompactResearchService(self.path)
        c = self.service.conn
        self.assertEqual(before, {t: table_digest(c, t) for t in STRUCTURE})
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(json.loads(c.execute("SELECT value_json FROM storage_metadata WHERE key='update_triggers'").fetchone()[0]), {'revision': 2})
        version = c.execute('PRAGMA schema_version').fetchone()[0]
        refresh_update_triggers(c)
        self.assertEqual(c.execute('PRAGMA schema_version').fetchone()[0], version)
        self.service.close()
        self.service = CompactResearchService(self.path)
        self.assertEqual(self.service.conn.execute('PRAGMA schema_version').fetchone()[0], version)

    def test_failed_trigger_refresh_rolls_back_all_changes(self):
        c = self.service.conn
        install_legacy_updates(c)
        before = trigger_snapshot(c)
        def deny(action, name, *unused):
            if action == sqlite3.SQLITE_CREATE_TRIGGER and name == 'models_update':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(deny)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                refresh_update_triggers(c)
        finally:
            c.set_authorizer(None)
        self.assertFalse(c.in_transaction)
        self.assertEqual(trigger_snapshot(c), before)
        self.assertIsNone(c.execute("SELECT 1 FROM storage_metadata WHERE key='update_triggers'").fetchone())

    def test_unchanged_identity_does_not_scan_growing_weight_references(self):
        c = self.service.conn
        model_columns = [row[1] for row in c.execute('PRAGMA table_info(_models)')]
        model_select = ','.join('?' if column == 'model_id' else column for column in model_columns)
        state_id = c.execute('SELECT state_id FROM _states WHERE state_key=?', (self.key,)).fetchone()[0]
        def add_models(start, stop):
            with self.service.repo.transaction():
                for n in range(start, stop):
                    model = 'extra-' + str(n)
                    c.execute('INSERT INTO _models SELECT ' + model_select + ' FROM _models WHERE model_id=?', (model, self.model))
                    c.execute('INSERT INTO _model_states VALUES(?,?,NULL,NULL)', (model, state_id))
                    c.execute('INSERT INTO _weights VALUES(?,?,11,3,0)', (model, state_id))
        def steps():
            count = 0
            def tick():
                nonlocal count
                count += 1
                return 0
            c.set_progress_handler(tick, 1)
            try:
                c.execute('UPDATE state_edges SET to_state_key=to_state_key WHERE from_state_key=? AND group_rep=11', (self.key,))
            finally:
                c.set_progress_handler(None, 0)
            return count
        add_models(0, 100)
        small = steps()
        add_models(100, 1000)
        large = steps()
        self.assertLess(large, small * 2)
        install_legacy_updates(c)
        legacy = steps()
        self.assertGreater(legacy, large * 5)


if __name__ == '__main__':
    unittest.main()
