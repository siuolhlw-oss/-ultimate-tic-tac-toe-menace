/* 封閉小盤版超級井字棋。原始資料固定 X 先手；顯示符號由介面處理。 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.UTTT = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const RULE_VERSION = "uttt-closed-v1";
  const LINES = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6],
    [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]];
  const positionMaps = Array.from({length: 8}, (_, t) =>
    Array.from({length: 9}, (_, i) => {
      let row = Math.floor(i / 3), col = i % 3;
      if (t >= 4) col = 2 - col;
      for (let k = 0; k < t % 4; k++) [row, col] = [col, 2 - row];
      return row * 3 + col + 1;
    }));
  const boardMaps = positionMaps.map(map => Array.from({length: 81}, (_, i) =>
    (map[Math.floor(i / 9)] - 1) * 9 + map[i % 9] - 1));

  function winner(cells) {
    const winners = new Set();
    for (const [a, b, c] of LINES) {
      if ((cells[a] === "X" || cells[a] === "O") &&
          cells[a] === cells[b] && cells[a] === cells[c]) winners.add(cells[a]);
    }
    if (winners.size > 1) throw new Error("同一棋盤不能同時由 X 與 O 連線。");
    return winners.size ? [...winners][0] : null;
  }

  // 如 Python State：只驗證結構；任意外部棋譜的可達性仍須 replay。
  function makeState(board, turn, forced, ruleVersion) {
    if (typeof board !== "string" || !/^[.XO]{81}$/.test(board))
      throw new Error("棋盤必須是 81 個 .、X、O 字元。");
    if (turn !== "X" && turn !== "O") throw new Error("行動方必須是 X 或 O。");
    if (!Number.isInteger(forced) || forced < -1 || forced > 9)
      throw new Error("指定盤必須為 -1、0 或 1–9。");
    if (typeof ruleVersion !== "string" || !ruleVersion)
      throw new Error("規則版本不可為空。");
    const locals = Array.from({length: 9}, (_, i) => {
      const cells = board.slice(i * 9, i * 9 + 9);
      return winner(cells) || (cells.includes(".") ? "." : "D");
    });
    const status = winner(locals) || (locals.includes(".") ? "active" : "draw");
    if (status !== "active") forced = -1;
    else if (forced === -1) throw new Error("尚未終局不能使用終局指定盤標記。");
    else if (forced > 0 && locals[forced - 1] !== ".") forced = 0;
    return Object.freeze({board, turn, forced, rule_version: ruleVersion,
      local_status: Object.freeze(locals), status, ply: board.replace(/\./g, "").length});
  }

  function checked(state) {
    if (!state || typeof state !== "object") throw new Error("缺少完整局面。");
    const clean = makeState(state.board, state.turn, state.forced, state.rule_version);
    if (clean.forced !== state.forced) throw new Error("指定盤與封閉／終局狀態不一致。");
    for (const key of ["local_status", "status", "ply"]) {
      if (Object.prototype.hasOwnProperty.call(state, key) &&
          JSON.stringify(state[key]) !== JSON.stringify(clean[key]))
        throw new Error("儲存的 " + key + " 與棋盤不一致。");
    }
    return clean;
  }

  function moveIndex(move) {
    if (!Number.isInteger(move)) throw new Error("落子位置必須是兩位數整數。");
    const big = Math.floor(move / 10), small = move % 10;
    if (big < 1 || big > 9 || small < 1 || small > 9)
      throw new Error("位置的十位及個位必須均為 1–9。");
    return (big - 1) * 9 + small - 1;
  }

  function checkTransform(t) {
    if (!Number.isInteger(t) || t < 0 || t > 7)
      throw new Error("D4 轉換代號必須是 0–7 的整數。");
  }

  function initial() { return makeState(".".repeat(81), "X", 0, RULE_VERSION); }

  function movesOf(state) {
    if (state.rule_version !== RULE_VERSION) throw new Error("不支援的規則版本。");
    if (state.status !== "active") return [];
    const moves = [];
    for (let big = 1; big <= 9; big++) {
      if ((state.forced && state.forced !== big) || state.local_status[big - 1] !== ".") continue;
      for (let small = 1; small <= 9; small++) {
        if (state.board[(big - 1) * 9 + small - 1] === ".") moves.push(10 * big + small);
      }
    }
    return moves;
  }

  function legalMoves(state) { return movesOf(checked(state)); }

  function play(state, move) {
    const index = moveIndex(move), clean = checked(state);
    if (!movesOf(clean).includes(move)) throw new Error("不合法落子：" + move);
    return makeState(clean.board.slice(0, index) + clean.turn + clean.board.slice(index + 1),
      clean.turn === "X" ? "O" : "X", move % 10, clean.rule_version);
  }

  function replay(moves) {
    if (!Array.isArray(moves)) throw new Error("棋譜必須為依序落子陣列。");
    const states = [initial()];
    for (const move of moves) states.push(play(states[states.length - 1], move));
    return states;
  }

  function transformMove(move, t) {
    moveIndex(move); checkTransform(t);
    const map = positionMaps[t];
    return 10 * map[Math.floor(move / 10) - 1] + map[move % 10 - 1];
  }

  function parts(state, t) {
    const board = new Array(81);
    for (let source = 0; source < 81; source++) board[boardMaps[t][source]] = state.board[source];
    return {board: board.join(""), forced: state.forced > 0 ? positionMaps[t][state.forced - 1] : state.forced};
  }

  function transformState(state, t) {
    checkTransform(t);
    const clean = checked(state), transformed = parts(clean, t);
    return makeState(transformed.board, clean.turn, transformed.forced, clean.rule_version);
  }

  function canonical(state) {
    const clean = checked(state);
    let best = parts(clean, 0), transform = 0;
    for (let t = 1; t < 8; t++) {
      const candidate = parts(clean, t);
      if (candidate.board < best.board || (candidate.board === best.board && candidate.forced < best.forced)) {
        best = candidate; transform = t;
      }
    }
    const normalized = makeState(best.board, clean.turn, best.forced, clean.rule_version);
    // 網頁專用 compact key；不是 SQLite 中含衍生欄位的 Python JSON key。
    return {state: normalized, transform,
      key: normalized.board + "|" + normalized.turn + "|" + normalized.forced + "|" + normalized.rule_version};
  }

  function actionGroups(state) {
    const clean = checked(state), moves = movesOf(clean), allowed = new Set(moves);
    const stabilizer = [];
    for (let t = 0; t < 8; t++) {
      const transformed = parts(clean, t);
      if (transformed.board === clean.board && transformed.forced === clean.forced) stabilizer.push(t);
    }
    const remaining = new Set(moves), groups = [];
    while (remaining.size) {
      const rep = Math.min(...remaining);
      const members = [...new Set(stabilizer.map(t => transformMove(rep, t)))].sort((a, b) => a - b);
      if (members.some(move => !allowed.has(move))) throw new Error("同構分組包含非法動作。");
      groups.push({rep: members[0], members});
      for (const move of members) remaining.delete(move);
    }
    return groups;
  }

  function canonicalHistory(moves) {
    if (!Array.isArray(moves)) throw new Error("棋譜必須為依序落子陣列。");
    // 每個候選整場固定同一個 D4 變換，不逐手重新轉向。
    let best = null;
    for (let t = 0; t < 8; t++) {
      const candidate = moves.map(move => String(transformMove(move, t))).join("");
      if (best === null || candidate < best) best = candidate;
    }
    return best;
  }

  return Object.freeze({RULE_VERSION, initial, legalMoves, play, replay,
    transformMove, transformState, canonical, actionGroups, canonicalHistory});
});
