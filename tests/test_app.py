"""介面流程回歸測試；全部使用臨時資料庫，不是正式研究資料。"""

from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from streamlit.testing.v1 import AppTest

from uttt.engine import legal_moves
from uttt.service import ResearchService


APP = Path(__file__).resolve().parents[1] / "app.py"


class AppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="uttt-ui-tests-")
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "research.sqlite3"
        env = patch.dict(os.environ, {"UTTT_DB_PATH": str(self.db_path), "UTTT_PLAY_MODE": "eval"})
        env.start()
        self.addCleanup(env.stop)

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assert_clean(app)
        return app

    def widget(self, app, kind, label):
        matches = [item for item in getattr(app, kind) if item.label == label]
        self.assertEqual(len(matches), 1, f"找不到唯一介面元件：{kind} {label}")
        return matches[0]

    def assert_clean(self, app):
        self.assertFalse(list(app.exception), [item.message for item in app.exception])
        self.assertFalse(list(app.error), [item.value for item in app.error])

    def click(self, app, label):
        self.widget(app, "button", label).click().run()
        self.assert_clean(app)

    def page(self, app, name):
        self.widget(app, "radio", "工作區").set_value(name).run()
        self.assert_clean(app)

    def rows(self, table):
        with closing(sqlite3.connect(self.db_path)) as conn:
            # table 只由測試內的固定常數傳入。
            return tuple(conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall())

    def learning_snapshot(self):
        return {table: self.rows(table) for table in
                ("models", "model_states", "weights", "learning_updates", "model_events")}

    def database_snapshot(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return tuple(conn.iterdump())

    def create_model_in_ui(self, app, name):
        self.widget(app, "text_input", "模型名稱").set_value(name)
        self.click(app, "建立模型")
        with ResearchService(self.db_path) as service:
            return next(m["model_id"] for m in service.list_models() if m["name"] == name)

    def test_demo_legal_moves_auto_reply_and_reopen_do_not_train(self):
        app = self.app()
        model_id = self.create_model_in_ui(app, "示範 AI")
        before = self.learning_snapshot()
        self.widget(app, "radio", "對局模式").set_value("demo").run()
        self.widget(app, "selectbox", "O（後手）").set_value(model_id)
        self.click(app, "建立對局")
        self.click(app, "35")
        with ResearchService(self.db_path) as service:
            game = service.list_games()[0]
            game_id = game["game_id"]
            self.assertEqual(game["mode"], "demo")
            self.assertEqual(game["is_example"], 1)
            self.assertEqual(game["ply"], 2)  # 人類一手 + 自動應手一手。
            moves = service.moves(game_id)
            self.assertEqual(moves[0]["move_code"], 35)
            self.assertEqual(moves[1]["move_code"] // 10, 5)
            allowed = set(legal_moves(service.state(game_id)))
        buttons = [b for b in app.button if b.key and b.key.startswith(f"game_{game_id}_")]
        self.assertEqual(len(buttons), 81)
        enabled = {int(b.key.rsplit("_", 1)[1]) for b in buttons if not b.disabled}
        self.assertEqual(enabled, allowed)
        self.assertEqual(before, self.learning_snapshot())

        # 新的 AppTest 代表沒有舊 session；必須從持久化清單續玩。
        reopened = self.app()
        self.widget(reopened, "selectbox", "未完對局").set_value(game_id)
        self.click(reopened, "載入對局")
        self.assertEqual(reopened.session_state["current_game_id"], game_id)
        with ResearchService(self.db_path) as service:
            self.assertEqual(service.get_game(game_id)["ply"], 2)
        self.click(reopened, str(min(allowed)))
        with ResearchService(self.db_path) as service:
            self.assertEqual(service.get_game(game_id)["ply"], 4)
            # 跑到終局，確認示範 UI 的 mode 不會在結算時偷偷學習。
            while service.get_game(game_id)["status"] == "active":
                state = service.state(game_id)
                player = service.get_game(game_id)["x_player"] if state.turn == "X" else model_id
                service.step(game_id, min(legal_moves(state)) if player == "human" else None)
            self.assertEqual(service.get_game(game_id)["status"], "finished")
        self.assertEqual(before, self.learning_snapshot())

    def test_default_human_pk_freezes_trained_model_and_records_game(self):
        with ResearchService(self.db_path) as service:
            model_a = service.create_model("PK AI", seed=41)
            model_b = service.create_model("陪練 AI", seed=42)
            service.train(model_a, model_b, games=2, seed=43)
        before = self.learning_snapshot()  # models 亦包含 RNG、version 與 revision。
        app = self.app()
        self.assertEqual(self.widget(app, "radio", "對局模式").value, "eval")
        self.widget(app, "selectbox", "X（先手）").set_value(model_a)
        self.widget(app, "selectbox", "O（後手）").set_value("human")
        self.click(app, "建立對局")
        with ResearchService(self.db_path) as service:
            game_id = app.session_state["current_game_id"]
            game = service.get_game(game_id)
            self.assertEqual(game["mode"], "eval")
            self.assertEqual(game["is_example"], 0)
            self.assertEqual(game["ply"], 1)  # AI 自動先手，停下等待真實 UI 輸入。
            self.assertIsNotNone(game["x_checkpoint_id"])
            self.assertEqual(service.moves(game_id)[0]["branch"], "weighted")
            human_move = min(legal_moves(service.state(game_id)))
        self.assertEqual(before, self.learning_snapshot())
        self.click(app, str(human_move))
        with ResearchService(self.db_path) as service:
            self.assertEqual(service.get_game(game_id)["ply"], 3)
            # 以下自動走法僅在臨時測試資料庫，驗證整局結算仍不學習。
            while service.get_game(game_id)["status"] == "active":
                state = service.state(game_id)
                service.step(game_id, min(legal_moves(state)) if state.turn == "O" else None)
            game = service.get_game(game_id)
            self.assertEqual(game["status"], "finished")
            moves = service.moves(game_id)
            self.assertEqual(len(moves), game["ply"])
            self.assertFalse(any(move["branch"] == "explore" for move in moves))
        self.assertEqual(before, self.learning_snapshot())

    def test_checkpoint_strategy_navigation_is_read_only(self):
        with ResearchService(self.db_path) as service:
            model_a = service.create_model("策略 A", seed=11)
            model_b = service.create_model("策略 B", seed=12)
            service.train(model_a, model_b, games=2, seed=20)
            checkpoint_id = service.save_checkpoint(model_a)
            service.train(model_a, model_b, games=1, seed=21)
        before = self.database_snapshot()
        app = self.app()
        self.page(app, "策略探索")
        self.widget(app, "selectbox", "查看模型").set_value(model_a).run()
        self.widget(app, "selectbox", "資料版本").set_value(checkpoint_id).run()
        self.widget(app, "selectbox", "載入起點").set_value("模型已記錄局面").run()
        self.assert_clean(app)
        self.click(app, "載入起點並清除預覽路徑")
        self.assertEqual(len(app.session_state["preview_history"]), 1)
        self.click(app, "預覽下一局面")
        self.assertEqual(len(app.session_state["preview_history"]), 2)
        self.assertEqual(app.session_state["preview_history"][-1]["board"].count("X"), 1)
        self.click(app, "回上一步")
        self.widget(app, "checkbox", "顯示訓練抽選機率（含 10% 探索）").uncheck().run()
        self.assert_clean(app)
        self.assertEqual(len(app.session_state["preview_history"]), 1)
        self.assertTrue(list(app.get("graphviz_chart")))
        self.assertEqual(before, self.database_snapshot())

    def test_batch_evaluation_replay_and_export_workflow(self):
        app = self.app()
        model_a = self.create_model_in_ui(app, "實驗 A")
        self.assertEqual(self.widget(app, "number_input", "模型種子").value, 1)
        model_b = self.create_model_in_ui(app, "實驗 B")
        self.page(app, "訓練與評估")
        self.widget(app, "selectbox", "模型 A").set_value(model_a)
        self.widget(app, "selectbox", "模型 B").set_value(model_b)
        self.widget(app, "number_input", "本批訓練局數").set_value(2)
        self.click(app, "開始這一批訓練")
        self.assertEqual(app.session_state["last_training"]["games"], 2)
        before = self.learning_snapshot()
        self.widget(app, "number_input", "評估局數（建議偶數以平衡先後手）").set_value(2)
        self.click(app, "開始凍結評估")
        evaluation = app.session_state["last_evaluation"]
        self.assertEqual(evaluation["games"], 2)
        self.assertEqual(sum(evaluation[k] for k in ("wins", "draws", "losses")), 2)
        self.assertEqual(before, self.learning_snapshot())

        self.page(app, "棋譜回放")
        with ResearchService(self.db_path) as service:
            game = service.list_games()[0]
        self.widget(app, "selectbox", "選擇對局（最近 500 局）").set_value(game["game_id"]).run()
        self.widget(app, "slider", "回放至第幾手").set_value(game["ply"]).run()
        self.assert_clean(app)
        replay_buttons = [b for b in app.button if b.key and b.key.startswith("replay_")]
        self.assertEqual(len(replay_buttons), 81)
        self.assertTrue(all(button.disabled for button in replay_buttons))

        self.page(app, "模型與匯出")
        with patch.object(ResearchService, "export_data", side_effect=AssertionError("完整匯出不可整表載入")):
            self.click(app, "準備 ZIP 檔")
        payload = app.session_state["export_payload"]
        saved_files = list((self.db_path.parent / "exports").glob("*.zip"))
        self.assertEqual(len(saved_files), 1)
        self.assertEqual(saved_files[0].read_bytes(), payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith(".csv") for name in names))
            self.assertTrue(any(name.endswith(".json") for name in names))
            self.assertIsNone(archive.testzip())
        with ResearchService(self.db_path) as service:
            self.assertEqual(len(service.list_games()), 4)
            service.conn.execute("CREATE TABLE pilot_experiments (experiment_id TEXT)")
            service.conn.execute("INSERT INTO pilot_experiments VALUES ('test-pilot')")
        app.run()
        self.assert_clean(app)
        self.assertNotIn("run_id（可留白）", [item.label for item in app.text_input])
        with patch.object(ResearchService, "export_data", side_effect=AssertionError("批次實驗不可整表載入")):
            self.click(app, "準備 ZIP 檔")
        with zipfile.ZipFile(io.BytesIO(app.session_state["export_payload"])) as archive:
            data = json.loads(archive.read("research.json"))
            self.assertEqual(data["pilot_experiments"], [{"experiment_id": "test-pilot"}])
        self.assertEqual(len(list((self.db_path.parent / "exports").glob("*.zip"))), 2)


if __name__ == "__main__":
    unittest.main()
