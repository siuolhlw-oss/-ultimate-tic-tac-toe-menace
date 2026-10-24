"""封閉小盤版超級井字棋：不可變局面與純規則函式。"""

from dataclasses import dataclass
from functools import cached_property
from typing import Iterable, Mapping

RULE_VERSION = "uttt-closed-v1"
LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8),
         (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


class InvalidMove(ValueError):
    """輸入位置不符合編碼或當前遊戲規則。"""


def _winner(cells: str | tuple[str, ...]) -> str | None:
    winners = {cells[a] for a, b, c in LINES
               if cells[a] in ("X", "O") and cells[a] == cells[b] == cells[c]}
    if len(winners) > 1:
        raise ValueError("同一棋盤不能同時由 X 與 O 連線。")
    return next(iter(winners), None)


def move_index(move: int) -> int:
    """兩位數位置轉為 board 的 0–80 索引；不接受 bool 或字串。"""
    if type(move) is not int:
        raise InvalidMove("位置必須是十位與個位均為 1–9 的整數。")
    big, small = divmod(move, 10)
    if not (1 <= big <= 9 and 1 <= small <= 9):
        raise InvalidMove("位置必須是十位與個位均為 1–9 的兩位數。")
    return (big - 1) * 9 + small - 1


@dataclass(frozen=True)
class State:
    """完整局面；封閉的指定盤正規化為自由，終局指定盤統一為 -1。

    建構子檢查結構及局部勝負一致性，但不宣稱任意輸入棋盤皆可達。
    外部棋譜的可達性須從 State() 逐手 replay 驗證。
    """

    board: str = "." * 81
    turn: str = "X"
    forced: int = 0
    rule_version: str = RULE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.board, str) or len(self.board) != 81:
            raise ValueError("棋盤必須是 81 個字元。")
        if any(cell not in ".XO" for cell in self.board):
            raise ValueError("棋盤字元只能是 .、X、O。")
        if self.turn not in ("X", "O"):
            raise ValueError("行動方必須是 X 或 O。")
        if type(self.forced) is not int or not -1 <= self.forced <= 9:
            raise ValueError("指定盤必須為 -1、0 或 1–9。")
        if not isinstance(self.rule_version, str) or not self.rule_version:
            raise ValueError("規則版本不可為空。")
        if self.status != "active":
            object.__setattr__(self, "forced", -1)
        elif self.forced == -1:
            raise ValueError("尚未終局不能使用終局指定盤標記。")
        elif self.forced and self.local_status[self.forced - 1] != ".":
            object.__setattr__(self, "forced", 0)

    @cached_property
    def local_status(self) -> tuple[str, ...]:
        statuses = []
        for start in range(0, 81, 9):
            cells = self.board[start:start + 9]
            statuses.append(_winner(cells) or ("." if "." in cells else "D"))
        return tuple(statuses)

    @cached_property
    def status(self) -> str:
        winner = _winner(self.local_status)
        if winner:
            return winner
        return "active" if "." in self.local_status else "draw"

    @property
    def ply(self) -> int:
        return 81 - self.board.count(".")


def legal_moves(state: State) -> tuple[int, ...]:
    """先判定終局及指定盤，再列出所有實際合法位置。"""
    if state.rule_version != RULE_VERSION:
        raise ValueError(f"不支援的規則版本：{state.rule_version}")
    if state.status != "active":
        return ()
    bigs = (state.forced,) if state.forced else range(1, 10)
    return tuple(10 * big + small
                 for big in bigs if state.local_status[big - 1] == "."
                 for small in range(1, 10)
                 if state.board[(big - 1) * 9 + small - 1] == ".")


def play(state: State, move: int) -> State:
    """接受一手合法落子；終局也切換 turn，但不再提供合法動作。"""
    index = move_index(move)
    if move not in legal_moves(state):
        if state.status != "active":
            raise InvalidMove("整場已結束，不能繼續落子。")
        big = move // 10
        if state.local_status[big - 1] != ".":
            raise InvalidMove("這個小盤已封閉。")
        if state.board[index] != ".":
            raise InvalidMove("這個小格已經有棋子。")
        raise InvalidMove(f"本手必須在大格 {state.forced} 落子。")
    board = state.board[:index] + state.turn + state.board[index + 1:]
    return State(board=board, turn="O" if state.turn == "X" else "X",
                 forced=move % 10, rule_version=state.rule_version)


def state_to_dict(state: State) -> dict:
    return {"board": state.board, "turn": state.turn, "forced": state.forced,
            "rule_version": state.rule_version,
            "local_status": list(state.local_status), "status": state.status,
            "ply": state.ply}


def state_from_dict(data: Mapping) -> State:
    """讀取局面並核對提供的衍生欄位；可達性另以原始棋譜驗證。"""
    if not isinstance(data, Mapping):
        raise ValueError("局面資料必須是欄位對照表。")
    required = ("board", "turn", "forced", "rule_version")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError("局面缺少欄位：" + ", ".join(missing))
    state = State(**{key: data[key] for key in required})
    if state.forced != data["forced"]:
        raise ValueError("儲存的指定盤與棋盤的封閉／終局狀態不一致。")
    expected = state_to_dict(state)
    for key in ("local_status", "status", "ply"):
        if key in data:
            if key == "local_status" and not isinstance(data[key], (list, tuple)):
                raise ValueError("儲存的 local_status 必須是九個小盤狀態的串列。")
            if key == "ply" and type(data[key]) is not int:
                raise ValueError("儲存的 ply 必須是整數。")
            value = list(data[key]) if key == "local_status" else data[key]
            if value != expected[key]:
                raise ValueError(f"儲存的 {key} 與棋盤不一致。")
    return state


def replay(moves: Iterable[int]) -> list[State]:
    """依原始方向重播，回傳含空盤在內的全部局面。"""
    states = [State()]
    for move in moves:
        states.append(play(states[-1], move))
    return states
