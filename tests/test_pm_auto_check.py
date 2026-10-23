import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from pm_auto_check import package_setup_command, python_test_commands


class PMAutomaticCheckTests(unittest.TestCase):
    def test_runs_only_matching_unittest_when_pytest_is_missing(self):
        with patch.object(importlib.util,'find_spec',return_value=object()):
            self.assertEqual([[sys.executable,'-m','pytest','-q',str(ROOT/'tests'/'test_dashboard.py')]],
                             python_test_commands(ROOT,['src/autocoding/dashboard.py']))

    def test_skips_unrelated_tests_for_config_only_change(self):
        with patch.object(importlib.util,'find_spec',return_value=None):
            self.assertEqual([],python_test_commands(ROOT,['.github/workflows/ci.yml','.gitignore']))

    def test_falls_back_to_matching_unittest_module(self):
        with patch.object(importlib.util,'find_spec',return_value=None):
            self.assertEqual([[sys.executable,'-m','unittest','discover','-s',str(ROOT/'tests'),
                               '-p','test_pm_auto_check.py']],
                             python_test_commands(ROOT,['tools/pm_auto_check.py']))

    def test_does_not_treat_archived_evidence_as_product_python(self):
        with patch.object(importlib.util,'find_spec',return_value=object()):
            self.assertEqual([],python_test_commands(ROOT,[
                'evidence/task_06_ci_test_infra/second-review/verify.py']))

    def test_runs_full_suite_for_product_python_without_a_matching_test(self):
        with patch.object(importlib.util,'find_spec',return_value=object()):
            self.assertEqual([[sys.executable,'-m','pytest','-q']],
                             python_test_commands(ROOT,['src/autocoding/new_module.py']))

    def test_installs_locked_dependencies_in_a_clean_verification_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'package-lock.json').write_text('{}')
            self.assertEqual(['npm','ci','--no-audit','--no-fund'],
                             package_setup_command(root))
            (root/'node_modules').mkdir()
            self.assertIsNone(package_setup_command(root))
