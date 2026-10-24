'use client';
import { useEffect, useState } from 'react';
type Row = {id:string;createdAt:string;gameIndex:number;ply:number;kind:string;observation:string;suggestion:string;pathJson:string};
const labels:Record<string,string> = {rule:'規則理解',strategy:'策略觀察',symmetry:'同構問題',interface:'介面建議',other:'其他'};
export default function TeacherPanel() {
  const [rows,setRows]=useState<Row[]>([]),[total,setTotal]=useState(0),[offset,setOffset]=useState(0),[message,setMessage]=useState('載入中…');
  async function load(start:number) {
    setMessage('載入中…');
    try {const response=await fetch('/api/feedback?offset='+start,{cache:'no-store'});const data=await response.json() as {error?:string;rows:Row[];total:number};if(!response.ok)throw Error(data.error);setRows(data.rows);setTotal(data.total);setOffset(start);setMessage(data.total ? '' : '目前尚無學生回饋。');}
    catch(error){setMessage(error instanceof Error?error.message:'讀取失敗，請重試。');}
  }
  useEffect(()=>{load(0);},[]);
  return <><div className="teacher-actions"><strong>共 {total} 則回饋</strong><button onClick={()=>load(offset)}>重新整理</button><a href="/api/feedback?format=csv" className="primary-link">匯出全部 CSV</a></div>
    <p className="small">學生未提供帳號或姓名。以下為觀察建議，與正式訓練棋譜分開保存。</p><p role="status">{message}</p>
    <div className="feedback-list">{rows.map(row=><article className="feedback-card" key={row.id}>
      <div className="feedback-heading"><strong>{labels[row.kind]||row.kind} · 第 {row.gameIndex} 局 · 已下 {row.ply} 手</strong><time>{new Date(row.createdAt).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})}</time></div>
      <h3>觀察到什麼？</h3><p className="preserve-text">{row.observation}</p><h3>建議如何修正或驗證？</h3><p className="preserve-text">{row.suggestion}</p>
      <details><summary>查看當時的探索路徑與收件編號</summary><p className="preserve-text">{row.pathJson}</p><code>{row.id}</code></details>
    </article>)}</div>
    <div className="pagination"><button disabled={offset===0} onClick={()=>load(Math.max(0,offset-50))}>上一頁</button><span>{total?offset+1:0}–{Math.min(total,offset+50)} / {total}</span><button disabled={offset+50>=total} onClick={()=>load(offset+50)}>下一頁</button></div>
  </>;
}
