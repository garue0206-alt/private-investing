from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_jobs, select_jobs
from .notebook_tools import NotebookResult, run_notebook_job
from .telegram_client import TelegramClient, TelegramError

KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="6개 시장 노트북 실행 및 Telegram 자동전송")
    parser.add_argument("--jobs", default=os.getenv("RUN_JOBS", "all"), help="all 또는 job id 쉼표 목록")
    parser.add_argument("--no-telegram", action="store_true", help="실행만 하고 Telegram 전송 생략")
    parser.add_argument("--no-documents", action="store_true", help="Telegram 파일 첨부 생략")
    parser.add_argument("--output-base", default="outputs", help="결과 저장 상위 폴더")
    parser.add_argument("--state-dir", default="state", help="실행 간 유지할 캐시/기록 폴더")
    return parser.parse_args()


def choose_attachments(result: NotebookResult) -> list[Path]:
    """Telegram은 읽기 좋은 요약을 본문으로 보내고, 의미 있는 결과 파일만 첨부한다.

    성공/부분성공 시 notebook_output.txt와 runner.log를 보내지 않는다.
    이 두 파일에는 tqdm 진행률·전체 종목 검사 로그가 들어 있어 모바일에서 매우 지저분하다.
    실패/시간초과일 때만 디버깅을 위해 로그를 첨부한다.
    """
    candidates = [Path(x) for x in result.files]
    preferred: list[Path] = []
    data_suffixes = {".csv", ".json", ".xlsx", ".png", ".pdf"}
    for path in candidates:
        if path.suffix.lower() in data_suffixes and path.name != "result.json":
            preferred.append(path)

    if result.status in {"FAILED", "TIMEOUT"}:
        for name in ("runner.log", "notebook_output.txt"):
            path = Path(result.run_dir) / name
            if path.is_file() and path not in preferred:
                preferred.append(path)

    return preferred[:6]


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    jobs = select_jobs(load_jobs(project_root), args.jobs)
    run_started = datetime.now(KST)
    run_id = run_started.strftime("%Y%m%d_%H%M%S_KST")
    output_root = (project_root / args.output_base / run_id).resolve()
    state_dir = (project_root / args.state_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    results: list[NotebookResult] = []
    for index, job in enumerate(jobs, start=1):
        print(f"\n{'=' * 80}\n[{index}/{len(jobs)}] {job.title} 시작\n{'=' * 80}", flush=True)
        result = run_notebook_job(project_root, output_root, state_dir, job)
        results.append(result)
        print(result.summary, flush=True)

    counts = {status: sum(r.status == status for r in results) for status in ("SUCCESS", "PARTIAL", "FAILED", "TIMEOUT")}
    aggregate = {
        "run_id": run_id,
        "started_at_kst": run_started.isoformat(),
        "jobs_requested": args.jobs,
        "counts": counts,
        "results": [r.to_dict() for r in results],
        "telegram": {"attempted": not args.no_telegram, "success": None, "errors": []},
    }

    telegram_errors: list[str] = []
    if not args.no_telegram:
        try:
            telegram = TelegramClient.from_env()
            identity = telegram.validate()
            telegram.send_text(
                "📡 시장 스크리너 자동실행 시작\n"
                f"기준시각: {run_started.strftime('%Y-%m-%d %H:%M:%S KST')}\n"
                f"작업수: {len(results)}개\n"
                f"Bot: @{identity['bot']} / 대상: {identity['chat']}"
            )
            for result in results:
                icon = {"SUCCESS": "✅", "PARTIAL": "⚠️", "FAILED": "❌", "TIMEOUT": "⏱️"}.get(result.status, "ℹ️")
                telegram.send_text(f"{icon} {result.summary}")
                if not args.no_documents:
                    errors = telegram.send_documents(choose_attachments(result), caption_prefix=result.title)
                    telegram_errors.extend(errors)
            telegram.send_text(
                "🏁 전체 실행 종료\n"
                f"성공 {counts['SUCCESS']} / 부분성공 {counts['PARTIAL']} / 실패 {counts['FAILED']} / 시간초과 {counts['TIMEOUT']}"
            )
            aggregate["telegram"]["success"] = not telegram_errors
        except Exception as exc:
            telegram_errors.append(f"{type(exc).__name__}: {exc}")
            aggregate["telegram"]["success"] = False
    aggregate["telegram"]["errors"] = telegram_errors

    summary_path = output_root / "run_summary.json"
    summary_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = project_root / args.output_base / "latest"
    if latest.exists() or latest.is_symlink():
        if latest.is_symlink() or latest.is_file():
            latest.unlink()
        else:
            shutil.rmtree(latest)
    shutil.copytree(output_root, latest)

    print(f"\n결과 폴더: {output_root}")
    print(f"요약 파일: {summary_path}")
    if telegram_errors:
        print("Telegram 오류:")
        for error in telegram_errors:
            print("-", error)

    # 실행 노트북이 실패하거나 Telegram 전송이 실패하면 Actions를 빨간색으로 표시.
    unhealthy = any(r.status in {"FAILED", "TIMEOUT", "PARTIAL"} for r in results)
    if not args.no_telegram and telegram_errors:
        unhealthy = True
    return 1 if unhealthy else 0


if __name__ == "__main__":
    raise SystemExit(main())
