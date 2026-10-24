"""沿用核心行為測試，僅替換儲存入口與唯讀 SQL 解碼器。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

import test_service as baseline
import test_revision as revision
from storage_v3.repository import CompactResearchService, register_functions


class CompactServiceRegression(baseline.ResearchServiceTests):
    def setUp(self):
        service_patch = patch.object(baseline, 'ResearchService', CompactResearchService)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        super().setUp()

    def _rows(self, table, path=None):
        database = Path(path or self.db_path).resolve()
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            register_functions(connection)
            return [dict(row) for row in connection.execute('SELECT * FROM "' + table + '"')]


class CompactRevisionRegression(revision.RevisionTests):
    def setUp(self):
        service_patch = patch.object(revision, 'ResearchService', CompactResearchService)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        super().setUp()
