/* 從已核對的原始手序生成SVG棋譜，保留方向；不更動模型或資料。 */
(() => {
  const cache=new Map(), E=window.UTTT;
  function svg(game){
    const state=E.replay(game.moves).at(-1), orders=new Map(game.moves.map((m,i)=>[m,i+1]));
    const parts=['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1860" viewBox="0 0 1600 1860"><rect width="1600" height="1860" fill="#fffef9"/><g font-family="system-ui, sans-serif" fill="#243d34">',
      `<text x="90" y="75" font-size="44">第 ${game.index} 局 · 完整 ${game.ply} 手棋譜</text>`,
      '<text x="90" y="132" font-size="27">奇數手 ○（原始X） · 偶數手 ×（原始O）；棋子下方數字為手序</text>',
      `<text x="90" y="181" font-size="25">○：${game.xModel} · ×：${game.oModel} · 原始方向 · 小盤取得或填滿即封閉</text>`];
    for(let b=1;b<=9;b++){
      const x=90+((b-1)%3)*480,y=260+Math.floor((b-1)/3)*480,status=state.local_status[b-1];
      parts.push(`<rect x="${x}" y="${y-38}" width="438" height="38" fill="#e5ebe0"/><text x="${x+10}" y="${y-11}" font-size="24">大格 ${b}${status==='X'?' · ○取得':status==='O'?' · ×取得':status==='D'?' · 和局':''}</text>`);
      for(let s=1;s<=9;s++){
        const move=b*10+s,cx=x+((s-1)%3)*146,cy=y+Math.floor((s-1)/3)*146,actor=state.board[(b-1)*9+s-1],order=orders.get(move),color=actor==='X'?'#276a65':'#af6147';
        parts.push(`<rect x="${cx}" y="${cy}" width="146" height="146" fill="#fffef9" stroke="#bcc6b8"/><text x="${cx+8}" y="${cy+23}" font-size="19" fill="#7e8b7b">${s}</text>`);
        if(actor==='X')parts.push(`<circle cx="${cx+73}" cy="${cy+60}" r="25" fill="none" stroke="${color}" stroke-width="6"/>`);
        if(actor==='O')parts.push(`<path d="M${cx+50},${cy+37}l46,46m0,-46l-46,46" stroke="${color}" stroke-width="6"/>`);
        if(order)parts.push(`<text x="${cx+73}" y="${cy+124}" text-anchor="middle" font-size="33">${order}</text>`);
      }
      parts.push(`<rect x="${x}" y="${y}" width="438" height="438" fill="none" stroke="#536a58" stroke-width="3"/>`);
    }
    parts.push('<text x="90" y="1705" font-size="25">大小格編號皆按左上至右下；兩位數位置＝10×大格＋小格。</text>',
      `<text x="90" y="1750" font-size="25">終局：${game.result==='draw'?'和局':game.result==='X'?'○（先手）獲勝':'×（後手）獲勝'} · 第${game.index}局是訓練紀錄，不能單獨證明最佳策略。</text>`,
      '<text x="90" y="1795" font-size="23">資料範圍：累計6,000局雙AI自我對弈；不含學生探索或回饋。</text></g></svg>');
    return parts.join('');
  }
  function url(game){
    if(cache.has(game.index)){const value=cache.get(game.index);cache.delete(game.index);cache.set(game.index,value);return value;}
    const value=URL.createObjectURL(new Blob([svg(game)],{type:'image/svg+xml;charset=utf-8'}));cache.set(game.index,value);
    if(cache.size>120){const [key,old]=cache.entries().next().value;URL.revokeObjectURL(old);cache.delete(key);}
    return value;
  }
  window.UTTT_FIGURES={svg,url};
})();
