import { requireChatGPTUser } from '../chatgpt-auth';
import { isTeacher } from '../../lib/teacher';
import TeacherPanel from './panel';
export const dynamic = 'force-dynamic';
export default async function Teacher() {
  await requireChatGPTUser('/teacher');
  const allowed = await isTeacher();
  return <><link rel="stylesheet" href="/explore/styles.css"/><link rel="stylesheet" href="/explore/student.css"/>
    <main className="teacher-page"><p className="eyebrow">TEACHER DESK</p><h1>學生觀察與建議</h1><a href="/">← 返回棋譜探索</a>
      {allowed ? <TeacherPanel/> : <p role="alert">此頁僅開放本網站的教師帳號。請使用建立網站時的帳號登入。</p>}
    </main></>;
}
