"""在獨立 Streamlit 程序使用 v3 儲存層與既有介面。"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage_v3.repository import CompactResearchService
import uttt.service

database = Path(os.environ.get('UTTT_DB_PATH', ROOT / 'data/storage-v3/pilot-1000.sqlite3'))
if not database.is_file():
    import streamlit as st
    st.error('找不到優化副本，請先執行遷移並設定 UTTT_DB_PATH。')
    st.stop()
os.environ['UTTT_DB_PATH'] = str(database.resolve())
uttt.service.ResearchService = CompactResearchService
runpy.run_path(str(ROOT / 'app.py'), run_name='__main__')
