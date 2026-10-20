"""Public distribution checks: both entry points and the self-contained skill work offline."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'wondercv'


class DistributionTest(unittest.TestCase):
    def test_original_entry_point_still_shows_help(self):
        result = subprocess.run([sys.executable, str(ROOT / 'wondercv.py'), '--help'],
                                cwd=tempfile.gettempdir(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('resume', result.stdout)
        self.assertIn('recover', result.stdout)

    def test_skill_runs_without_the_repository(self):
        with tempfile.TemporaryDirectory(prefix='wondercv-skill-') as directory:
            installed = Path(directory) / 'installed-skill'
            shutil.copytree(SKILL, installed)
            env = dict(os.environ, WONDERCV_TOKEN='synthetic-distribution-check', PYTHONDONTWRITEBYTECODE='1')
            env.pop('PYTHONPATH', None)
            args = [sys.executable, str(installed / 'scripts' / 'wondercv.py'), '--json', 'format', 'options']
            result = subprocess.run(args, cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertIn('font', data)
            self.assertIn('top_format', data)
            self.assertTrue((installed / 'SKILL.md').is_file())

    def test_installed_skill_includes_the_mit_permission_notice(self):
        notice = (SKILL / 'LICENSE').read_text()
        self.assertEqual(notice, (ROOT / 'LICENSE').read_text())
        self.assertIn('Permission is hereby granted', notice)


if __name__ == '__main__':
    unittest.main()
