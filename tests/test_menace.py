import random
import unittest

from uttt.engine import InvalidMove, State, legal_moves, play, replay
from uttt.menace import ModelConsistencyError, choose, distribution
from uttt.symmetry import action_groups, canonicalize, transform_move, transform_state

EXAMPLE = [14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36]


def initial_weights(state):
    canonical, _ = canonicalize(state)
    return {rep: 3 for rep in action_groups(canonical)}


class MenaceTests(unittest.TestCase):
    def test_unseen_training_starts_uniform_per_position(self):
        rows = distribution(State(), None)
        self.assertEqual(len(rows), 81)
        self.assertAlmostEqual(sum(row["probability"] for row in rows), 1)
        self.assertTrue(all(row["weight"] == 3 for row in rows))
        self.assertTrue(all(abs(row["probability"] - 1 / 81) < 1e-12 for row in rows))

    def test_only_legal_positions_enter_the_pool(self):
        states = replay([14, 41, 15])
        self.assertEqual([row["move"] for row in distribution(states[1], None)], list(range(41, 50)))
        self.assertEqual(len(distribution(states[2], None)), 8)
        self.assertEqual([row["move"] for row in distribution(states[3], None)], list(range(51, 60)))
        for state in replay(EXAMPLE)[:-1]:
            for seed in range(10):
                choice = choose(state, None, random.Random(seed))
                self.assertIn(choice.move, legal_moves(state))
                self.assertEqual(transform_move(choice.move, choice.transform), choice.canonical_move)

    def test_shared_per_position_reward_example(self):
        weights = initial_weights(State())
        weights[12] += 3  # 一次經驗更新一個共享值，不依八個位置重複更新。
        rows = {row["move"]: row for row in distribution(State(), weights)}
        expected = 0.9 * 6 / (8 * 6 + 73 * 3) + 0.1 / 81
        self.assertAlmostEqual(rows[14]["probability"], expected)
        self.assertEqual(rows[14]["group_size"], 8)
        self.assertEqual(sum(row["weight"] == 6 for row in rows.values()), 8)
        self.assertAlmostEqual(sum(row["probability"] for row in rows.values()), 1)
        evaluation = {row["move"]: row for row in distribution(State(), weights, training=False)}
        self.assertAlmostEqual(evaluation[14]["probability"], 6 / 267)
        self.assertEqual(weights[12], 6)

    def test_invalid_weight_tables_are_consistency_errors(self):
        valid = initial_weights(State())
        missing = dict(valid)
        missing.pop(next(iter(missing)))
        extra = dict(valid, **{"99": 3})
        negative = dict(valid)
        negative[12] = -1
        fractional = dict(valid)
        fractional[12] = 1.5
        boolean = dict(valid)
        boolean[12] = True
        for weights in (missing, extra, negative, fractional, boolean, {}):
            with self.subTest(weights=weights), self.assertRaises(ModelConsistencyError):
                choose(State(), weights, random.Random(0))

    def test_zero_weights_use_uniform_without_reinitializing(self):
        weights = {rep: 0 for rep in action_groups(State())}
        original = dict(weights)
        for training in (False, True):
            rows = distribution(State(), weights, training)
            self.assertTrue(all(abs(row["probability"] - 1 / 81) < 1e-12 for row in rows))
            choice = choose(State(), weights, random.Random(4), training)
            self.assertEqual(choice.branch, "zero_uniform")
            self.assertEqual(choice.weight_before_choice, 0)
        self.assertEqual(weights, original)

    def test_unseen_evaluation_is_uniform_and_has_no_fabricated_weight(self):
        state = play(State(), 35)
        rows = distribution(state, None, training=False)
        self.assertTrue(all(row["weight"] is None for row in rows))
        self.assertTrue(all(abs(row["probability"] - 1 / 9) < 1e-12 for row in rows))
        choice = choose(state, None, random.Random(0), training=False)
        self.assertEqual(choice.branch, "unseen_uniform")
        self.assertTrue(choice.unseen)
        self.assertIsNone(choice.weight_before_choice)

    def test_zero_action_weight_still_has_training_exploration(self):
        weights = {rep: 0 for rep in action_groups(State())}
        weights[55] = 3
        rows = {row["move"]: row for row in distribution(State(), weights)}
        self.assertAlmostEqual(rows[14]["probability"], 0.1 / 81)
        self.assertAlmostEqual(rows[55]["probability"], 0.9 + 0.1 / 81)
        for seed in range(5):
            choice = choose(State(), weights, random.Random(seed), training=False)
            self.assertEqual((choice.move, choice.branch, choice.probability), (55, "weighted", 1.0))

    def test_sampling_is_reproducible_and_does_not_update_model(self):
        weights = initial_weights(State())
        original = dict(weights)
        first_rng, second_rng = random.Random(1234), random.Random(1234)
        first = [choose(State(), weights, first_rng) for _ in range(30)]
        second = [choose(State(), weights, second_rng) for _ in range(30)]
        self.assertEqual(first, second)
        self.assertEqual(weights, original)

    def test_distribution_is_equivariant_under_board_symmetry(self):
        state = replay(EXAMPLE[:6])[-1]
        weights = initial_weights(state)
        for index, rep in enumerate(weights):
            weights[rep] = index
        before = {row["move"]: row["probability"] for row in distribution(state, weights)}
        for transform in range(8):
            other = transform_state(state, transform)
            after = {row["move"]: row["probability"] for row in distribution(other, weights)}
            for move, probability in before.items():
                self.assertAlmostEqual(after[transform_move(move, transform)], probability)

    def test_terminal_state_has_no_box_or_choice(self):
        final = replay(EXAMPLE)[-1]
        self.assertEqual(distribution(final, None), [])
        with self.assertRaises(InvalidMove):
            choose(final, None, random.Random(0))


if __name__ == "__main__":
    unittest.main()
