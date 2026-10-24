"""公開站點只做唯讀及無效表單測試；不產生測試回饋。"""
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error
import uuid

ROOT=Path(__file__).resolve().parents[1]
url=sys.argv[1].rstrip('/')
assert url=='https://uttt-student-atlas.zestylion4.chatgpt.site'
checks=[]
def fetch(path,method='GET',value=None,origin=None):
    headers={'User-Agent':'UTTT-deployment-check/1.0'}
    if value is not None:headers['Content-Type']='application/json'
    if origin:headers['Origin']=origin
    request=urllib.request.Request(url+path,data=None if value is None else json.dumps(value).encode(),headers=headers,method=method)
    try:
        with urllib.request.urlopen(request,timeout=30) as response:return response.status,response.read(),response.geturl()
    except urllib.error.HTTPError as error:
        result=(error.code,error.read(),error.geturl());error.close();return result
def check(name,condition):
    assert condition,name
    checks.append(name)
status,html,final=fetch('/')
check('public_root_without_signin',status==200 and b'feedbackForm' in html and '/signin' not in final)
status,raw,_=fetch('/explore/data.json');data=json.loads(raw)
local=(ROOT/'sites/student-atlas/public/explore/data.json').read_bytes()
check('exact_public_6000_data',status==200 and sha256(raw).digest()==sha256(local).digest() and len(data['games'])==6000 and data['summary']['moves']==353495)
for name in ('engine.js','app.js','figures.js','feedback.js','styles.css','student.css','example-A.png','example-B.png'):
    status,raw,_=fetch('/explore/'+name)
    check('asset_'+name,status==200 and sha256(raw).digest()==sha256((ROOT/'sites/student-atlas/public/explore'/name).read_bytes()).digest())
check('anonymous_feedback_read_denied',fetch('/api/feedback')[0]==403)
check('anonymous_csv_denied',fetch('/api/feedback?format=csv')[0]==403)
invalid={'requestId':str(uuid.uuid4()),'datasetId':'invalid-dataset-for-validation-only','gameIndex':6000,'ply':0,'path':[],
         'kind':'other','observation':'部署核對：無效資料不應保存','suggestion':'部署核對：無效資料不應保存','website':''}
check('invalid_submission_rejected_no_write',fetch('/api/feedback','POST',invalid,url)[0]==400)
report={'checked_at':datetime.now(timezone.utc).isoformat(),'url':url,'passed':len(checks),'checks':checks,
        'scope':'匿名無登入HTTP檢查；只有無效資料集POST，沒有建立測試或學生回饋。',
        'browser_visual_qa':'未完成：Windows畫面工具無法可靠辨識網址而停止；未以其他UI控制方式繞過。'}
(ROOT/'data/student-site/published.validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
