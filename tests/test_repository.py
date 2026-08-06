from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import nbformat

from src.config import load_jobs, select_jobs
from src.notebook_tools import _patch_source, prepare_notebook
from src.telegram_client import split_message


class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.jobs = load_jobs(cls.root)

    def test_exactly_five_jobs(self) -> None:
        self.assertEqual(len(self.jobs), 5)
        self.assertEqual(len({j.id for j in self.jobs}), 5)

    def test_job_selection(self) -> None:
        selected = select_jobs(self.jobs, "news,alt_bull")
        self.assertEqual([j.id for j in selected], ["alt_bull", "news"])
        with self.assertRaises(ValueError):
            select_jobs(self.jobs, "unknown")

    def test_magic_and_colab_path_are_patched(self) -> None:
        source = '!pip install abc \\\n    "def"\nfrom pathlib import Path\nHISTORY_FILE = Path("/content/alt_fire_alarm_history.csv")\n'
        patched = _patch_source(source)
        self.assertNotIn("!pip", patched)
        self.assertNotIn("/content/alt_fire_alarm_history.csv", patched)
        compile(patched, "<patched>", "exec")

    def test_prepare_every_notebook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for job in self.jobs:
                destination = tmp_path / f"{job.id}.ipynb"
                prepare_notebook(self.root / job.notebook, destination, job)
                notebook = nbformat.read(destination, as_version=4)
                nbformat.validate(notebook)
                self.assertGreater(len(notebook.cells), 0)

    def test_telegram_split_limit(self) -> None:
        text = "\n".join(["가" * 500 for _ in range(20)])
        chunks = split_message(text, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 1000 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
