"""唯讀核對並匯出累計6,000局公開展示資料；不匯出模型或本機設定。"""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage_v3.repository import CompactRepository
from tools.export_explorer_data import javascript_data, json_text
from uttt.engine import State, play, state_to_dict
from uttt.symmetry import state_key, transform_move

def main():
    source = ROOT / 'data/storage-v3/training-6000.sqlite3'
    status = json.loads((source.parent / 'finalization-status.json').read_text(encoding='utf-8'))
    assert status['phase'] == 'ready'
    out = ROOT / 'sites/student-atlas/public/explore'
    audit = ROOT / 'data/student-site'
    out.mkdir(parents=True, exist_ok=True)
    audit.mkdir(parents=True, exist_ok=True)
    repo = CompactRepository(source, read_only=True)
    conn = repo.conn
    conn.execute('BEGIN')
    try:
        pilot = conn.execute('select * from pilot_experiments where id=1').fetchone()
        model_ids = (pilot['model_a'], pilot['model_b'])
        rows = conn.execute("select * from games where mode='train' and status='finished' and is_example=0 order by x_version,game_id").fetchall()
        assert len(rows) == 6000
        games, states, prefixes, complete = [], {state_key(State())}, {()}, set()
        histogram = Counter()
        maps = [{b*10+s: transform_move(b*10+s, t) for b in range(1,10) for s in range(1,10)} for t in range(8)]
        for index, game in enumerate(rows, 1):
            expected = model_ids if index % 2 else model_ids[::-1]
            assert (game['x_player'], game['o_player']) == expected
            assert game['x_version'] == game['o_version'] == index-1
            assert game['learning_applied'] == 1 and game['rule_version'] == 'uttt-closed-v1'
            records = conn.execute('select ply,actor,move_code,state_key,next_state_key,forced_before,forced_after from moves where game_id=? order by ply', (game['game_id'],)).fetchall()
            state, sequence, transforms = State(), [], [() for _ in range(8)]
            for ply, record in enumerate(records, 1):
                assert record['ply'] == ply and record['actor'] == state.turn
                assert record['state_key'] == state_key(state) and record['forced_before'] == state.forced
                move = record['move_code']
                state = play(state, move)
                key = state_key(state)
                assert record['next_state_key'] == key and record['forced_after'] == state.forced
                sequence.append(move)
                states.add(key)
                transforms = [history+(maps[t][move],) for t, history in enumerate(transforms)]
                prefixes.add(min(transforms))
            assert state.status != 'active' and state.status == game['result']
            assert len(sequence) == game['ply'] and state_to_dict(state) == json.loads(game['state_json'])
            complete.add(min(transforms))
            histogram[state.ply] += 1
            games.append({'index':index,'moves':sequence,'ply':state.ply,'result':state.status,
                          'xModel':'AI-A' if expected[0] == model_ids[0] else 'AI-B',
                          'oModel':'AI-A' if expected[1] == model_ids[0] else 'AI-B'})
            if index % 1000 == 0:
                print(f'合法重播核對：{index}/6000', flush=True)
        summary = {'games':6000,'moves':sum(g['ply'] for g in games),'minPly':min(histogram),'maxPly':max(histogram),
                   'lengthHistogram':[{'ply':p,'games':n} for p,n in sorted(histogram.items())],
                   'distinctLengths':len(histogram),'distinctGamesD4':len(complete),'observedStatesD4':len(states),
                   'observedPrefixesD4IncludingEmpty':len(prefixes),'maxDepthUpperBound':81,
                   'firstMoveRaw':81,'firstMoveD4':15,'fullGameTotal':None}
        assert summary['moves'] == 353495
        fingerprint = sha256(json_text(games).encode()).hexdigest()
        payload = {'schemaVersion':1,'ruleVersion':'uttt-closed-v1','datasetId':'selfplay-6000-v1',
                   'generatedAt':datetime.now(timezone.utc).isoformat(),
                   'source':{'recordsSha256':fingerprint,'selection':'累計6,000局已完成自我對弈；不含PK、人工示範或測試'},
                   'games':games,'summary':summary}
        for name, text in [('data.js',javascript_data(payload)),('data.json',json_text(payload)),('summary.json',json.dumps(summary,ensure_ascii=False,indent=2))]:
            (out/name).write_text(text,encoding='utf-8')
        (audit/'export.validation.json').write_text(json.dumps({'created_at':payload['generatedAt'],'source_database':str(source),
            'read_only':True,'summary':summary,'records_sha256':fingerprint,'source_runtime':json.loads(pilot['runtime_json']),
            'public_fields':'僅原始手序、局序、結果、AI-A/B標示及樣本計數；不含本機路徑、模型ID、亂數或憑證'},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    finally:
        conn.rollback()
        repo.close()

if __name__ == '__main__':
    main()
