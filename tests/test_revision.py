"""以資料庫開局當下的真實內容，驗證 checkpoint＋事件的歷史還原。"""

import unittest

from uttt.service import ResearchService


def capture_model(service, model_id):
    model = service.repo.one("SELECT * FROM models WHERE model_id=?", (model_id,))
    boxes = []
    for row in service.repo.all(
            "SELECT state_key FROM model_states WHERE model_id=? ORDER BY state_key", (model_id,)):
        weights = service.repo.all(
            "SELECT group_rep,bead_weight,update_count FROM weights "
            "WHERE model_id=? AND state_key=? ORDER BY group_rep", (model_id, row["state_key"]))
        boxes.append({"state_key": row["state_key"], "weights": weights})
    return {"model": model, "boxes": boxes}


def normalized(payload):
    fields = ("model_id", "seed", "rng_state", "version", "trained_games", "update_count", "revision")
    model = {field: payload["model"][field] for field in fields}
    boxes = sorted((box["state_key"], tuple(sorted(
        (w["group_rep"], w["bead_weight"], w["update_count"]) for w in box["weights"])))
        for box in payload["boxes"])
    return model, boxes


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.service = ResearchService(":memory:")
        self.a = self.service.create_model("A", seed=127)
        self.b = self.service.create_model("B", seed=991)

    def tearDown(self):
        self.service.close()

    def finish(self, game_id):
        while self.service.get_game(game_id)["status"] == "active":
            self.service.step(game_id)

    def test_empty_game_start_restores_original_weights_seed_and_rng(self):
        before_a = capture_model(self.service, self.a)
        before_b = capture_model(self.service, self.b)
        game = self.service.new_game(self.a, self.b, seed=40)
        for _ in range(4):
            self.service.step(game)
        self.assertEqual(normalized(self.service.model_at_game_start(game, "X")), normalized(before_a))
        self.assertEqual(normalized(self.service.model_at_game_start(game, "O")), normalized(before_b))

    def test_completed_game_deltas_restore_second_game_start_after_more_moves(self):
        first = self.service.new_game(self.a, self.b, seed=41)
        self.finish(first)
        before_a = capture_model(self.service, self.a)
        before_b = capture_model(self.service, self.b)
        self.assertEqual(before_a["model"]["version"], 1)
        self.assertGreater(before_a["model"]["update_count"], 0)
        self.assertTrue(before_a["boxes"])
        second = self.service.new_game(self.b, self.a, seed=42)
        for _ in range(6):
            self.service.step(second)
        self.assertEqual(normalized(self.service.model_at_game_start(second, "O")), normalized(before_a))
        self.assertEqual(normalized(self.service.model_at_game_start(second, "X")), normalized(before_b))
        # 後來的更新不應改變第一局的空模型起點。
        first_start = self.service.model_at_game_start(first, "X")
        self.assertEqual(first_start["model"]["version"], 0)
        self.assertEqual(first_start["boxes"], [])

    def test_abandoned_game_keeps_rng_and_new_boxes_without_endgame_reward(self):
        original = capture_model(self.service, self.a)
        abandoned = self.service.new_game(self.a, "random", seed=43)
        self.service.step(abandoned)
        self.service.step(abandoned)
        self.service.step(abandoned)
        self.service.abandon(abandoned)
        before = capture_model(self.service, self.a)
        self.assertEqual(before["model"]["version"], original["model"]["version"])
        self.assertEqual(before["model"]["trained_games"], 0)
        self.assertEqual(before["model"]["update_count"], 0)
        self.assertGreater(before["model"]["revision"], original["model"]["revision"])
        self.assertNotEqual(before["model"]["rng_state"], original["model"]["rng_state"])
        self.assertGreater(len(before["boxes"]), 0)
        self.assertTrue(all(w["bead_weight"] == 3 and w["update_count"] == 0
                            for box in before["boxes"] for w in box["weights"]))
        later = self.service.new_game(self.a, "random", seed=44)
        self.service.step(later)
        reconstructed = self.service.model_at_game_start(later, "X")
        self.assertEqual(normalized(reconstructed), normalized(before))

    def test_restored_model_branch_retains_its_own_revision_and_model_identity(self):
        game = self.service.new_game(self.a, self.b, seed=45)
        self.finish(game)
        checkpoint = self.service.save_checkpoint(self.a)
        restored = self.service.restore_checkpoint(checkpoint, name="A branch")
        before = capture_model(self.service, restored)
        branch_game = self.service.new_game("random", restored, seed=46)
        self.service.step(branch_game)
        self.service.step(branch_game)
        reconstructed = self.service.model_at_game_start(branch_game, "O")
        self.assertEqual(reconstructed["model"]["model_id"], restored)
        self.assertEqual(normalized(reconstructed), normalized(before))


if __name__ == "__main__":
    unittest.main()
