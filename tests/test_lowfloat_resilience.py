import json
from pathlib import Path
import unittest
import nbformat

ROOT = Path(__file__).resolve().parents[1]

class LowFloatResilienceTests(unittest.TestCase):
    def test_lowfloat_notebook_has_empty_result_guards(self):
        nb = nbformat.read(ROOT / 'notebooks/06_theme_fire_alarm.ipynb', as_version=4)
        src='\n'.join(str(c.source) for c in nb.cells if c.cell_type=='code')
        self.assertIn("if hist.empty:", src)
        self.assertIn("elif candidate.empty:", src)
        self.assertIn("DART_FALLBACK", src)
        self.assertIn('if "result" not in globals()', src)
        self.assertIn('LOWFLOAT_WARNINGS', src)

    def test_partial_no_longer_fails_entire_action(self):
        text=(ROOT/'src/main.py').read_text(encoding='utf-8')
        self.assertIn('r.status in {"FAILED", "TIMEOUT"}', text)
        self.assertIn('r.status == "PARTIAL" and r.cell_error_count > 0', text)
        self.assertNotIn('r.status in {"FAILED", "TIMEOUT", "PARTIAL"}', text)

if __name__ == '__main__':
    unittest.main()
