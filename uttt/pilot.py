"""固定局數的自我對弈；保留完整紀錄，可中斷續跑，不判定策略收斂。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

from .engine import replay, state_to_dict
from .service import dumps, now, runtime_info


PILOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_experiments (
 id INTEGER PRIMARY KEY CHECK(id=1), format_version INTEGER NOT NULL,
 total_games INTEGER NOT NULL, batch_size INTEGER NOT NULL, seed TEXT NOT NULL,
 model_a TEXT NOT NULL REFERENCES models(model_id),
 model_b TEXT NOT NULL REFERENCES models(model_id),
 runtime_json TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS pilot_batches (
 batch_index INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 start_index INTEGER NOT NULL, requested_games INTEGER NOT NULL,
 status TEXT NOT NULL, checkpoint_a TEXT REFERENCES checkpoints(checkpoint_id),
 checkpoint_b TEXT REFERENCES checkpoints(checkpoint_id), completed_at TEXT
);
"""


@contextmanager
def runner_lock(path):
    """作業系統鎖：程序結束即釋放，留下的鎖檔不妨礙續跑。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('此訓練已有另一個程序執行，請勿重複啟動。') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def game_seed(seed, index):
    """第 index 局（從 0 起）的穩定種子；模型自身 RNG 不重設。"""
    raw = hashlib.sha256(f'uttt-pilot-v1:{seed}:{index}'.encode('ascii')).digest()
    return int.from_bytes(raw[:8], 'big') & ((1 << 63) - 1)


def initialize(service, total_games=1000, batch_size=100, seed=20261013):
    if type(total_games) is not int or total_games < 2 or total_games % 2:
        raise ValueError('總局數須為正偶數，以平衡先後手。')
    if type(batch_size) is not int or batch_size < 1 or type(seed) is not int:
        raise ValueError('批次大小須為正整數，種子須為整數。')
    service.conn.executescript(PILOT_SCHEMA)
    experiment = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    if experiment:
        actual = (experiment['total_games'], experiment['batch_size'], experiment['seed'])
        if actual != (total_games, batch_size, str(seed)):
            raise ValueError('續跑須使用原本的局數、批次大小與種子；新實驗請另選資料夾。')
        original = json.loads(experiment['runtime_json'])
        current = runtime_info()
        for key in ('source_sha256', 'python', 'schema_version', 'rule_version', 'algorithm_version'):
            if original[key] != current[key]:
                raise ValueError(f'程式或環境已改變（{key}），不能悄悄接續同一訓練。')
        return experiment
    if any(service.repo.one('SELECT 1 FROM ' + name + ' LIMIT 1') for name in ('models', 'runs', 'games')):
        raise ValueError('新實驗需要空白的專用資料庫，不可混用既有對局。')
    with service.repo.transaction():
        a = service._create_model('自我對弈 AI-A', seed)
        b = service._create_model('自我對弈 AI-B', seed + 1)
        service.conn.execute('INSERT INTO pilot_experiments VALUES(1,1,?,?,?,?,?,?,?, ?,NULL)',
                             (total_games, batch_size, str(seed), a, b, dumps(runtime_info()), 'active', now()))
    return service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')


def check_consistency(service, experiment):
    """拒絕遺失、放棄、重複學習或混入其他訓練；不修改資料。"""
    a, b = experiment['model_a'], experiment['model_b']
    batches = service.repo.all('SELECT * FROM pilot_batches ORDER BY batch_index')
    completed = 0
    active_games = []
    for i, batch in enumerate(batches):
        if batch['batch_index'] != i or batch['start_index'] != i * experiment['batch_size']:
            raise ValueError('批次索引不連續。')
        run = service.repo.one('SELECT * FROM runs WHERE run_id=?', (batch['run_id'],))
        games = service.repo.all('SELECT * FROM games WHERE run_id=? ORDER BY x_version', (batch['run_id'],))
        finished = [g for g in games if g['status'] == 'finished']
        active = [g for g in games if g['status'] == 'active']
        if len(finished) != run['completed_games'] or len(active) > 1 or len(games) != len(finished) + len(active):
            raise ValueError('完成局數、未完對局或中止紀錄不一致。')
        if len(games) > batch['requested_games']:
            raise ValueError('批次包含超額對局。')
        for j, game in enumerate(games):
            index = batch['start_index'] + j
            players = (a, b) if index % 2 == 0 else (b, a)
            if ((game['x_player'], game['o_player']) != players or game['x_version'] != index
                    or game['o_version'] != index or game['mode'] != 'train'
                    or game['seed'] != str(game_seed(int(experiment['seed']), index))
                    or bool(game['learning_applied']) != (game['status'] == 'finished')):
                raise ValueError('棋譜的局次、行動方、種子或學習標記不一致。')
        if active and games[-1]['status'] != 'active':
            raise ValueError('未完成局後不應有新局。')
        if batch['status'] == 'completed':
            if len(finished) != batch['requested_games'] or run['status'] != 'completed':
                raise ValueError('已完成批次的局數不符。')
            for model, key in ((a, 'checkpoint_a'), (b, 'checkpoint_b')):
                cp = service.repo.one('SELECT model_id,version FROM checkpoints WHERE checkpoint_id=?', (batch[key],))
                if not cp or cp['model_id'] != model or cp['version'] != batch['start_index'] + len(finished):
                    raise ValueError('批次檢查點遺失或版本不符。')
        elif i != len(batches) - 1:
            raise ValueError('尚未完成的批次後不應已有下一批。')
        completed += len(finished)
        active_games.extend(active)
    if len(active_games) > 1:
        raise ValueError('有多個未完成訓練對局。')
    if service.repo.one("SELECT 1 FROM games WHERE mode='train' AND run_id NOT IN (SELECT run_id FROM pilot_batches)"):
        raise ValueError('專用資料庫混入其他訓練。')
    for model_id in (a, b):
        model = service._model(model_id)
        if model['version'] != completed or model['trained_games'] != completed:
            raise ValueError('模型學習次數與已完成棋譜數不一致。')
        lock = service.repo.one('SELECT game_id FROM model_locks WHERE model_id=?', (model_id,))
        if (lock['game_id'] if lock else None) != (active_games[0]['game_id'] if active_games else None):
            raise ValueError('模型占用狀態與未完成局不一致。')
    if completed > experiment['total_games']:
        raise ValueError('完成局數超過設定。')
    return completed


def train_pilot(service, total_games=1000, batch_size=100, seed=20261013, progress=None, event=None):
    """呼叫端必須以 runner_lock 包住整次工作。event 僅供驗證中斷點。"""
    experiment = initialize(service, total_games, batch_size, seed)
    done = check_consistency(service, experiment)
    a, b = experiment['model_a'], experiment['model_b']
    if progress:
        progress(done, total_games)
    for batch_index, start in enumerate(range(0, total_games, batch_size)):
        requested = min(batch_size, total_games - start)
        batch = service.repo.one('SELECT * FROM pilot_batches WHERE batch_index=?', (batch_index,))
        if batch and batch['status'] == 'completed':
            continue
        if batch is None:
            with service.repo.transaction():
                run_id = service._new_run('train', 'self_play_pilot', game_seed(seed, start),
                    {'model_a': a, 'model_b': b, 'requested_games': requested,
                     'starting_versions': [start, start], 'pilot_batch_index': batch_index,
                     'pilot_start_index': start, 'game_seed_method': 'sha256(uttt-pilot-v1:seed:index) first 63 bits'})
                service.conn.execute('INSERT INTO pilot_batches VALUES(?,?,?,?,?,NULL,NULL,NULL)',
                                     (batch_index, run_id, start, requested, 'active'))
            batch = service.repo.one('SELECT * FROM pilot_batches WHERE batch_index=?', (batch_index,))
        run_id = batch['run_id']
        while True:
            count = service.repo.one('SELECT completed_games FROM runs WHERE run_id=?', (run_id,))['completed_games']
            if count == requested:
                break
            index = start + count
            game = service.repo.one("SELECT * FROM games WHERE run_id=? AND status='active'", (run_id,))
            if game is None:
                players = (a, b) if index % 2 == 0 else (b, a)
                game_id = service.new_game(*players, mode='train', run_id=run_id, seed=game_seed(seed, index))
                game = service.get_game(game_id)
            while game['status'] == 'active':
                game = service.step(game['game_id'])
                if event:
                    event('after_step', game)
            if event:
                event('after_game', game)
            if progress:
                progress(index + 1, total_games)
        if event:
            event('before_checkpoint', {'batch_index': batch_index})
        with service.repo.transaction():
            cp_a, cp_b = service._save_checkpoint(a), service._save_checkpoint(b)
            service.conn.execute("UPDATE pilot_batches SET status='completed',checkpoint_a=?,checkpoint_b=?,completed_at=? WHERE batch_index=?",
                                 (cp_a, cp_b, now(), batch_index))
            service.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
            if start + requested == total_games:
                service.conn.execute("UPDATE pilot_experiments SET status='completed',completed_at=? WHERE id=1", (now(),))
    assert check_consistency(service, experiment) == total_games
    return {'total_games': total_games, 'batch_size': batch_size, 'model_a': a, 'model_b': b,
            'model_seeds': [seed, seed + 1], 'checkpoint_versions': [0] + list(range(batch_size, total_games, batch_size)) + [total_games],
            'purpose': '固定局數訓練；未分析策略相似、收斂或勝率，未執行真人PK'}


def verify_records(service):
    """只核對工程完整性，不分析棋力；逐局重播而不整表載入。"""
    experiment = service.repo.one('SELECT * FROM pilot_experiments WHERE id=1')
    if not experiment or experiment['status'] != 'completed':
        raise ValueError('訓練尚未完成。')
    complete = check_consistency(service, experiment)
    if complete != experiment['total_games']:
        raise ValueError('未達目標局數。')
    moves = 0
    first = {experiment['model_a']: 0, experiment['model_b']: 0}
    for row in service.conn.execute('SELECT g.* FROM games g JOIN pilot_batches b USING(run_id) ORDER BY g.x_version'):
        game = dict(row)
        sequence = [r[0] for r in service.conn.execute('SELECT move_code FROM moves WHERE game_id=? ORDER BY ply', (game['game_id'],))]
        state = replay(sequence)[-1]
        if state.status != game['result'] or state_to_dict(state) != json.loads(game['state_json']):
            raise ValueError('原始棋譜重播與結局不符。')
        updates = service.conn.execute('SELECT COUNT(*) FROM learning_updates WHERE game_id=?', (game['game_id'],)).fetchone()[0]
        if updates != len(sequence):
            raise ValueError('棋步與終局學習明細數不一致。')
        moves += len(sequence)
        first[game['x_player']] += 1
    if list(first.values()) != [complete // 2, complete // 2]:
        raise ValueError('先後手不平衡。')
    if service.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or service.conn.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('SQLite 完整性檢查失敗。')
    return {'games_replayed': complete, 'moves_verified': moves, 'first_player_counts': first,
            'sqlite_integrity': 'ok', 'learning_records_verified': True}
