"""棋譜圖片的資料／方向／序號與輸出完整性驗證。"""

from contextlib import closing
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from PIL import Image
from tools.export_game_images import (
    BLOCK, CELL, DISPLAY_MAPPING, GameRecord, ROOT, SIZE, annotations, cell_box,
    fonts, load_games, png_metadata, render_game, write_image,
)
from uttt.engine import RULE_VERSION, replay, state_to_dict

EXAMPLE = (14, 41, 15, 51, 16, 62, 24, 42, 25, 52, 26, 63, 34, 43, 35, 53, 36)


class GameImagesTests(unittest.TestCase):
    def setUp(self):
        self.record = GameRecord(1, "game_example", "run_example", 0, EXAMPLE, "X", replay(EXAMPLE)[-1])

    def test_original_macro_and_micro_coordinate_layout(self):
        boxes = [cell_box(10*b+s) for b in range(1, 10) for s in range(1, 10)]
        self.assertEqual(len(set(boxes)), 81)
        top_left, top_mid, top_right = cell_box(11), cell_box(12), cell_box(31)
        self.assertEqual(top_mid[0] - top_left[0], CELL)
        self.assertEqual(top_mid[1], top_left[1])
        self.assertGreater(top_right[0] - top_left[0], BLOCK * 2)
        self.assertEqual(top_right[1], top_left[1])
        self.assertGreater(cell_box(71)[1], cell_box(41)[1])
        self.assertEqual(cell_box(35)[0], cell_box(31)[0] + CELL)
        self.assertEqual(cell_box(35)[1], cell_box(31)[1] + CELL)
        for invalid in (10, 20, 100, 1, -11):
            with self.assertRaises(ValueError):
                cell_box(invalid)

    def test_every_move_has_separate_symbol_and_order_with_embedded_metadata(self):
        payload = render_game(self.record)
        with Image.open(BytesIO(payload)) as image:
            metadata = json.loads(image.info["uttt_game"])
            self.assertEqual(image.size, SIZE)
            self.assertEqual(metadata, png_metadata(self.record))
            self.assertEqual(metadata["moves"], list(EXAMPLE))
            self.assertEqual(metadata["display_mapping"], DISPLAY_MAPPING)
            self.assertEqual([a["ply"] for a in metadata["annotations"]], list(range(1, 18)))
            for item in metadata["annotations"]:
                self.assertEqual(item["display_symbol"], "○" if item["ply"] % 2 else "×")
                x, y, right, bottom = item["box"]
                # 圖形和數字位於不同縱向區域，兩者各自確實有深色筆畫。
                for crop in ((x+35, y+22, right-35, y+77), (x+32, y+88, right-32, bottom-11)):
                    pixels = list(image.crop(crop).get_flattened_data())
                    self.assertGreater(sum(max(pixel) < 180 for pixel in pixels), 100)

    def test_existing_unrelated_image_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "game.png"
            font_set = fonts()
            first = write_image(self.record, target, font_set)
            self.assertEqual(first, write_image(self.record, target, font_set))
            Image.new("RGB", SIZE, "white").save(target)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError):
                write_image(self.record, target, font_set)
            self.assertEqual(original, target.read_bytes())

    def test_batch_selection_excludes_pk_and_invalid_sequence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE games (game_id TEXT,run_id TEXT,x_version INTEGER,o_version INTEGER,
                      mode TEXT,is_example INTEGER,rule_version TEXT,ply INTEGER,status TEXT,result TEXT,state_json TEXT);
                    CREATE TABLE pilot_batches (run_id TEXT,batch_index INTEGER,start_index INTEGER,requested_games INTEGER,status TEXT);
                    CREATE TABLE moves (game_id TEXT,ply INTEGER,actor TEXT,move_code INTEGER);
                """)
                connection.execute("INSERT INTO pilot_batches VALUES ('run_example',0,0,1,'completed')")
                fields = ("game_example", "run_example", 0, 0, "train", 0, RULE_VERSION, 17,
                          "finished", "X", json.dumps(state_to_dict(self.record.terminal)))
                connection.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?)", fields)
                connection.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   ("pk_extra", "other_run", *fields[2:]))
                connection.executemany("INSERT INTO moves VALUES (?,?,?,?)",
                                       [("game_example", a["ply"], a["raw_actor"], a["move"])
                                        for a in annotations(self.record)])
            records = load_games(path, expected_games=1)
            self.assertEqual([r.game_id for r in records], ["game_example"])
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE moves SET move_code=14 WHERE ply=2")
            with self.assertRaises(ValueError):
                load_games(path, expected_games=1)

    @unittest.skipUnless((ROOT / "exports/game-atlas/image-manifest.json").exists(), "尚未產生本機完整圖集")
    def test_full_1000_artifact_manifest_png_metadata_and_archive_crc(self):
        folder = ROOT / "exports/game-atlas"
        manifest = json.loads((folder / "image-manifest.json").read_text(encoding="utf-8"))
        records = load_games(ROOT / "data/selfplay-1000/training.sqlite3")
        self.assertEqual(manifest["game_count"], 1000)
        self.assertEqual(manifest["total_plies"], 58733)
        self.assertEqual(len(manifest["images"]), 1000)
        with zipfile.ZipFile(folder / "1000-games-images.zip") as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), 1001)
            self.assertEqual(archive.read("image-manifest.json"), (folder / "image-manifest.json").read_bytes())
            for record, item in zip(records, manifest["images"], strict=True):
                self.assertEqual(item["number"], record.number)
                self.assertEqual(item["game_id"], record.game_id)
                content = (folder / item["file"]).read_bytes()
                self.assertEqual(sha256(content).hexdigest(), item["png_sha256"])
                self.assertEqual(archive.read(item["file"]), content)
                with Image.open(BytesIO(content)) as image:
                    self.assertEqual(json.loads(image.info["uttt_game"]), png_metadata(record))


if __name__ == "__main__":
    unittest.main()
