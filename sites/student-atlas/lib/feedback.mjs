export const DATASET_ID = 'selfplay-6000-v1';
const kinds = new Set(['rule','strategy','symmetry','interface','other']);
const moveValid = m => Number.isInteger(m) && m >= 11 && m <= 99 && m % 10 >= 1 && m % 10 <= 9;
export function validateFeedback(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('回饋格式不正確。');
  const { requestId, datasetId, gameIndex, ply, path, kind } = value;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(requestId || '')) throw Error('請重新整理後再提交。');
  if (datasetId !== DATASET_ID) throw Error('棋譜資料版本不符，請重新整理。');
  if (gameIndex !== null && (!Number.isInteger(gameIndex) || gameIndex < 1 || gameIndex > 6000)) throw Error('局號須為1至6000。');
  if (!Number.isInteger(ply) || ply < 0 || ply > 81 || !Array.isArray(path) || path.length !== ply || !path.every(moveValid) || new Set(path).size !== path.length) throw Error('手數與探索路徑不符。');
  if (!kinds.has(kind)) throw Error('請選擇建議類型。');
  if (typeof value.observation !== 'string' || typeof value.suggestion !== 'string') throw Error('請填寫觀察與建議。');
  const observation = value.observation.trim(), suggestion = value.suggestion.trim();
  if (observation.length < 2 || observation.length > 2000 || suggestion.length < 2 || suggestion.length > 2000) throw Error('觀察與建議各須為2至2000字。');
  if (value.website) throw Error('無法接受此回饋。');
  return {id: requestId, datasetId, gameIndex, ply, pathJson: JSON.stringify(path), kind, observation, suggestion};
}
export function teacherAllowed(user, expectedEmail) {
  return Boolean(user?.userId && user?.email && expectedEmail && user.email.trim().toLowerCase() === expectedEmail.trim().toLowerCase());
}
export function feedbackCsv(rows) {
  const cell = value => {let text = String(value ?? ''); if (/^[=+\-@\t\r]/.test(text)) text = "'"+text; return '"'+text.replaceAll('"','""')+'"';};
  const columns = ['id','createdAt','datasetId','gameIndex','ply','pathJson','kind','observation','suggestion'];
  return '\ufeff'+[columns.map(cell).join(','),...rows.map(row=>columns.map(k=>cell(row[k])).join(','))].join('\r\n');
}
