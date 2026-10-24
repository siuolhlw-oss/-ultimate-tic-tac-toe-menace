import { desc, sql } from 'drizzle-orm';
import { getDb } from '../../../db';
import { feedback } from '../../../db/schema';
import { validateFeedback, feedbackCsv } from '../../../lib/feedback.mjs';
import { isTeacher } from '../../../lib/teacher';

export const dynamic = 'force-dynamic';
const privateHeaders = {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'};

export async function POST(request: Request) {
  try {
    const reader = request.body?.getReader(), chunks:Uint8Array[]=[];
    let size=0;
    if (reader) while (true) {
      const part=await reader.read(); if(part.done)break;
      size+=part.value.length;
      if(size>24000){await reader.cancel();return Response.json({error:'內容太長。'},{status:413});}
      chunks.push(part.value);
    }
    const bytes=new Uint8Array(size);let cursor=0;for(const chunk of chunks){bytes.set(chunk,cursor);cursor+=chunk.length;}
    const text=new TextDecoder().decode(bytes);
    const origin = request.headers.get('origin');
    if (!origin || origin !== new URL(request.url).origin) return Response.json({error:'請從本站提交回饋。'}, {status:403});
    if (!request.headers.get('content-type')?.startsWith('application/json')) return Response.json({error:'請使用網站表單。'}, {status:415});
    const item = validateFeedback(JSON.parse(text));
    const db = getDb();
    const inserted = await db.insert(feedback).values({...item, createdAt:new Date().toISOString()}).onConflictDoNothing().returning({id:feedback.id});
    return Response.json({accepted:true, receipt:item.id}, {status:inserted.length ? 201 : 200, headers:privateHeaders});
  } catch (error) {
    if (error instanceof SyntaxError) return Response.json({error:'回饋格式不正確。'}, {status:400});
    const message = error instanceof Error ? error.message : '';
    if (/^(回饋|請|棋譜|局號|手數|觀察|無法接受)/.test(message)) return Response.json({error:message}, {status:400});
    return Response.json({error:'暫時無法保存，內容仍留在表單中，請稍後重試。'}, {status:503});
  }
}

export async function GET(request: Request) {
  if (!(await isTeacher())) return Response.json({error:'此功能僅限教師登入使用。'}, {status:403,headers:privateHeaders});
  const url = new URL(request.url), db = getDb();
  if (url.searchParams.get('format') === 'csv') {
    const rows = await db.select().from(feedback).orderBy(desc(feedback.createdAt), desc(feedback.id)).limit(10001);
    if (rows.length > 10000) return Response.json({error:'超過單次匯出上限，請聯絡管理者分批匯出。'}, {status:413,headers:privateHeaders});
    return new Response(feedbackCsv(rows), {headers:{...privateHeaders,'Content-Type':'text/csv; charset=utf-8','Content-Disposition':'attachment; filename="student-feedback.csv"'}});
  }
  const offset = Number(url.searchParams.get('offset') || 0);
  if (!Number.isInteger(offset) || offset < 0 || offset > 1000000) return Response.json({error:'頁次錯誤。'}, {status:400,headers:privateHeaders});
  const rows = await db.select().from(feedback).orderBy(desc(feedback.createdAt),desc(feedback.id)).limit(50).offset(offset);
  const [count] = await db.select({total:sql<number>`count(*)`}).from(feedback);
  return Response.json({rows,total:count.total,offset}, {headers:privateHeaders});
}
