import json
import unittest

from uttt.engine import State, legal_moves, play, replay
from uttt.symmetry import (CANONICAL_VERSION, action_groups, canonicalize,
                           inverse_transform_move, state_key, transform_move,
                           transform_state)

EXAMPLE = [14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36]


class SymmetryTests(unittest.TestCase):
    def test_all_position_maps_have_exact_inverse(self):
        for transform in range(8):
            transformed = set()
            for move in legal_moves(State()):
                other = transform_move(move, transform)
                transformed.add(other)
                self.assertEqual(inverse_transform_move(other, transform), move)
            self.assertEqual(len(transformed), 81)
        self.assertEqual(transform_move(35, 1), 95)
        self.assertEqual(transform_move(31, 1), 93)

    def test_every_symmetry_preserves_legal_moves_and_state_transition(self):
        for state in replay(EXAMPLE):
            for transform in range(8):
                other = transform_state(state, transform)
                expected_moves = {transform_move(move, transform) for move in legal_moves(state)}
                self.assertEqual(set(legal_moves(other)), expected_moves)
                self.assertEqual((other.status, other.ply, other.turn), (state.status, state.ply, state.turn))
                for move in legal_moves(state):
                    self.assertEqual(
                        transform_state(play(state, move), transform),
                        play(other, transform_move(move, transform)))

    def test_canonical_keys_are_identical_under_all_eight_transforms(self):
        for state in replay(EXAMPLE):
            canonical, transform = canonicalize(state)
            self.assertEqual(canonical, transform_state(state, transform))
            self.assertEqual(canonicalize(canonical)[0], canonical)
            for t in range(8):
                self.assertEqual(state_key(state), state_key(transform_state(state, t)))

    def test_first_ply_has_fifteen_classes_with_forced_board_included(self):
        keys = {state_key(play(State(), move)) for move in legal_moves(State())}
        self.assertEqual(len(keys), 15)
        groups = action_groups(State())
        self.assertEqual(len(groups), 15)
        self.assertEqual(sum(map(len, groups.values())), 81)
        self.assertEqual(groups[12], (12, 14, 32, 36, 74, 78, 96, 98))

    def test_key_includes_turn_forced_rule_and_canonical_version(self):
        state = play(State(), 14)
        self.assertNotEqual(state_key(state), state_key(State(state.board, state.turn, 5)))
        self.assertNotEqual(state_key(State()), state_key(State(turn="O")))
        self.assertNotEqual(state_key(State()), state_key(State(rule_version="another-version")))
        data = json.loads(state_key(state))
        self.assertEqual(data["canonical_version"], CANONICAL_VERSION)
        self.assertEqual(data["rule_version"], state.rule_version)
        self.assertEqual(len(data["board"]), 81)
        self.assertIn("local_status", data)

    def test_groups_partition_only_current_legal_moves(self):
        for state in replay(EXAMPLE):
            canonical, _ = canonicalize(state)
            groups = action_groups(canonical)
            flat = [move for group in groups.values() for move in group]
            self.assertEqual(len(flat), len(set(flat)))
            self.assertEqual(set(flat), set(legal_moves(canonical)))
            self.assertTrue(all(rep == min(group) for rep, group in groups.items()))
            for rep, group in groups.items():
                successors = {state_key(play(canonical, move)) for move in group}
                self.assertEqual(len(successors), 1)

    def test_returned_group_mapping_cannot_corrupt_cache(self):
        groups = action_groups(State())
        groups.clear()
        self.assertEqual(len(action_groups(State())), 15)


if __name__ == "__main__":
    unittest.main()
