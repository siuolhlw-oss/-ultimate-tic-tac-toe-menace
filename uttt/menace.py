"""合法動作、每格共享權重及 epsilon 探索；本模組不寫入模型。"""

from dataclasses import dataclass
import random

from .engine import InvalidMove, State, legal_moves
from .symmetry import (action_groups, canonicalize, inverse_transform_move,
                       transform_move)

INITIAL_WEIGHT = 3
EPSILON = 0.1


class ModelConsistencyError(ValueError):
    """模型動作類或權重與完整局面的合法動作不一致。"""


@dataclass(frozen=True)
class Choice:
    move: int
    canonical_move: int
    transform: int
    group_rep: int
    weight_before_choice: int | None
    probability: float
    branch: str
    unseen: bool


def _prepare(state: State, weights: dict[int, int] | None, training: bool):
    # 此順序不可倒置：先取得當下合法動作，再建立盒子視圖。
    moves = legal_moves(state)
    canonical, transform = canonicalize(state)
    groups = action_groups(canonical)
    if weights is not None:
        if not isinstance(weights, dict):
            raise ModelConsistencyError("權重必須是動作類代表碼對應整數的字典。")
        if any(type(key) is not int for key in weights) or set(weights) != set(groups):
            raise ModelConsistencyError("模型的動作類必須恰好包含全部合法動作類。")
        if any(type(value) is not int or value < 0 for value in weights.values()):
            raise ModelConsistencyError("權重必須是非負整數。")
        effective = dict(weights)
    else:
        effective = {rep: INITIAL_WEIGHT if training else None for rep in groups}
    move_to_group = {move: rep for rep, members in groups.items() for move in members}
    return moves, canonical, transform, groups, effective, move_to_group


def _rows(moves, transform, groups, effective, move_to_group, training, unseen):
    if not moves:
        return []
    total = sum(len(groups[rep]) * (weight or 0) for rep, weight in effective.items())
    uniform = total == 0 or (unseen and not training)
    epsilon = EPSILON if training else 0.0
    rows = []
    for move in moves:
        canonical_move = transform_move(move, transform)
        rep = move_to_group[canonical_move]
        weight = effective[rep]
        probability = (1.0 / len(moves) if uniform else
                       (1 - epsilon) * weight / total + epsilon / len(moves))
        rows.append({"move": move, "canonical_move": canonical_move,
                     "group_rep": rep, "group_size": len(groups[rep]),
                     "weight": weight, "probability": probability})
    return rows


def distribution(state: State, weights: dict[int, int] | None,
                 training: bool = True) -> list[dict]:
    """唯讀計算每個實際方向合法位置的總機率；終局回傳空串列。"""
    moves, _, transform, groups, effective, move_to_group = _prepare(state, weights, training)
    return _rows(moves, transform, groups, effective, move_to_group, training, weights is None)


def choose(state: State, weights: dict[int, int] | None, rng: random.Random,
           training: bool = True) -> Choice:
    moves, canonical, transform, groups, effective, move_to_group = _prepare(state, weights, training)
    if not moves:
        raise InvalidMove("終局沒有可抽選的合法動作。")
    unseen = weights is None
    total = sum(len(groups[rep]) * (weight or 0) for rep, weight in effective.items())
    canonical_moves = legal_moves(canonical)
    if unseen and not training:
        branch = "unseen_uniform"
        canonical_move = rng.choice(canonical_moves)
    elif total == 0:
        branch = "zero_uniform"
        canonical_move = rng.choice(canonical_moves)
    elif training and rng.random() < EPSILON:
        branch = "explore"
        canonical_move = rng.choice(canonical_moves)
    else:
        branch = "weighted"
        ticket = rng.randrange(total)
        canonical_move = None
        for rep, members in groups.items():
            tickets = len(members) * effective[rep]
            if ticket < tickets:
                canonical_move = rng.choice(members)
                break
            ticket -= tickets
        if canonical_move is None:
            raise ModelConsistencyError("珠數總量與動作類不一致。")
    move = inverse_transform_move(canonical_move, transform)
    if move not in moves:
        raise ModelConsistencyError("標準動作轉回畫面後並非合法位置。")
    rep = move_to_group[canonical_move]
    rows = _rows(moves, transform, groups, effective, move_to_group, training, unseen)
    probability = next(row["probability"] for row in rows if row["move"] == move)
    return Choice(move, canonical_move, transform, rep, effective[rep],
                  probability, branch, unseen)
