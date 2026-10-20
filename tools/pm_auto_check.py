"""PM-owned baseline verifier run inside an isolated project worktree."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def command(args):
    result = subprocess.run(args, cwd=Path.cwd(), text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(result.returncode)


def main():
    # Always reject whitespace damage in the candidate, even before a project
    # has chosen a language or test runner.
    command(['git', 'diff', '--check', 'HEAD'])
    root = Path.cwd()
    has_py_tests = (root / 'pytest.ini').exists() or (root / 'tests').is_dir()
    if has_py_tests:
        command([sys.executable, '-m', 'pytest', '-q'])
    package = root / 'package.json'
    if package.exists():
        # npm itself determines the project-local test script; --if-present
        # keeps the first scaffold task verifiable before tests are introduced.
        command(['npm.cmd' if sys.platform == 'win32' else 'npm', 'run', 'test', '--if-present'])
    print('PM automatic baseline verification passed.')


if __name__ == '__main__':
    main()
