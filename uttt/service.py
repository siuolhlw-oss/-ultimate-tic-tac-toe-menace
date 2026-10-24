"""研究流程：合法落子、持久化、終局學習、凍結評估與匯出。

介面與命令列共用此層。此檔只依賴 Python 標準函式庫及本專案模組。
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from random import Random
import csv
import hashlib
import io
import json
import platform
import sqlite3
import uuid
import zipfile

from .engine import State, InvalidMove, legal_moves, play, state_to_dict, state_from_dict
from .symmetry import canonicalize, state_key, action_groups, transform_move
from .menace import choose, distribution
from .repository import Repository, SCHEMA_VERSION

ALGORITHM_VERSION = 'menace-d4-v1'
RULE_VERSION = 'uttt-closed-v1'
CODE_VERSION = '0.1.0'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + '_' + uuid.uuid4().hex


def rng_from(text):
    def tuples(value):
        return tuple(tuples(x) for x in value) if isinstance(value, list) else value
    rng = Random()
    rng.setstate(tuples(json.loads(text)))
    return rng


def runtime_info():
    root = Path(__file__).resolve().parent
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(root.glob('*.py'))}
    return {'code_version': CODE_VERSION, 'source_sha256': sources,
            'python': platform.python_version(), 'platform': platform.platform(),
            'sqlite': sqlite3.sqlite_version, 'schema_version': SCHEMA_VERSION,
            'algorithm_version': ALGORITHM_VERSION, 'rule_version': RULE_VERSION,
            'rng': 'random.Random/MT19937', 'epsilon_train': 0.1,
            'initial_weight': 3, 'reward': {'win': 3, 'draw': 1, 'loss': -1}}


class ResearchService:
    def __init__(self, db_path='data/research.sqlite3'):
        self.repo = Repository(db_path)
        self.conn = self.repo.conn
        self._checkpoint_cache = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.repo.close()

    def _model(self, model_id):
        model = self.repo.one('SELECT * FROM models WHERE model_id=?', (model_id,))
        if model is None:
            raise ValueError('找不到模型。')
        if model['rule_version'] != RULE_VERSION or model['algorithm_version'] != ALGORITHM_VERSION:
            raise ValueError('模型規則或演算法版本不相容。')
        return model

    def list_models(self):
        return self.repo.all('SELECT * FROM models ORDER BY created_at,model_id')

    def create_model(self, name, seed=0):
        with self.repo.transaction():
            return self._create_model(name, seed)

    def _create_model(self, name, seed=0):
        """由呼叫者持有交易，供批次設定與初始模型一併提交。"""
        name = str(name).strip()
        if not name or len(name) > 80:
            raise ValueError('模型名稱須為 1–80 個字。')
        model_id = identifier('model')
        self.conn.execute('INSERT INTO models(model_id,name,seed,rng_state,created_at,rule_version,algorithm_version) VALUES(?,?,?,?,?,?,?)',
                          (model_id, name, str(int(seed)), dumps(Random(int(seed)).getstate()), now(), RULE_VERSION, ALGORITHM_VERSION))
        self._save_checkpoint(model_id)
        return model_id

    def _new_run(self, mode, arm, seed, extra=None):
        run_id = identifier('run')
        config = runtime_info()
        config.update(extra or {})
        self.conn.execute('INSERT INTO runs(run_id,mode,arm,seed,created_at,status,config_json) VALUES(?,?,?,?,?,?,?)',
                          (run_id, mode, arm, str(int(seed)), now(), 'active', dumps(config)))
        return run_id

    def _ensure_state(self, state):
        canonical, _ = canonicalize(state)
        key = state_key(canonical)
        self.conn.execute('INSERT OR IGNORE INTO states VALUES(?,?,?)', (key, canonical.ply, canonical.status))
        return key

    def _ensure_edges(self, canonical):
        key = self._ensure_state(canonical)
        for rep, members in action_groups(canonical).items():
            self.conn.execute('INSERT OR IGNORE INTO state_edges(from_state_key,group_rep,members_json,group_size) VALUES(?,?,?,?)',
                              (key, rep, dumps(members), len(members)))
        return key

    def _weights(self, model_id, key):
        exists = self.repo.one('SELECT 1 FROM model_states WHERE model_id=? AND state_key=?', (model_id, key))
        if not exists:
            return None
        return {row['group_rep']: row['bead_weight'] for row in self.repo.all(
            'SELECT group_rep,bead_weight FROM weights WHERE model_id=? AND state_key=?', (model_id, key))}

    def _ensure_box(self, model_id, canonical, game_id, ply):
        # 必須先取得合法集合；終局不建盒、不因舊資料包含非法動作而放行。
        if not legal_moves(canonical):
            raise ValueError('終局不可建立行動盒子。')
        key = state_key(canonical)
        if self._weights(model_id, key) is not None:
            return False
        self._ensure_edges(canonical)
        self.conn.execute('INSERT INTO model_states VALUES(?,?,?,?)', (model_id, key, game_id, ply))
        for rep in action_groups(canonical):
            self.conn.execute('INSERT INTO weights VALUES(?,?,?,?,0)', (model_id, key, rep, 3))
        return True

    def _save_checkpoint(self, model_id):
        if self.repo.one('SELECT 1 FROM model_locks WHERE model_id=?', (model_id,)):
            raise ValueError('此模型仍有進行中的訓練對局，請完成或放棄後再存檢查點。')
        model = self._model(model_id)
        boxes = []
        for row in self.repo.all('SELECT state_key FROM model_states WHERE model_id=? ORDER BY state_key', (model_id,)):
            key = row['state_key']
            weights = self.repo.all('SELECT group_rep,bead_weight,update_count FROM weights WHERE model_id=? AND state_key=? ORDER BY group_rep', (model_id, key))
            boxes.append({'state_key': key, 'weights': weights})
        payload = dumps({'format_version': 1, 'model': model, 'boxes': boxes, 'runtime': runtime_info()})
        checkpoint_id = identifier('checkpoint')
        self.conn.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?)',
                          (checkpoint_id, model_id, model['version'], now(), payload, hashlib.sha256(payload.encode()).hexdigest()))
        return checkpoint_id

    def save_checkpoint(self, model_id):
        with self.repo.transaction():
            return self._save_checkpoint(model_id)

    def list_checkpoints(self, model_id):
        return self.repo.all('SELECT checkpoint_id,model_id,version,created_at,sha256 FROM checkpoints WHERE model_id=? ORDER BY created_at DESC', (model_id,))

    def _checkpoint(self, checkpoint_id):
        if checkpoint_id not in self._checkpoint_cache:
            row = self.repo.one('SELECT * FROM checkpoints WHERE checkpoint_id=?', (checkpoint_id,))
            if not row:
                raise ValueError('找不到檢查點。')
            if hashlib.sha256(row['payload_json'].encode()).hexdigest() != row['sha256']:
                raise ValueError('檢查點校驗失敗。')
            payload = json.loads(row['payload_json'])
            if payload['format_version'] != 1 or payload['model']['rule_version'] != RULE_VERSION or payload['model']['algorithm_version'] != ALGORITHM_VERSION:
                raise ValueError('檢查點版本不相容。')
            payload['weight_map'] = {b['state_key']: {w['group_rep']: w['bead_weight'] for w in b['weights']} for b in payload['boxes']}
            if len(self._checkpoint_cache) >= 8:
                self._checkpoint_cache.clear()
            self._checkpoint_cache[checkpoint_id] = payload
        return self._checkpoint_cache[checkpoint_id]

    def restore_checkpoint(self, checkpoint_id, name=None):
        payload = self._checkpoint(checkpoint_id)
        old = payload['model']
        label = (name or old['name'] + '（檢查點分支）').strip()
        if not label or len(label) > 80:
            raise ValueError('模型名稱須為 1–80 個字。')
        with self.repo.transaction():
            model_id = identifier('model')
            self.conn.execute('INSERT INTO models(model_id,name,seed,rng_state,version,trained_games,update_count,created_at,parent_checkpoint_id,rule_version,algorithm_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                              (model_id, label, old['seed'], old['rng_state'], old['version'], old['trained_games'],
                               old['update_count'], now(), checkpoint_id, old['rule_version'], old['algorithm_version']))
            for box in payload['boxes']:
                state = state_from_dict(json.loads(box['state_key']))
                key = self._ensure_edges(state)
                if key != box['state_key']:
                    raise ValueError('檢查點局面鍵不一致。')
                # distribution 會驗證組別完整且權重為非負整數。
                distribution(state, payload['weight_map'][key], training=True)
                self.conn.execute('INSERT INTO model_states VALUES(?,?,NULL,NULL)', (model_id, key))
                for w in box['weights']:
                    self.conn.execute('INSERT INTO weights VALUES(?,?,?,?,?)', (model_id, key, w['group_rep'], w['bead_weight'], w['update_count']))
            self._save_checkpoint(model_id)
        return model_id

    def new_game(self, x_player, o_player, seed=0, mode='train', run_id=None, is_example=False):
        if mode not in ('train', 'eval', 'demo'):
            raise ValueError('模式須為 train、eval 或 demo。')
        if is_example and mode == 'train':
            raise ValueError('示範對局不得開啟訓練。')
        players = (x_player, o_player)
        model_ids = [p for p in players if p not in ('human', 'random')]
        if len(model_ids) == 2 and model_ids[0] == model_ids[1] and mode == 'train':
            raise ValueError('自我對弈需要兩個獨立模型。')
        with self.repo.transaction():
            versions, checkpoints, revisions = [], [], []
            run = self.repo.one('SELECT * FROM runs WHERE run_id=?', (run_id,)) if run_id else None
            if run_id and (not run or run['mode'] != mode):
                raise ValueError('實驗編號不存在或模式不符。')
            run_config = json.loads(run['config_json']) if run else {}
            for player in players:
                if player in ('human', 'random'):
                    versions.append(None)
                    checkpoints.append(None)
                    revisions.append(None)
                    continue
                model = self._model(player)
                if mode == 'train' and self.repo.one('SELECT 1 FROM model_locks WHERE model_id=?', (player,)):
                    raise ValueError('模型已有進行中的對局；請先續局或明確放棄。')
                if mode in ('eval', 'demo'):
                    checkpoint = run_config.get('frozen_checkpoints', {}).get(player)
                    checkpoint = checkpoint or self._save_checkpoint(player)
                    frozen = self._checkpoint(checkpoint)['model']
                    versions.append(frozen['version'])
                    revisions.append(frozen['revision'])
                else:
                    checkpoint = self.list_checkpoints(player)[0]['checkpoint_id']
                    versions.append(model['version'])
                    revisions.append(model['revision'])
                checkpoints.append(checkpoint)
            if run_id is None:
                run_id = self._new_run(mode, 'interactive', seed, {'players': list(players)})
            game_id = identifier('game')
            state = State()
            self.conn.execute('INSERT INTO games(game_id,run_id,mode,x_player,o_player,x_version,o_version,x_checkpoint_id,o_checkpoint_id,rule_version,algorithm_version,seed,rng_state,state_json,is_example,created_at,x_revision,o_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                              (game_id, run_id, mode, x_player, o_player, *versions, *checkpoints, RULE_VERSION,
                               ALGORITHM_VERSION, str(int(seed)), dumps(Random(int(seed)).getstate()), dumps(state_to_dict(state)), int(is_example), now(), *revisions))
            if mode == 'train':
                for player in model_ids:
                    self.conn.execute('INSERT INTO model_locks VALUES(?,?)', (player, game_id))
        return game_id

    def get_game(self, game_id):
        game = self.repo.one('SELECT * FROM games WHERE game_id=?', (game_id,))
        if not game:
            raise ValueError('找不到對局。')
        return game

    def state(self, game_id):
        return state_from_dict(json.loads(self.get_game(game_id)['state_json']))

    def list_games(self, limit=100):
        return self.repo.all('SELECT * FROM games ORDER BY created_at DESC LIMIT ?', (max(1, min(int(limit), 10000)),))

    def moves(self, game_id):
        return self.repo.all('SELECT * FROM moves WHERE game_id=? ORDER BY ply', (game_id,))

    def step(self, game_id, move=None, expected_ply=None):
        with self.repo.transaction():
            game = self.get_game(game_id)
            if game['status'] != 'active':
                raise InvalidMove('本局已結束，不能再落子。')
            if expected_ply is not None and game['ply'] != expected_ply:
                raise InvalidMove('畫面已過期，請重新讀取棋盤。')
            state = state_from_dict(json.loads(game['state_json']))
            legal = legal_moves(state)  # 必須在任何盒子查詢／新增之前。
            if not legal:
                raise ValueError('進行中的對局沒有合法動作，資料不一致。')
            canonical, transform = canonicalize(state)
            key = state_key(canonical)
            groups = action_groups(canonical)
            player = game['x_player'] if state.turn == 'X' else game['o_player']
            is_model = player not in ('human', 'random')
            new_box = False
            probability = weight = rng_before = rng_after = None
            if player == 'human':
                if move not in legal or type(move) is not int:
                    raise InvalidMove('此位置不符合本回合規則。')
                branch = 'human'
                canonical_move = transform_move(move, transform)
                rep = next(r for r, members in groups.items() if canonical_move in members)
            else:
                if move is not None:
                    raise InvalidMove('AI 回合由模型選棋，不能指定位置。')
                if is_model and game['mode'] == 'train':
                    model = self._model(player)
                    lock = self.repo.one('SELECT game_id FROM model_locks WHERE model_id=?', (player,))
                    if not lock or lock['game_id'] != game_id:
                        raise ValueError('模型的對局占用紀錄不一致。')
                    rng_before = model['rng_state']
                    new_box = self._ensure_box(player, canonical, game_id, state.ply + 1)
                    weights = self._weights(player, key)
                else:
                    rng_before = game['rng_state']
                    if is_model and game['mode'] in ('eval', 'demo'):
                        cp = game['x_checkpoint_id'] if state.turn == 'X' else game['o_checkpoint_id']
                        weights = self._checkpoint(cp)['weight_map'].get(key)
                    elif is_model:
                        weights = self._weights(player, key)
                    else:
                        weights = None
                rng = rng_from(rng_before)
                if player == 'random':
                    move = rng.choice(legal)
                    canonical_move = transform_move(move, transform)
                    rep = next(r for r, members in groups.items() if canonical_move in members)
                    branch, probability = 'random', 1 / len(legal)
                else:
                    choice = choose(state, weights, rng, training=game['mode'] == 'train')
                    move, canonical_move, rep = choice.move, choice.canonical_move, choice.group_rep
                    branch, probability, weight = choice.branch, choice.probability, choice.weight_before_choice
                    if move not in legal:
                        raise ValueError('模型選出非法動作，拒絕保存。')
                rng_after = dumps(rng.getstate())
                if is_model and game['mode'] == 'train':
                    self.conn.execute('UPDATE models SET rng_state=? WHERE model_id=?', (rng_after, player))
                else:
                    self.conn.execute('UPDATE games SET rng_state=? WHERE game_id=?', (rng_after, game_id))
            next_state = play(state, move)  # 原始方向再次檢查合法性。
            self._ensure_edges(canonical)
            next_key = self._ensure_state(next_state)
            edge = self.repo.one('SELECT to_state_key FROM state_edges WHERE from_state_key=? AND group_rep=?', (key, rep))
            if edge['to_state_key'] not in (None, next_key):
                raise ValueError('同構轉移不一致，拒絕保存。')
            self.conn.execute('UPDATE state_edges SET to_state_key=? WHERE from_state_key=? AND group_rep=?', (next_key, key, rep))
            self.conn.execute('INSERT INTO moves VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                              (game_id, next_state.ply, state.turn, player if is_model else None, move,
                               state.forced, next_state.forced, key, next_key, transform, canonical_move,
                               rep, branch, probability, weight, int(new_box), rng_before, rng_after))
            if is_model and game['mode'] == 'train':
                self.conn.execute('UPDATE models SET revision=revision+1 WHERE model_id=?', (player,))
                revision = self._model(player)['revision']
                self.conn.execute('INSERT INTO model_events VALUES(?,?,?,?,?)', (player, revision, 'move', game_id, next_state.ply))
            self.conn.execute('UPDATE games SET state_json=?,ply=? WHERE game_id=?', (dumps(state_to_dict(next_state)), next_state.ply, game_id))
            if next_state.status != 'active':
                self._finish(game, next_state)
        return self.get_game(game_id)

    def _finish(self, game, state):
        game_id = game['game_id']
        if game['learning_applied']:
            raise ValueError('本局已施加獎懲。')
        if game['mode'] == 'train':
            for actor, player in (('X', game['x_player']), ('O', game['o_player'])):
                if player in ('human', 'random'):
                    continue
                model = self._model(player)
                delta = 1 if state.status == 'draw' else (3 if state.status == actor else -1)
                decisions = self.repo.all('SELECT * FROM moves WHERE game_id=? AND model_id=? ORDER BY ply', (game_id, player))
                for decision in decisions:
                    key, rep = decision['state_key'], decision['group_rep']
                    old = self.repo.one('SELECT bead_weight FROM weights WHERE model_id=? AND state_key=? AND group_rep=?', (player, key, rep))['bead_weight']
                    new = max(0, old + delta)
                    self.conn.execute('INSERT INTO learning_updates VALUES(?,?,?,?,?,?,?,?,?,?)',
                                      (game_id, player, decision['ply'], key, rep, old, delta, new - old, new, model['version'] + 1))
                    self.conn.execute('UPDATE weights SET bead_weight=?,update_count=update_count+1 WHERE model_id=? AND state_key=? AND group_rep=?', (new, player, key, rep))
                self.conn.execute('UPDATE models SET version=version+1,trained_games=trained_games+1,update_count=update_count+?,revision=revision+1 WHERE model_id=?', (len(decisions), player))
                revision = self._model(player)['revision']
                self.conn.execute('INSERT INTO model_events VALUES(?,?,?,?,NULL)', (player, revision, 'finish', game_id))
        self.conn.execute('UPDATE games SET status=?,result=?,end_reason=?,learning_applied=?,ended_at=? WHERE game_id=?',
                          ('finished', state.status, 'all_boards_closed' if state.status == 'draw' else 'macro_line',
                           int(game['mode'] == 'train'), now(), game_id))
        self.conn.execute('DELETE FROM model_locks WHERE game_id=?', (game_id,))
        self.conn.execute('UPDATE runs SET completed_games=completed_games+1 WHERE run_id=?', (game['run_id'],))

    def abandon(self, game_id):
        with self.repo.transaction():
            game = self.get_game(game_id)
            if game['status'] != 'active':
                raise ValueError('只能放棄進行中的對局。')
            self.conn.execute("UPDATE games SET status='abandoned',end_reason='abandoned',ended_at=? WHERE game_id=?", (now(), game_id))
            self.conn.execute('DELETE FROM model_locks WHERE game_id=?', (game_id,))
        return self.get_game(game_id)

    def train(self, model_a, model_b, games=10, seed=0, progress=None):
        if model_a == model_b:
            raise ValueError('請選擇兩個獨立模型。')
        if type(games) is not int or games < 1:
            raise ValueError('局數須為正整數。')
        a, b = self._model(model_a), self._model(model_b)
        with self.repo.transaction():
            run_id = self._new_run('train', 'self_play', seed, {'model_a': model_a, 'model_b': model_b, 'requested_games': games, 'starting_versions': [a['version'], b['version']]})
        rng = Random(int(seed))
        results = Counter()
        try:
            for i in range(games):
                # 接續批次時仍依 A 的累計訓練局數交替先後手。
                players = (model_a, model_b) if (a['trained_games'] + i) % 2 == 0 else (model_b, model_a)
                game_id = self.new_game(*players, seed=rng.getrandbits(63), mode='train', run_id=run_id)
                while self.get_game(game_id)['status'] == 'active':
                    self.step(game_id)
                result = self.get_game(game_id)['result']
                results[result] += 1
                if progress:
                    progress(i + 1, games)
            self.save_checkpoint(model_a)
            self.save_checkpoint(model_b)
            self.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
        except BaseException:
            self.conn.execute("UPDATE runs SET status='interrupted' WHERE run_id=?", (run_id,))
            raise
        return {'run_id': run_id, 'games': games, 'results': dict(results)}

    def evaluate(self, model_id, games=20, seed=0, progress=None):
        if type(games) is not int or games < 2 or games % 2:
            raise ValueError('評估局數須為正偶數，確保先後手平衡。')
        checkpoint = self.save_checkpoint(model_id)
        with self.repo.transaction():
            run_id = self._new_run('eval', 'fixed_random', seed, {'frozen_checkpoints': {model_id: checkpoint}, 'opponent': 'uniform-legal-v1', 'requested_games': games, 'epsilon': 0})
        rng = Random(int(seed))
        counts = Counter()
        roles = {'X': Counter(), 'O': Counter()}
        try:
            for i in range(games):
                role = 'X' if i % 2 == 0 else 'O'
                players = (model_id, 'random') if role == 'X' else ('random', model_id)
                game_id = self.new_game(*players, seed=rng.getrandbits(63), mode='eval', run_id=run_id)
                while self.get_game(game_id)['status'] == 'active':
                    self.step(game_id)
                result = self.get_game(game_id)['result']
                outcome = 'draws' if result == 'draw' else ('wins' if result == role else 'losses')
                counts[outcome] += 1
                roles[role][outcome] += 1
                if progress:
                    progress(i + 1, games)
            self.conn.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))
        except BaseException:
            self.conn.execute("UPDATE runs SET status='interrupted' WHERE run_id=?", (run_id,))
            raise
        return {'run_id': run_id, 'games': games, 'checkpoint_id': checkpoint,
                **{k: counts[k] for k in ('wins', 'draws', 'losses')},
                'score': (counts['wins'] + counts['draws'] * 0.5) / games,
                'by_role': {r: dict(c) for r, c in roles.items()}}

    def inspect(self, state, model_id, training=True, checkpoint_id=None):
        with self.repo.snapshot():
            return self._inspect(state, model_id, training, checkpoint_id)

    def _inspect(self, state, model_id, training=True, checkpoint_id=None):
        legal = legal_moves(state)
        canonical, transform = canonicalize(state)
        key = state_key(canonical)
        model = self._model(model_id)
        if checkpoint_id:
            payload = self._checkpoint(checkpoint_id)
            if payload['model']['model_id'] != model_id:
                raise ValueError('檢查點不屬於所選模型。')
            weights = payload['weight_map'].get(key)
            version = payload['model']['version']
        else:
            weights = self._weights(model_id, key)
            version = model['version']
        actions = distribution(state, weights, training=training) if legal else []
        groups = []
        for rep, members in action_groups(canonical).items():
            rows = [a for a in actions if a['group_rep'] == rep]
            edge = None if checkpoint_id else self.repo.one('SELECT to_state_key FROM state_edges WHERE from_state_key=? AND group_rep=?', (key, rep))
            visit = None if checkpoint_id else self.repo.one("SELECT COUNT(*) AS n FROM moves m JOIN games g ON m.game_id=g.game_id WHERE m.model_id=? AND m.state_key=? AND m.group_rep=? AND g.mode='train' AND g.is_example=0", (model_id, key, rep))
            groups.append({'rep': rep, 'members': list(members), 'weight': rows[0]['weight'],
                           'probability': sum(r['probability'] for r in rows),
                           'child_key': edge['to_state_key'] if edge else None, 'visits': visit['n'] if visit else None})
        return {'canonical_state': canonical, 'state_key': key, 'transform': transform,
                'actions': actions, 'groups': groups, 'known': weights is not None, 'model_version': version}

    def list_states(self, model_id, limit=100, checkpoint_id=None):
        if checkpoint_id:
            payload = self._checkpoint(checkpoint_id)
            if payload['model']['model_id'] != model_id:
                raise ValueError('檢查點不屬於所選模型。')
            states = [{'state_key': b['state_key'], 'ply': state_from_dict(json.loads(b['state_key'])).ply} for b in payload['boxes']]
            return sorted(states, key=lambda s: (s['ply'], s['state_key']))[:max(1, min(int(limit), 10000))]
        return self.repo.all('SELECT s.state_key,s.ply FROM model_states ms JOIN states s USING(state_key) WHERE ms.model_id=? ORDER BY s.ply,s.state_key LIMIT ?', (model_id, max(1, min(int(limit), 10000))))

    def model_at_game_start(self, game_id, actor):
        """基底檢查點＋有序變更紀錄，還原指定對局開局模型；不寫回。

        version 是已完成學習局數版本，revision 另涵蓋局中初始化與亂數變化。
        因此中止局也能精確追溯，且不必每局複製不斷增大的完整模型。
        """
        with self.repo.snapshot():
            return self._model_at_game_start(game_id, actor)

    def _model_at_game_start(self, game_id, actor):
        if actor not in ('X', 'O'):
            raise ValueError('行動方須為 X 或 O。')
        game = self.get_game(game_id)
        side = actor.lower()
        model_id = game[side + '_player']
        checkpoint_id = game[side + '_checkpoint_id']
        if not checkpoint_id:
            raise ValueError('此行動方沒有 MENACE 模型。')
        original = self._checkpoint(checkpoint_id)
        payload = json.loads(dumps({k: v for k, v in original.items() if k != 'weight_map'}))
        model = payload['model']
        target_revision = game[side + '_revision']
        boxes = {b['state_key']: {w['group_rep']: w for w in b['weights']} for b in payload['boxes']}
        events = self.repo.all('SELECT * FROM model_events WHERE model_id=? AND revision>? AND revision<=? ORDER BY revision',
                               (model_id, model['revision'], target_revision))
        for event in events:
            if event['revision'] != model['revision'] + 1:
                raise ValueError('模型變更紀錄不連續，無法還原。')
            if event['kind'] == 'move':
                move = self.repo.one('SELECT * FROM moves WHERE game_id=? AND ply=? AND model_id=?', (event['game_id'], event['ply'], model_id))
                if not move or move['rng_before'] != model['rng_state']:
                    raise ValueError('逐手亂數紀錄不一致。')
                key = move['state_key']
                if move['new_box']:
                    if key in boxes:
                        raise ValueError('新盒子紀錄與已存局面重複。')
                    state = state_from_dict(json.loads(key))
                    boxes[key] = {rep: {'group_rep': rep, 'bead_weight': 3, 'update_count': 0} for rep in action_groups(state)}
                model['rng_state'] = move['rng_after']
            else:
                updates = self.repo.all('SELECT * FROM learning_updates WHERE game_id=? AND model_id=? ORDER BY ply', (event['game_id'], model_id))
                for update in updates:
                    weight = boxes[update['state_key']][update['group_rep']]
                    if weight['bead_weight'] != update['weight_before_update']:
                        raise ValueError('珠數變更紀錄不一致。')
                    weight['bead_weight'] = update['weight_after_update']
                    weight['update_count'] += 1
                model['version'] += 1
                model['trained_games'] += 1
                model['update_count'] += len(updates)
            model['revision'] = event['revision']
        if model['revision'] != target_revision or model['version'] != game[side + '_version']:
            raise ValueError('模型起點紀錄不足，無法還原到指定版本。')
        payload['boxes'] = [{'state_key': k, 'weights': list(v.values())} for k, v in sorted(boxes.items())]
        return payload

    def export_data(self, run_id=None):
        """單一一致讀取快照。JSON保留原值，CSV避免文字被Excel當公式執行。"""
        tables = ('runs', 'models', 'states', 'state_edges', 'model_states', 'weights',
                  'checkpoints', 'games', 'moves', 'learning_updates', 'model_events')
        self.conn.execute('BEGIN')
        try:
            data = {table: self.repo.all('SELECT * FROM ' + table) for table in tables}
        finally:
            self.conn.rollback()
        dependencies = {}
        if run_id is not None:
            for table in ('runs', 'games', 'moves', 'learning_updates'):
                dependencies[table] = data[table]
            data['runs'] = [r for r in data['runs'] if r['run_id'] == run_id]
            if not data['runs']:
                raise ValueError('找不到此實驗。')
            data['games'] = [g for g in data['games'] if g['run_id'] == run_id]
            ids = {g['game_id'] for g in data['games']}
            data['moves'] = [m for m in data['moves'] if m['game_id'] in ids]
            data['learning_updates'] = [u for u in data['learning_updates'] if u['game_id'] in ids]
            dependencies['runs'] = [r for r in dependencies['runs'] if r['run_id'] != run_id]
            for table in ('games', 'moves', 'learning_updates'):
                dependencies[table] = [r for r in dependencies[table] if r['game_id'] not in ids]
        manifest = {'exported_at': now(), 'format_version': 1, 'selected_run': run_id,
                    'runtime': runtime_info(), 'note': '頂層對局及CSV依實驗篩選；完整模型與事件保留，其他來源對局及更新放dependencies以供還原。CSV文字公式加單引號，原值見JSON。'}
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('research.json', dumps({'manifest': manifest, **data, 'dependencies': dependencies}))
            for table, rows in data.items():
                # 檢查點的完整 JSON 已在research.json；避免重複產生極大CSV。
                if table == 'checkpoints':
                    rows = [{k: v for k, v in r.items() if k != 'payload_json'} for r in rows]
                if not rows:
                    continue
                text = io.StringIO(newline='')
                writer = csv.DictWriter(text, fieldnames=list(rows[0]))
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v) for k, v in row.items()})
                archive.writestr(table + '.csv', text.getvalue().encode('utf-8-sig'))
        return output.getvalue()

    def backup(self, destination):
        """使用SQLite備份API，避免直接複製使用中的WAL資料庫。"""
        destination = Path(destination)
        if self.repo.path != ':memory:' and destination.resolve() == Path(self.repo.path).resolve():
            raise ValueError('備份位置不可與使用中的資料庫相同。')
        if destination.exists():
            raise FileExistsError('備份檔已存在，請使用新檔名。')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            self.conn.backup(target)
        return str(destination)
