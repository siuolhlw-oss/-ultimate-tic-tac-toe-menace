"""一次性依 Notion 上傳工具的契約傳送 PNG；不輸出憑證。"""
import json
from pathlib import Path
import sys

import requests

root = Path(__file__).resolve().parents[1] / 'data/research-figures/forced-board'
label = sys.argv[1]
assert label in ('A', 'B')
config_path = root / f'.upload-{label}.tmp.json'
try:
    config = json.loads(config_path.read_text(encoding='utf-8'))
    assert config['upload_method'] == 'POST' and config['upload_form_field'] == 'file'
    filename = f'example-{label}.png'
    assert config['filename'] == filename
    with (root / filename).open('rb') as file:
        response = requests.post(config['upload_url'], headers=config['upload_headers'],
                                 files={'file': (filename, file, 'image/png')},
                                 timeout=60, allow_redirects=False)
    print(json.dumps({'label': label, 'http_status': response.status_code}))
    if response.status_code != 200:
        raise SystemExit('Upload failed; credential-bearing response omitted.')
    result = response.json()
    assert 'markdown_source' in result and 'suggested_markdown' in result
    (root / f'notion-upload-{label}.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'label': label, 'uploaded': True, 'response_keys': list(result)}))
except requests.RequestException as error:
    print(json.dumps({'label': label, 'network_error_type': type(error).__name__}))
    raise SystemExit(1)
finally:
    config_path.unlink(missing_ok=True)
