from __future__ import annotations

import copy
import html as html_lib
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
PROGRESS_RE = re.compile(r"\b\d+\s*/\s*\d+\b.*(?:it/s|s/it|<\d{2}:\d{2}|\d+%\|)", re.I)
PERCENT_BAR_RE = re.compile(r"\b\d{1,3}%\|.*\|\s*\d+\s*/\s*\d+", re.I)


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
    # tqdm는 같은 줄을 \r로 덮어쓰므로 \r을 없애 붙이지 말고 줄바꿈으로 바꾼다.
    return ANSI_RE.sub("", text).replace("\r", "\n")


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
    source = source.replace(
        'Path("/content/alt_fire_alarm_history.csv")',
        'Path(os.environ.get("ALT_FIRE_HISTORY_FILE", "alt_fire_alarm_history.csv"))',
    )
    source = re.sub(
        r'(?m)^CACHE_FILE\s*=\s*["\']obv_cache\.json["\']',
        'CACHE_FILE = os.environ.get("OBV_CACHE_FILE", "obv_cache.json")',
        source,
    )
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


def _html_table_to_text(raw_html: str) -> str:
    """Jupyter Styler/DataFrame HTML을 텔레그램 요약에 쓸 수 있는 간단한 TSV로 변환."""
    if "<table" not in raw_html.lower():
        return ""
    text = re.sub(r"(?is)<br\s*/?>", " ", raw_html)
    text = re.sub(r"(?is)</tr\s*>", "\n", text)
    text = re.sub(r"(?is)</t[dh]\s*>", "\t", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html_lib.unescape(text)
    rows: list[str] = []
    for row in text.splitlines():
        cells = [re.sub(r"\s+", " ", c).strip() for c in row.split("\t")]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


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
                plain = str(data.get("text/plain") or "")
                raw_html = str(data.get("text/html") or "")
                # pandas Styler의 text/plain은 메모리 주소만 보여준다. 이때 HTML 표를 텍스트화한다.
                if "pandas.io.formats.style.Styler" in plain and raw_html:
                    table = _html_table_to_text(raw_html)
                    if table:
                        chunks.append(table + "\n")
                elif plain:
                    chunks.append(plain + "\n")
                elif raw_html:
                    table = _html_table_to_text(raw_html)
                    if table:
                        chunks.append(table + "\n")
    text = strip_ansi("".join(chunks))
    return text, error_count


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if PERCENT_BAR_RE.search(s) or PROGRESS_RE.search(s):
        return True
    if any(token in s for token in (
        "조건 검증 중:", "종목 분석 중:", "바닥 다지기 종목 분석 중:",
        "진행:", "스크리닝을 시작합니다", "스크리닝 시작", "분석 중...",
        "구글 뉴스에서", "▶ runner job:", "▶ python:",
    )):
        return True
    if s.startswith("<pandas.io.formats.style.Styler"):
        return True
    if re.match(r"^\d+단계:\s*.*(?:수집|분석)", s):
        return True
    if re.match(r"^\d+\s*/\s*\d+\s+\S+\s+완료$", s):
        return True
    # 진행률 블록 문자와 숫자만 잔뜩 남는 줄
    if ("█" in s or "▏" in s or "▎" in s or "▍" in s or "▋" in s) and re.search(r"\d+\s*/\s*\d+", s):
        return True
    return False


def _clean_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        s = re.sub(r"\s+", " ", raw).strip()
        if _is_noise_line(s):
            continue
        # 긴 구분선은 텔레그램에 가치가 없음
        if len(s) >= 20 and set(s) <= {"=", "-", "─", " ", "_"}:
            continue
        out.append(s)
    return out


def _dedupe(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(x for x in lines if x.strip()))


def _format_duration(seconds: float) -> str:
    seconds_i = int(round(seconds))
    if seconds_i < 60:
        return f"{seconds_i}초"
    m, s = divmod(seconds_i, 60)
    return f"{m}분 {s}초"


def _summary_news(lines: list[str]) -> list[str]:
    keep: list[str] = []
    category = None
    count = 0
    for line in lines:
        if line.startswith("[") and "뉴스 TOP 5" in line:
            category = line
            count = 0
            keep.append("\n📰 " + line.strip("[]"))
            continue
        if category and line.startswith("-"):
            if count < 5:
                keep.append("• " + line.lstrip("- "))
                count += 1
            continue
        if category and (line.startswith("🔗") or line.startswith("http://") or line.startswith("https://")):
            keep.append("  " + line)
    return keep


def _summary_alt(lines: list[str]) -> list[str]:
    keys = (
        "현재 단계", "화재 점수", "데이터 범위", "데이터 신뢰도", "Binance 보조검사",
        "점수 변화", "판정 설명", "기준 시각", "Binance 조회 실패/대체",
    )
    keep = [line for line in lines if any(k in line for k in keys)]
    notes = [line for line in lines if line.startswith("•")][:3]
    if notes:
        keep.append("\n📌 해석")
        keep.extend(notes)
    candidates = [line for line in lines if line.startswith("🔥 ") and "| 점화 " in line][:5]
    if candidates:
        keep.append("\n🔥 불꽃 후보 TOP 5")
        keep.extend(candidates)
    return _dedupe(keep)


def _take_table_after(lines: list[str], start: int, max_rows: int = 5) -> list[str]:
    rows: list[str] = []
    for line in lines[start + 1 : start + 12]:
        if "결과]" in line or "적합도" in line or line.startswith("🎯"):
            break
        if "없습니다" in line or "데이터 로드 실패" in line:
            rows.append(line)
            break
        if " | " in line:
            rows.append(line)
            if len(rows) >= max_rows + 1:  # 헤더 + 상위 N행
                break
    return rows


def _extract_tg_records(text: str) -> list[dict[str, Any]]:
    """노트북이 명시적으로 출력한 Telegram용 JSON 레코드를 읽는다.

    console/Jupyter 표를 추측해서 파싱하는 것보다 안정적이다.
    """
    records: list[dict[str, Any]] = []
    for raw in strip_ansi(text).splitlines():
        line = raw.strip()
        if not line.startswith("__TG__"):
            continue
        try:
            obj = json.loads(line[len("__TG__"):])
            if isinstance(obj, dict):
                records.append(obj)
        except Exception:
            continue
    return records


def _fmt_candidate(row: dict[str, Any], rank: int) -> str:
    name = str(row.get("종목명") or row.get("Name") or row.get("symbol") or row.get("종목코드") or "후보")
    code = str(row.get("종목코드") or row.get("Code") or "").strip()
    head = f"{rank}) {name}" + (f" ({code})" if code and code not in name else "")
    skip = {"종목명", "Name", "symbol", "종목코드", "Code"}
    details: list[str] = []
    preferred = [
        "현재가", "오늘등락률", "당일상승률", "고점대비조정", "돌파강도",
        "거래량비율", "거래대금", "거래대금(억)", "지지선", "손바뀜이력", "바닥대비상승률",
    ]
    for key in preferred:
        if key in row and key not in skip:
            val = str(row[key])
            label = "거래대금" if key == "거래대금(억)" else key
            details.append(f"{label} {val}")
    if not details:
        for key, val in row.items():
            if key in skip:
                continue
            details.append(f"{key} {val}")
            if len(details) >= 4:
                break
    return head + (" · " + " / ".join(details[:4]) if details else "")


def _summary_lowfloat_structured(records: list[dict[str, Any]]) -> list[str]:
    rec = next((r for r in records if str(r.get("id")) == "lowfloat_fire"), None)
    if not rec:
        return []
    if rec.get("error"):
        return [f"⚠️ 품절주 스크리너 내부 오류: {rec['error']}"]

    counts = rec.get("counts") if isinstance(rec.get("counts"), dict) else {}
    rows = rec.get("rows") if isinstance(rec.get("rows"), list) else []
    mode = str(rec.get("mode") or "LITE")
    keep = [
        f"모드: {mode} · 분석대상 {rec.get('total', '?')}개",
        "🔥 IGNITION {ign} · 🟠 PRE {pre} · 🟡 WATCH {watch} · 🔴 LATE {late}".format(
            ign=counts.get("IGNITION_점화", 0),
            pre=counts.get("PRE_점화직전", 0),
            watch=counts.get("WATCH_감시", 0),
            late=counts.get("LATE_과열주의", 0),
        ),
    ]
    warnings = rec.get("warnings") if isinstance(rec.get("warnings"), list) else []
    if warnings:
        keep.append("⚠️ 데이터 주의: " + " / ".join(map(str, warnings[:3])))
    if not rows:
        keep.append("\n📌 PRE/IGNITION 후보 없음")
        return keep

    keep.append("\n📌 PRE / IGNITION TOP 5")
    for i, row in enumerate(rows[:5], 1):
        if not isinstance(row, dict):
            continue
        sig = str(row.get("화재경보") or "")
        icon = "🔥" if sig == "IGNITION_점화" else "🟠"
        name = str(row.get("Name") or "후보")
        code = str(row.get("Code") or "")
        theme = str(row.get("Theme") or "테마 미상")
        keep.append(f"{icon} {i}) {name}" + (f" ({code})" if code else "") + f" · {theme}")
        keep.append(
            f"   최종 {row.get('최종점수', '?')} · 구조 {row.get('구조점수', '?')} · "
            f"수급 {row.get('종목수급점화점수', '?')} · 테마 {row.get('테마점화점수', '?')}"
        )
        ar = row.get("거래대금20배")
        r1 = row.get("ret1_%")
        br = row.get("테마_확산도_%")
        parts = []
        if isinstance(ar, (int, float)): parts.append(f"거래대금 {ar:.1f}배")
        if isinstance(r1, (int, float)): parts.append(f"오늘 {r1:+.1f}%")
        if isinstance(br, (int, float)): parts.append(f"테마확산 {br:.0f}%")
        if parts: keep.append("   " + " · ".join(parts))
        fc = row.get("추정유통시총_억원")
        mh = row.get("최대주주등지분율_%")
        ov = row.get("희석위험프록시_%")
        parts2 = []
        if isinstance(fc, (int, float)): parts2.append(f"유통시총≈{fc:.0f}억")
        if isinstance(mh, (int, float)): parts2.append(f"최대주주등 {mh:.1f}%")
        if isinstance(ov, (int, float)): parts2.append(f"희석프록시 {ov:.1f}%")
        if parts2: keep.append("   " + " · ".join(parts2))
    return keep


def _summary_leader_collection_structured(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return []
    by_id = {str(r.get("id")): r for r in records if r.get("id")}
    order = [
        "close_bet_volume",
        "pullback_support",
        "breakout_60d",
        "first_volume_candle",
        "bottom_leader",
        "swing_ma60_convergence",
        "rotation_support",
    ]
    keep: list[str] = []
    for sid in order:
        rec = by_id.get(sid)
        if not rec:
            continue
        title = str(rec.get("title") or sid)
        err = rec.get("error")
        rows = rec.get("rows") if isinstance(rec.get("rows"), list) else []
        total = int(rec.get("total") or len(rows))
        keep.append(f"\n📌 {title}")
        if err:
            keep.append(f"⚠️ 데이터 오류: {err}")
        elif not rows:
            keep.append("후보 없음")
        else:
            keep.append(f"후보 {total}개" + (" · 상위 5개 표시" if total > 5 else ""))
            for rank, row in enumerate(rows[:5], start=1):
                if isinstance(row, dict):
                    keep.append(_fmt_candidate(row, rank))

    close_reg = by_id.get("close_bet_regime")
    if close_reg:
        pct = close_reg.get("pct", "?")
        label = close_reg.get("label", "")
        icon = "🟢" if label == "추천" else ("🟡" if label == "주의" else "🔴")
        keep.append(f"\n{icon} 종가배팅 레짐: {close_reg.get('score')}/{close_reg.get('max_score')} ({pct}%) · {label}")

    swing_reg = by_id.get("swing_regime")
    if swing_reg:
        pct = swing_reg.get("pct", "?")
        label = swing_reg.get("label", "")
        icon = "🟢" if label == "추천" else ("🟡" if label == "선별 진입" else "🔴")
        keep.append(f"{icon} 스윙 레짐: {swing_reg.get('score')}/{swing_reg.get('max_score')} ({pct}%) · {label}")
        warnings = swing_reg.get("warnings") if isinstance(swing_reg.get("warnings"), list) else []
        if warnings:
            keep.append("⚠️ 보유자 경고: " + " / ".join(map(str, warnings[:3])))
    return keep


def _summary_leader_collection(lines: list[str]) -> list[str]:
    keep: list[str] = []
    for i, line in enumerate(lines):
        if any(tag in line for tag in (
            "[종가배팅 필터링 결과]", "[눌림목 지지 필터링 결과]", "돌파 종가마감 결과]",
            "장대양봉 종가강세 결과]", "[영상 참조:", "[순환매 선취매 타점:",
        )):
            keep.append("\n📌 " + line.strip("🔥📊 "))
            keep.extend(_take_table_after(lines, i, 5))
        elif any(tag in line for tag in (
            "🟢 [추천]", "🟡 [주의]", "🔴 [비추천]", "스윙 적합도 점수:",
            "🟡 [선별 진입]", "🔴 [관망]", "주요 추세 이탈 신호 없음",
            "보유자 경고 신호", "데이터 로드 실패:",
        )):
            keep.append(line)
        elif "조건" in line and "만족하는 종목이 없습니다" in line:
            keep.append(line)
    return _dedupe(keep)[:45]


def _summary_crypto(lines: list[str]) -> list[str]:
    keep: list[str] = []

    # OBV / 휩소는 상위 후보 몇 개만 보여준다. 전체 수십 종목 표는 보내지 않는다.
    for i, line in enumerate(lines):
        if line.startswith("전체 분석:"):
            keep.append("\n📌 OBV 매집 스크리너")
            keep.append(line)
            rows = []
            for x in lines[i + 1 : i + 10]:
                if x.startswith("[") or "※" in x:
                    break
                if x and not _is_noise_line(x):
                    rows.append(x)
            keep.extend(rows[:4])  # 헤더 + TOP3 정도
        elif line.startswith("휩소 후보:"):
            keep.append("\n📌 휩소 후보")
            keep.append(line)
            rows = []
            for x in lines[i + 1 : i + 10]:
                if x.startswith("[") or "※" in x:
                    break
                if x and not _is_noise_line(x):
                    rows.append(x)
            keep.extend(rows[:4])

    # 전략별 핵심 판정만 수집
    signal_lines = [x for x in lines if "현재신호=" in x and "현재신호=없음" not in x]
    if signal_lines:
        keep.append("\n📌 평균회귀 현재 신호")
        keep.extend(signal_lines[:6])
    elif any("횡보장 평균회귀 백테스트" in x for x in lines):
        keep.append("\n📌 평균회귀: 현재 즉시 신호 없음")

    for line in lines:
        if any(tag in line for tag in (
            "강함★★★ 0개", "※ 강함★★★", "조건 만족 후보 없음",
            "[시장 종합]", ">>>", "✓ 숏 진입 자격 통과:",
            "✓ 롱 진입 자격 통과:", "지금 숏 진입 자격을 통과한 종목이 없습니다",
            "지금 롱 진입 자격을 통과한 종목이 없습니다", "롱 자격 종목 없음",
        )):
            if "[등급]" not in line:
                keep.append(line)

    # 실제 자격 통과 블록만: 부적격 사유 10개를 Telegram에 쏟지 않는다.
    for i, line in enumerate(lines):
        if not line.startswith("──"):
            continue
        window = lines[i + 1 : i + 12]
        if not any("✓" in x and ("롱" in x or "숏" in x or "추세 확인" in x) for x in window):
            continue
        keep.append("\n🎯 " + line.lstrip("─ "))
        for x in window:
            if any(k in x for k in ("✓", "진입가:", "손절가:", "익절가:", "리스크%:", "손익비:")):
                keep.append(x)

    failures = [x for x in lines if " 실패:" in x or "조회 실패:" in x]
    if failures:
        keep.append(f"\n⚠️ 데이터 조회 실패 {len(failures)}건 — 해당 종목은 판정에서 제외")

    return _dedupe(keep)[:60]


def _summary_leader_all(lines: list[str]) -> list[str]:
    keys = (
        "ALL-IN-ONE 실행 요약", "종목목록:", "가격 다운로드:", "종가배팅 레짐:",
        "스윙 레짐:", "최종 후보:", "다운로드 실패율", "오늘 7개 전략",
    )
    return _dedupe([line for line in lines if any(k in line for k in keys)])[:25]


def build_summary(job: JobConfig, status: str, duration: float, text: str, error_count: int, files: list[Path]) -> str:
    lines = _clean_lines(text)
    tg_records = _extract_tg_records(text)

    if job.id == "news":
        picked = _summary_news(lines)
    elif job.id == "alt_bull":
        picked = _summary_alt(lines)
    elif job.id == "leader_all":
        picked = _summary_leader_all(lines)
    elif job.id == "leader_collection":
        picked = _summary_leader_collection_structured(tg_records) or _summary_leader_collection(lines)
    elif job.id == "lowfloat_fire":
        picked = _summary_lowfloat_structured(tg_records)
    else:
        picked = _summary_crypto(lines)

    if not picked:
        picked = [x for x in lines[-15:] if not _is_noise_line(x)]

    icon = {"SUCCESS": "✅", "PARTIAL": "⚠️", "FAILED": "❌", "TIMEOUT": "⏱️"}.get(status, "ℹ️")
    header = [
        f"{icon} [{job.title}]",
        f"상태 {status} · 실행 {_format_duration(duration)} · 셀 오류 {error_count}개",
    ]
    if status in {"FAILED", "TIMEOUT"}:
        header.append("디버그 로그는 실패한 경우에만 별도 첨부합니다.")

    body = "\n".join(picked).strip()
    summary = "\n".join(header + (["", body] if body else []))
    if len(summary) > 3400:
        summary = summary[:3350].rstrip() + "\n… 핵심 결과만 표시했습니다."
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
        tg_records = _extract_tg_records(notebook_text)
        structured_error = any(bool(r.get("error")) for r in tg_records)

        if timed_out:
            status = "TIMEOUT"
        elif not executed.is_file():
            status = "FAILED"
        elif cell_errors or semantic_failure or structured_error:
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
