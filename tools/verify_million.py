"""在隔離 API 備份上比較真實來源的續訓，正式來源與目的皆不學習。"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_v3.repository import CompactResearchService
from storage_v4.repository import SlimService
from storage_v4.migration import code_contract
from storage_v4.codec import legacy_key, from_legacy
from tools.continue_selfplay import StorageBudget, RESERVE_GAME, file_hash, save_json
from uttt.pilot import game_seed, runner_lock
from uttt.service import now


def compare_game(old, new):
    version = new.head()
    config = new.meta('config')
    ids = [r['original_id'] for r in new.models()]
    players = ids if version % 2 == 0 else list(reversed(ids))
    game_id = old.new_game(*players, seed=game_seed(config['training_seed'], version), mode='train')
    started = time.perf_counter()
    while old.get_game(game_id)['status'] == 'active':
        old.step(game_id)
    old_seconds = time.perf_counter() - started
    started = time.perf_counter()
    result = new.train_one(capture=True)
    new_seconds = time.perf_counter() - started
    old_moves = old.repo.all('SELECT move_code,canonical_move,group_rep,branch,weight_before_choice,probability,state_key,model_id '
                             'FROM moves WHERE game_id=? ORDER BY ply', (game_id,))
    expected = [{'move': r['move_code'], 'canonical_move': r['canonical_move'], 'group_rep': r['group_rep'],
                 'branch': r['branch'], 'weight': r['weight_before_choice'], 'probability': r['probability']} for r in old_moves]
    if expected != result['moves'] or old.get_game(game_id)['result'] != result['result']:
        raise AssertionError('相同 RNG 的原始棋譜、抽選機率或結果不同。')
    for number, mid in enumerate(ids, 1):
        if old._model(mid) != new.legacy_model(number):
            raise AssertionError('模型欄位／RNG／version／revision 不同。')
    for move in old_moves:
        model = ids.index(move['model_id']) + 1
        key = from_legacy(move['state_key'])
        sid = new.conn.execute('SELECT id FROM states WHERE key=?', (key,)).fetchone()[0]
        sparse = {r['action']: (r['weight'], r['updates']) for r in new.conn.execute(
            'SELECT action,weight,updates FROM weights WHERE model=? AND state=?', (model, sid))}
        original = old.repo.all('SELECT group_rep,bead_weight,update_count FROM weights WHERE model_id=? AND state_key=?',
                                (move['model_id'], move['state_key']))
        legal_groups = {r['group_rep'] for r in original}
        if not set(sparse) <= legal_groups:
            raise AssertionError('目的權重含多餘動作類。')
        for weight in original:
            if sparse.get(weight['group_rep'], (3, 0)) != (weight['bead_weight'], weight['update_count']):
                raise AssertionError('終局獎懲後完整權重或更新次數不一致。')
    return {'game': result['game'], 'moves': len(expected), 'old_seconds': old_seconds, 'new_seconds': new_seconds}


def verify(directory, *, games=20):
    directory = Path(directory).resolve()
    with runner_lock(directory / 'runner.lock'):
        with SlimService(directory / 'training.sqlite3', read_only=True) as service:
            config, provenance = service.meta('config'), service.meta('provenance')
            if service.head() != config['base']:
                raise ValueError('正式續訓已開始，不能覆寫起點驗證。')
            if service.meta('code_contract') != code_contract():
                raise ValueError('程式／環境已改變，須重新建立並驗證來源轉換。')
        if games < 2 or games % 2 or config['base'] + games > config['target']:
            raise ValueError('驗證局數須為可容納的正偶數。')
        budget = StorageBudget(directory, config['budget_bytes'], config['min_free_bytes'])
        temporary = directory / ('.verify-' + uuid.uuid4().hex)
        temporary.mkdir()
        copies = [temporary / 'old.sqlite3', temporary / 'new.sqlite3']
        try:
            for source, target in zip([Path(provenance['source_path']), directory / 'training.sqlite3'], copies):
                with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
                    with closing(sqlite3.connect(target)) as dst:
                        src.backup(dst, pages=4096,
                                   progress=lambda status, remaining, total: budget.check(remaining * 4096 + RESERVE_GAME))
            if file_hash(copies[0]) != provenance['snapshot_sha256']:
                raise ValueError('來源一致快照已變動，拒絕混用來源驗證。')
            timings, moves = [], 0
            with CompactResearchService(copies[0]) as old, SlimService(copies[1]) as new:
                for number, model in enumerate(new.models(), 1):
                    if old._model(model['original_id']) != new.legacy_model(number):
                        raise AssertionError('驗證起點模型不一致。')
                for _ in range(games):
                    row = compare_game(old, new)
                    timings.append(row)
                    moves += row['moves']
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                before = new.models()
                count_before = [new.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                                for table in ('states', 'boxes', 'weights')]
                for model in (1, 2):
                    new.evaluate(model, games=4, seed=config['eval_seed'])
                if new.models() != before or count_before != [new.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                                                              for table in ('states', 'boxes', 'weights')]:
                    raise AssertionError('評估寫入了訓練模型。')
                if new.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or new.conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise AssertionError('續訓後完整性失敗。')
            report = {'passed': True, 'created_at': now(), 'base': config['base'], 'games': games, 'moves': moves,
                      'source_snapshot_sha256': provenance['snapshot_sha256'], 'code_contract': code_contract(),
                      'same_moves_probabilities_weights_rng_versions': True, 'frozen_evaluation': True,
                      'old_seconds': sum(t['old_seconds'] for t in timings),
                      'new_seconds': sum(t['new_seconds'] for t in timings),
                      'note': '真實起點之隔離短程相等驗證，非百萬局完成或長期效能保證。'}
            save_json(directory / 'acceptance.json', report)
            return report
        finally:
            for base in copies:
                for suffix in ('', '-wal', '-shm'):
                    candidate = Path(str(base) + suffix)
                    if candidate.exists():
                        candidate.unlink()
            temporary.rmdir()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--games', type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, games=args.games), ensure_ascii=False, indent=2))
