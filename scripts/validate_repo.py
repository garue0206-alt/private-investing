from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ast

import nbformat

from src.config import load_jobs
from src.notebook_tools import _patch_source


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    jobs = load_jobs(root)
    for job in jobs:
        path = root / job.notebook
        nb = nbformat.read(path, as_version=4)
        nbformat.validate(nb)
        for idx, cell in enumerate(nb.cells):
            if cell.cell_type != "code":
                continue
            code = _patch_source(str(cell.source))
            try:
                ast.parse(code)
            except SyntaxError as exc:
                raise RuntimeError(f"{job.id} cell {idx} 문법 오류: {exc}") from exc
        print(f"OK {job.id}: {path.name} / {len(nb.cells)} cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
