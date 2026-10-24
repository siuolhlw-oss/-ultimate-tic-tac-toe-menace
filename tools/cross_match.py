"""由兩份 v3 的完整雙模型檢查點建立凍結交叉賽；不寫入來源。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import zlib

from storage_v3.repository import CompactResearchService
from tools.continue_selfplay import StorageBudget, RESERVE_GAME, file_hash, save_json, validate_runtime
from uttt.engine import replay, state_to_dict
from uttt.pilot import game_seed, runner_lock
from uttt.service import dumps, now, runtime_info, RULE_VERSION, ALGORITHM_VERSION

PAIRS = (('C', 'A'), ('C', 'B'), ('D', 'A'), ('D', 'B'))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS match_config(id INTEGER PRIMARY KEY CHECK(id=1),config_json TEXT NOT NULL,
 sources_json TEXT NOT NULL,frozen_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match_sources(label TEXT PRIMARY KEY,original_payload_zlib BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS match_pairs(pair_index INTEGER PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id));
'''


def code_hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: file_hash(root / name) for name in
            ('tools/cross_match.py', 'tools/run_nine_thousand.py', 'storage_v3/repository.py')}


def read_pair(path, expected_version=None):
    """短暫讀取快照：只取完整批次的兩份壓縮檢查點，離開交易後才解碼。"""
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        if conn.execute('PRAGMA user_version').fetchone()[0] != 3:
            raise ValueError('比賽來源須為 v3。')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='extension_experiments'").fetchone()
        prefix = 'extension' if exists else 'continuation'
        experiment = dict(conn.execute('SELECT * FROM ' + prefix + '_experiments WHERE id=1').fetchone())
        batch = conn.execute('SELECT * FROM ' + prefix + "_batches WHERE status='completed' ORDER BY batch_index DESC LIMIT 1").fetchone()
        ids = ((batch['checkpoint_a'], batch['checkpoint_b']) if batch else
               (experiment['initial_checkpoint_a'], experiment['initial_checkpoint_b']))
        rows = [dict(conn.execute('SELECT * FROM _checkpoints WHERE checkpoint_id=?', (cp,)).fetchone()) for cp in ids]
        conn.rollback()
    versions = {r['version'] for r in rows}
    if len(versions) != 1 or (expected_version is not None and versions != {expected_version}):
        raise ValueError('雙模型檢查點局數不一致或未達指定目標。')
    if [r['model_id'] for r in rows] != [experiment['model_a'], experiment['model_b']]:
        raise ValueError('雙模型檢查點來源不符。')
    return rows


def frozen_digest(service):
    digest = hashlib.sha256()
    # 同時涵蓋評估不得新增／修改的訓練表；檢查點以實際壓縮內容計算。
    for table in ('_models', '_checkpoints', '_model_states', '_weights', 'model_events', '_learning_updates', 'model_locks', 'match_sources'):
        digest.update(table.encode())
        for row in service.conn.execute('SELECT * FROM ' + table + ' ORDER BY 1,2'):
            for value in row:
                raw = value if isinstance(value, bytes) else dumps(value).encode()
                digest.update(len(raw).to_bytes(8, 'big'))
                digest.update(raw)
    return digest.hexdigest()


def import_frozen(service, label, row, path):
    raw = zlib.decompress(row['payload_zlib']).decode('utf-8')
    if hashlib.sha256(raw.encode()).hexdigest() != row['sha256']:
        raise ValueError('來源檢查點校驗失敗。')
    payload = json.loads(raw)
    validate_runtime(payload['runtime'])
    model = payload['model']
    if (payload['format_version'] != 1 or model['model_id'] != row['model_id']
            or model['version'] != row['version'] or model['trained_games'] != row['version']
            or model['rule_version'] != RULE_VERSION or model['algorithm_version'] != ALGORITHM_VERSION):
        raise ValueError('來源檢查點模型或規則不符。')
    source = dict(label=label, database=str(Path(path).resolve()), checkpoint_id=row['checkpoint_id'],
                  checkpoint_sha256=row['sha256'], source_model_id=row['model_id'],
                  trained_games=model['trained_games'], revision=model['revision'], captured_at=now())
    # A/C 與 B/D 的來源 ID 相同；在比賽庫中加上組別命名空間，原文另存以供追溯。
    model['model_id'] = 'evaluation_' + label
    model['name'] = 'AI-' + label
    model['parent_checkpoint_id'] = row['checkpoint_id']
    fields = tuple(model)
    service.conn.execute('INSERT INTO models (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                         tuple(model[k] for k in fields))
    text = dumps(payload)
    cp = 'frozen_' + label
    service.conn.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?)',
                         (cp, model['model_id'], model['version'], now(), text, hashlib.sha256(text.encode()).hexdigest()))
    service.conn.execute('INSERT INTO match_sources VALUES(?,?)', (label, row['payload_zlib']))
    source.update(evaluation_model_id=model['model_id'], evaluation_checkpoint_id=cp)
    return source


def configuration(new_source, old_source, games_per_pair, seed, expected_new_version):
    if type(games_per_pair) is not int or games_per_pair < 2 or games_per_pair % 2:
        raise ValueError('每組比賽局數須為正偶數。')
    if Path(new_source).resolve() == Path(old_source).resolve():
        raise ValueError('新舊組須來自不同資料庫。')
    return dict(new_source=str(Path(new_source).resolve()), old_source=str(Path(old_source).resolve()),
                games_per_pair=games_per_pair, seed=int(seed), expected_new_version=expected_new_version,
                pairs=PAIRS, epsilon=0, code_hashes=code_hashes(), runtime=runtime_info())


def prepare_match(directory, new_source, old_source, games_per_pair=200, seed=20261025,
                  expected_new_version=9000, guard=None):
    directory = Path(directory).resolve()
    db = directory / 'evaluation.sqlite3'
    if directory in (Path(new_source).resolve().parent, Path(old_source).resolve().parent):
        raise ValueError('比賽須使用獨立資料夾。')
    directory.mkdir(parents=True, exist_ok=True)
    guard = guard or StorageBudget(directory)
    config = configuration(new_source, old_source, games_per_pair, seed, expected_new_version)
    with CompactResearchService(db) as service:
        service.conn.executescript(SCHEMA)
        stored = service.repo.one('SELECT * FROM match_config WHERE id=1')
        if stored:
            if stored['config_json'] != dumps(config) or frozen_digest(service) != stored['frozen_digest']:
                raise ValueError('比賽設定、程式或凍結模型已改變。')
            return db
        if service.conn.execute('SELECT COUNT(*) FROM models').fetchone()[0]:
            raise ValueError('比賽庫已有未確認的模型。')
        sources = []
        with service.repo.transaction():
            for labels, path, version in ((('C', 'D'), new_source, expected_new_version),
                                          (('A', 'B'), old_source, None)):
                for label, row in zip(labels, read_pair(path, version)):
                    guard.check(max(RESERVE_GAME, 4 * len(row['payload_zlib'])))
                    sources.append(import_frozen(service, label, row, path))
            service.conn.execute('INSERT INTO match_config VALUES(1,?,?,?)',
                                 (dumps(config), dumps(sources), frozen_digest(service)))
    return db


def wilson(wins, total):
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0.0, center-half), min(1.0, center+half)]


def validate_games(service, run_id, pair_index, config, allow_active):
    left, right = PAIRS[pair_index]
    rows = service.repo.all('SELECT * FROM games WHERE run_id=? ORDER BY created_at,game_id', (run_id,))
    finished = 0
    for index, game in enumerate(rows):
        labels = (left, right) if index % 2 == 0 else (right, left)
        if (index >= config['games_per_pair'] or game['mode'] != 'eval' or game['is_example']
                or game['learning_applied'] or game['seed'] != str(game_seed(config['seed'], pair_index*config['games_per_pair']+index))
                or (game['x_player'], game['o_player']) != tuple('evaluation_' + x for x in labels)
                or (game['x_checkpoint_id'], game['o_checkpoint_id']) != tuple('frozen_' + x for x in labels)):
            raise ValueError('比賽棋譜與固定設定不符。')
        if game['status'] == 'finished':
            finished += 1
        elif not (allow_active and game['status'] == 'active' and index == len(rows)-1):
            raise ValueError('比賽含中止或不連續的局次。')
    if service.repo.one('SELECT completed_games FROM runs WHERE run_id=?', (run_id,))['completed_games'] != finished:
        raise ValueError('比賽完成計數不符。')
    return rows, finished


def run_match(db, guard=None, progress=None, event=None):
    db = Path(db)
    guard = guard or StorageBudget(db.parent)
    with runner_lock(db.parent / 'match.lock'), CompactResearchService(db) as service:
        item = service.repo.one('SELECT * FROM match_config WHERE id=1')
        config = json.loads(item['config_json'])
        if config['code_hashes'] != code_hashes() or config['runtime'] != runtime_info():
            raise ValueError('比賽程式或環境改變。')
        if frozen_digest(service) != item['frozen_digest']:
            raise ValueError('凍結模型已改變。')
        for pair_index, labels in enumerate(PAIRS):
            pair = service.repo.one('SELECT * FROM match_pairs WHERE pair_index=?', (pair_index,))
            if not pair:
                with service.repo.transaction():
                    run_id = service._new_run('eval', 'frozen_cross_match', config['seed'],
                        {'frozen_checkpoints': {'evaluation_' + x: 'frozen_' + x for x in labels},
                         'requested_games': config['games_per_pair'], 'epsilon': 0, 'pair': labels})
                    service.conn.execute('INSERT INTO match_pairs VALUES(?,?)', (pair_index, run_id))
                pair = dict(run_id=run_id)
            run_id = pair['run_id']
            rows, finished = validate_games(service, run_id, pair_index, config, True)
            for index in range(finished, config['games_per_pair']):
                guard.check(RESERVE_GAME)
                players = labels if index % 2 == 0 else labels[::-1]
                game = rows[-1] if index == finished and rows and rows[-1]['status'] == 'active' else None
                if game is None:
                    gid = service.new_game(*('evaluation_' + x for x in players), mode='eval', run_id=run_id,
                                           seed=game_seed(config['seed'], pair_index*config['games_per_pair']+index))
                    game = service.get_game(gid)
                while game['status'] == 'active':
                    game = service.step(game['game_id'])
                    if event:
                        event('after_step', game)
                if progress:
                    progress(pair_index*config['games_per_pair']+index+1, 4*config['games_per_pair'])
            with service.repo.transaction():
                service.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
        report = summarize(service)
    save_json(db.parent / 'results.json', report)
    lines = ['# 四模型凍結交叉比賽', '', '各組勝率＝新組獲勝局數／全部局數，和局保留在分母。', '',
             '| 對戰 | 新組訓練局數 | 原組訓練局數 | 勝 | 和 | 負 | 勝率 | 勝率 95% 近似區間 |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    for p in report['pairs']:
        lo, hi = p['win_rate_wilson_95']
        lines.append(f"| {p['pair']} | {p['new_trained_games']} | {p['old_trained_games']} | {p['wins']} | {p['draws']} | {p['losses']} | {p['win_rate']:.1%} | {lo:.1%}–{hi:.1%} |")
    lines += ['', report['interpretation']]
    (db.parent / '比賽結果.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report


def summarize(service):
    item = service.repo.one('SELECT * FROM match_config WHERE id=1')
    config = json.loads(item['config_json'])
    sources = {s['label']: s for s in json.loads(item['sources_json'])}
    results = []
    for pair_index, (left, right) in enumerate(PAIRS):
        run = service.repo.one('SELECT run_id FROM match_pairs WHERE pair_index=?', (pair_index,))
        rows, finished = validate_games(service, run['run_id'], pair_index, config, False)
        if finished != config['games_per_pair']:
            raise ValueError('交叉比賽尚未完成。')
        wins = draws = 0
        first = {'X': 0, 'O': 0}
        for game in rows:
            moves = service.moves(game['game_id'])
            if [m['ply'] for m in moves] != list(range(1, len(moves)+1)):
                raise ValueError('比賽棋譜手數不連續。')
            state = replay([m['move_code'] for m in moves])[-1]
            if state.status != game['result'] or state_to_dict(state) != json.loads(game['state_json']):
                raise ValueError('比賽棋譜重播不符。')
            if any(m['new_box'] or m['branch'] == 'explore' for m in moves):
                raise ValueError('凍結比賽不可探索或建立訓練盒子。')
            actor = 'X' if game['x_player'] == 'evaluation_' + left else 'O'
            first[actor] += 1
            wins += game['result'] == actor
            draws += game['result'] == 'draw'
        if first != {'X': finished//2, 'O': finished//2}:
            raise ValueError('先後手不平衡。')
        results.append(dict(pair=left+'-'+right, games=finished, wins=wins, draws=draws, losses=finished-wins-draws,
                            first_player_counts=first, win_rate=wins/finished, win_rate_wilson_95=wilson(wins, finished),
                            score_rate=(wins+draws/2)/finished, new_trained_games=sources[left]['trained_games'],
                            old_trained_games=sources[right]['trained_games']))
    if (frozen_digest(service) != item['frozen_digest']
            or service.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
            or service.conn.execute('PRAGMA foreign_key_check').fetchone()):
        raise ValueError('比賽凍結或資料完整性核對失敗。')
    return dict(created_at=now(), pairs=results, sources=sources, configuration=config,
                frozen_models_unchanged=True, games_replayed=sum(p['games'] for p in results),
                interpretation='這是同一來源的續訓分支與固定檢查點之比較，沒有獨立訓練重複。'
                'Wilson 區間只描述固定模型抽樣對弈的近似不確定性；四組未作多重比較校正。'
                '不能只因勝率較高就宣稱顯著差異、收斂或新版演算法提升棋力；請同時看和局、敗局及先後手。')
