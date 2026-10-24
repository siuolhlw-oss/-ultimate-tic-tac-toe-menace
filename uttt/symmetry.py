"""同時變換大小格及指定盤的全盤 D4 對稱。"""

import json
from functools import lru_cache

from .engine import State, legal_moves, move_index, state_to_dict

CANONICAL_VERSION = "d4-v1"


def _position(position: int, transform_id: int) -> int:
    row, col = divmod(position - 1, 3)
    if transform_id >= 4:
        col = 2 - col  # F：左右鏡射，再順時針旋轉。
    for _ in range(transform_id % 4):
        row, col = col, 2 - row
    return row * 3 + col + 1


POSITION_MAPS = tuple(tuple(_position(p, t) for p in range(1, 10))
                      for t in range(8))
BOARD_MAPS = tuple(tuple((mapping[b] - 1) * 9 + mapping[s] - 1
                        for b in range(9) for s in range(9))
                   for mapping in POSITION_MAPS)
INVERSE_IDS = (0, 3, 2, 1, 4, 5, 6, 7)


def _check_transform(transform_id: int) -> None:
    if type(transform_id) is not int or not 0 <= transform_id < 8:
        raise ValueError("D4 轉換代號必須是 0–7 的整數。")


def transform_move(move: int, transform_id: int) -> int:
    move_index(move)
    _check_transform(transform_id)
    big, small = divmod(move, 10)
    mapping = POSITION_MAPS[transform_id]
    return 10 * mapping[big - 1] + mapping[small - 1]


def inverse_transform_move(move: int, transform_id: int) -> int:
    _check_transform(transform_id)
    return transform_move(move, INVERSE_IDS[transform_id])


def _transformed_parts(state: State, transform_id: int) -> tuple[str, int]:
    board = ["."] * 81
    for source, target in enumerate(BOARD_MAPS[transform_id]):
        board[target] = state.board[source]
    forced = (POSITION_MAPS[transform_id][state.forced - 1]
              if state.forced > 0 else state.forced)
    return "".join(board), forced


def transform_state(state: State, transform_id: int) -> State:
    _check_transform(transform_id)
    board, forced = _transformed_parts(state, transform_id)
    return State(board, state.turn, forced, state.rule_version)


@lru_cache(maxsize=8192)
def canonicalize(state: State) -> tuple[State, int]:
    """按 (board, forced) 字典序取最小值，同值取最小轉換代號。"""
    candidates = [(*_transformed_parts(state, t), t) for t in range(8)]
    board, forced, transform = min(candidates)
    return State(board, state.turn, forced, state.rule_version), transform


@lru_cache(maxsize=8192)
def state_key(state: State) -> str:
    canonical, _ = canonicalize(state)
    data = state_to_dict(canonical)
    data["canonical_version"] = CANONICAL_VERSION
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@lru_cache(maxsize=8192)
def _group_items(state: State) -> tuple[tuple[int, tuple[int, ...]], ...]:
    moves = legal_moves(state)
    stabilizer = [t for t in range(8) if transform_state(state, t) == state]
    remaining = set(moves)
    groups = []
    while remaining:
        move = min(remaining)
        orbit = tuple(sorted({transform_move(move, t) for t in stabilizer}))
        if not set(orbit) <= set(moves):
            raise ValueError("對稱轉換產生了不合法動作。")
        groups.append((min(orbit), orbit))
        remaining.difference_update(orbit)
    return tuple(groups)


def action_groups(canonical_state: State) -> dict[int, tuple[int, ...]]:
    """只使用保持完整局面不變的轉換分組；回傳獨立字典供唯讀使用。"""
    return dict(_group_items(canonical_state))
