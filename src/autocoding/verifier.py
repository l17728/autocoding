"""Execute owner-defined checks against a fresh, exact-commit worktree."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from .core import DomainError, digest
from .redaction import Redactor
from .workspaces import Workspaces, git


def execute(argv, cwd, *, timeout=300, redactor=None):
    if (not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv)
            or not Path(argv[0]).is_absolute() or Path(argv[0]).suffix.lower() in {'.cmd', '.bat', '.ps1'}
            or not 0 < timeout <= 3600):
        raise DomainError('INVALID_CHECK_COMMAND')
    sanitizer = redactor or Redactor()
    sensitive_names = ('KEY', 'TOKEN', 'PASSWORD', 'SECRET', 'CREDENTIAL')
    removed_secrets = [value for name, value in os.environ.items()
                       if any(part in name.upper() for part in sensitive_names) and value]
    sanitizer = Redactor((*sanitizer.secrets, *removed_secrets))
    with tempfile.TemporaryDirectory(prefix='verify-env-') as temp:
        env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC'}}
        env.update(TEMP=temp, TMP=temp, HOME=temp, USERPROFILE=temp, PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        started = time.monotonic()
        worker = Path(__file__).with_name('execution_worker.py')
        try:
            result = subprocess.run([sys.executable, '-I', str(worker)], input=json.dumps({
                'argv': argv, 'cwd': str(cwd), 'env': env}), capture_output=True,
                text=True, encoding='utf-8', timeout=timeout,
                creationflags=0x08000000 if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'passed': False, 'duration_seconds': time.monotonic() - started}
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {'status': 'runner_failed'}
        report['passed'] = (result.returncode == 0 and report.get('status') == 'finished'
                            and report.get('exit_code') == 0 and report.get('streams_complete') is True)
        streams = report.get('streams', {})
        for name in ('stdout', 'stderr'):
            stream = streams.get(name)
            if isinstance(stream, dict) and isinstance(stream.get('excerpt'), str):
                stream['excerpt'] = sanitizer.text(stream['excerpt'])[:16384]
        report['duration_seconds'] = time.monotonic() - started
        return report


class Verifier:
    def __init__(self, core, repository, storage, checks, redactor=None):
        self.core = core
        self.repository = Path(repository).resolve()
        self.storage = Path(storage).resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.workspaces = Workspaces(self.storage / 'worktrees')
        # Supplied by Owner configuration, never from an agent completion report.
        self.checks = checks
        self.redactor = redactor or Redactor()

    def run(self, job, *, timeout=300, guard=None):
        if guard:
            guard()
        with self.core.connection() as conn:
            task = self.core._get(conn, 'tasks', job['task_id'])
            criteria = json.loads(task['acceptance'])
            if (task['state'] != 'verifying' or task['verification_id'] != job['verification_id']
                    or task['revision'] != job['task_revision'] or task['head_sha'] != job['head_sha']):
                raise DomainError('STALE_VERIFICATION')
        if set(criteria) != set(self.checks):
            raise DomainError('CHECK_CONFIGURATION_INCOMPLETE')
        workspace = self.workspaces.create(self.repository, job['head_sha'])
        cwd = Path(workspace['path'])
        evidence, verdict = {}, {}
        for criterion in criteria:
            if git(cwd, 'rev-parse', 'HEAD') != job['head_sha'] or git(cwd, 'status', '--porcelain', '--untracked-files=no'):
                raise DomainError('VERIFICATION_WORKSPACE_CHANGED')
            result = execute(self.checks[criterion], cwd, timeout=timeout, redactor=self.redactor)
            if git(cwd, 'rev-parse', 'HEAD') != job['head_sha'] or git(cwd, 'status', '--porcelain', '--untracked-files=no'):
                result.update(passed=False, status='workspace_changed')
            evidence[criterion] = {**result, 'argv_hash': digest(self.checks[criterion])}
        artifact = {'job': job, 'workspace_id': workspace['id'], 'checks': evidence}
        data = json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
        sha = hashlib.sha256(data).hexdigest()
        path = self.storage / (sha + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
        for criterion, result in evidence.items():
            verdict[criterion] = {'passed': result['passed'], 'artifact_ref': 'sha256:' + sha}
        if guard:
            guard()
        state = self.core.verify(job, 'verifier:local', verdict, key='verification:' + job['verification_id'])
        return {**state, 'artifact': str(path), 'sha256': sha, 'checks': evidence}
