"""匯出千局圖片與唯讀網頁；原始訓練資料庫不修改。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ('index.html', 'styles.css', 'app.js', 'engine.js')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/selfplay-1000/training.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT / 'exports/game-atlas')
    parser.add_argument('--assets-only', action='store_true', help='已有完整資料及圖片時，只更新網頁檔案')
    parser.add_argument('--package', action='store_true', help='另產生可離線開啟的完整網頁 ZIP（含圖片）')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.assets_only:
        for script in ('export_explorer_data.py', 'export_game_images.py'):
            subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'tools' / script),
                            '--db', str(args.db.resolve()), '--output', str(output)], check=True)
    data = json.loads((output / 'data.json').read_text(encoding='utf-8'))
    assert len(data['games']) == data['summary']['games'] == 1000
    for game in data['games']:
        if not (output / 'images' / f"game-{game['index']:04d}.png").is_file():
            raise FileNotFoundError(f"缺少第 {game['index']} 局圖片；請先完整匯出。")
    for filename in ASSETS:
        shutil.copy2(ROOT / 'web/game-atlas' / filename, output / filename)
    (output / 'README.txt').write_text(
        '千局棋譜圖譜\n\n解壓縮後直接開啟 index.html，或使用專案 explore_games.cmd。\n'
        '網頁無外部套件、字型或網路服務依賴，不寫入原始資料。\n'
        '01 逐步探索：選局、前進或退回；點棋盤合法格或樹的「走這一步」探索。\n'
        '箭頭展開下一層，可切換全部合法下法與本批走過的分支。\n'
        '02 千局圖集：每局一張PNG，可下載或切回逐步探索。\n'
        '03 變化與界限：樣本計數與有限性說明，全遊戲總數尚未求得。\n'
        '顯示奇數○對應原始X，偶數×對應原始O；只更換顯示，不改棋譜及同構。\n'
        '1,000局全部圖片另有1000-games-images.zip。離線網頁包已包含images資料夾；\n'
        '離線包頂端提供圖片清單，1,000張PNG都在images資料夾。\n', encoding='utf-8')
    report = {'games': len(data['games']), 'moves': data['summary']['moves'],
              'source_records_sha256': data['source']['recordsSha256'],
              'assets': {f: hashlib.sha256((output/f).read_bytes()).hexdigest() for f in ASSETS}}
    if args.package:
        destination = output / 'game-atlas-offline.zip'
        temporary = output / 'game-atlas-offline.zip.partial'
        names = list(ASSETS) + ['data.js', 'data.json', 'summary.json', 'image-manifest.json', 'README.txt']
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in names:
                if name == 'index.html':
                    offline_index = (output/name).read_text(encoding='utf-8').replace(
                        'href="1000-games-images.zip" download>下載 1,000 張棋譜圖',
                        'href="image-manifest.json" download>下載圖片清單')
                    archive.writestr(name, offline_index.encode('utf-8'))
                else:
                    archive.write(output/name, name)
            for image in sorted((output/'images').glob('game-*.png')):
                archive.write(image, image.relative_to(output).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('離線 ZIP CRC 核對失敗。')
        temporary.replace(destination)
        with destination.open('rb') as handle:
            report['offline_zip_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
        report['offline_zip_bytes'] = destination.stat().st_size
    (output/'build-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'directory': str(output), **report},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
