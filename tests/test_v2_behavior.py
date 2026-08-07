from __future__ import annotations

import unittest
from pathlib import Path

import nbformat

from src.config import load_jobs
from src.main import choose_attachments
from src.notebook_tools import NotebookResult, _clean_lines, build_summary


class V2BehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.jobs = {j.id: j for j in load_jobs(cls.root)}

    def test_progress_lines_are_removed(self) -> None:
        text = "바닥 다지기 종목 분석 중: 10%|██ | 15/150 [00:18<03:05, 1.37s/it]\n최종 후보: 2개"
        lines = _clean_lines(text)
        self.assertEqual(lines, ["최종 후보: 2개"])

    def test_news_summary_keeps_urls(self) -> None:
        text = "[코인 뉴스 TOP 5]\n- 제목 A\n🔗 https://example.com/a\n"
        summary = build_summary(self.jobs["news"], "SUCCESS", 5, text, 0, [])
        self.assertIn("https://example.com/a", summary)
        self.assertIn("제목 A", summary)

    def test_success_does_not_attach_raw_logs(self) -> None:
        result = NotebookResult(
            job_id="news", title="news", status="SUCCESS", duration_seconds=1,
            return_code=0, cell_error_count=0, run_dir=str(self.root),
            prepared_notebook="", executed_notebook=None,
            output_text_file=str(self.root / "README.md"), runner_log_file=str(self.root / "README.md"),
            summary="ok", files=[str(self.root / "README.md")], fatal_error=None,
        )
        self.assertEqual(choose_attachments(result), [])

    def test_alt_notebook_has_reliability_weighting(self) -> None:
        nb = nbformat.read(self.root / "notebooks/02_alt_bull_fire_alarm.ipynb", as_version=4)
        source = "\n".join(str(c.source) for c in nb.cells if c.cell_type == "code")
        self.assertIn("success_ratio", source)
        self.assertIn("10 * binance_reliability", source)
        self.assertIn("데이터 신뢰도", source)

    def test_news_notebook_prints_original_links(self) -> None:
        nb = nbformat.read(self.root / "notebooks/03_market_news.ipynb", as_version=4)
        source = "\n".join(str(c.source) for c in nb.cells if c.cell_type == "code")
        self.assertGreaterEqual(source.count("print(f\"🔗 {row['원문링크']}\")"), 3)


if __name__ == "__main__":
    unittest.main()
