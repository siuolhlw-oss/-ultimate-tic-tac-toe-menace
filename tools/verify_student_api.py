"""僅測試loopback隔離D1；不對正式網站新增測試回饋。"""
import json
from pathlib import Path
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:5173'
def request(path,method='GET',value=None,headers=None):
    selected={'Content-Type':'application/json',**(headers or {})}
    req=urllib.request.Request(BASE+path,data=None if value is None else json.dumps(value).encode(),headers=selected,method=method)
    try:
        with urllib.request.urlopen(req,timeout=20) as response:return response.status,response.read(),dict(response.headers)
    except urllib.error.HTTPError as error:return error.code,error.read(),dict(error.headers)

results=[]
def check(name,condition):
    assert condition,name
    results.append(name)

check('public_html',request('/explore/index.html')[0]==200)
status,raw,_=request('/explore/data.json');data=json.loads(raw)
check('public_6000_records',status==200 and len(data['games'])==6000 and data['summary']['moves']==353495)
check('anonymous_cannot_read',request('/api/feedback')[0]==403)
check('anonymous_csv_denied',request('/api/feedback?format=csv')[0]==403)
body={'requestId':str(uuid.uuid4()),'datasetId':'selfplay-6000-v1','gameIndex':6000,'ply':4,'path':[12,23,34,41],
      'kind':'symmetry','observation':'隔離測試：指定盤不同。','suggestion':'隔離測試：比較完整局面。','website':''}
check('cross_origin_rejected',request('/api/feedback','POST',body,{'Origin':'https://example.test'})[0]==403)
bad_status,bad_response,_=request('/api/feedback','POST',{**body,'datasetId':'bad'},{'Origin':BASE})
assert bad_status==400,(bad_status,bad_response.decode())
check('wrong_dataset_rejected',True)
status,raw,_=request('/api/feedback','POST',body,{'Origin':BASE})
check('anonymous_durable_insert',status==201 and json.loads(raw)['accepted'])
check('retry_idempotent',request('/api/feedback','POST',body,{'Origin':BASE})[0]==200)
student={'oai-authenticated-user-id':'local-student','oai-authenticated-user-email':'student@sites.test'}
teacher={'oai-authenticated-user-id':'local_seedy','oai-authenticated-user-email':'seedy@sites.test'}
check('signed_student_cannot_read',request('/api/feedback',headers=student)[0]==403)
status,raw,headers=request('/api/feedback',headers=teacher);items=json.loads(raw)
check('teacher_reads_saved_record',status==200 and sum(r['id']==body['requestId'] for r in items['rows'])==1)
check('private_no_store',headers.get('Cache-Control')=='private, no-store')
status,raw,_=request('/api/feedback?format=csv',headers=teacher)
check('teacher_csv',status==200 and body['requestId'].encode() in raw)
report={'created_at':datetime.now(timezone.utc).isoformat(),'scope':'loopback隔離D1；測試教師標頭僅用於本機，不送至公開站點',
        'passed':len(results),'checks':results,'receipt':body['requestId']}
folder=ROOT/'data/student-site';folder.mkdir(exist_ok=True)
(folder/'api-local.validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
