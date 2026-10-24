/* 6000局資料與DOM行為核對，和真實瀏覽器目視檢查分開。 */
const test=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {createRequire}=require('node:module');
const root=path.resolve(__dirname,'..'), site=path.join(root,'sites/student-atlas/public/explore');
const {JSDOM,VirtualConsole}=createRequire(path.join(root,'web/game-atlas/package.json'))('jsdom');
const data=JSON.parse(fs.readFileSync(path.join(site,'data.json'),'utf8'));
const sandbox={module:{exports:{}}};vm.runInNewContext(fs.readFileSync(path.join(site,'engine.js'),'utf8'),sandbox);const E=sandbox.module.exports;
test('6000局與353495手，全部依公開規則重播到相同終局',()=>{assert.equal(data.games.length,6000);let moves=0;for(const game of data.games){const state=E.replay(game.moves).at(-1);assert.equal(state.status,game.result);assert.equal(state.ply,game.ply);moves+=game.ply;}assert.equal(moves,353495);assert.equal(data.summary.moves,moves);assert.ok(!JSON.stringify(data).includes('C:\\Users'));assert.ok(!JSON.stringify(data).includes('rng_state'));});
test('棋盤、分支、6000局跳轉、SVG及匿名提交與重試',async()=>{
  const errors=[],vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(String(e)));
  const dom=new JSDOM(fs.readFileSync(path.join(site,'index.html'),'utf8'),{url:'http://localhost:5173/explore/index.html',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
  const w=dom.window,d=w.document,$=id=>d.getElementById(id);w.scrollTo=()=>{};
  let blob=0;w.URL.createObjectURL=()=>`blob:test-${++blob}`;w.URL.revokeObjectURL=()=>{};
  for(const file of ['engine.js','data.js','figures.js','app.js','feedback.js'])w.eval(fs.readFileSync(path.join(site,file),'utf8'));
  const before=JSON.stringify(w.UTTT_DATA);
  assert.equal($('gameSelect').options.length,6000);assert.equal(d.querySelectorAll('.cell').length,81);
  assert.equal(d.querySelectorAll('#tree > details').length,15);
  $('groupSymmetry').checked=false;$('groupSymmetry').dispatchEvent(new w.Event('change'));assert.equal(d.querySelectorAll('#tree > details').length,81);
  $('gameJump').value='6000';$('jumpGame').click();assert.match($('gameNumber').textContent,/6000/);
  const game=data.games.at(-1);for(let ply=0;ply<game.ply;ply++){
    const expected=w.UTTT.replay(game.moves.slice(0,ply)).at(-1),legal=new Set(w.UTTT.legalMoves(expected));
    for(const cell of d.querySelectorAll('.cell'))assert.equal(!cell.disabled,legal.has(Number(cell.dataset.move)));
    $('stepNext').click();
  }
  assert.equal(d.querySelectorAll('.cell:not(:disabled)').length,0);assert.match($('turnStatus').textContent,/整局結束/);
  const svg=w.UTTT_FIGURES.svg(game);const parsed=new w.DOMParser().parseFromString(svg,'image/svg+xml');assert.equal(parsed.querySelector('parsererror'),null);assert.equal(parsed.querySelectorAll('circle').length,Math.ceil(game.ply/2));
  $('observeHere').click();assert.equal($('feedbackView').hidden,false);assert.match($('feedbackContext').textContent,/第 6000 局/);
  $('observation').value='第6000局的指定盤值得比較';$('suggestion').value='比較相同完整狀態的下法';
  const calls=[];w.fetch=async(url,options)=>{calls.push(JSON.parse(options.body));throw Error('測試離線');};
  $('feedbackForm').dispatchEvent(new w.Event('submit',{cancelable:true}));await new Promise(setImmediate);assert.equal($('observation').value,'第6000局的指定盤值得比較');assert.match($('feedbackMessage').textContent,/內容已保留/);
  w.fetch=async(url,options)=>{const value=JSON.parse(options.body);calls.push(value);return{ok:true,json:async()=>({accepted:true,receipt:value.requestId})};};
  $('feedbackForm').dispatchEvent(new w.Event('submit',{cancelable:true}));await new Promise(setImmediate);assert.equal(calls.length,2);assert.equal(calls[0].requestId,calls[1].requestId);assert.equal(calls[1].gameIndex,6000);assert.equal(calls[1].path.length,game.ply);assert.match($('feedbackMessage').textContent,/已保存/);assert.equal($('observation').value,'');assert.equal(JSON.stringify(w.UTTT_DATA),before);assert.deepEqual(errors,[]);dom.window.close();
});
