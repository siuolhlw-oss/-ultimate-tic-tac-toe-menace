"""指定盤須參與完整局面身分；對稱與儲存以合法棋譜驗證。"""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageColor
from storage_v3.repository import CompactResearchService
from tools.document_state_identity import EXAMPLES, FIGURE_SIZE, GREEN, LEGAL, render_example
from tools.export_game_images import cell_box
from uttt.engine import InvalidMove, legal_moves, play, replay
from uttt.service import ResearchService
from uttt.symmetry import (action_groups, canonicalize, state_key,
                           transform_move, transform_state)


class StateIdentityTests(unittest.TestCase):
    def setUp(self):
        self.a = replay(EXAMPLES['A'])[-1]
        self.b = replay(EXAMPLES['B'])[-1]

    def test_same_marks_with_different_forced_are_not_equivalent(self):
        self.assertEqual(self.a.board, self.b.board)
        self.assertEqual(self.a.turn, self.b.turn)
        self.assertEqual((self.a.forced, self.b.forced), (1, 3))
        self.assertNotEqual(state_key(self.a), state_key(self.b))
        for transform in range(8):
            self.assertNotEqual(transform_state(self.a, transform), self.b)

    def test_legal_sets_and_wrong_forced_moves(self):
        self.assertEqual(legal_moves(self.a), LEGAL['A'])
        self.assertEqual(legal_moves(self.b), LEGAL['B'])
        with self.assertRaises(InvalidMove):
            play(self.a, 31)
        with self.assertRaises(InvalidMove):
            play(self.b, 11)

    def test_different_last_moves_with_same_effect_can_share_key(self):
        one = replay((12, 21, 13, 31))[-1]
        other = replay((13, 31, 12, 21))[-1]
        self.assertEqual(one, other)
        self.assertEqual(one.forced, 1)
        self.assertEqual(state_key(one), state_key(other))

    def test_all_true_symmetries_keep_keys_and_legal_correspondence(self):
        for state in (self.a, self.b):
            for transform in range(8):
                other = transform_state(state, transform)
                self.assertEqual(state_key(state), state_key(other))
                self.assertEqual(set(legal_moves(other)),
                                 {transform_move(m, transform) for m in legal_moves(state)})

    def test_both_repositories_separate_boxes_and_reuse_true_symmetry(self):
        for factory in (ResearchService, CompactResearchService):
            with self.subTest(repository=factory.__name__), tempfile.TemporaryDirectory() as folder:
                with factory(Path(folder) / 'isolated.sqlite3') as service:
                    model = service.create_model('指定盤核對模型', seed=37)
                    a, _ = canonicalize(self.a)
                    b, _ = canonicalize(self.b)
                    rotated, _ = canonicalize(transform_state(self.a, 1))
                    with service.repo.transaction():
                        self.assertTrue(service._ensure_box(model, a, None, None))
                        self.assertTrue(service._ensure_box(model, b, None, None))
                        self.assertFalse(service._ensure_box(model, rotated, None, None))
                    boxes = service.repo.all('SELECT state_key FROM model_states WHERE model_id=?', (model,))
                    self.assertEqual({row['state_key'] for row in boxes},
                                     {state_key(self.a), state_key(self.b)})
                    for canonical in (a, b):
                        weights = service._weights(model, state_key(canonical))
                        self.assertEqual(set(weights), set(action_groups(canonical)))
                        self.assertTrue(all(weight == 3 for weight in weights.values()))
                    self.assertEqual(service._model(model)['version'], 0)

    def test_figures_match_sequences_and_highlight_only_legal_cells(self):
        for label, state in (('A', self.a), ('B', self.b)):
            with self.subTest(example=label):
                content, metadata = render_example(label)
                with Image.open(BytesIO(content)) as image:
                    self.assertEqual(image.size, FIGURE_SIZE)
                    self.assertEqual(json.loads(image.info['uttt_example']), metadata)
                    self.assertEqual(metadata['moves'], list(EXAMPLES[label]))
                    self.assertEqual(metadata['forced'], state.forced)
                    self.assertEqual(metadata['legal_moves'], list(LEGAL[label]))
                    for move in legal_moves(state):
                        x, y, _, _ = cell_box(move)
                        self.assertEqual(image.getpixel((x + 40, y + 35)), ImageColor.getrgb(GREEN))
                    occupied = 12 if label == 'A' else 34
                    x, y, _, _ = cell_box(occupied)
                    self.assertNotEqual(image.getpixel((x + 40, y + 35)), ImageColor.getrgb(GREEN))
                    wrong_board = 31 if label == 'A' else 11
                    x, y, _, _ = cell_box(wrong_board)
                    self.assertNotEqual(image.getpixel((x + 40, y + 35)), ImageColor.getrgb(GREEN))


if __name__ == '__main__':
    unittest.main()
