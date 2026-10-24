"""優化副本的介面入口冒煙驗證，不使用正式資料。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import uttt.service
from storage_v3.repository import CompactResearchService


class CompactAppTests(unittest.TestCase):
    def test_existing_compact_database_opens_in_default_frozen_pk_mode(self):
        with tempfile.TemporaryDirectory(prefix='uttt-v3-app-') as directory:
            database = Path(directory) / 'app.sqlite3'
            with CompactResearchService(database) as service:
                service.create_model('優化測試 AI', seed=19)
            original_class = uttt.service.ResearchService
            try:
                with patch.dict(os.environ, {'UTTT_DB_PATH': str(database), 'UTTT_PLAY_MODE': 'eval'}):
                    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'tools/compact_app.py'), default_timeout=30).run()
                    self.assertFalse(list(app.exception))
                    self.assertFalse(list(app.error))
                    modes = [item for item in app.radio if item.label == '對局模式']
                    self.assertEqual(modes[0].value, 'eval')
            finally:
                uttt.service.ResearchService = original_class


if __name__ == '__main__':
    unittest.main()
