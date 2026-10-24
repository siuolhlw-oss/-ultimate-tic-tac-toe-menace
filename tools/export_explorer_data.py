"""唯讀匯出已完成的自我對弈實驗，並完整重播核對棋譜。

輸出僅含原始棋譜與樣本計數；不載入模型權重，不將樣本數當作全遊戲總數。
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from uttt.engine import RULE_VERSION, State, legal_moves, play, state_to_dict
from uttt.symmetry import action_groups, state_key, transform_move


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def javascript_data(value):
    """安全的外部 JS 資料賦值，也適用於 inline script 的特殊字元。"""
    encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
    for char, escaped in (('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026')):
        encoded = encoded.replace(char, escaped)
    return 'window.UTTT_DATA = ' + encoded + ';\n'


def canonical_history(sequence):
    """同一個 D4 變換套用整條歷史；不可逐步各自旋轉。"""
    return min(tuple(transform_move(move, t) for move in sequence) for t in range(8))


def _oracle_row(index, state):
    return {'game': index, 'ply': state.ply, 'board': state.board,
            'turn': state.turn, 'forced': state.forced, 'status': state.status,
            'localStatus': list(state.local_status), 'legal': list(legal_moves(state)),
            'stateKey': state_key(state)}


def read_experiment(database, expected_games=1000, oracle=None):
    """開啟唯讀交易；若提供 oracle，寫入逐狀態 JSONL 規則核對資料。"""
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        try:
            result = _read_snapshot(conn, database, expected_games, oracle)
        finally:
            conn.rollback()
    return result


def _read_snapshot(conn, database, expected_games, oracle):
    experiment = conn.execute('SELECT * FROM pilot_experiments WHERE id=1').fetchone()
    if not experiment or experiment['status'] != 'completed':
        raise ValueError('指定資料庫沒有已完成的自我對弈實驗。')
    if expected_games is not None and experiment['total_games'] != expected_games:
        raise ValueError('實驗總局數與指定的匯出局數不同。')
    models = []
    for role in ('model_a', 'model_b'):
        model = conn.execute('SELECT model_id,name,seed FROM models WHERE model_id=?',
                             (experiment[role],)).fetchone()
        if model is None:
            raise ValueError('實驗模型識別資料缺失。')
        models.append({'id': model['model_id'], 'name': model['name'], 'seed': model['seed']})
    model_names = {m['id']: m['name'] for m in models}
    rows = conn.execute("""SELECT g.* FROM games g
        JOIN pilot_batches b ON b.run_id=g.run_id
        WHERE b.status='completed' AND g.mode='train'
          AND g.status='finished' AND g.is_example=0
        ORDER BY g.x_version, g.game_id""").fetchall()
    if len(rows) != experiment['total_games']:
        raise ValueError('已完成訓練棋譜數量與實驗設定不同。')
    games, states, prefixes, full_games = [], {state_key(State())}, {()}, set()
    histogram = Counter()
    move_maps = [{10 * b + s: transform_move(10 * b + s, t)
                  for b in range(1, 10) for s in range(1, 10)} for t in range(8)]
    for index, game in enumerate(rows, 1):
        expected_players = ((experiment['model_a'], experiment['model_b']) if index % 2
                            else (experiment['model_b'], experiment['model_a']))
        if (game['x_version'] != index - 1 or game['o_version'] != index - 1
                or (game['x_player'], game['o_player']) != expected_players
                or game['rule_version'] != RULE_VERSION or not game['learning_applied']):
            raise ValueError(f'第 {index} 局的版本、角色、規則或學習標記不一致。')
        records = conn.execute("""SELECT ply,actor,model_id,move_code,forced_before,
            forced_after,state_key,next_state_key FROM moves WHERE game_id=? ORDER BY ply""",
            (game['game_id'],)).fetchall()
        state = State()
        transformed = [() for _ in range(8)]
        sequence = []
        if oracle:
            oracle.write(json_text(_oracle_row(index, state)) + '\n')
        for ply, record in enumerate(records, 1):
            expected_actor = 'X' if ply % 2 else 'O'
            if (record['ply'] != ply or record['actor'] != expected_actor
                    or record['model_id'] != game['x_player' if ply % 2 else 'o_player']
                    or record['forced_before'] != state.forced
                    or record['state_key'] != state_key(state)):
                raise ValueError(f'第 {index} 局第 {ply} 手的原始角色、局面或索引不一致。')
            move = record['move_code']
            state = play(state, move)
            key = state_key(state)
            if record['next_state_key'] != key or record['forced_after'] != state.forced:
                raise ValueError(f'第 {index} 局第 {ply} 手的後繼局面索引不一致。')
            sequence.append(move)
            states.add(key)
            transformed = [history + (move_maps[t][move],)
                           for t, history in enumerate(transformed)]
            prefixes.add(min(transformed))
            if oracle:
                oracle.write(json_text(_oracle_row(index, state)) + '\n')
        if (state.status == 'active' or state.status != game['result']
                or len(sequence) != game['ply']
                or state_to_dict(state) != json.loads(game['state_json'])):
            raise ValueError(f'第 {index} 局的完整重播與終局紀錄不符。')
        full_games.add(min(transformed))
        histogram[len(sequence)] += 1
        games.append({'index': index, 'id': game['game_id'], 'moves': sequence,
                      'ply': len(sequence), 'result': state.status,
                      'xModel': model_names[game['x_player']],
                      'oModel': model_names[game['o_player']]})
    try:
        source_path = database.relative_to(ROOT).as_posix()
    except ValueError:
        source_path = database.name
    summary = {'games': len(games), 'moves': sum(g['ply'] for g in games),
               'minPly': min(histogram), 'maxPly': max(histogram),
               'lengthHistogram': [{'ply': ply, 'games': count}
                                   for ply, count in sorted(histogram.items())],
               'distinctLengths': len(histogram), 'distinctGamesD4': len(full_games),
               'observedStatesD4': len(states),
               'observedPrefixesD4IncludingEmpty': len(prefixes),
               'maxDepthUpperBound': 81, 'firstMoveRaw': len(legal_moves(State())),
               'firstMoveD4': len(action_groups(State())), 'fullGameTotal': None}
    return {'schemaVersion': 1, 'ruleVersion': RULE_VERSION,
            'generatedAt': datetime.now(timezone.utc).isoformat(),
            'source': {'db': source_path, 'models': models,
                       'experimentSeed': experiment['seed'],
                       'completedAt': experiment['completed_at'],
                       'recordsSha256': hashlib.sha256(json_text(games).encode('utf-8')).hexdigest(),
                       'selection': 'completed pilot_batches; finished train games; non-example'},
            'games': games, 'summary': summary}


def export_experiment(database, output, expected_games=1000, include_oracle=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    oracle_tmp = None
    try:
        if include_oracle:
            validation = output / 'validation'
            validation.mkdir(exist_ok=True)
            oracle_tmp = validation / 'rules-oracle.jsonl.partial'
            with oracle_tmp.open('w', encoding='utf-8', newline='\n') as oracle:
                payload = read_experiment(database, expected_games, oracle)
        else:
            payload = read_experiment(database, expected_games)
        for name, content in (('data.json', json_text(payload) + '\n'),
                              ('data.js', javascript_data(payload)),
                              ('summary.json', json.dumps(payload['summary'], ensure_ascii=False, indent=2) + '\n')):
            destination = output / name
            temporary = output / (name + '.partial')
            temporary.write_text(content, encoding='utf-8', newline='\n')
            temporary.replace(destination)
        if oracle_tmp:
            oracle_tmp.replace(oracle_tmp.with_suffix(''))
        return payload
    finally:
        if oracle_tmp and oracle_tmp.exists():
            oracle_tmp.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/selfplay-1000/training.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT / 'exports/game-atlas')
    parser.add_argument('--expected-games', type=int, default=1000)
    parser.add_argument('--oracle', action='store_true', help='另匯出每手規則比對 JSONL')
    args = parser.parse_args()
    payload = export_experiment(args.db, args.output, args.expected_games, args.oracle)
    print(json.dumps(payload['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
