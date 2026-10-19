import os
from pathlib import Path
import shutil


def native_executable(name):
    path = shutil.which(name + '.exe') or shutil.which(name)
    if path and Path(path).suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
        return path
    relative = {'claude': '@anthropic-ai/claude-code/bin/claude.exe',
                'opencode': 'opencode-ai/bin/opencode.exe'}.get(name)
    if relative:
        path = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules' / relative
        if path.is_file():
            return str(path)
    return None
