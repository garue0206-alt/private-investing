from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class JobConfig:
    id: str
    title: str
    notebook: str
    total_timeout_seconds: int
    cell_timeout_seconds: int
    send_globs: tuple[str, ...]


def load_jobs(project_root: Path, config_path: str = "config/jobs.json") -> list[JobConfig]:
    path = project_root / config_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs: list[JobConfig] = []
    seen: set[str] = set()
    for item in raw:
        job = JobConfig(
            id=str(item["id"]),
            title=str(item["title"]),
            notebook=str(item["notebook"]),
            total_timeout_seconds=int(item["total_timeout_seconds"]),
            cell_timeout_seconds=int(item["cell_timeout_seconds"]),
            send_globs=tuple(str(x) for x in item.get("send_globs", [])),
        )
        if job.id in seen:
            raise ValueError(f"중복 job id: {job.id}")
        if not (project_root / job.notebook).is_file():
            raise FileNotFoundError(f"노트북 파일 없음: {job.notebook}")
        if job.total_timeout_seconds < 60 or job.cell_timeout_seconds < 30:
            raise ValueError(f"제한시간이 너무 짧음: {job.id}")
        seen.add(job.id)
        jobs.append(job)
    if not jobs:
        raise ValueError("jobs.json에 실행 작업이 없습니다.")
    return jobs


def select_jobs(jobs: Iterable[JobConfig], selection: str) -> list[JobConfig]:
    all_jobs = list(jobs)
    selection = selection.strip()
    if not selection or selection.lower() == "all":
        return all_jobs
    wanted = {x.strip() for x in selection.split(",") if x.strip()}
    known = {j.id for j in all_jobs}
    unknown = wanted - known
    if unknown:
        raise ValueError(f"알 수 없는 작업: {', '.join(sorted(unknown))}")
    return [j for j in all_jobs if j.id in wanted]
