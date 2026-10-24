"""核對指定盤示例及前三手計數，產生兩張研究用棋譜圖；不讀寫訓練模型。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, PngImagePlugin
from tools.export_game_images import (BLOCK, CELL, DISPLAY_MAPPING, HEADER,
                                      INK, PALETTE, cell_box, fonts)
from uttt.engine import State, legal_moves, replay, play
from uttt.service import runtime_info
from uttt.symmetry import canonicalize, state_key, transform_state

EXAMPLES = {"A": (12, 23, 34, 41), "B": (34, 41, 12, 23)}
FIGURE_SIZE = (1600, 1900)
LEGAL = {"A": (11, 13, 14, 15, 16, 17, 18, 19),
         "B": (31, 32, 33, 35, 36, 37, 38, 39)}
GREEN = "#e7f5ee"
GREEN_DARK = "#187047"
ORANGE = "#bf681f"


def example_states():
    states = {label: replay(moves)[-1] for label, moves in EXAMPLES.items()}
    a, b = states["A"], states["B"]
    assert a.board == b.board and a.turn == b.turn == "X"
    assert a.forced == 1 and b.forced == 3
    assert not any(transform_state(a, t) == b for t in range(8))
    assert state_key(a) != state_key(b)
    assert all(legal_moves(states[label]) == LEGAL[label] for label in EXAMPLES)
    return states


def opening_counts():
    level = {State()}
    rows = []
    expected = ((1, 1), (81, 15), (720, 102), (6336, 822))
    for depth in range(4):
        fixed = [sum(transform_state(s, t) == s for s in level) for t in range(8)]
        classes = len({canonicalize(s)[0] for s in level})
        assert (len(level), classes) == expected[depth]
        assert sum(fixed) == 8 * classes
        rows.append({"moves_played": depth, "raw_states": len(level),
                     "d4_classes": classes, "fixed_by_transform": fixed})
        if depth < 3:
            level = {play(s, m) for s in level for m in legal_moves(s)}
    return rows


def render_example(label, font_set=None):
    state = example_states()[label]
    font_set = font_set or fonts()
    moves = EXAMPLES[label]
    figure = 1 if label == "A" else 2
    canvas = Image.new("RGB", FIGURE_SIZE, "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((121, 43), f"圖 {figure}｜棋譜 {label}：下一手指定大格 {state.forced}",
              font=font_set[46], fill=INK)
    draw.text((121, 110), "合法規則示例（4 手）｜下一手：○（原始 X）｜尚未終局",
              font=font_set[28], fill=INK)
    draw.text((121, 160), "奇數手 ○（原始 X）｜偶數手 ×（原始 O）｜棋子下方數字＝手序",
              font=font_set[25], fill=INK)
    draw.text((121, 201), "棋譜：" + " → ".join(map(str, moves)), font=font_set[28], fill=INK)
    legal = set(legal_moves(state))
    for big in range(1, 10):
        x, y, _, _ = cell_box(big * 10 + 1)
        forced = big == state.forced
        draw.rectangle((x, y - HEADER, x + BLOCK, y),
                       fill=GREEN if forced else "#f1f3f6")
        draw.text((x + 12, y - HEADER + 2),
                  f"大格 {big}" + (" · 下一手指定盤" if forced else ""),
                  font=font_set[25], fill=GREEN_DARK if forced else INK)
        for small in range(1, 10):
            move = big * 10 + small
            cx, cy, ex, ey = cell_box(move)
            if move in legal:
                draw.rectangle((cx, cy, ex, ey), fill=GREEN)
        for line in range(4):
            width = 3 if line in (0, 3) else 1
            color = "#586270" if width == 3 else "#c2c9d0"
            draw.line((x + line * CELL, y, x + line * CELL, y + BLOCK), fill=color, width=width)
            draw.line((x, y + line * CELL, x + BLOCK, y + line * CELL), fill=color, width=width)
        for small in range(1, 10):
            cx, cy, _, _ = cell_box(big * 10 + small)
            draw.text((cx + 8, cy + 5), str(small), font=font_set[18], fill="#89929c")
        if forced:
            draw.rectangle((x - 2, y - HEADER - 2, x + BLOCK + 2, y + BLOCK + 2),
                           outline=GREEN_DARK, width=5)
    annotations = []
    for ply, move in enumerate(moves, 1):
        actor = "X" if ply % 2 else "O"
        x, y, ex, ey = cell_box(move)
        center, sy = x + CELL // 2, y + 49
        color = PALETTE[actor][1]
        if actor == "X":
            draw.ellipse((center - 24, sy - 24, center + 24, sy + 24), outline=color, width=6)
        else:
            draw.line((center - 23, sy - 23, center + 23, sy + 23), fill=color, width=6)
            draw.line((center - 23, sy + 23, center + 23, sy - 23), fill=color, width=6)
        draw.text((center, y + 108), str(ply), font=font_set[36], fill=INK, anchor="mm")
        if ply == len(moves):
            draw.rectangle((x + 4, y + 4, ex - 4, ey - 4), outline=ORANGE, width=5)
        annotations.append({"ply": ply, "move": move, "raw_actor": actor,
                            "display_symbol": DISPLAY_MAPPING[actor]})
    draw.text((121, 1750), "綠色空格＝下一手合法位置；橙框＝最後一手；大小格皆按左上至右下編號。",
              font=font_set[25], fill=INK)
    draw.text((121, 1795), "下一手合法位置：" + "、".join(map(str, sorted(legal))),
              font=font_set[25], fill=GREEN_DARK)
    draw.text((121, 1840), "相同棋子配置仍須比較指定盤；本圖為規則示例，不是 AI 訓練結果。",
              font=font_set[23], fill="#536171")
    metadata = {"render_version": "forced-board-example-v1", "example": label,
                "moves": list(moves), "annotations": annotations,
                "raw_board": state.board, "turn": state.turn, "forced": state.forced,
                "legal_moves": sorted(legal), "rule_version": state.rule_version,
                "display_mapping": DISPLAY_MAPPING, "orientation": "original",
                "canonical_key_sha256": sha256(state_key(state).encode()).hexdigest(),
                "scope": "合法非終局規則示例；沒有訓練或模型寫入。"}
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_itxt("uttt_example", json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    stream = BytesIO()
    canvas.save(stream, format="PNG", pnginfo=pnginfo, compress_level=6, dpi=(240, 240))
    return stream.getvalue(), metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/research-figures/forced-board'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    example_states()
    font_set = fonts()
    images = []
    for label in EXAMPLES:
        content, metadata = render_example(label, font_set)
        path = args.output / f'example-{label}.png'
        if path.exists() and path.read_bytes() != content:
            raise FileExistsError(f'既有圖片內容不同，拒絕覆寫：{path}')
        if not path.exists():
            path.write_bytes(content)
        images.append({'path': str(path.resolve()), 'bytes': len(content),
                       'sha256': sha256(content).hexdigest(), 'metadata': metadata})
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'same_board': True, 'same_turn': True, 'd4_equivalent': False,
              'canonical_keys_equal': False, 'images': images,
              'opening_counts': opening_counts(), 'runtime': runtime_info(),
              'scope': '純規則示例及前三手完整枚舉核對；沒有修改研究模型。'}
    report_path = args.output / 'verification.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(report_path.resolve()), 'images': [x['path'] for x in images],
                      'd4_equivalent': False, 'opening_counts': report['opening_counts']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
