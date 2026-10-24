"""唯讀重播原始棋譜，並與 v2 原檔比較歷史開局模型。"""
import hashlib
import json
from pathlib import Path
import sqlite3

from uttt.engine import replay, state_to_dict
from uttt.repository import Repository
from uttt.service import ResearchService, dumps
from .repository import register_functions


class ReadOnlyRepository(Repository):
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        self.conn = sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=20, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version == 3:
            register_functions(self.conn)
        elif version != 2:
            self.conn.close()
            raise ValueError('唯讀驗證只接受 v2／v3 資料庫。')
        self.conn.execute('PRAGMA query_only=ON')


class ReadOnlyService(ResearchService):
    def __init__(self, path):
        self.repo = ReadOnlyRepository(path)
        self.conn = self.repo.conn
        self._checkpoint_cache = {}


def verify_replays(database):
    games = moves = finished = 0
    with ReadOnlyService(database) as service:
        with service.repo.snapshot():
            for game in service.conn.execute('SELECT game_id,ply,state_json,status,result,mode,learning_applied,x_version FROM games ORDER BY created_at,game_id'):
                rows = service.repo.all('SELECT ply,move_code,actor,model_id FROM moves WHERE game_id=? ORDER BY ply', (game['game_id'],))
                if [r['ply'] for r in rows] != list(range(1, game['ply'] + 1)):
                    raise ValueError('原始棋譜手數不連續。')
                state = replay([r['move_code'] for r in rows])[-1]
                if state_to_dict(state) != json.loads(game['state_json']):
                    raise ValueError('原始方向重播與已存局面不同。')
                if game['status'] == 'finished' and state.status != game['result']:
                    raise ValueError('重播結局與結果不同。')
                updates = service.repo.all('SELECT * FROM learning_updates WHERE game_id=? ORDER BY ply', (game['game_id'],))
                learning = game['mode'] == 'train' and game['status'] == 'finished'
                model_rows = [r for r in rows if r['model_id']]
                if bool(game['learning_applied']) != learning or len(updates) != (len(model_rows) if learning else 0):
                    raise ValueError('終局學習標記或記錄數不符。')
                for move, update in zip(model_rows, updates):
                    delta = 1 if game['result'] == 'draw' else (3 if game['result'] == move['actor'] else -1)
                    if (move['ply'] != update['ply'] or move['model_id'] != update['model_id']
                            or update['reward_delta'] != delta
                            or update['weight_after_update'] != max(0, update['weight_before_update'] + delta)
                            or update['applied_delta'] != update['weight_after_update'] - update['weight_before_update']):
                        raise ValueError('終局獎懲原值不符。')
                games += 1
                moves += len(rows)
                finished += game['status'] == 'finished'
    return {'games_replayed': games, 'finished_games': finished, 'moves_verified': moves,
            'raw_orientation': 'preserved', 'learning_records': 'verified'}


def compare_histories(source, compact):
    checked = []
    with ReadOnlyService(source) as old, ReadOnlyService(compact) as new:
        with old.repo.snapshot(), new.repo.snapshot():
            games = new.repo.all("SELECT game_id,x_version FROM games WHERE mode='train' AND status='finished' ORDER BY x_version,game_id")
            if not games:
                return {'sampled_games': 0, 'model_starts': []}
            indices = sorted({min(i, len(games) - 1) for i in (0, 1, 98, 99, 100, 101, len(games)//2, len(games)-1)})
            for index in indices:
                game = games[index]
                for actor in ('X', 'O'):
                    if not new.get_game(game['game_id'])[actor.lower() + '_checkpoint_id']:
                        continue
                    left = old.model_at_game_start(game['game_id'], actor)
                    digest = hashlib.sha256(dumps(left).encode('utf-8')).hexdigest()
                    old._checkpoint_cache.clear()
                    del left
                    right = new.model_at_game_start(game['game_id'], actor)
                    if digest != hashlib.sha256(dumps(right).encode('utf-8')).hexdigest():
                        raise ValueError('歷史模型還原不一致。')
                    checked.append({'game_id': game['game_id'], 'actor': actor,
                                    'version': right['model']['version'], 'revision': right['model']['revision'],
                                    'sha256': digest})
                    new._checkpoint_cache.clear()
                    del right
    return {'sampled_games': len(indices), 'model_starts': checked}
