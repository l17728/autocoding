"""Build and verify an isolated integration candidate without moving main."""
import hashlib
import json
from pathlib import Path

from .core import DomainError, digest, encoded
from .verifier import execute
from .workspaces import Workspaces, git


class Integration:
    def __init__(self, core, repository, storage, checks):
        self.core, self.repository = core, Path(repository).resolve()
        self.storage = Path(storage).resolve()
        self.workspaces = Workspaces(self.storage / 'worktrees')
        self.checks = checks

    def prepare(self, base_sha, task_ids, *, timeout=60):
        if (not isinstance(task_ids, list) or not task_ids or len(set(task_ids)) != len(task_ids)
                or not isinstance(self.checks, dict) or not self.checks):
            raise DomainError('INTEGRATION_INPUT_INVALID')
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            tasks = [dict(self.core._get(conn, 'tasks', tid)) for tid in task_ids]
            for task in tasks:
                if task['state'] != 'done' or not task['head_sha']:
                    raise DomainError('TASK_NOT_VERIFIED')
                event = conn.execute("SELECT payload FROM events WHERE type='VerificationFinished' AND task_id=? "
                                     "ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
                verification = json.loads(event['payload']) if event else {}
                if (verification.get('passed') is not True or verification.get('head_sha') != task['head_sha']
                        or verification.get('attempt') != task['attempt']):
                    raise DomainError('TASK_NOT_VERIFIED')
                task['completed_verification_id'] = verification['verification_id']
                unmet = conn.execute("SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.depends_on "
                                     "WHERE d.task_id=? AND p.state!='done'", (task['id'],)).fetchone()
                if unmet:
                    raise DomainError('DEPENDENCY_NOT_VERIFIED')
            conn.commit()
        record = self.workspaces.create(self.repository, base_sha, branch=True)
        root = record['path']
        evidence = {'base_sha': base_sha, 'workspace': record,
                    'tasks': [{'id': t['id'], 'revision': t['revision'], 'head_sha': t['head_sha'],
                               'verification_id': t['completed_verification_id']} for t in tasks], 'checks': {}}
        for task in tasks:
            try:
                git(root, '-c', 'user.name=Autocoding Integration', '-c', 'user.email=integration@localhost',
                    '-c', 'commit.gpgSign=false', 'merge', '--no-edit', '--no-ff', '--no-verify', task['head_sha'])
            except DomainError:
                # Preserve conflict files in the registered worktree for review.
                if not git(root, 'diff', '--name-only', '--diff-filter=U'):
                    raise
                evidence.update(state='conflict', conflicting_task=task['id'])
                return self._save(evidence)
        head = git(root, 'rev-parse', 'HEAD')
        evidence['head_sha'] = head
        with self.core.connection() as conn:
            for task in tasks:
                dependencies = conn.execute('SELECT p.head_sha FROM dependencies d JOIN tasks p ON p.id=d.depends_on '
                                            'WHERE d.task_id=?', (task['id'],)).fetchall()
                for dependency in dependencies:
                    if not dependency['head_sha']:
                        raise DomainError('DEPENDENCY_NOT_IN_CANDIDATE')
                    try:
                        git(root, 'merge-base', '--is-ancestor', dependency['head_sha'], head)
                    except DomainError:
                        raise DomainError('DEPENDENCY_NOT_IN_CANDIDATE') from None
        for name, argv in self.checks.items():
            if git(root, 'rev-parse', 'HEAD') != head or git(root, 'status', '--porcelain', '--untracked-files=no'):
                raise DomainError('INTEGRATION_WORKSPACE_CHANGED')
            result = execute(argv, root, timeout=timeout)
            if git(root, 'rev-parse', 'HEAD') != head or git(root, 'status', '--porcelain', '--untracked-files=no'):
                result.update(passed=False, status='workspace_changed')
            evidence['checks'][name] = {**result, 'argv_hash': digest(argv)}
        evidence['state'] = 'verified_candidate' if all(c['passed'] for c in evidence['checks'].values()) else 'checks_failed'
        # Recheck task versions after external work. Cancellation invalidates
        # the candidate even if the operating-system checks passed.
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for before in tasks:
                current = self.core._get(conn, 'tasks', before['id'])
                if any(current[k] != before[k] for k in ('revision','state','head_sha','verification_id')):
                    evidence['state'] = 'stale'
            result = self._save(evidence)
            self.core._event(conn, 'IntegrationVerified', 'verifier:local', state=evidence['state'],
                             head_sha=head, task_ids=task_ids, artifact_ref='sha256:' + result['sha256'])
            conn.commit()
        return result

    def _save(self, evidence):
        data = encoded(evidence).encode('utf-8')
        sha = hashlib.sha256(data).hexdigest()
        path = self.storage / (sha + '.json')
        path.write_bytes(data)
        return {**evidence, 'artifact': str(path), 'sha256': sha, 'published': False}
