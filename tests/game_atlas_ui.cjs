/* 純 DOM 互動回歸：使用 jsdom，不啟動瀏覽器，也不宣稱完成目視版面 QA。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '..');
const site = path.join(root, 'web', 'game-atlas');
const requireSite = createRequire(path.join(site, 'package.json'));
const {JSDOM, VirtualConsole} = requireSite('jsdom');
const E = require(path.join(site, 'engine.js'));
const dataPath = path.join(root, 'exports', 'game-atlas', 'data.js');
if (!fs.existsSync(dataPath)) throw new Error('請先執行 tools/build_game_atlas.py 產生 1,000 局圖譜資料。');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', error => errors.push(String(error)));
const dom = new JSDOM(fs.readFileSync(path.join(site, 'index.html'), 'utf8'), {
  url: 'http://localhost/game-atlas/index.html', runScripts: 'outside-only',
  pretendToBeVisual: true, virtualConsole: vc,
});
const w = dom.window, d = w.document;
w.scrollTo = () => {}; // jsdom 無版面捲動；此測試只核對導覽與 DOM。
w.addEventListener('error', event => errors.push(String(event.error || event.message)));
const timers = new Map(); let timerId = 0;
w.setInterval = callback => {timers.set(++timerId, callback); return timerId;};
w.clearInterval = id => timers.delete(id);
function freezeDeep(value) {
  if (value && typeof value === 'object') {
    Object.freeze(value);
    for (const child of Object.values(value)) freezeDeep(child);
  }
}
w.eval(fs.readFileSync(path.join(site, 'engine.js'), 'utf8'));
w.eval(fs.readFileSync(dataPath, 'utf8'));
const DATA = w.UTTT_DATA;
const beforeData = JSON.stringify(DATA);
const sourceHash = crypto.createHash('sha256').update(fs.readFileSync(dataPath)).digest('hex');
freezeDeep(DATA); // 嚴格模式下任何誤寫原始資料都應報错。
w.eval(fs.readFileSync(path.join(site, 'app.js'), 'utf8'));
const $ = id => d.getElementById(id);
const click = id => {const el = $(id); assert(el, id); assert(!el.disabled, id + ' 不應停用'); el.click();};
const text = id => $(id).textContent;
const roots = () => [...$('tree').querySelectorAll(':scope > details.branch')];
const symbol = actor => actor === 'X' ? '○' : '×';
let boardChecks = 0;
function change(id, value) {
  const el = $(id);
  if (typeof value === 'boolean') el.checked = value; else el.value = String(value);
  el.dispatchEvent(new w.Event('change', {bubbles: true}));
}
function range(value) {
  $('stepRange').value = String(value);
  $('stepRange').dispatchEvent(new w.Event('input', {bubbles: true}));
}
function assertBoard(moves) {
  const state = E.replay(moves).at(-1), legal = E.legalMoves(state);
  const cells = [...$('board').querySelectorAll('button.cell')];
  assert.equal(cells.length, 81);
  assert.deepEqual(cells.filter(cell => !cell.disabled).map(cell => Number(cell.dataset.move)), legal);
  assert.equal($('board').querySelectorAll('.piece').length, moves.length);
  const order = new Map(moves.map((move, index) => [move, index + 1]));
  for (const cell of cells) {
    const move = Number(cell.dataset.move), ply = order.get(move);
    if (ply) {
      const actor = state.board[(Math.floor(move / 10) - 1) * 9 + move % 10 - 1];
      assert.equal(cell.querySelector('.piece').textContent, symbol(actor));
      assert.equal(cell.querySelector('.move-order').textContent, String(ply));
      assert.equal(symbol(actor), ply % 2 ? '○' : '×');
      assert.equal(cell.getAttribute('aria-label'), `第 ${ply} 手 ${symbol(actor)}，位置 ${move}`);
    } else assert.equal(cell.querySelector('.piece'), null);
    assert.equal(cell.classList.contains('last'), move === moves.at(-1));
  }
  assert.equal(text('branchCount'), `${legal.length} 個合法位置 / ${E.actionGroups(state).length} 個同構組`);
  assert.equal(Number($('stepRange').value), moves.length);
  assert.equal($('breadcrumbs').querySelectorAll('button').length, moves.length + 1);
  boardChecks++;
}

async function main() {
  const checks = [];
  assert.equal(DATA.games.length, 1000);
  assert.equal($('gameSelect').options.length, 1000);
  assert.equal($('loadError').hidden, true);
  assertBoard([]); assert.equal(roots().length, 15);
  assert.equal($('prevGame').disabled, true);
  assert.equal(text('sampleStatus').includes('1,000'), true);
  checks.push('空盤有 81 個合法位置及 15 個同構組');

  // 展開兩層樹，但不切換目前棋盤；每層都由規則引擎計算。
  const branch = roots()[0]; branch.open = true; branch.dispatchEvent(new w.Event('toggle'));
  assert(branch.querySelector('.branch-content .member-list button'));
  const child = branch.querySelector('.children > details.branch');
  assert(child); child.open = true; child.dispatchEvent(new w.Event('toggle'));
  assert(child.querySelector('.branch-content'));
  assert.equal(Number($('stepRange').value), 0);
  const firstRep = E.actionGroups(E.initial())[0].rep;
  branch.querySelector(':scope > summary .jump-node').click();
  assertBoard([firstRep]);
  click('toStart');
  change('groupSymmetry', false); assert.equal(roots().length, 81);
  change('groupSymmetry', true); assert.equal(roots().length, 15);
  checks.push('遞迴樹分支、走這一步與取消同構合併');

  change('gameSelect', 0);
  const game = DATA.games[0];
  for (let ply = 1; ply <= game.moves.length; ply++) {
    click('stepNext'); assertBoard(game.moves.slice(0, ply));
  }
  assert.equal($('stepNext').disabled, true);
  assert.equal($('toEnd').disabled, true);
  assert(text('turnStatus').includes('整局結束'));
  assert.equal(roots().length, 0);
  assert(text('tree').includes('沒有後續合法落子'));
  click('stepBack'); assertBoard(game.moves.slice(0, -1));
  assert.equal($('stepNext').disabled, false);
  range(17); assertBoard(game.moves.slice(0, 17));
  click('toEnd'); assertBoard(game.moves);
  click('toStart'); assertBoard([]);
  checks.push('完整原局逐手、拉桿、終局、上一手及首末導覽');

  // 真實計时由測試控制，核對播放／暫停，不等待整局動畫。
  click('playPause'); assert.equal(timers.size, 1);
  assert.equal(text('playPause'), '暫停');
  [...timers.values()][0](); assertBoard(game.moves.slice(0, 1));
  click('playPause'); assert.equal(timers.size, 0);
  assert.equal(text('playPause'), '播放');
  click('toStart');
  checks.push('自動播放及暫停');

  // 從原始方向的樣本路徑找一個尚未觀察過的合法分支。
  let unseen = null;
  for (let depth = 0; depth < Math.min(12, game.moves.length) && !unseen; depth++) {
    const prefix = Array.from(game.moves.slice(0, depth));
    for (const move of E.legalMoves(E.replay(prefix).at(-1))) {
      const candidate = [...prefix, move];
      if (!DATA.games.some(g => candidate.every((value, i) => value === g.moves[i]))) {unseen = candidate; break;}
    }
  }
  assert(unseen, '應能找到合法但未在樣本出現的前綴');
  for (const move of unseen) {
    const button = $('board').querySelector(`[data-move="${move}"]`);
    assert(button && !button.disabled); button.click();
  }
  assertBoard(unseen);
  assert.equal(text('pathBadge'), '合法分支預覽');
  assert(text('sampleStatus').includes('本批 0 局'));
  change('branchFilter', 'observed');
  assert.equal(roots().length, 0); assert(text('tree').includes('沒有後續樣本'));
  change('branchFilter', 'all'); assert(roots().length > 0);
  const extra = E.legalMoves(E.replay(unseen).at(-1))[0];
  $('board').querySelector(`[data-move="${extra}"]`).click(); assertBoard([...unseen, extra]);
  click('restoreGame'); assertBoard(game.moves.slice(0, unseen.length + 1));
  assert.equal(text('pathBadge'), '原始棋譜');
  // 原始前綴篩選只保留至少有一個樣本的組。
  click('toStart'); change('branchFilter', 'observed');
  assert(roots().length > 0);
  assert(roots().every(node => !node.querySelector(':scope > summary').textContent.includes('本批未走過')));
  change('branchFilter', 'all');
  checks.push('棋盤選分支、零樣本可繼續探索、樣本篩選及返回原棋譜');

  // 圖集使用全部 1,000 筆，但每頁僅插入 20 張圖片。
  d.querySelector('[data-view="gallery"]').click();
  assert.equal($('galleryView').hidden, false); assert.equal($('exploreView').hidden, true);
  assert.equal($('galleryGrid').querySelectorAll('.gallery-card').length, 20);
  $('galleryJump').value = '1000'; click('jumpGallery');
  assert(text('galleryPage').includes('第 50 / 50 頁'));
  assert(text('galleryPage').includes('981–1000'));
  assert.equal($('galleryNext').disabled, true);
  const lastCard = [...$('galleryGrid').querySelectorAll('.gallery-card')].at(-1);
  assert(lastCard.querySelector('img').src.endsWith('/images/game-1000.png'));
  assert(lastCard.querySelector('a[download]').download === 'game-1000.png');
  lastCard.querySelector('button').click();
  assert.equal($('exploreView').hidden, false);
  assert.equal($('galleryView').hidden, true);
  assert.equal($('gameSelect').value, '999'); assert.equal($('nextGame').disabled, true);
  assert(text('gameNumber').includes('1000 / 1000'));
  assertBoard([]); click('toEnd'); assertBoard(DATA.games[999].moves);
  assert($('currentImage').href.endsWith('/images/game-1000.png'));
  click('prevGame'); assert.equal($('gameSelect').value, '998');
  click('nextGame'); assert.equal($('gameSelect').value, '999');
  checks.push('圖集最後一頁、1,000 局選擇、圖片連結及局次前後導覽');

  d.querySelector('[data-view="counts"]').click();
  assert.equal($('countsView').hidden, false); assert.equal($('exploreView').hidden, true);
  assert.equal(d.querySelector('[data-view="counts"]').getAttribute('aria-selected'), 'true');
  assert(text('quickStats').includes('1,000')); assert(text('quickStats').includes('58,733'));
  assert(text('quickStats').includes('34–77'));
  const cards = [...$('countCards').querySelectorAll('.count-card strong')].map(el => el.textContent.replace(/,/g, ''));
  assert.deepEqual(cards.map(Number), [DATA.summary.distinctGamesD4, DATA.summary.observedStatesD4, DATA.summary.observedPrefixesD4IncludingEmpty]);
  assert.equal(DATA.summary.observedStatesD4, 56161);
  assert.equal($('lengthTable').rows.length, 38);
  assert.equal($('histogram').children.length, 44);
  assert.equal([...$('lengthTable').rows].reduce((total,row) => total + Number(row.cells[1].textContent), 0), 1000);
  assert.equal(text('lengthSummary'), '38 種終局手數 · 34 至 77 手');
  assert($('countsView').textContent.includes('81 是保守的上界'));
  assert($('countsView').textContent.includes('目前仍未求得精確總數'));
  checks.push('計數摘要、38 種手數分布、有限上界與未求得精確總數說明');

  await new Promise(resolve => setTimeout(resolve, 30));
  assert.deepEqual(errors, []);
  assert.equal(JSON.stringify(DATA), beforeData);
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(dataPath)).digest('hex'), sourceHash);
  console.log(JSON.stringify({status: 'passed', kind: 'jsdom DOM regression (not visual browser QA)',
    games: DATA.games.length, boardChecks, checks, rawDataUnchanged: true, consoleErrors: errors.length}, null, 2));
  dom.window.close();
}
main().catch(error => {console.error(error);dom.window.close();process.exitCode = 1;});
