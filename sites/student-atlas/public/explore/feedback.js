(() => {
  'use strict';
  const $=id=>document.getElementById(id), form=$('feedbackForm');
  let selected=null, requestId=null;
  function reference(){
    const current=window.STUDENT_CONTEXT;
    if(!current)return;
    selected={gameIndex:current.gameIndex,ply:current.ply,path:[...current.path]};
    $('feedbackContext').textContent=`第 ${current.gameIndex} 局 · 已下 ${current.ply} 手 · ${current.isOriginal?'原始棋譜':'合法分支預覽'}；路徑：${current.path.join(' → ')||'空盤'}`;
    requestId=null;
  }
  $('useCurrentContext').addEventListener('click',reference);
  window.addEventListener('uttt:view',event=>{if(event.detail==='feedback')reference();});
  form.addEventListener('input',()=>{requestId=null;});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!form.reportValidity())return;if(!selected)reference();
    const button=$('submitFeedback'), message=$('feedbackMessage');button.disabled=true;button.textContent='保存中…';message.textContent='';message.className='feedback-message';
    requestId ||= crypto.randomUUID();
    const payload={requestId,datasetId:window.UTTT_DATA.datasetId,...selected,kind:$('feedbackKind').value,observation:$('observation').value,suggestion:$('suggestion').value,website:$('website').value};
    try {
      const response=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();if(!response.ok||!result.accepted)throw Error(result.error||'未完成保存，請稍後重試。');
      form.reset();message.textContent='已保存，謝謝你的觀察！收件編號：'+result.receipt;message.classList.add('success-text');requestId=null;
    }catch(error){message.textContent=(error.message||'連線失敗')+' 你填寫的內容已保留，可再次提交。';message.classList.add('error-text');}
    finally {button.disabled=false;button.textContent='匿名提交建議';}
  });
  reference();
})();
