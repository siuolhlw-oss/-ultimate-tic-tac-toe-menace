"""可重現的小批次訓練、凍結評估及研究資料匯出。

例：python -m uttt.cli train --games 10 --seed 7
    python -m uttt.cli evaluate --model AI-A --games 20 --seed 8
    python -m uttt.cli export --output exports/research.zip
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

from .service import ResearchService


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("局數必須大於 0。")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="超級井字棋 MENACE 本機研究工具")
    result.add_argument("--db", default=os.environ.get("UTTT_DB_PATH", "data/research.sqlite3"),
                        help="SQLite 路徑；預設採 UTTT_DB_PATH 或 data/research.sqlite3")
    commands = result.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train", help="兩個模型自我對弈，名稱不存在時從零建立")
    train.add_argument("--model-a", default="AI-A", help="模型 ID 或唯一名稱，預設 AI-A")
    train.add_argument("--model-b", default="AI-B", help="模型 ID 或唯一名稱，預設 AI-B")
    train.add_argument("--games", type=positive_int, default=10)
    train.add_argument("--seed", type=int, default=0,
                       help="批次對局種子；新模型分別以此值與此值+1初始化，既有模型亂數不重設")
    train.add_argument("--quiet", action="store_true", help="不印進度，仍輸出結果 JSON")
    evaluate = commands.add_parser("evaluate", help="凍結既有模型，對抗合法均勻隨機")
    evaluate.add_argument("--model", default="AI-A", help="既有模型 ID 或唯一名稱")
    evaluate.add_argument("--games", type=positive_int, default=20, help="建議偶數，交替先後手")
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--quiet", action="store_true")
    export = commands.add_parser("export", help="匯出含 CSV 與 JSON 的 ZIP，不覆寫既有檔案")
    export.add_argument("--run-id", default=None, help="僅匯出指定批次；省略則匯出全部")
    export.add_argument("--output", type=Path, default=None, help="ZIP 路徑；預設 exports/ 時間戳檔名")
    return result


def resolve_model(service: ResearchService, selector: str, *, create: bool, seed: int) -> str:
    selector = selector.strip()
    if not selector:
        raise ValueError("模型名稱不可為空。")
    models = service.list_models()
    exact = next((m for m in models if m["model_id"] == selector), None)
    if exact:
        return exact["model_id"]
    matches = [m for m in models if m["name"] == selector]
    if len(matches) > 1:
        choices = ", ".join(m["model_id"] for m in matches)
        raise ValueError(f"有多個同名模型 {selector}，請指定 ID：{choices}")
    if matches:
        return matches[0]["model_id"]
    if create:
        return service.create_model(selector, seed=seed)
    raise ValueError(f"找不到模型 {selector}。請先執行 train，或指定已存在的 ID。")


def progress_reporter(quiet: bool):
    last = -1

    def report(done: int, total: int) -> None:
        nonlocal last
        bucket = done * 10 // total
        if not quiet and (bucket != last or done == total):
            print(f"已完成 {done}/{total} 局", file=sys.stderr, flush=True)
            last = bucket

    return report


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with ResearchService(db_path=args.db) as service:
            if args.command == "train":
                if args.model_a.strip() == args.model_b.strip():
                    raise ValueError("自我對弈請指定兩個不同模型。")
                model_a = resolve_model(service, args.model_a, create=True, seed=args.seed)
                model_b = resolve_model(service, args.model_b, create=True, seed=args.seed + 1)
                if model_a == model_b:
                    raise ValueError("兩個選項指向同一模型；請指定不同模型。")
                output = service.train(model_a, model_b, games=args.games, seed=args.seed,
                                       progress=progress_reporter(args.quiet))
                output = {"model_a": model_a, "model_b": model_b, **output}
            elif args.command == "evaluate":
                model_id = resolve_model(service, args.model, create=False, seed=args.seed)
                output = service.evaluate(model_id, games=args.games, seed=args.seed,
                                          progress=progress_reporter(args.quiet))
                output = {"model_id": model_id, **output}
            else:
                destination = args.output or Path("exports") / f"research_{datetime.now():%Y%m%d_%H%M%S_%f}.zip"
                if destination.exists():
                    raise FileExistsError(f"檔案已存在，請另選檔名：{destination}")
                payload = service.export_data(run_id=args.run_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(payload)
                output = {"output": str(destination.resolve()), "bytes": len(payload), "run_id": args.run_id}
            print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("已停止；已完成的對局仍保留在資料庫。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"操作失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
