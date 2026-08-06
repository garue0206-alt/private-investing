from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import nbformat

from .config import JobConfig


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass
class NotebookResult:
    job_id: str
    title: str
    status: str
    duration_seconds: float
    return_code: int | None
    cell_error_count: int
    run_dir: str
    prepared_notebook: str
    executed_notebook: str | None
    output_text_file: str
    runner_log_file: str
    summary: str
    files: list[str]
    fatal_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def _remove_shell_magics(source: str) -> str:
    """GitHub Actions에서는 requirements.txt로 설치하므로 노트북 내 pip/magic을 제거한다."""
    output: list[str] = []
    skipping_continuation = False
    removed = 0
    for line in source.splitlines():
        stripped = line.lstrip()
        if skipping_continuation:
            removed += 1
            if not line.rstrip().endswith("\\"):
                skipping_continuation = False
            continue
        if stripped.startswith("!") or stripped.startswith("%pip") or stripped.startswith("%conda"):
            removed += 1
            skipping_continuation = line.rstrip().endswith("\\")
            continue
        output.append(line)
    if removed:
        output.insert(0, f"# runner: 노트북 내부 설치/magic {removed}줄 제거")
    return "\n".join(output).rstrip() + "\n"


def _patch_source(source: str) -> str:
    source = _remove_shell_magics(source)
    # Colab 전용 절대경로 및 로컬 캐시 경로를 GitHub Actions state로 이동.
    source = source.replace(
        'Path("/content/alt_fire_alarm_history.csv")',
        'Path(os.environ.get("ALT_FIRE_HISTORY_FILE", "alt_fire_alarm_history.csv"))',
    )
    source = re.sub(
        r'(?m)^CACHE_FILE\s*=\s*["\']obv_cache\.json["\']',
        'CACHE_FILE = os.environ.get("OBV_CACHE_FILE", "obv_cache.json")',
        source,
    )
    # notebook 환경에서 tqdm.notebook가 위젯 경고를 내는 경우가 있어 표준 tqdm으로 치환.
    source = source.replace("from tqdm.notebook import tqdm", "from tqdm.auto import tqdm")
    return source


def prepare_notebook(source_path: Path, destination_path: Path, job: JobConfig) -> None:
    nb = nbformat.read(source_path, as_version=4)
    nb = copy.deepcopy(nb)
    for cell in nb.cells:
        if cell.cell_type == "code":
            cell.source = _patch_source(str(cell.source))

    preamble = nbformat.v4.new_code_cell(
        source=(
            "# GitHub Actions runner compatibility preamble\n"
            "import os, sys, warnings\n"
            "from pathlib import Path\n"
            "os.environ.setdefault('MPLBACKEND', 'Agg')\n"
            "state_dir = Path(os.environ.get('SCREENER_STATE_DIR', 'state')).resolve()\n"
            "state_dir.mkdir(parents=True, exist_ok=True)\n"
            "warnings.filterwarnings('ignore', category=FutureWarning)\n"
            f"print('▶ runner job: {job.id} / {job.title}')\n"
            "print('▶ python:', sys.version.split()[0])\n"
        )
    )
    nb.cells.insert(0, preamble)
    for cell in nb.cells:
        if not cell.get("id"):
            cell["id"] = uuid.uuid4().hex[:8]
    nb.nbformat = 4
    nb.nbformat_minor = max(int(getattr(nb, "nbformat_minor", 0) or 0), 5)
    nb.metadata.setdefault("kernelspec", {"display_name": "Python 3", "language": "python", "name": "python3"})
    nbformat.validate(nb)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, destination_path)


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def execute_prepared_notebook(
    prepared_path: Path,
    executed_path: Path,
    run_dir: Path,
    job: JobConfig,
    env: dict[str, str],
) -> tuple[int | None, str, bool]:
    command = [
        sys.executable,
        "-m",
        "jupyter",
        "nbconvert",
        "--to",
        "notebook",
        "--execute",
        prepared_path.name,
        "--output",
        executed_path.name,
        f"--ExecutePreprocessor.timeout={job.cell_timeout_seconds}",
        "--ExecutePreprocessor.allow_errors=True",
        "--ExecutePreprocessor.kernel_name=python3",
    ]
    popen_kwargs: dict[str, Any] = {
        "cwd": str(run_dir),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **popen_kwargs)
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=job.total_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:
            output = ""
        output += f"\n[TIMEOUT] 전체 제한시간 {job.total_timeout_seconds}초 초과\n"
    return proc.returncode, strip_ansi(output), timed_out


def extract_notebook_output(executed_path: Path) -> tuple[str, int]:
    if not executed_path.is_file():
        return "", 0
    nb = nbformat.read(executed_path, as_version=4)
    chunks: list[str] = []
    error_count = 0
    for cell_index, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue
        for output in cell.get("outputs", []):
            output_type = output.get("output_type")
            if output_type == "stream":
                chunks.append(str(output.get("text", "")))
            elif output_type == "error":
                error_count += 1
                ename = output.get("ename", "Error")
                evalue = output.get("evalue", "")
                traceback = "\n".join(output.get("traceback", [])[-8:])
                chunks.append(f"\n[CELL {cell_index} ERROR] {ename}: {evalue}\n{traceback}\n")
            elif output_type in {"display_data", "execute_result"}:
                data = output.get("data", {})
                plain = data.get("text/plain")
                if plain:
                    chunks.append(str(plain) + "\n")
    text = strip_ansi("".join(chunks))
    return text, error_count


def _line_subset(lines: list[str], keywords: tuple[str, ...], limit: int = 80) -> list[str]:
    selected: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if any(k in s for k in keywords):
            selected.append(s)
    # 중복 제거, 순서 유지
    deduped = list(dict.fromkeys(selected))
    return deduped[:limit]


def build_summary(job: JobConfig, status: str, duration: float, text: str, error_count: int, files: list[Path]) -> str:
    lines = text.splitlines()
    picked: list[str]
    if job.id == "news":
        picked = _line_subset(lines, ("[코인 뉴스", "[한국 증시 뉴스", "[미국 증시 뉴스", "- "), 40)
    elif job.id == "alt_bull":
        picked = _line_subset(
            lines,
            ("현재 단계", "화재 점수", "데이터 범위", "Binance 보조검사", "점수 변화", "판정 설명", "기준 시각", "•"),
            30,
        )
    elif job.id == "leader_all":
        picked = _line_subset(
            lines,
            ("ALL-IN-ONE", "종목목록", "가격 다운로드", "종가배팅 레짐", "스윙 레짐", "최종 후보", "실패율", "결과 CSV", "오류 CSV"),
            30,
        )
    elif job.id == "leader_collection":
        picked = _line_subset(lines, ("최종", "후보", "포착", "통과", "추천", "관망", "경고", "실패", "종목"), 45)
    else:
        picked = _line_subset(lines, ("레짐", "신호", "후보", "진입", "자격", "LONG", "SHORT", "롱", "숏", "오류", "실패", "종목"), 55)

    if not picked:
        nonempty = [x.strip() for x in lines if x.strip()]
        picked = nonempty[-20:]

    file_names = [p.name for p in files if p.is_file()]
    header = [
        f"[{job.title}]",
        f"상태: {status} | 실행 {duration:.1f}초 | 셀 오류 {error_count}개",
    ]
    if file_names:
        header.append("생성파일: " + ", ".join(file_names[:8]))
    body = "\n".join(picked)
    summary = "\n".join(header + ([body] if body else []))
    if len(summary) > 3300:
        summary = summary[:3250] + "\n…(나머지는 첨부 로그 확인)"
    return summary


def collect_files(run_dir: Path, globs: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for pattern in globs:
        for path in sorted(run_dir.glob(pattern)):
            if path.is_file() and path not in found:
                found.append(path)
    return found


def run_notebook_job(project_root: Path, output_root: Path, state_dir: Path, job: JobConfig) -> NotebookResult:
    started = time.monotonic()
    run_dir = output_root / job.id
    run_dir.mkdir(parents=True, exist_ok=True)
    prepared = run_dir / "prepared.ipynb"
    executed = run_dir / "executed.ipynb"
    runner_log = run_dir / "runner.log"
    output_text = run_dir / "notebook_output.txt"
    fatal_error: str | None = None

    try:
        prepare_notebook(project_root / job.notebook, prepared, job)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "MPLBACKEND": "Agg",
                "TZ": "Asia/Seoul",
                "SCREENER_STATE_DIR": str(state_dir.resolve()),
                "ALT_FIRE_HISTORY_FILE": str((state_dir / "alt_fire_alarm_history.csv").resolve()),
                "OBV_CACHE_FILE": str((state_dir / "obv_cache.json").resolve()),
            }
        )
        return_code, process_log, timed_out = execute_prepared_notebook(prepared, executed, run_dir, job, env)
        runner_log.write_text(process_log, encoding="utf-8")
        notebook_text, cell_errors = extract_notebook_output(executed)
        output_text.write_text(notebook_text, encoding="utf-8")

        semantic_failure_markers = (
            "데이터 연결 실패",
            "프로그램은 정상 종료됐지만 현재 공개 데이터원에서 충분한 자료를 받지 못했습니다",
        )
        semantic_failure = any(marker in notebook_text for marker in semantic_failure_markers)

        if timed_out:
            status = "TIMEOUT"
        elif not executed.is_file():
            status = "FAILED"
        elif cell_errors or semantic_failure:
            status = "PARTIAL"
        elif return_code == 0:
            status = "SUCCESS"
        else:
            status = "FAILED"
    except Exception as exc:
        status = "FAILED"
        return_code = None
        cell_errors = 0
        fatal_error = f"{type(exc).__name__}: {exc}"
        runner_log.write_text(fatal_error + "\n", encoding="utf-8")
        output_text.write_text(fatal_error + "\n", encoding="utf-8")

    duration = time.monotonic() - started
    files = collect_files(run_dir, job.send_globs)
    summary = build_summary(job, status, duration, output_text.read_text(encoding="utf-8", errors="replace"), cell_errors, files)
    result = NotebookResult(
        job_id=job.id,
        title=job.title,
        status=status,
        duration_seconds=round(duration, 2),
        return_code=return_code,
        cell_error_count=cell_errors,
        run_dir=str(run_dir),
        prepared_notebook=str(prepared),
        executed_notebook=str(executed) if executed.is_file() else None,
        output_text_file=str(output_text),
        runner_log_file=str(runner_log),
        summary=summary,
        files=[str(x) for x in files],
        fatal_error=fatal_error,
    )
    (run_dir / "result.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return result
