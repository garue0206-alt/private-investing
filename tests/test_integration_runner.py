from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import nbformat

from src.config import JobConfig
from src.notebook_tools import run_notebook_job


class RunnerIntegrationTest(unittest.TestCase):
    def test_minimal_notebook_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notebooks").mkdir()
            notebook_path = root / "notebooks" / "smoke.ipynb"
            notebook = nbformat.v4.new_notebook(
                cells=[
                    nbformat.v4.new_code_cell("print('SMOKE_OK')\nfrom pathlib import Path\nPath('result.csv').write_text('a,b\\n1,2\\n', encoding='utf-8')")
                ]
            )
            nbformat.write(notebook, notebook_path)
            job = JobConfig(
                id="smoke",
                title="smoke",
                notebook="notebooks/smoke.ipynb",
                total_timeout_seconds=120,
                cell_timeout_seconds=60,
                send_globs=("*.csv", "notebook_output.txt", "runner.log"),
            )
            result = run_notebook_job(root, root / "outputs", root / "state", job)
            self.assertEqual(result.status, "SUCCESS", result.summary)
            self.assertIn("SMOKE_OK", Path(result.output_text_file).read_text(encoding="utf-8"))
            self.assertTrue((Path(result.run_dir) / "result.csv").is_file())


if __name__ == "__main__":
    unittest.main()
