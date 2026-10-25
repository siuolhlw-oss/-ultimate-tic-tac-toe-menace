"""27-byte 完整局面鍵；固定且明確包含棋規及 D4 版本。"""
import json

from uttt.engine import State, RULE_VERSION, state_from_dict, state_to_dict
from uttt.symmetry import CANONICAL_VERSION
from uttt.service import dumps


def pack(state):
    if state.rule_version != RULE_VERSION or CANONICAL_VERSION != 'd4-v1':
        raise ValueError('不支援的規則／同構版本。')
    number = 0
    for i, cell in enumerate(state.board):
        number |= '.XO'.index(cell) << (2 * i)
    flags = state.forced + 1 | (16 if state.turn == 'O' else 0)
    return number.to_bytes(21, 'little') + bytes([flags]) + (1).to_bytes(4, 'little') + b'\x01'


def unpack(key):
    if not isinstance(key, bytes) or len(key) != 27 or key[22:] != b'\x01\x00\x00\x00\x01':
        raise ValueError('局面鍵長度或版本不符。')
    number, flags = int.from_bytes(key[:21], 'little'), key[21]
    if number >> 162 or flags & ~31 or (flags & 15) > 10:
        raise ValueError('局面鍵含無效位元。')
    values = [(number >> (2 * i)) & 3 for i in range(81)]
    if 3 in values:
        raise ValueError('局面鍵含無效棋子。')
    forced = (flags & 15) - 1
    state = State(''.join('.XO'[x] for x in values), 'O' if flags & 16 else 'X', forced)
    if state.forced != forced:
        raise ValueError('指定盤與封閉／終局狀態不符。')
    return state


def from_legacy(text):
    data = json.loads(text)
    if data.get('canonical_version') != CANONICAL_VERSION:
        raise ValueError('來源同構版本不符。')
    state = state_from_dict(data)
    key = pack(state)
    if legacy_key(key) != text:
        raise ValueError('局面鍵無法無損往返。')
    return key


def legacy_key(key):
    data = state_to_dict(unpack(key))
    data['canonical_version'] = CANONICAL_VERSION
    return dumps(data)
