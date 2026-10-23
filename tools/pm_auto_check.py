"""PM-owned baseline verifier run inside an isolated project worktree."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import importlib.util
import os
import tempfile


def command(args, *, env=None):
    result = subprocess.run(args, cwd=Path.cwd(), text=True, encoding='utf-8',
                            errors='replace', capture_output=True, env=env)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(result.returncode)


def changed_paths(root):
    result=subprocess.run(['git','diff','--name-only','HEAD^','HEAD'],cwd=root,
                          text=True,encoding='utf-8',errors='replace',capture_output=True)
    if result.returncode:
        result=subprocess.run(['git','diff-tree','--root','--no-commit-id','--name-only','-r','HEAD'],
                              cwd=root,text=True,encoding='utf-8',errors='replace',capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return [line.replace('\\','/') for line in result.stdout.splitlines() if line]


def python_test_commands(root, paths, interpreter=sys.executable):
    """Run tests relevant to changed Python files, without requiring pytest."""
    python_paths=[p for p in paths if p.endswith('.py') and p.split('/',1)[0] not in
                  {'evidence','reports','docs'}]
    if not python_paths:
        return []
    selected=set()
    has_product_python=False
    for path in python_paths:
        candidate=Path(path)
        if candidate.parts[0]=='tests' and candidate.name.startswith('test_'):
            selected.add(candidate.name)
        elif candidate.parts[0] in {'src','tools'}:
            has_product_python=True
            related=root/'tests'/('test_'+candidate.stem+'.py')
            if related.is_file():
                selected.add(related.name)
    if not selected and not has_product_python:
        return []
    if importlib.util.find_spec('pytest') is not None:
        targets=([str(root/'tests'/name) for name in sorted(selected)]
                 if selected else [])
        return [[interpreter,'-m','pytest','-q',*targets]]
    patterns=sorted(selected) or ['test_*.py']
    return [[interpreter,'-m','unittest','discover','-s',str(root/'tests'),'-p',pattern]
            for pattern in patterns]


def package_setup_command(root, npm='npm'):
    """Install a locked package in clean verifier worktrees before running tests."""
    if (root/'package-lock.json').is_file() and not (root/'node_modules').is_dir():
        return [npm,'ci','--no-audit','--no-fund']
    return None


def main():
    # Always reject whitespace damage in the candidate, even before a project
    # has chosen a language or test runner.
    command(['git', 'diff', '--check', 'HEAD'])
    root = Path.cwd()
    for check in python_test_commands(root,changed_paths(root)):
        command(check)
    package = root / 'package.json'
    if package.exists():
        # npm itself determines the project-local test script; --if-present
        # keeps the first scaffold task verifiable before tests are introduced.
        with tempfile.TemporaryDirectory(prefix='bookplayer-test-reports-') as reports:
            env = os.environ.copy()
            env['BOOKPLAYER_TEST_REPORTS_DIR'] = reports
            npm = 'npm.cmd' if sys.platform == 'win32' else 'npm'
            setup = package_setup_command(root, npm)
            if setup:
                command(setup, env=env)
            command([npm, 'run', 'test', '--if-present'], env=env)
    print('PM automatic baseline verification passed.')


if __name__ == '__main__':
    main()
