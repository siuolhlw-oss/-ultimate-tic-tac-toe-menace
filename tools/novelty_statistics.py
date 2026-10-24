"""唯讀統計完成訓練棋譜的新穎率；不改模型、亂數或研究資料庫。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uttt.symmetry import transform_move
from uttt.service import now
from tools.continue_selfplay import save_json

MOVE_MAPS = tuple({m: transform_move(m, t) for m in range(11, 100) if m % 10} for t in range(8))


def sequence_key(moves):
    """一整局只用同一個 D4 變換；保留順序、不交換 X/O。"""
    return min(bytes(mapping[m] for m in moves) for mapping in MOVE_MAPS)


def collect(database, block_size=1000):
    if block_size < 1:
        raise ValueError('統計區間必須大於零。')
    path = Path(database).resolve()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        if connection.execute('PRAGMA user_version').fetchone()[0] != 3:
            raise ValueError('此統計入口只接受 v3。')
        games = connection.execute("SELECT game_id,x_version,o_version,ply,rule_version FROM _games "
                                   "WHERE mode='train' AND status='finished' AND is_example=0 ORDER BY x_version").fetchall()
        seen_states, seen_raw, seen_d4 = set(), set(), set()
        per_game, blocks, depths = [], {}, {}
        rules = {g['rule_version'] for g in games}
        if len(rules) > 1:
            raise ValueError('不能把不同規則合併計數。')
        for index, game in enumerate(games):
            if game['x_version'] != index or game['o_version'] != index:
                raise ValueError('訓練局次有缺漏、重複或版本不符。')
            rows = connection.execute('SELECT ply,move_code,state_id,new_box,model_id FROM _moves WHERE game_id=? ORDER BY ply',
                                      (game['game_id'],)).fetchall()
            if [r['ply'] for r in rows] != list(range(1, game['ply'] + 1)):
                raise ValueError('完整棋譜手數不符。')
            block_index = index // block_size
            b = blocks.setdefault(block_index, dict(first_game=index + 1, last_game=index + 1, games=0,
                                                    moves=0, new_states=0, new_boxes=0, new_raw_games=0, new_d4_games=0,
                                                    models={}))
            codes = [r['move_code'] for r in rows]
            raw, canonical = bytes(codes), sequence_key(codes)
            fresh_raw, fresh_d4 = int(raw not in seen_raw), int(canonical not in seen_d4)
            seen_raw.add(raw)
            seen_d4.add(canonical)
            new_states = new_boxes = 0
            for r in rows:
                fresh = int(r['state_id'] not in seen_states)
                seen_states.add(r['state_id'])
                new_states += fresh
                new_boxes += r['new_box']
                d_index = (r['ply'] - 1) // 10
                d = depths.setdefault((block_index, d_index), dict(first_game=b['first_game'],
                    first_ply=d_index * 10 + 1, last_ply=min(d_index * 10 + 10, 81), moves=0, new_states=0, new_boxes=0))
                d['moves'] += 1
                d['new_states'] += fresh
                d['new_boxes'] += r['new_box']
                m = b['models'].setdefault(r['model_id'], dict(moves=0, new_boxes=0))
                m['moves'] += 1
                m['new_boxes'] += r['new_box']
            b.update(last_game=index + 1, games=b['games'] + 1, moves=b['moves'] + len(rows),
                     new_states=b['new_states'] + new_states, new_boxes=b['new_boxes'] + new_boxes,
                     new_raw_games=b['new_raw_games'] + fresh_raw, new_d4_games=b['new_d4_games'] + fresh_d4)
            per_game.append(dict(game_number=index + 1, game_id=game['game_id'], moves=len(rows), new_states=new_states,
                                 new_boxes=new_boxes, new_raw_game=fresh_raw, new_d4_game=fresh_d4))
        for b in blocks.values():
            b['new_state_pct'] = 100 * b['new_states'] / b['moves']
            b['new_box_pct'] = 100 * b['new_boxes'] / b['moves']
            b['new_raw_game_pct'] = 100 * b['new_raw_games'] / b['games']
            b['new_d4_game_pct'] = 100 * b['new_d4_games'] / b['games']
            b['mean_moves'] = b['moves'] / b['games']
            for m in b['models'].values():
                m['new_box_pct'] = 100 * m['new_boxes'] / m['moves']
        for d in depths.values():
            d['new_state_pct'] = 100 * d['new_states'] / d['moves']
            d['new_box_pct'] = 100 * d['new_boxes'] / d['moves']
        return dict(created_at=now(), database=str(path), block_size=block_size, completed_games=len(games),
                    definitions={
                        'new_states': '依全域局次及手數首次遇到的落子前完整 D4 標準局面；合併兩模型的觀測，不含終局盤。',
                        'new_state_pct': '區間內首次觀測的標準局面數／區間總落子數 × 100；較早區間及本區間較早棋步都納入已見集合。',
                        'new_boxes': '原始 new_box 標記加總；各模型個別的新盒子，同一局面可在兩模型各建立一次。',
                        'new_raw_games': '依原始方向及完整落子順序，首次出現的正常終局棋譜。',
                        'new_d4_games': '完整落子序列固定使用同一全局 D4 變換後首次出現；不交換 X/O。',
                        'scope': '僅完成訓練局，排除 eval、demo 及未完局；這是同一模型組的相依歷程，不是獨立重複或收斂檢定。'},
                    blocks=list(blocks.values()), depths=list(depths.values()), per_game=per_game,
                    totals=dict(moves=sum(b['moves'] for b in blocks.values()), unique_decision_states=len(seen_states),
                                unique_raw_games=len(seen_raw), unique_d4_games=len(seen_d4)))
    finally:
        connection.close()


def write_csv(path, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as output:
        if rows:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def publish(database, directory, block_size=1000, baseline_games=6000):
    result = collect(database, block_size)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    per_game = result.pop('per_game')
    write_csv(directory / 'novelty-per-game.csv', per_game)
    write_csv(directory / 'novelty-by-depth.csv', result['depths'])
    write_csv(directory / 'novelty-by-block.csv', [{k: v for k, v in b.items() if k != 'models'} for b in result['blocks']])
    full = [b for b in result['blocks'] if b['games'] == block_size]
    reference = next((b for b in full if b['last_game'] == baseline_games), None)
    latest = full[-1] if full else None
    comparison = None
    if reference and latest and latest['last_game'] > baseline_games:
        comparison = {k + '_change_pp': latest[k] - reference[k]
                      for k in ('new_state_pct', 'new_box_pct', 'new_d4_game_pct')}
        comparison.update(reference=[reference['first_game'], reference['last_game']],
                          latest=[latest['first_game'], latest['last_game']])
    result['comparison_to_last_baseline_block'] = comparison
    save_json(directory / 'novelty-summary.json', result)
    lines = ['# 新棋譜與新局面統計', '', f"統計至完成訓練 {result['completed_games']:,} 局；更新時間：{result['created_at']}。",
             '由程式協助計算；每列以此前所有訓練棋步為已見基準，完整棋譜與局面分開計數。', '',
             '| 局次 | 落子數 | 新標準局面 | 新局面比例 | 新盒子比例 | 新完整棋譜（D4）比例 | 平均手數 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for b in result['blocks']:
        lines.append(f"| {b['first_game']:,}–{b['last_game']:,} | {b['moves']:,} | {b['new_states']:,} | {b['new_state_pct']:.3f}% | {b['new_box_pct']:.3f}% | {b['new_d4_game_pct']:.3f}% | {b['mean_moves']:.2f} |")
    lines += ['', '## 如何解讀', '', '新局面比例的分母是落子數；只計落子前的完整標準局面，不含終局盤。新盒子按模型各自計數。完整棋譜比較原始落子順序，另列固定全局 D4 合併後的新穎率。',
              '按手數區段的明細另存 novelty-by-depth.csv，可檢查下降是否只發生在開局。平均手數一併列出，避免把局長變化誤認為新穎率改變。']
    if comparison:
        delta = comparison['new_state_pct_change_pp']
        lines += ['', f"最新完整區間相較續訓前最後完整區間，新局面比例{'下降' if delta < 0 else '上升' if delta > 0 else '不變'} {abs(delta):.3f} 個百分點。這是區間差異，不表示每個區間都單調變化。"]
    else:
        lines += ['', '追加訓練尚未產生可比較的完整統計區間；目前不判定追加後是否下降。']
    lines += ['', '各批屬同一組模型的連續學習，彼此相依。新穎率下降只表示重訪增加，不能直接證明棋力提高、過度訓練或收斂。', '',
              '本報告與 CSV／JSON 是實驗支援資料；未自動修改科展主稿或 Notion。', '']
    (directory / '新局面統計.md').write_text('\n'.join(lines), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--block-size', type=int, default=1000)
    parser.add_argument('--baseline-games', type=int, default=6000)
    args = parser.parse_args()
    result = publish(args.database, args.directory, args.block_size, args.baseline_games)
    print(json.dumps({k: result[k] for k in ('completed_games', 'totals', 'comparison_to_last_baseline_block')}, ensure_ascii=False, indent=2))
