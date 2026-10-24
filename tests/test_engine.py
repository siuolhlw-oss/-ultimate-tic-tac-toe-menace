import dataclasses
import unittest

from uttt.engine import (InvalidMove, State, legal_moves, move_index, play,
                         replay, state_from_dict, state_to_dict)

EXAMPLE = [14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36]
DRAW_BOARD = "XOXXOOOXX"


def fixture(board_parts, *, turn="X", forced=0):
    """局部規則邊界用棋盤；不宣稱任意配置都可由開局到達。"""
    parts = ["." * 9] * 9
    for big, cells in board_parts.items():
        parts[big - 1] = cells
    return State("".join(parts), turn, forced)


class EngineTests(unittest.TestCase):
    def test_initial_board_and_encoding(self):
        state = State()
        self.assertEqual(len(legal_moves(state)), 81)
        self.assertEqual(state.local_status, (".",) * 9)
        self.assertEqual((state.status, state.turn, state.forced, state.ply), ("active", "X", 0, 0))
        self.assertEqual(move_index(35), 22)
        self.assertEqual(play(state, 35).board[22], "X")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.turn = "O"

    def test_forced_board_and_occupied_square(self):
        state = play(State(), 14)
        self.assertEqual(legal_moves(state), tuple(range(41, 50)))
        with self.assertRaises(InvalidMove):
            play(state, 51)
        state = play(state, 41)
        self.assertEqual(legal_moves(state), (11, 12, 13, 15, 16, 17, 18, 19))
        with self.assertRaises(InvalidMove):
            play(state, 14)

    def test_invalid_move_codes_do_not_change_state(self):
        state = State()
        for move in (0, 7, 10, 20, 100, -11, 35.0, "35", True, None):
            with self.subTest(move=move), self.assertRaises(InvalidMove):
                play(state, move)
        self.assertEqual(state, State())

    def test_won_board_is_closed_and_targeting_it_is_free(self):
        state = fixture({1: "XXXOO...."}, forced=2)
        self.assertEqual(state.local_status[0], "X")
        with self.assertRaises(InvalidMove):
            play(state, 16)
        child = play(state, 21)
        self.assertEqual(child.forced, 0)
        self.assertTrue(all(move // 10 != 1 for move in legal_moves(child)))

    def test_drawn_board_is_closed_and_belongs_to_neither_player(self):
        state = fixture({1: DRAW_BOARD}, forced=2)
        self.assertEqual(state.local_status[0], "D")
        self.assertEqual(state.status, "active")
        self.assertEqual(play(state, 21).forced, 0)
        with self.assertRaises(InvalidMove):
            play(state, 11)

    def test_closing_the_target_during_this_move_releases_next_player(self):
        state = fixture({1: ".XXOO...."}, forced=1)
        child = play(state, 11)
        self.assertEqual(child.local_status[0], "X")
        self.assertEqual(child.forced, 0)
        self.assertNotIn(14, legal_moves(child))

    def test_seventeen_move_example_and_immediate_macro_win(self):
        states = replay(EXAMPLE)
        self.assertEqual(len(states), 18)
        for ply, state in enumerate(states):
            self.assertEqual(state.ply, ply)
        for ply, big in ((5, 1), (11, 2), (17, 3)):
            self.assertEqual(states[ply].local_status[big - 1], "X")
        self.assertEqual(states[14].local_status[3], "O")
        self.assertEqual(states[16].local_status[4], "O")
        self.assertTrue(all(s.status == "active" for s in states[:-1]))
        final = states[-1]
        self.assertEqual((final.status, final.forced, final.turn), ("X", -1, "O"))
        self.assertEqual(legal_moves(final), ())
        with self.assertRaises(InvalidMove):
            play(final, 64)

    def test_all_drawn_boards_end_in_draw(self):
        state = State(DRAW_BOARD * 9)
        self.assertEqual(state.local_status, ("D",) * 9)
        self.assertEqual((state.status, state.forced), ("draw", -1))
        self.assertEqual(legal_moves(state), ())

    def test_replay_rejects_illegal_move_and_post_terminal_move(self):
        with self.assertRaises(InvalidMove):
            replay([14, 51])
        with self.assertRaises(InvalidMove):
            replay(EXAMPLE + [64])

    def test_serialization_checks_derived_values(self):
        for state in replay(EXAMPLE):
            self.assertEqual(state_from_dict(state_to_dict(state)), state)
        data = state_to_dict(play(State(), 14))
        data["local_status"][0] = "X"
        with self.assertRaises(ValueError):
            state_from_dict(data)
        for key, bad_value in (("local_status", None), ("ply", True)):
            data = state_to_dict(play(State(), 14))
            data[key] = bad_value
            with self.subTest(key=key), self.assertRaises(ValueError):
                state_from_dict(data)
        data = state_to_dict(replay(EXAMPLE)[-1])
        data["forced"] = 6
        with self.assertRaises(ValueError):
            state_from_dict(data)

    def test_malformed_board_and_unsupported_rule(self):
        for board in ("." * 80, "?" + "." * 80, "XXXOOO..." + "." * 72):
            with self.subTest(board=board), self.assertRaises(ValueError):
                State(board)
        with self.assertRaises(ValueError):
            legal_moves(State(rule_version="unsupported"))


if __name__ == "__main__":
    unittest.main()
