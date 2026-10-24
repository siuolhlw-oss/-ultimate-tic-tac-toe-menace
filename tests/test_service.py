"""研究服務的黑箱整合測試；測試資料不是科展實驗成果。"""

from __future__ import annotations

import csv
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from uttt.service import ResearchService


DEMO_MOVES = (14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36)


class ResearchServiceTests(unittest.TestCase):
    """各測試各用暫存資料庫，不接觸使用者的研究資料。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="uttt-service-tests-")
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "research.sqlite3"
        self.service = ResearchService(self.db_path)
        self.addCleanup(self._close_current_service)

    def _close_current_service(self):
        if self.service is not None:
            self.service.close()
            self.service = None

    def _rows(self, table, path=None):
        self.assertIn(table, {"models", "weights", "model_states", "model_events", "learning_updates", "games"})
        with closing(sqlite3.connect(path or self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]

    def _snapshot(self, *tables):
        return {
            table: sorted(self._rows(table), key=lambda row: json.dumps(row, sort_keys=True))
            for table in tables
        }

    def _model(self, model_id, service=None):
        service = service or self.service
        return next(row for row in service.list_models() if row["model_id"] == model_id)

    def _finish_automatic_game(self, game_id, service=None):
        service = service or self.service
        for _ in range(82):
            game = service.get_game(game_id)
            if game["status"] != "active":
                self.assertEqual(game["status"], "finished")
                self.assertIn(game["result"], ("X", "O", "draw"))
                return game
            service.step(game_id)
        self.fail("合法對局應在最多 81 手內結束")

    @staticmethod
    def _move_fingerprint(rows):
        return [
            (row["ply"], row["actor"], int(row["move_code"]), row["branch"],
             row["probability"], row["weight_before_choice"])
            for row in rows
        ]

    def test_illegal_moves_leave_game_board_and_log_unchanged(self):
        game_id = self.service.new_game("human", "human", seed=1, mode="demo", is_example=True)
        for move in (0, 10, 100):
            with self.subTest(move=move):
                before = (self.service.get_game(game_id), self.service.state(game_id),
                          self.service.moves(game_id))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.service.step(game_id, move)
                after = (self.service.get_game(game_id), self.service.state(game_id),
                         self.service.moves(game_id))
                self.assertEqual(before, after)
        self.service.step(game_id, 11)
        for move in (11, 25):  # 已占用；上一手指定大格 1 卻下大格 2。
            with self.subTest(move=move):
                before = (self.service.get_game(game_id), self.service.state(game_id),
                          self.service.moves(game_id))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.service.step(game_id, move)
                self.assertEqual(before, (self.service.get_game(game_id),
                                         self.service.state(game_id),
                                         self.service.moves(game_id)))

    def test_seventeen_move_example_replays_without_learning(self):
        self.service.create_model("未參與示範的模型", seed=2)
        before = self._snapshot("models", "weights", "learning_updates")
        game_id = self.service.new_game("human", "human", seed=3, mode="demo", is_example=True)
        states = [self.service.state(game_id)]
        for ply, move in enumerate(DEMO_MOVES, 1):
            game = self.service.step(game_id, move)
            self.assertEqual(game["ply"], ply)
            states.append(self.service.state(game_id))
        self.assertEqual((game["status"], game["result"]), ("finished", "X"))
        self.assertEqual([int(row["move_code"]) for row in self.service.moves(game_id)],
                         list(DEMO_MOVES))
        self.assertEqual(before, self._snapshot("models", "weights", "learning_updates"))

        replay_id = self.service.new_game("human", "human", mode="demo", is_example=True)
        self.assertEqual(states[0], self.service.state(replay_id))
        for index, move in enumerate(DEMO_MOVES, 1):
            self.service.step(replay_id, move)
            self.assertEqual(states[index], self.service.state(replay_id))

    def test_manual_override_of_ai_does_not_consume_rng_or_initialize_weights(self):
        model_id = self.service.create_model("不可手動代下", seed=4)
        game_id = self.service.new_game(model_id, "random", seed=5)
        before = self._snapshot("models", "weights", "model_states", "learning_updates")
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.step(game_id, 11)
        self.assertEqual(self.service.moves(game_id), [])
        self.assertEqual(before, self._snapshot("models", "weights", "model_states", "learning_updates"))

    def test_inspection_is_read_only_and_preserves_uniform_initial_actions(self):
        model_id = self.service.create_model("檢視測試", seed=10)
        game_id = self.service.new_game("human", "human", mode="demo", is_example=True)
        before = self._snapshot("models", "weights", "learning_updates")
        details = self.service.inspect(self.service.state(game_id), model_id)
        self.assertFalse(details["known"])
        self.assertEqual(sum(len(group["members"]) for group in details["groups"]), 81)
        self.assertEqual(len(details["actions"]), 81)
        self.assertAlmostEqual(sum(group["probability"] for group in details["groups"]), 1.0)
        for group in details["groups"]:
            self.assertEqual(group["weight"], 3)
            self.assertAlmostEqual(group["probability"], len(group["members"]) / 81)
        self.assertEqual(before, self._snapshot("models", "weights", "learning_updates"))

    def test_same_model_cannot_train_both_sides_or_in_two_active_games(self):
        model_id = self.service.create_model("排他訓練", seed=11)
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.new_game(model_id, model_id)
        first = self.service.new_game(model_id, "human")
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.new_game("random", model_id)
        self.assertEqual(self.service.get_game(first)["status"], "active")
        self.service.abandon(first)
        second = self.service.new_game("random", model_id)
        self.assertEqual(self.service.get_game(second)["status"], "active")

    def test_abandoned_game_has_no_terminal_reward(self):
        model_id = self.service.create_model("中止測試", seed=12)
        original = self._model(model_id)
        game_id = self.service.new_game(model_id, "random", seed=13)
        for _ in range(6):
            self.service.step(game_id)
        weights_before_abandon = self._snapshot("weights", "learning_updates")
        self.service.abandon(game_id)
        self.assertEqual(self.service.get_game(game_id)["status"], "abandoned")
        self.assertEqual(weights_before_abandon, self._snapshot("weights", "learning_updates"))
        self.assertEqual(original["version"], self._model(model_id)["version"])
        self.assertEqual(original["trained_games"], self._model(model_id)["trained_games"])
        self.assertEqual(self._rows("learning_updates"), [])

    def test_checkpoint_requires_a_completed_or_abandoned_game_boundary(self):
        model_id = self.service.create_model("檢查點邊界", seed=14)
        game_id = self.service.new_game(model_id, "random", seed=15)
        self.service.step(game_id)
        before = self.service.list_checkpoints(model_id)
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.save_checkpoint(model_id)
        self.assertEqual(before, self.service.list_checkpoints(model_id))
        self.service.abandon(game_id)
        checkpoint = self.service.save_checkpoint(model_id)
        self.assertIn(checkpoint, {row["checkpoint_id"] for row in self.service.list_checkpoints(model_id)})

    def test_each_ai_learns_its_own_decisions_only_once(self):
        model_x = self.service.create_model("甲", seed=21)
        model_o = self.service.create_model("乙", seed=22)
        game_id = self.service.new_game(model_x, model_o, seed=23)
        final = self._finish_automatic_game(game_id)
        moves = self.service.moves(game_id)
        updates = [row for row in self._rows("learning_updates") if row["game_id"] == game_id]
        self.assertEqual(len(updates), len(moves))
        expected_owner = {row["ply"]: model_x if row["actor"] == "X" else model_o
                          for row in moves}
        self.assertEqual({(row["ply"], row["model_id"]) for row in updates},
                         set(expected_owner.items()))
        for update in updates:
            actor = next(move["actor"] for move in moves if move["ply"] == update["ply"])
            delta = 1 if final["result"] == "draw" else (3 if final["result"] == actor else -1)
            self.assertEqual(update["weight_before_update"], 3)
            self.assertEqual(update["reward_delta"], delta)
            self.assertEqual(update["applied_delta"], delta)
            self.assertEqual(update["weight_after_update"], 3 + delta)
            self.assertEqual(update["model_version_after"], 1)
        self.assertEqual(self._model(model_x)["trained_games"], 1)
        self.assertEqual(self._model(model_o)["trained_games"], 1)
        before_retry = self._snapshot("models", "weights", "learning_updates")
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.step(game_id)
        self.assertEqual(before_retry, self._snapshot("models", "weights", "learning_updates"))
        self.assertEqual(final, self.service.get_game(game_id))

    def test_database_reopen_continues_the_same_random_sequence(self):
        model_x = self.service.create_model("甲", seed=31)
        model_o = self.service.create_model("乙", seed=32)
        uninterrupted_id = self.service.new_game(model_x, model_o, seed=33)
        other_path = Path(self.temp_dir.name) / "resumed.sqlite3"
        other = ResearchService(other_path)
        try:
            other_x = other.create_model("甲", seed=31)
            other_o = other.create_model("乙", seed=32)
            resumed_id = other.new_game(other_x, other_o, seed=33)
            for _ in range(8):
                self.service.step(uninterrupted_id)
                other.step(resumed_id)
            self.assertEqual(self._move_fingerprint(self.service.moves(uninterrupted_id)),
                             self._move_fingerprint(other.moves(resumed_id)))
            other.close()
            other = ResearchService(other_path)
            first_result = self._finish_automatic_game(uninterrupted_id)
            second_result = self._finish_automatic_game(resumed_id, other)
            self.assertEqual(first_result["result"], second_result["result"])
            self.assertEqual(self._move_fingerprint(self.service.moves(uninterrupted_id)),
                             self._move_fingerprint(other.moves(resumed_id)))
        finally:
            other.close()

    def test_evaluation_freezes_weights_versions_training_rng_and_updates(self):
        model_a = self.service.create_model("受測甲", seed=41)
        model_b = self.service.create_model("陪練乙", seed=42)
        self.service.train(model_a, model_b, games=2, seed=43)
        before = self._snapshot("models", "weights", "model_states", "learning_updates", "model_events")
        report = self.service.evaluate(model_a, games=4, seed=44)
        self.assertEqual(before, self._snapshot("models", "weights", "model_states", "learning_updates", "model_events"))
        self.assertEqual(report["games"], 4)
        self.assertEqual(report["wins"] + report["draws"] + report["losses"], 4)
        self.assertAlmostEqual(report["score"], (report["wins"] + 0.5 * report["draws"]) / 4)
        games = [row for row in self._rows("games") if row["run_id"] == report["run_id"]]
        self.assertEqual(len(games), 4)
        details = [self.service.get_game(row["game_id"]) for row in games]
        self.assertEqual(sum(game["x_player"] == model_a for game in details), 2)
        self.assertEqual(sum(game["o_player"] == model_a for game in details), 2)

    def test_unseen_evaluation_does_not_create_training_weights(self):
        model_id = self.service.create_model("未訓練", seed=50)
        before = self._snapshot("models", "weights", "model_states", "learning_updates", "model_events")
        self.service.evaluate(model_id, games=2, seed=51)
        self.assertEqual(before, self._snapshot("models", "weights", "model_states", "learning_updates", "model_events"))
        game_id = self.service.new_game("human", "human", mode="demo", is_example=True)
        details = self.service.inspect(self.service.state(game_id), model_id, training=False)
        self.assertFalse(details["known"])

    def test_stale_game_revision_rejects_double_click_without_rng_changes(self):
        model_id = self.service.create_model("避免重複按鈕", seed=52)
        game_id = self.service.new_game(model_id, "random", seed=53)
        self.service.step(game_id, expected_ply=0)
        before = self._snapshot("models", "weights", "model_states", "learning_updates")
        game_before = self.service.get_game(game_id)
        moves_before = self.service.moves(game_id)
        with self.assertRaises((ValueError, RuntimeError)):
            self.service.step(game_id, expected_ply=0)
        self.assertEqual(game_before, self.service.get_game(game_id))
        self.assertEqual(moves_before, self.service.moves(game_id))
        self.assertEqual(before, self._snapshot("models", "weights", "model_states", "learning_updates"))

    def test_backup_restores_an_active_game_and_releases_the_destination_file(self):
        model_id = self.service.create_model("備份續局", seed=54)
        game_id = self.service.new_game(model_id, "random", seed=55)
        for _ in range(6):
            self.service.step(game_id)
        destination = Path(self.temp_dir.name) / "backup.sqlite3"
        self.service.backup(destination)
        backup_before = destination.read_bytes()
        with self.assertRaises((ValueError, FileExistsError)):
            self.service.backup(destination)
        self.assertEqual(backup_before, destination.read_bytes())
        with ResearchService(destination) as restored:
            self.assertEqual(self.service.state(game_id), restored.state(game_id))
            self._finish_automatic_game(game_id)
            self._finish_automatic_game(game_id, restored)
            self.assertEqual(self._move_fingerprint(self.service.moves(game_id)),
                             self._move_fingerprint(restored.moves(game_id)))
        destination.unlink()  # Windows 上若 backup() 留有連線，這裡會失敗。

    def test_restored_checkpoint_is_independent_and_reproduces_play(self):
        original = self.service.create_model("檢查點原型", seed=61)
        trainer = self.service.create_model("訓練對手", seed=62)
        self.service.train(original, trainer, games=2, seed=63)
        checkpoint = self.service.save_checkpoint(original)
        self.assertIn(checkpoint, {row["checkpoint_id"] for row in self.service.list_checkpoints(original)})
        cloned = self.service.restore_checkpoint(checkpoint, name="檢查點分支")
        self.assertNotEqual(original, cloned)
        self.assertEqual(self._model(original)["trained_games"], self._model(cloned)["trained_games"])
        original_game = self.service.new_game(original, "random", seed=64)
        cloned_game = self.service.new_game(cloned, "random", seed=64)
        self._finish_automatic_game(original_game)
        self._finish_automatic_game(cloned_game)
        self.assertEqual(self._move_fingerprint(self.service.moves(original_game)),
                         self._move_fingerprint(self.service.moves(cloned_game)))

    def test_game_start_reconstruction_includes_aborted_game_rng_and_new_boxes(self):
        model_id = self.service.create_model("歷史模型還原", seed=81)
        aborted = self.service.new_game(model_id, "random", seed=82)
        for _ in range(6):
            self.service.step(aborted)
        self.service.abandon(aborted)
        completed = self.service.new_game("random", model_id, seed=83)
        self._finish_automatic_game(completed)
        expected_model = self._model(model_id)
        expected_weights = sorted(
            (row["state_key"], row["group_rep"], row["bead_weight"], row["update_count"])
            for row in self._rows("weights") if row["model_id"] == model_id
        )
        target = self.service.new_game(model_id, "random", seed=84)
        for _ in range(4):
            self.service.step(target)
        before = self._snapshot("models", "weights", "model_states", "model_events", "learning_updates")
        restored = self.service.model_at_game_start(target, "X")
        self.assertEqual(expected_model, restored["model"])
        restored_weights = sorted(
            (box["state_key"], weight["group_rep"], weight["bead_weight"], weight["update_count"])
            for box in restored["boxes"] for weight in box["weights"]
        )
        self.assertEqual(expected_weights, restored_weights)
        self.assertEqual(before, self._snapshot("models", "weights", "model_states", "model_events", "learning_updates"))

    def test_export_csv_and_json_have_identical_original_game_sequences(self):
        model_a = self.service.create_model("匯出甲", seed=71)
        model_b = self.service.create_model("匯出乙", seed=72)
        report = self.service.train(model_a, model_b, games=2, seed=73)
        outside = self.service.new_game("human", "human", mode="demo", is_example=True)
        self.service.step(outside, 35)
        archive_bytes = self.service.export_data(run_id=report["run_id"])
        self.assertIsInstance(archive_bytes, bytes)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            moves_name = next(name for name in archive.namelist() if Path(name).name == "moves.csv")
            csv_moves = list(csv.DictReader(io.StringIO(archive.read(moves_name).decode("utf-8-sig"))))
            json_documents = [json.loads(archive.read(name)) for name in archive.namelist()
                              if name.endswith(".json")]
            data = next(document for document in json_documents
                        if isinstance(document, dict) and "moves" in document and "games" in document)
        normalized = lambda rows: sorted((str(row["game_id"]), int(row["ply"]),
                                          row["actor"], int(row["move_code"])) for row in rows)
        self.assertEqual(normalized(csv_moves), normalized(data["moves"]))
        game_ids = {row["game_id"] for row in data["games"]}
        self.assertEqual(len(game_ids), 2)
        self.assertNotIn(outside, game_ids)
        self.assertTrue(all(row["run_id"] == report["run_id"] for row in data["games"]))
        api_moves = [dict(move, game_id=game_id)
                     for game_id in game_ids for move in self.service.moves(game_id)]
        self.assertEqual(normalized(data["moves"]), normalized(api_moves))

    def test_selected_export_preserves_other_game_dependencies_for_shared_models(self):
        model_a = self.service.create_model("來源追溯甲", seed=91)
        model_b = self.service.create_model("來源追溯乙", seed=92)
        report = self.service.train(model_a, model_b, games=1, seed=93)
        outside = self.service.new_game(model_a, "random", seed=94)
        for _ in range(6):
            self.service.step(outside)
        self.service.abandon(outside)
        with zipfile.ZipFile(io.BytesIO(self.service.export_data(report["run_id"]))) as archive:
            data = json.loads(archive.read("research.json"))
        selected_ids = {game["game_id"] for game in data["games"]}
        self.assertEqual(len(selected_ids), 1)
        self.assertNotIn(outside, selected_ids)
        dependencies = data["dependencies"]
        supporting_ids = {game["game_id"] for game in dependencies["games"]}
        self.assertIn(outside, supporting_ids)
        available_games = selected_ids | supporting_ids
        source_games = {row["created_game_id"] for row in data["model_states"]
                        if row["created_game_id"] is not None}
        self.assertTrue(source_games <= available_games)
        available_runs = {run["run_id"] for run in data["runs"] + dependencies["runs"]}
        self.assertTrue(all(game["run_id"] in available_runs
                            for game in data["games"] + dependencies["games"]))
        self.assertTrue(all(move["game_id"] in available_games
                            for move in data["moves"] + dependencies["moves"]))
        self.assertTrue(all(event["game_id"] in available_games for event in data["model_events"]))
        self.assertEqual(sum(move["game_id"] == outside for move in dependencies["moves"]), 6)


if __name__ == "__main__":
    unittest.main()
