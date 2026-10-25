"""來源先用 SQLite backup API 固定快照；只發布驗證通過的新 v4 檔。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import uuid

from storage_v3.repository import CompactResearchService
from uttt.service import dumps, now
from tools.continue_selfplay import StorageBudget, RESERVE_GAME, file_hash, save_json
from .codec import from_legacy, unpack
from .repository import SlimService, CURRENT
from uttt.symmetry import canonicalize, action_groups


def validate_all_boxes(old, new, progress=None):
    """逐盒重建所有合法權重；缺列、非法列與不完整預設不可被稀疏格式掩蓋。"""
    checked, total_weights = 0, 0
    for model in new.models():
        updates, boxes = 0, 0
        for row in new.conn.execute('SELECT b.state,s.key FROM boxes b JOIN states s ON s.id=b.state '
                                    'WHERE b.model=? ORDER BY b.state', (model['id'],)):
            state = unpack(row['key'])
            if canonicalize(state)[0] != state:
                raise ValueError('來源盒子不是全盤 D4 標準局面。')
            groups = action_groups(state)
            original = {r[0]: (r[1], r[2]) for r in old.conn.execute(
                'SELECT group_rep,bead_weight,update_count FROM _weights WHERE model_id=? AND state_id=?',
                (model['original_id'], row['state']))}
            if not groups or set(original) != set(groups):
                raise ValueError('來源盒子缺少合法下法或含非法下法。')
            sparse = {r[0]: (r[1], r[2]) for r in new.conn.execute(
                'SELECT action,weight,updates FROM weights WHERE model=? AND state=?', (model['id'], row['state']))}
            expanded = {rep: sparse.get(rep, (3, 0)) for rep in groups}
            if not set(sparse) <= set(groups) or expanded != original:
                raise ValueError('稀疏珠數還原與來源完整權重不一致。')
            updates += sum(v[1] for v in original.values())
            total_weights += len(original)
            checked += 1
            boxes += 1
            if progress and checked % 25000 == 0:
                progress('validated_boxes', checked)
        if boxes != model['box_count'] or updates != model['update_count']:
            raise ValueError('來源模型盒數或總更新次數不一致。')
    if total_weights != old.conn.execute('SELECT COUNT(*) FROM _weights').fetchone()[0]:
        raise ValueError('來源含無法對應盒子的權重。')
    return {'boxes': checked, 'expanded_weights': total_weights, 'all_equal': True}


def code_contract():
    root = Path(__file__).resolve().parents[1]
    names = ['uttt/engine.py', 'uttt/menace.py', 'uttt/symmetry.py', 'uttt/service.py',
             'uttt/pilot.py', 'tools/continue_selfplay.py', 'tools/train_million.py', 'tools/verify_million.py']
    names += ['storage_v4/' + p.name for p in sorted((root / 'storage_v4').glob('*.py'))]
    return {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'files': {name: file_hash(root / name) for name in names}}


def _digest(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        data = dumps(tuple(row)).encode()
        digest.update(len(data).to_bytes(4, 'little'))
        digest.update(data)
        count += 1
    return {'count': count, 'sha256': digest.hexdigest()}


def _insert_rows(connection, table, rows, columns, progress=None):
    digest, count, batch = hashlib.sha256(), 0, []
    for row in rows:
        row = tuple(row)
        # bytes 的穩定十六進位表示僅用於驗證，不寫入資料庫。
        data = dumps(tuple(x.hex() if isinstance(x, bytes) else x for x in row)).encode()
        digest.update(len(data).to_bytes(4, 'little'))
        digest.update(data)
        count += 1
        batch.append(row)
        if len(batch) >= 1000:
            connection.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?' * columns) + ')', batch)
            batch.clear()
            if progress:
                progress(table, count)
    if batch:
        connection.executemany('INSERT INTO ' + table + ' VALUES(' + ','.join('?' * columns) + ')', batch)
    return {'count': count, 'sha256': digest.hexdigest()}


def _read_digest(connection, table, order):
    return _digest(tuple(x.hex() if isinstance(x, bytes) else x for x in row)
                   for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY ' + order))


def migrate(source, destination, *, target=1_000_000, expected_base=9000, eval_games=200,
            eval_seed=20261025, budget=None, require_extension=True, progress=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not source.is_file():
        raise ValueError('需要不同的既有來源與新目的檔。')
    report_path = destination.with_suffix('.migration.json')
    if destination.exists() or report_path.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    budget = budget or StorageBudget(destination.parent)
    token = uuid.uuid4().hex
    snapshot = destination.parent / ('.source-' + token + '.sqlite3')
    partial = destination.parent / ('.target-' + token + '.sqlite3')
    report = None
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as live:
            size = live.execute('PRAGMA page_count').fetchone()[0] * live.execute('PRAGMA page_size').fetchone()[0]
            budget.check(size + RESERVE_GAME)
            with closing(sqlite3.connect(snapshot)) as fixed:
                live.backup(fixed, pages=4096,
                            progress=lambda status, remaining, total: budget.check(remaining * 4096 + RESERVE_GAME))
        snapshot_sha = file_hash(snapshot)
        if progress:
            progress('source_snapshot', snapshot.stat().st_size)
        with CompactResearchService(snapshot, read_only=True) as old, SlimService(partial, create=True) as new:
            extension = old.repo.one('SELECT * FROM extension_experiments WHERE id=1') if require_extension else None
            if require_extension and (not extension or extension['status'] != 'completed'):
                raise ValueError('來源須為已完成的 C／D 延伸訓練。')
            source_models = old.repo.all('SELECT * FROM models ORDER BY model_id')
            if len(source_models) != 2:
                raise ValueError('來源必須恰有兩個模型。')
            if extension:
                by_id = {m['model_id']: m for m in source_models}
                source_models = [by_id[extension['model_a']], by_id[extension['model_b']]]
            elif expected_base:
                first = old.repo.one("SELECT x_player,o_player FROM _games WHERE mode='train' AND x_version=0 LIMIT 1")
                if not first:
                    raise ValueError('找不到原始訓練起點。')
                by_id = {m['model_id']: m for m in source_models}
                source_models = [by_id[first['x_player']], by_id[first['o_player']]]
            if any(m['version'] != expected_base or m['trained_games'] != expected_base for m in source_models):
                raise ValueError('來源模型版本與預期起點不符。')
            if type(target) is not int or target <= expected_base or target % 2:
                raise ValueError('累計目標必須是比起點大的偶數。')
            if type(eval_games) is not int or eval_games < 2 or eval_games % 2:
                raise ValueError('每模型評估局數須為正偶數。')
            if old.conn.execute('SELECT COUNT(*) FROM model_locks').fetchone()[0]:
                raise ValueError('來源仍有模型被對局占用。')
            if old.conn.execute("SELECT COUNT(*) FROM _games WHERE mode='train' AND status!='finished'").fetchone()[0]:
                raise ValueError('來源含未完成或放棄的訓練，不可悄悄丟棄。')
            a, b = [m['model_id'] for m in source_models]
            train_seed = int(extension['seed']) if extension else 20261013
            config = {'base': expected_base, 'target': target, 'training_seed': train_seed,
                      'eval_every': 10000, 'eval_games': eval_games, 'eval_seed': eval_seed,
                      'milestones': [100000, 500000, 1000000], 'budget_bytes': budget.budget_bytes,
                      'min_free_bytes': budget.min_free_bytes, 'retention': 'latest+game-summary-v1',
                      'rule_version': 'uttt-closed-v1', 'algorithm_version': 'menace-d4-v1'}
            counts = {}

            def check_progress(table, count):
                if count % 25000 == 0:
                    budget.check(RESERVE_GAME)
                    if progress:
                        progress(table, count)

            with new.transaction():
                new.set_meta('config', config)
                new.set_meta('code_contract', code_contract())
                new.set_meta('provenance', {'source_path': str(source), 'snapshot_sha256': snapshot_sha,
                                           'created_at': now(), 'aliases': {'C': a, 'D': b},
                                           'historical_detail_ends_at': expected_base,
                                           'source_extension': extension})
                for number, model in enumerate(source_models, 1):
                    metadata = {k: v for k, v in model.items() if k not in CURRENT}
                    box_count = old.conn.execute('SELECT COUNT(*) FROM _model_states WHERE model_id=?',
                                                (model['model_id'],)).fetchone()[0]
                    new.conn.execute('INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?)',
                                     (number, 'C' if number == 1 else 'D', model['model_id'], dumps(metadata),
                                      *(model[k] for k in CURRENT), box_count))
                counts['states'] = _insert_rows(new.conn, 'states',
                    ((r[0], from_legacy(r[1])) for r in old.conn.execute('SELECT state_id,state_key FROM _states ORDER BY state_id')),
                    2, check_progress)

                def boxes():
                    for number, mid in enumerate((a, b), 1):
                        for r in old.conn.execute('SELECT ms.state_id,g.x_version,ms.created_ply '
                            'FROM _model_states ms LEFT JOIN _games g ON g.game_id=ms.created_game_id '
                            'WHERE ms.model_id=? ORDER BY ms.state_id', (mid,)):
                            yield number, r[0], None if r[1] is None else r[1] + 1, r[2]
                counts['boxes'] = _insert_rows(new.conn, 'boxes', boxes(), 4, check_progress)

                def weights():
                    for number, mid in enumerate((a, b), 1):
                        for r in old.conn.execute('SELECT state_id,group_rep,bead_weight,update_count FROM _weights '
                            'WHERE model_id=? AND NOT(bead_weight=3 AND update_count=0) ORDER BY state_id,group_rep', (mid,)):
                            yield (number, *r)
                counts['weights'] = _insert_rows(new.conn, 'weights', weights(), 5, check_progress)

                def summaries():
                    cursor = old.conn.execute("SELECT x_version,x_player,o_player,result,ply,seed,ended_at,game_id,o_version "
                                              "FROM _games WHERE mode='train' AND status='finished' ORDER BY x_version")
                    for index, r in enumerate(cursor):
                        expected_players = (a, b) if index % 2 == 0 else (b, a)
                        if r[0] != index or r[8] != index or (r[1], r[2]) != expected_players:
                            raise ValueError('來源訓練順序、先後手或版本不符。')
                        births = dict(old.conn.execute('SELECT model_id,SUM(new_box) FROM _moves WHERE game_id=? GROUP BY model_id', (r[7],)))
                        yield index + 1, 1 if r[1] == a else 2, r[3], r[4], births.get(a, 0), births.get(b, 0), int(r[5]), None, r[6]
                counts['summaries'] = _insert_rows(new.conn, 'summaries', summaries(), 9, check_progress)
                if counts['summaries']['count'] != expected_base:
                    raise ValueError('來源完成局數不符。')
                original_weights = old.conn.execute('SELECT COUNT(*) FROM _weights').fetchone()[0]
                new.set_meta('migration_counts', {'tables': counts, 'source_weight_rows': original_weights})
            verified = {}
            for table, order in [('states', 'id'), ('boxes', 'model,state'), ('weights', 'model,state,action'), ('summaries', 'game')]:
                actual = _read_digest(new.conn, table, order)
                if actual != counts[table]:
                    raise ValueError('遷移逐列核對失敗：' + table)
                verified[table] = actual
            for number, model in enumerate(source_models, 1):
                if new.legacy_model(number) != model:
                    raise ValueError('模型原值、亂數或版本不一致。')
            expanded = validate_all_boxes(old, new, check_progress)
            new.check_head()
            if new.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or new.conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('目的資料庫完整性或外鍵檢查失敗。')
            new.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            report = {'created_at': now(), 'source_path': str(source), 'source_snapshot_sha256': snapshot_sha,
                      'base_games': expected_base, 'target_games': target, 'aliases': {'C': a, 'D': b},
                      'verified_tables': verified, 'models_equal': True, 'integrity': 'ok',
                      'source_bytes': snapshot.stat().st_size, 'target_bytes': partial.stat().st_size,
                      'source_weight_rows': original_weights,
                      'full_weight_validation': expanded,
                      'note': '來源舊歷史保留於原 v3；v4 僅遷移最新模型及逐局摘要。'}
        budget.check(RESERVE_GAME)
        partial.rename(destination)
        report['target_sha256'] = file_hash(destination)
        save_json(report_path, report)
        return report
    finally:
        # 僅移除這次 UUID 建立的暫存，絕不碰來源、既有成果或備份。
        for base in (snapshot, partial):
            for suffix in ('', '-wal', '-shm'):
                candidate = Path(str(base) + suffix)
                if candidate.exists():
                    candidate.unlink()
