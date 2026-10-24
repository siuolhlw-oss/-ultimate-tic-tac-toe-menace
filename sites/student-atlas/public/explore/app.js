/* 唯讀圖譜：顯示符號映射獨立於規則和 D4 計數。 */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const E = window.UTTT, D = window.UTTT_DATA;
  if (!E || !D || D.ruleVersion !== 'uttt-closed-v1' || !D.games?.length) {
    $('loadError').hidden = false;
    $('loadError').textContent = '資料尚未生成或載入失敗。請先執行 build_game_atlas.py，並從匯出資料夾開啟本頁。';
    return;
  }
  const format = (n) => Number(n).toLocaleString('zh-TW');
  const pad = (n) => String(n).padStart(4, '0');
  const glyph = (actor) => actor === 'X' ? '○' : '×';
  const resultLabel = (r) => r === 'draw' ? '和局' : `${glyph(r)} ${r === 'X' ? '先手' : '後手'}獲勝`;
  const imagePath = (game) => window.UTTT_FIGURES.url(game);
  const state = {game: 0, trail: D.games[0].moves.slice(), cursor: 0, galleryPage: 0, timer: null, treeNodes: 0};
  const pageSize = 20;
  const pathNow = () => state.trail.slice(0, state.cursor);
  const samePrefix = (full, prefix) => prefix.length <= full.length && prefix.every((m, i) => full[i] === m);
  const matching = (path, candidates = D.games) => candidates.filter(g => samePrefix(g.moves, path));
  const knownNext = (games, ply) => {
    const counts = new Map();
    for (const g of games) if (g.moves.length > ply) counts.set(g.moves[ply], (counts.get(g.moves[ply]) || 0) + 1);
    return counts;
  };
  const element = (tag, className, text) => {const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el;};
  function stop() {if (state.timer) clearInterval(state.timer); state.timer = null; $('playPause').textContent = '播放';}
  function showView(view) {
    stop();
    for (const name of ['explore', 'gallery', 'counts', 'feedback']) $(name + 'View').hidden = name !== view;
    document.querySelectorAll('.tabs button').forEach(b => {b.classList.toggle('active', b.dataset.view === view); b.setAttribute('aria-selected', String(b.dataset.view === view));});
    if (view === 'gallery') renderGallery(); window.dispatchEvent(new CustomEvent('uttt:view',{detail:view}));
  }
  function selectGame(index, final = false) {
    stop(); state.game = Math.max(0, Math.min(D.games.length - 1, index));
    state.trail = D.games[state.game].moves.slice(); state.cursor = final ? state.trail.length : 0;
    $('gameSelect').value = String(state.game); render();
  }
  function navigate(path) {
    stop();
    if (samePrefix(state.trail, path)) state.cursor = path.length;
    else {state.trail = path.slice(); state.cursor = path.length;}
    render();
  }
  function moveHere(move) {const path = pathNow(); E.play(E.replay(path).at(-1), move); navigate([...path, move]);}
  function miniBoard(s, size = 44) {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 90 90'); svg.setAttribute('width', size); svg.setAttribute('height', size); svg.classList.add('mini-board'); svg.setAttribute('aria-hidden', 'true');
    for (let b = 0; b < 9; b++) for (let c = 0; c < 9; c++) {
      const x = (b % 3) * 30 + (c % 3) * 10, y = Math.floor(b / 3) * 30 + Math.floor(c / 3) * 10;
      const cell = document.createElementNS(ns, 'rect'); cell.setAttribute('x', x + .7); cell.setAttribute('y', y + .7); cell.setAttribute('width', 8.6); cell.setAttribute('height', 8.6);
      cell.setAttribute('fill', s.board[b * 9 + c] === 'X' ? '#399499' : s.board[b * 9 + c] === 'O' ? '#c78064' : '#ecede4'); svg.append(cell);
    }
    for (const n of [30,60]) {for(const vertical of [true,false]) {const line = document.createElementNS(ns,'line'); line.setAttribute('x1',vertical?n:0);line.setAttribute('x2',vertical?n:90);line.setAttribute('y1',vertical?0:n);line.setAttribute('y2',vertical?90:n);line.setAttribute('stroke','#718272');line.setAttribute('stroke-width',1.5);svg.append(line);}}
    return svg;
  }
  function renderBoard(s, path) {
    const legal = new Set(E.legalMoves(s)), orders = new Map(path.map((m,i)=>[m,i+1])); $('board').replaceChildren();
    for (let b = 1; b <= 9; b++) {
      const status = s.local_status[b-1], box = element('div','local-board');
      box.classList.toggle('target', s.status === 'active' && (s.forced === b || s.forced === 0 && status === '.'));
      if (status !== '.') box.classList.add('closed'); if (status === 'X' || status === 'O') box.classList.add('won-'+status);
      const head = element('div','local-head'); head.append(element('span','',String(b)),element('span','',status==='.'?'':status==='D'?'和局':glyph(status)+' 取得')); box.append(head);
      const grid = element('div','local-grid');
      for (let c = 1; c <= 9; c++) {
        const move = b*10+c, actor = s.board[(b-1)*9+c-1], order=orders.get(move); const cell=element('button','cell');cell.type='button';cell.disabled=!legal.has(move);cell.dataset.move=move;
        if (actor !== '.') {cell.append(element('span','piece '+(actor==='X'?'circle':'cross'),glyph(actor)),element('span','move-order',String(order)));}
        else {cell.append(element('span','coordinate',String(move)));if(legal.has(move))cell.classList.add('playable');}
        cell.classList.toggle('last',move===path.at(-1));
        cell.setAttribute('aria-label',order?`第 ${order} 手 ${glyph(actor)}，位置 ${move}`:`位置 ${move}，${legal.has(move)?'可落子':'不可落子'}`);cell.title=cell.getAttribute('aria-label');cell.addEventListener('click',()=>moveHere(move));grid.append(cell);
      }
      box.append(grid);$('board').append(box);
    }
  }
  function targetLabel(s) {return s.status!=='active'?resultLabel(s.status):s.forced?`下一手指定大格 ${s.forced}`:'下一手自由選盤';}
  function groupsFor(s) {return $('groupSymmetry').checked?E.actionGroups(s):E.legalMoves(s).map(m=>({rep:m,members:[m]}));}
  function renderBranches(container, s, path, candidates) {
    const next=knownNext(candidates,path.length), original=D.games[state.game];
    const actualNext=samePrefix(original.moves,path)?original.moves[path.length]:undefined;
    const observedOnly=$('branchFilter').value==='observed';
    const groups=groupsFor(s).filter(g=>!observedOnly||g.members.some(m=>next.has(m)));
    if (!groups.length) {container.append(element('div','empty',s.status!=='active'?`已到終局：${resultLabel(s.status)}。沒有後續合法落子。`:'這條路徑沒有後續樣本；切換「所有合法下法」可繼續探索。'));return;}
    for (const group of groups) {
      if (state.treeNodes>=600) {container.append(element('div','empty','目前已展開 600 個節點。請點「走這一步」以該局面繼續探索，並釋放上層畫面。'));break;} state.treeNodes++;
      const representative=observedOnly?(group.members.includes(actualNext)?actualNext:group.members.find(m=>next.has(m))):group.rep;
      const nextPath=[...path,representative], child=E.play(s,representative); const count=group.members.reduce((n,m)=>n+(next.get(m)||0),0);
      const details=element('details','branch');if(group.members.includes(actualNext))details.classList.add('current-choice');
      const summary=element('summary');summary.append(miniBoard(child));const body=element('div','node-body');
      body.append(element('div','node-title',`${glyph(s.turn)} 第 ${path.length+1} 手 · 位置 ${representative}`));
      const meta=element('div','node-meta');meta.append(element('span','',group.members.length>1?`${group.members.length} 個等價位置`:'1 個位置'),element('span','',count?`本批走過 ${count} 局`:'本批未走過'),element('span','',targetLabel(child)));body.append(meta);summary.append(body);
      const enter=element('button','jump-node','走這一步');enter.addEventListener('click',e=>{e.preventDefault();e.stopPropagation();navigate(nextPath);});summary.append(enter);details.append(summary);
      let built=false;
      details.addEventListener('toggle',()=>{if(!details.open||built)return;built=true;const content=element('div','branch-content');const members=element('div','member-list');members.append(element('span','',group.members.length>1?'選擇組內位置：':'位置：'));
        for(const m of group.members){const button=element('button',next.has(m)?'seen':'',String(m));if(m===actualNext)button.classList.add('recorded');button.title=`位置 ${m}，本批 ${next.get(m)||0} 局${m===actualNext?'，所選棋譜的下一手':''}`;button.addEventListener('click',()=>navigate([...path,m]));members.append(button);}
        content.append(members,element('p','representative-note',`下方展開代表位置 ${representative}；同組其他位置仍可個別選擇。`));const children=element('div','children');renderBranches(children,child,nextPath,matching(nextPath,candidates));content.append(children);details.append(content);
      });container.append(details);
    }
  }
  function renderTree(s,path) {
    state.treeNodes=0;const candidates=matching(path), legal=E.legalMoves(s);$('branchCount').textContent=`${legal.length} 個合法位置 / ${E.actionGroups(s).length} 個同構組`;
    $('sampleStatus').textContent=`這條原始方向的路徑，在本批 ${format(candidates.length)} 局中出現。綠色框標出所選棋譜的下一手所在組。`;
    $('breadcrumbs').replaceChildren();for(let i=0;i<=path.length;i++){if(i)$('breadcrumbs').append(element('span','','→'));const b=element('button','',i?`${glyph(i%2?'X':'O')}${i} · ${path[i-1]}`:'空盤');b.addEventListener('click',()=>{stop();state.cursor=i;render();});$('breadcrumbs').append(b);} $('breadcrumbs').scrollLeft=$('breadcrumbs').scrollWidth;
    $('treeRoot').replaceChildren(miniBoard(s,42));const text=element('div');text.append(element('div','',path.length?`目前局面 · 已下 ${path.length} 手`:'目前局面 · 空盤'),element('small','',s.status==='active'?`${glyph(s.turn)} 行動 · ${targetLabel(s)}`:targetLabel(s)));$('treeRoot').append(text);
    $('tree').replaceChildren();renderBranches($('tree'),s,path,candidates);
  }
  function render() {
    const game=D.games[state.game],path=pathNow(),s=E.replay(path).at(-1);const follows=samePrefix(game.moves,path);
    $('gameNumber').textContent=`GAME ${pad(game.index)} / ${pad(D.games.length)}`;$('stepTitle').textContent=path.length?`第 ${path.length} 手`:'從空盤開始';
    $('pathBadge').textContent=follows?'原始棋譜':'合法分支預覽';$('pathBadge').classList.toggle('preview',!follows);
    $('turnStatus').textContent=s.status==='active'?`${glyph(s.turn)} 準備落子 · ${s.forced?'必須下在第 '+s.forced+' 大格':'可自由選擇未封閉的大格'}`:`整局結束 · ${resultLabel(s.status)}`;
    $('currentImage').href=imagePath(game);$('gameDetails').textContent=`原始棋譜：${game.ply} 手 · ${resultLabel(game.result)}。○：${game.xModel}；×：${game.oModel}。`;
    $('prevGame').disabled=state.game===0;$('nextGame').disabled=state.game===D.games.length-1;
    $('stepRange').max=String(state.trail.length);$('stepRange').value=String(state.cursor);$('rangeLabel').textContent=`${state.cursor} / ${state.trail.length}`;
    $('stepBack').disabled=$('toStart').disabled=state.cursor===0;$('stepNext').disabled=$('toEnd').disabled=state.cursor===state.trail.length;
    $('playPause').disabled=state.cursor===state.trail.length&&!state.timer;
    renderBoard(s,path);renderTree(s,path); window.STUDENT_CONTEXT={gameIndex:game.index,ply:path.length,path:[...path],isOriginal:follows}; $('gameJump').value=String(game.index);
  }
  function renderGallery() {
    const start=state.galleryPage*pageSize;$('galleryGrid').replaceChildren();
    D.games.slice(start,start+pageSize).forEach((game,offset)=>{const card=element('article','gallery-card'),anchor=element('a');anchor.href=imagePath(game);anchor.target='_blank';anchor.rel='noopener';const img=element('img');img.src=imagePath(game);img.loading='lazy';img.alt=`第${game.index}局，${game.ply}手，含所有落子順序的完整棋譜`;anchor.append(img);card.append(anchor);const caption=element('div','gallery-caption');caption.append(element('strong','',`第 ${pad(game.index)} 局`),element('p','',`${game.ply} 手 · ${resultLabel(game.result)}`));const actions=element('div','gallery-actions'),button=element('button','','逐步探索');button.addEventListener('click',()=>{selectGame(start+offset);showView('explore');window.scrollTo({top:0,behavior:'instant'});});const link=element('a','','下載SVG棋譜 ↗');link.href=imagePath(game);link.download=`game-${pad(game.index)}.svg`;actions.append(button,link);caption.append(actions);card.append(caption);$('galleryGrid').append(card);});
    $('galleryPage').textContent=`第 ${state.galleryPage+1} / ${Math.ceil(D.games.length/pageSize)} 頁 · ${start+1}–${Math.min(start+pageSize,D.games.length)} 局`;$('galleryPrev').disabled=state.galleryPage===0;$('galleryNext').disabled=start+pageSize>=D.games.length;
  }
  function renderCounts() {
    const s=D.summary;$('quickStats').replaceChildren();for(const [n,label] of [[format(s.games),'完整棋譜'],[format(s.moves),'原始落子'],[`${s.minPly}–${s.maxPly}`,'樣本終局手數'],[String(s.firstMoveD4),'開局同構類']]){const box=element('div','stat');box.append(element('strong','',n),element('span','',label));$('quickStats').append(box);}
    const cards=[['完整棋譜',s.distinctGamesD4,'保留整場順序；對整場使用固定的全盤 D4 變換去重。'],['觀察到的完整局面',s.observedStatesD4,'D4 去重，包含空盤及終局；不區分到達順序。'],['觀察到的歷史前綴',s.observedPrefixesD4IncludingEmpty,'保留從空盤到此刻的落子順序，再做整段 D4 去重；包含空前綴。']];
    $('countCards').replaceChildren();for(const [name,n,note] of cards){const c=element('article','count-card');c.append(element('p','eyebrow','本批樣本 / 同構後'),element('h3','',name),element('strong','',format(n)),element('p','',note));$('countCards').append(c);}
    $('lengthSummary').textContent=`${s.distinctLengths} 種終局手數 · ${s.minPly} 至 ${s.maxPly} 手`;
    const histogram=new Map(s.lengthHistogram.map(r=>[r.ply,r.games])),max=Math.max(...histogram.values());$('histogram').replaceChildren();$('lengthTable').replaceChildren();
    for(let ply=s.minPly;ply<=s.maxPly;ply++){const n=histogram.get(ply)||0;const wrap=element('div','bar-wrap'),bar=element('div','bar');bar.style.height=(100*n/max)+'%';bar.tabIndex=0;bar.title=`${ply} 手：${n} 局`;bar.setAttribute('aria-label',bar.title);wrap.append(bar);if(ply%2===0||ply===s.maxPly)wrap.append(element('label','',String(ply)));$('histogram').append(wrap);if(n){const row=element('tr');row.append(element('td','',String(ply)),element('td','',String(n)));$('lengthTable').append(row);}}
  }
  D.games.forEach((g,i)=>{const option=element('option','',`第 ${pad(g.index)} 局 · ${g.ply} 手 · ${resultLabel(g.result)}`);option.value=String(i);$('gameSelect').append(option);});
  document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));
  $('gameSelect').addEventListener('change',e=>selectGame(Number(e.target.value)));$('prevGame').onclick=()=>selectGame(state.game-1);$('nextGame').onclick=()=>selectGame(state.game+1);
  $('toStart').onclick=()=>{stop();state.cursor=0;render();};$('toEnd').onclick=()=>{stop();state.cursor=state.trail.length;render();};$('stepBack').onclick=()=>{stop();state.cursor--;render();};$('stepNext').onclick=()=>{stop();state.cursor++;render();};
  $('stepRange').oninput=e=>{stop();state.cursor=Number(e.target.value);render();};$('restoreGame').onclick=()=>{stop();state.trail=D.games[state.game].moves.slice();state.cursor=Math.min(state.cursor,state.trail.length);render();};
  $('playPause').onclick=()=>{if(state.timer){stop();return;}state.timer=setInterval(()=>{if(state.cursor>=state.trail.length){stop();render();return;}state.cursor++;render();if(state.cursor>=state.trail.length){stop();render();}},850);$('playPause').textContent='暫停';};
  $('groupSymmetry').onchange=()=>{stop();render();};$('branchFilter').onchange=()=>{stop();render();};$('galleryPrev').onclick=()=>{state.galleryPage--;renderGallery();};$('galleryNext').onclick=()=>{state.galleryPage++;renderGallery();};$('galleryJump').max=D.games.length;$('jumpGallery').onclick=()=>{const n=Number($('galleryJump').value);if(Number.isInteger(n)&&n>=1&&n<=D.games.length){state.galleryPage=Math.floor((n-1)/pageSize);renderGallery();}};
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
  $('jumpGame').onclick=()=>{const n=Number($('gameJump').value);if(Number.isInteger(n)&&n>=1&&n<=D.games.length)selectGame(n-1);};
  $('observeHere').onclick=$('headerFeedback').onclick=event=>{event.preventDefault();showView('feedback');};
  const initial=Number(new URLSearchParams(location.search).get('game'));if(Number.isInteger(initial)&&initial>=1&&initial<=D.games.length){state.game=initial-1;state.trail=D.games[state.game].moves.slice();$('gameSelect').value=String(state.game);}
  renderCounts();render();
})();
