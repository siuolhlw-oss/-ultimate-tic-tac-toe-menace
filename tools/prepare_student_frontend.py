"""以現有圖譜建立學生站靜態介面；不改原本機圖譜或核心規則。"""
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'web/game-atlas'
out=ROOT/'sites/student-atlas/public/explore'
out.mkdir(parents=True,exist_ok=True)
for name in ('engine.js','styles.css'):
    shutil.copy2(source/name,out/name)
html=(source/'index.html').read_text(encoding='utf-8')
html=html.replace('千局棋譜圖譜','6,000局觀察研究室').replace('千局圖集','6,000局圖集').replace('1,000','6,000').replace('max="1000"','max="6000"')
html=html.replace('<link rel="stylesheet" href="styles.css">','<meta name="description" content="探索已驗證的6,000局雙AI自我對弈，觀察合法下法、全盤同構與原始棋譜，匿名提出研究建議。"><link rel="stylesheet" href="styles.css"><link rel="stylesheet" href="student.css">')
html=html.replace('<script defer src="app.js"></script>','<script defer src="figures.js"></script><script defer src="app.js"></script><script defer src="feedback.js"></script>')
start=html.index('<a class="download-link"')
end=html.index('</a>',start)+4
html=html[:start]+'<nav class="site-links" aria-label="頁面入口"><a href="#" id="headerFeedback">提出觀察 ↗</a><a href="/teacher">教師回饋管理</a></nav>'+html[end:]
html=html.replace('從已發生的 6,000 局出發，展開其他合法走法。這裡的探索不會寫入訓練資料。','從真實棋譜找線索，試試其他合法走法，再把你的觀察交給研究者。這裡的探索會保留在你的畫面，與原始訓練紀錄分開。')
html=html.replace('<button data-view="counts" aria-selected="false">03　變化與界限</button>','<button data-view="counts" aria-selected="false">03　樣本與界限</button><button data-view="feedback" aria-selected="false">04　匿名建議</button>')
html=html.replace('<label for="gameSelect">選擇棋譜</label>','<label for="gameSelect">選擇棋譜</label><label for="gameJump" class="sr-only">直接輸入局號</label><input class="search-game" id="gameJump" type="number" min="1" max="6000" value="1" aria-label="直接輸入局號"><button id="jumpGame">前往</button>')
html=html.replace('<section id="exploreView" class="view">','<section id="exploreView" class="view"><div class="observation-nudge"><span class="small">看見值得討論的一步？回饋會附上目前局號、手數與探索路徑。</span><button id="observeHere">針對這個局面提建議</button></div>')
html=html.replace('開啟本局圖片 ↗','開啟本局棋譜圖 ↗').replace('圖片上的數字就是落子手數。空白格代表整局沒有在該位置落子。','每張圖依原始手序即時繪製，棋子下方數字表示第幾手。可開啟或下載向量棋譜，放大仍清楚。')
html=html.replace('本機唯讀圖譜','學生觀察版 · 唯讀棋譜')
feedback='''<section id="feedbackView" class="view" hidden>
<div class="section-heading"><div><p class="eyebrow">YOUR OBSERVATION MATTERS</p><h2>把「我發現」變成下一個研究問題。</h2><p>不必先知道正確答案。描述你看到的現象，提出可以比較或驗證的方法。</p></div></div>
<div class="feedback-layout"><aside class="feedback-guide"><h3>從哪裡開始觀察？</h3><ol><li>有沒有某一步讓你覺得意外？換一種下法會怎樣？</li><li>同一張棋盤，指定盤不同時，合法下法怎麼改變？</li><li>旋轉或鏡射前後，哪些資訊必須一起變換？</li><li>要比較AI是否進步，還需要記錄或安排什麼？</li></ol><p>看到的6,000局是訓練樣本；某一步被選中，不代表已證明它是最佳策略。</p><p class="privacy-note">不需要登入或填姓名、學號與聯絡方式。回饋只供教師查看，請勿在文字中填入個人資料。</p></aside>
<form id="feedbackForm" class="feedback-form"><div id="feedbackContext" class="context-card" aria-live="polite"></div><button type="button" id="useCurrentContext">引用目前探索局面</button>
<label for="feedbackKind">建議類型<select id="feedbackKind" required><option value="strategy">策略觀察</option><option value="rule">規則理解</option><option value="symmetry">同構問題</option><option value="interface">介面建議</option><option value="other">其他研究建議</option></select></label>
<label for="observation">你觀察到什麼？<textarea id="observation" required minlength="2" maxlength="2000" placeholder="例如：這一步指定的盤已封閉，因此下一手可以自由選盤。我想知道……"></textarea></label>
<label for="suggestion">建議如何修正或進一步驗證？<textarea id="suggestion" required minlength="2" maxlength="2000" placeholder="例如：比較同一個模型在不同版本的選擇，並固定先後手與評估條件。"></textarea></label>
<label class="honeypot" aria-hidden="true">Website<input id="website" name="website" tabindex="-1" autocomplete="off"></label>
<button class="primary-button" id="submitFeedback" type="submit">匿名提交建議</button><p id="feedbackMessage" class="feedback-message" role="status"></p><p class="privacy-note">看到「已保存」與收件編號才表示提交成功。建議另存於網站，不會改變訓練模型。</p></form></div></section>'''
note='''<div class="research-note"><h3>棋子圖形相同，也可能是不同局面。</h3><p>A：12 → 23 → 34 → 41，下一手指定大格1。B：34 → 41 → 12 → 23，下一手指定大格3。兩者棋子配置相同，但八種全盤D4變換都無法使完整局面互相對應，不能共用盒子。</p><div class="example-pair"><figure><img src="example-A.png" loading="lazy" alt="棋譜A，最後一手41，綠色標示下一手在大格1的合法位置"><figcaption>A：最後一手41，下一手指定1。</figcaption></figure><figure><img src="example-B.png" loading="lazy" alt="棋譜B，最後一手23，綠色標示下一手在大格3的合法位置"><figcaption>B：最後一手23，下一手指定3。</figcaption></figure></div><p class="small">以上兩圖是合法規則示例，與6,000局AI紀錄分開。最後一手不同不一定代表非同構；須比較棋盤、行動方、指定盤或自由標記與規則版本。</p></div>'''
html=html.replace('<div class="scope-note">',note+'<div class="scope-note">')
html=html.replace('    <footer>',feedback+'\n    <footer>')
(out/'index.html').write_text(html,encoding='utf-8')
js=(source/'app.js').read_text(encoding='utf-8')
js=js.replace("const imagePath = (game) => `images/game-${pad(game.index)}.png`;","const imagePath = (game) => window.UTTT_FIGURES.url(game);")
js=js.replace("['explore', 'gallery', 'counts']","['explore', 'gallery', 'counts', 'feedback']")
js=js.replace("if (view === 'gallery') renderGallery();","if (view === 'gallery') renderGallery(); window.dispatchEvent(new CustomEvent('uttt:view',{detail:view}));")
js=js.replace('下載圖片 ↗','下載SVG棋譜 ↗').replace('`game-${pad(game.index)}.png`','`game-${pad(game.index)}.svg`')
js=js.replace('renderBoard(s,path);renderTree(s,path);',"renderBoard(s,path);renderTree(s,path); window.STUDENT_CONTEXT={gameIndex:game.index,ply:path.length,path:[...path],isOriginal:follows}; $('gameJump').value=String(game.index);")
js=js.replace("  renderCounts();render();",'''  $('jumpGame').onclick=()=>{const n=Number($('gameJump').value);if(Number.isInteger(n)&&n>=1&&n<=D.games.length)selectGame(n-1);};
  $('observeHere').onclick=$('headerFeedback').onclick=event=>{event.preventDefault();showView('feedback');};
  const initial=Number(new URLSearchParams(location.search).get('game'));if(Number.isInteger(initial)&&initial>=1&&initial<=D.games.length){state.game=initial-1;state.trail=D.games[state.game].moves.slice();$('gameSelect').value=String(state.game);}
  renderCounts();render();''')
assert 'images/game-' not in js
(out/'app.js').write_text(js,encoding='utf-8')
for label in ('A','B'):
    shutil.copy2(ROOT/f'data/research-figures/forced-board/example-{label}.png',out/f'example-{label}.png')
print('已建立學生版介面；原圖譜與核心引擎保持原值。')
