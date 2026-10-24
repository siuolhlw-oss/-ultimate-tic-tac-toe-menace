"""唯讀匯出已完成的 1,000 局：原始方向、每手序號、PNG 與 ZIP。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from uttt.engine import RULE_VERSION, State, replay, state_from_dict

RENDER_VERSION = "numbered-games-v1"
DISPLAY_MAPPING = {"X": "○", "O": "×"}
SIZE = (1600, 1840)
CELL = 146
BLOCK = 3 * CELL
GAP_X = 22
GAP_Y = 18
HEADER = 38
ORIGIN = (121, 264)
INK = "#263342"
PALETTE = {"X": ("#e5f2f1", "#125650"),
           "O": ("#f4e9e7", "#853b32"),
           "D": ("#e8ebef", "#465364"),
           ".": ("#f1f3f6", "#465364")}


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class GameRecord:
    number: int
    game_id: str
    run_id: str
    batch_index: int
    moves: tuple[int, ...]
    result: str
    terminal: State

    def source_payload(self) -> dict:
        return {"number": self.number, "game_id": self.game_id,
                "run_id": self.run_id, "batch_index": self.batch_index,
                "moves": list(self.moves), "result": self.result,
                "board": self.terminal.board, "rule_version": self.terminal.rule_version}

    @property
    def source_sha256(self) -> str:
        return sha256(json_text(self.source_payload()).encode("utf-8")).hexdigest()


def load_games(database: Path, expected_games: int = 1000) -> list[GameRecord]:
    """明確限定 pilot_batches，避免把後續真人 PK 包含進來。"""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = connection.execute("""
            SELECT g.game_id,g.run_id,g.x_version,g.o_version,g.mode,g.is_example,
                   g.rule_version,g.ply,g.status,g.result,g.state_json,
                   b.batch_index,b.start_index,b.requested_games,b.status AS batch_status
            FROM games AS g JOIN pilot_batches AS b ON g.run_id=b.run_id
            ORDER BY g.x_version
        """).fetchall()
        if len(rows) != expected_games:
            raise ValueError(f"批次應有 {expected_games} 局，實際為 {len(rows)} 局。")
        records = []
        for expected, row in enumerate(rows):
            if not (row["x_version"] == row["o_version"] == expected
                    and row["start_index"] <= expected < row["start_index"] + row["requested_games"]
                    and row["mode"] == "train" and not row["is_example"]
                    and row["status"] == "finished" and row["batch_status"] == "completed"
                    and row["rule_version"] == RULE_VERSION):
                raise ValueError(f"第 {expected + 1} 局的批次／版本／狀態不一致。")
            moves = connection.execute(
                "SELECT ply,actor,move_code FROM moves WHERE game_id=? ORDER BY ply",
                (row["game_id"],)).fetchall()
            if [m["ply"] for m in moves] != list(range(1, row["ply"] + 1)):
                raise ValueError("落子序號缺漏或重複。")
            if any(m["actor"] != ("X" if m["ply"] % 2 else "O") for m in moves):
                raise ValueError("行動方與奇偶手不一致。")
            sequence = tuple(m["move_code"] for m in moves)
            terminal = replay(sequence)[-1]  # 逐手合法性、封閉盤、終局與占用位置一併查核。
            if (terminal != state_from_dict(json.loads(row["state_json"]))
                    or terminal.status != row["result"] or terminal.status == "active"):
                raise ValueError("重播終局與資料庫不符。")
            records.append(GameRecord(expected + 1, row["game_id"], row["run_id"],
                                      row["batch_index"], sequence, row["result"], terminal))
        return records
    finally:
        connection.close()


def cell_box(move: int) -> tuple[int, int, int, int]:
    big, small = divmod(move, 10)
    if not 1 <= big <= 9 or not 1 <= small <= 9:
        raise ValueError("大小格編號都必須在 1–9。")
    br, bc = divmod(big - 1, 3)
    sr, sc = divmod(small - 1, 3)
    x = ORIGIN[0] + bc * (BLOCK + GAP_X) + sc * CELL
    y = ORIGIN[1] + br * (BLOCK + HEADER + GAP_Y) + HEADER + sr * CELL
    return x, y, x + CELL, y + CELL


def annotations(record: GameRecord) -> list[dict]:
    return [{"ply": ply, "move": move, "raw_actor": "X" if ply % 2 else "O",
             "display_symbol": "○" if ply % 2 else "×", "box": list(cell_box(move))}
            for ply, move in enumerate(record.moves, 1)]


def png_metadata(record: GameRecord) -> dict:
    return {"render_version": RENDER_VERSION, "game_id": record.game_id,
            "game_number": record.number, "rule_version": RULE_VERSION,
            "raw_result": record.result, "total_plies": len(record.moves),
            "moves": list(record.moves), "display_mapping": DISPLAY_MAPPING,
            "orientation": "original", "source_sha256": record.source_sha256,
            "annotations": annotations(record)}


def fonts() -> dict[int, ImageFont.FreeTypeFont]:
    candidates = [Path("C:/Windows/Fonts/msjh.ttc"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        raise FileNotFoundError("需安裝微軟正黑體或 Noto Sans CJK 正體中文字型。")
    return {size: ImageFont.truetype(str(path), size) for size in (18, 23, 25, 28, 31, 36, 46)}


def render_game(record: GameRecord, font_set: dict | None = None) -> bytes:
    font_set = font_set or fonts()
    canvas = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((121, 43), f"第 {record.number:04d} 局｜超級井字棋棋譜", font=font_set[46], fill=INK)
    result_label = {"X": "○ 勝（原始 X）", "O": "× 勝（原始 O）", "draw": "和局"}[record.result]
    draw.text((121, 110), f"共 {len(record.moves)} 手    結果：{result_label}    原始棋盤方向", font=font_set[28], fill=INK)
    draw.text((121, 160), "奇數手 ○（原始 X）｜偶數手 ×（原始 O）｜棋子下方數字＝第幾手", font=font_set[25], fill=INK)
    draw.text((121, 201), "大小格皆由左上到右下編號 1–9；位置 35＝大格 3 的小格 5。", font=font_set[25], fill=INK)

    for big in range(1, 10):
        x, y, _, _ = cell_box(big * 10 + 1)
        status = record.terminal.local_status[big - 1]
        tint, accent = PALETTE[status]
        status_label = {"X": "○ 取得", "O": "× 取得", "D": "和局封閉", ".": "未取得"}[status]
        draw.rectangle((x, y - HEADER, x + BLOCK, y), fill=tint)
        draw.text((x + 12, y - HEADER + 2), f"大格 {big}  ·  {status_label}", font=font_set[25], fill=accent)
        for line in range(4):
            width = 3 if line in (0, 3) else 1
            colour = "#586270" if width == 3 else "#c2c9d0"
            draw.line((x + line * CELL, y, x + line * CELL, y + BLOCK), fill=colour, width=width)
            draw.line((x, y + line * CELL, x + BLOCK, y + line * CELL), fill=colour, width=width)
        for small in range(1, 10):
            cx, cy, _, _ = cell_box(big * 10 + small)
            draw.text((cx + 8, cy + 5), str(small), font=font_set[18], fill="#89929c")

    for annotation in annotations(record):
        x, y, _, _ = annotation["box"]
        center = x + CELL // 2
        symbol_y = y + 49
        colour = PALETTE[annotation["raw_actor"]][1]
        if annotation["raw_actor"] == "X":
            draw.ellipse((center - 24, symbol_y - 24, center + 24, symbol_y + 24), outline=colour, width=6)
        else:
            draw.line((center - 23, symbol_y - 23, center + 23, symbol_y + 23), fill=colour, width=6)
            draw.line((center - 23, symbol_y + 23, center + 23, symbol_y - 23), fill=colour, width=6)
        draw.text((center, y + 108), str(annotation["ply"]), font=font_set[36], fill=INK, anchor="mm")

    draw.text((121, 1750), f"原始局號：{record.game_id}", font=font_set[23], fill="#536171")
    draw.text((121, 1790), f"第 {record.batch_index + 1} 批｜{RULE_VERSION}｜僅對調顯示符號，原始資料保持不變。", font=font_set[23], fill="#536171")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt("uttt_game", json_text(png_metadata(record)))
    stream = BytesIO()
    canvas.save(stream, format="PNG", pnginfo=metadata, compress_level=6, dpi=(240, 240))
    return stream.getvalue()


def write_image(record: GameRecord, path: Path, font_set: dict) -> dict:
    if path.exists():
        with Image.open(path) as existing:
            if (existing.size != SIZE or json.loads(existing.info.get("uttt_game", "{}"))
                    != png_metadata(record)):
                raise FileExistsError(f"既有圖片來源不符，拒絕覆寫：{path}")
            existing.verify()
    else:
        content = render_game(record, font_set)
        with path.open("xb") as target:
            target.write(content)
    content = path.read_bytes()
    return {"number": record.number, "game_id": record.game_id,
            "file": "images/" + path.name, "plies": len(record.moves),
            "raw_result": record.result, "source_sha256": record.source_sha256,
            "png_sha256": sha256(content).hexdigest(), "bytes": len(content)}


def export(database: Path, destination: Path, preview: bool = False) -> dict:
    database = database.resolve()
    records = load_games(database)
    if preview:
        # 包含最短、最長局，以檢查稠密棋盤與序號可讀性。
        selected_numbers = {1, min(records, key=lambda r: len(r.moves)).number,
                            max(records, key=lambda r: len(r.moves)).number}
        records = [r for r in records if r.number in selected_numbers]
    folder = destination / "images"
    folder.mkdir(parents=True, exist_ok=True)
    font_set = fonts()
    items = []
    for index, record in enumerate(records, 1):
        items.append(write_image(record, folder / f"game-{record.number:04d}.png", font_set))
        if index % 100 == 0:
            print(f"已產生／核對 {index}/{len(records)} 張", flush=True)
    if preview:
        return {"preview": [item["file"] for item in items]}
    collection_hash = sha256(json_text([r.source_payload() for r in records]).encode("utf-8")).hexdigest()
    manifest_path = destination / "image-manifest.json"
    manifest = {"format_version": 1, "render_version": RENDER_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_database": str(database.as_posix()),
                "selection": "games JOIN pilot_batches ON games.run_id=pilot_batches.run_id; ORDER BY games.x_version",
                "source_collection_sha256": collection_hash,
                "game_count": len(records), "total_plies": sum(len(r.moves) for r in records),
                "display_mapping": DISPLAY_MAPPING, "orientation": "original", "image_size": list(SIZE),
                "all_games_legally_replayed": True, "images": items}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if {k: v for k, v in previous.items() if k != "created_at"} != {k: v for k, v in manifest.items() if k != "created_at"}:
            raise FileExistsError("既有清單來源或圖檔不同，拒絕覆寫。")
        manifest = previous
    else:
        with manifest_path.open("x", encoding="utf-8") as target:
            json.dump(manifest, target, ensure_ascii=False, indent=2)
    archive_path = destination / "1000-games-images.zip"
    expected_names = {item["file"] for item in items} | {manifest_path.name}
    if archive_path.exists():
        with zipfile.ZipFile(archive_path) as archive:
            if (len(archive.namelist()) != len(expected_names) or set(archive.namelist()) != expected_names
                    or archive.testzip() is not None
                    or archive.read(manifest_path.name) != manifest_path.read_bytes()
                    or any(sha256(archive.read(item["file"])).hexdigest() != item["png_sha256"] for item in items)):
                raise FileExistsError("既有 ZIP 內容不同，拒絕覆寫。")
    else:
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_STORED) as archive:
            for item in items:
                archive.write(destination / item["file"], item["file"])
            archive.write(manifest_path, manifest_path.name)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP CRC 驗證失敗。")
    return {"images": len(items), "plies": manifest["total_plies"],
            "archive": str(archive_path), "archive_bytes": archive_path.stat().st_size,
            "archive_sha256": sha256(archive_path.read_bytes()).hexdigest(),
            "source_collection_sha256": collection_hash, "manifest": str(manifest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", "--database", dest="database", type=Path,
                        default=ROOT / "data/selfplay-1000/training.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "exports/game-atlas")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(args.database, args.output, args.preview), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
