import test from 'node:test';
import assert from 'node:assert/strict';
import {validateFeedback,teacherAllowed,feedbackCsv} from '../lib/feedback.mjs';
const draft=()=>({requestId:crypto.randomUUID(),datasetId:'selfplay-6000-v1',gameIndex:6000,ply:4,path:[12,23,34,41],kind:'symmetry',observation:'同樣圖形，指定盤不同。',suggestion:'比較兩個局面的所有合法位置。',website:''});
test('匿名回饋只保存白名單欄位，包含原始探索路徑',()=>{const value=validateFeedback({...draft(),email:'do-not-store@example.test',studentName:'不保存'});assert.equal(value.gameIndex,6000);assert.equal(value.pathJson,'[12,23,34,41]');assert.equal('email' in value,false);assert.equal('studentName' in value,false);});
test('拒絕其他資料集、越界局號、矛盾手序與重複位置',()=>{for(const changed of [{datasetId:'wrong'},{gameIndex:6001},{gameIndex:0},{ply:5},{path:[12,23,12,41]},{path:[10,23,34,41]}])assert.throws(()=>validateFeedback({...draft(),...changed}));});
test('限制公開表單內容及機器欄位',()=>{for(const changed of [{observation:''},{suggestion:'a'.repeat(2001)},{kind:'admin'},{website:'bot'},{requestId:'1'}])assert.throws(()=>validateFeedback({...draft(),...changed}));});
test('只有指定教師且有平台身分時可查看，未設定時不開放',()=>{assert.equal(teacherAllowed(null,'teacher@example.test'),false);assert.equal(teacherAllowed({userId:'x',email:'student@example.test'},'teacher@example.test'),false);assert.equal(teacherAllowed({email:'teacher@example.test'},'teacher@example.test'),false);assert.equal(teacherAllowed({userId:'x',email:'teacher@example.test'},undefined),false);assert.equal(teacherAllowed({userId:'x',email:'TEACHER@example.test'},'teacher@example.test'),true);});
test('CSV保存換行引號且防止試算表公式執行',()=>{const csv=feedbackCsv([{observation:'=1+1',suggestion:'兩行\n含"引號"',gameIndex:6000}]);assert.ok(csv.startsWith('\ufeff'));assert.ok(csv.includes('"\'=1+1"'));assert.ok(csv.includes('兩行\n含""引號""'));});
