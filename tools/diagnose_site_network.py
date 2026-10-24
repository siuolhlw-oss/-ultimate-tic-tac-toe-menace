import json
from pathlib import Path
import subprocess
import sys
import urllib.request
import urllib.error

URL='https://uttt-student-atlas.zestylion4.chatgpt.site/explore/summary.json'
if '--worker' in sys.argv:
    try:
        with urllib.request.urlopen(URL,timeout=10) as response:
            print(json.dumps({'status':response.status,'body':response.read(1200).decode('utf-8','replace')}))
    except urllib.error.HTTPError as error:
        print(json.dumps({'status':error.code,'body':error.read(1800).decode('utf-8','replace'),
                          'server':error.headers.get('server'),'content_type':error.headers.get('content-type')}))
    except Exception as error:
        print(json.dumps({'error_type':type(error).__name__,'message':str(error)[:300]}))
else:
    try:
        response=subprocess.run([sys.executable,'-X','utf8','-B',str(Path(__file__).resolve()),'--worker'],capture_output=True,text=True,timeout=20)
        print(response.stdout)
        if response.stderr:print(response.stderr[:400])
    except subprocess.TimeoutExpired:
        print(json.dumps({'external_http_check':'timeout_after_20s'}))
