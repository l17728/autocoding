"""Registered Git worktrees. No implicit repository initialization or force cleanup."""
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from .core import DomainError, digest, encoded


def git(root, *args):
    environment = {k: v for k, v in os.environ.items() if k.upper() in {
        'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC', 'TEMP', 'TMP', 'HOME', 'USERPROFILE',
        'LANG', 'LC_ALL'}}
    environment.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL='NUL' if os.name == 'nt' else '/dev/null')
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=NUL' if os.name == 'nt' else 'core.hooksPath=/dev/null',
                                 '-C', str(root), *args], capture_output=True,
                                encoding='utf-8', errors='replace', timeout=60, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise DomainError('GIT_UNAVAILABLE') from None
    if result.returncode:
        raise DomainError('GIT_OPERATION_FAILED')
    return result.stdout.strip()


class Workspaces:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, repository, head, *, branch=False):
        repository = Path(repository).resolve(strict=True)
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', head):
            raise DomainError('INVALID_COMMIT')
        if Path(git(repository, 'rev-parse', '--show-toplevel')).resolve() != repository:
            raise DomainError('REPOSITORY_ROOT_REQUIRED')
        if git(repository, 'rev-parse', head + '^{commit}') != head:
            raise DomainError('COMMIT_MISMATCH')
        wid = 'workspace_' + uuid.uuid4().hex
        target = self.root / wid
        args = ['worktree', 'add']
        if branch:
            args += ['-b', 'agent/' + wid]
        else:
            args += ['--detach']
        git(repository, *args, str(target), head)
        record = dict(id=wid, repository=str(repository), path=str(target), base_sha=head,
                      branch=git(target, 'rev-parse', '--abbrev-ref', 'HEAD'))
        temporary = self.root / (wid + '.tmp')
        temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
        temporary.replace(self.root / (wid + '.json'))
        return record

    def get(self, workspace_id):
        if not re.fullmatch(r'workspace_[a-f0-9]{32}', workspace_id):
            raise DomainError('INVALID_WORKSPACE')
        try:
            record = json.loads((self.root / (workspace_id + '.json')).read_text(encoding='utf-8'))
            path = Path(record['path']).resolve(strict=True)
            if path != self.root / workspace_id or not path.is_dir():
                raise DomainError('WORKSPACE_ESCAPE')
            if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path:
                raise DomainError('WORKSPACE_MISMATCH')
            return record
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainError('WORKSPACE_UNAVAILABLE') from None

    def repair_evidence(self, repository, workspace_id, base_sha):
        if not re.fullmatch(r'workspace_[a-f0-9]{32}',workspace_id):
            raise DomainError('INVALID_WORKSPACE')
        repository=Path(repository).resolve(strict=True)
        target=self.root/workspace_id
        if not target.exists():
            raise DomainError('WORKSPACE_UNAVAILABLE')
        if target.resolve(strict=True)!=target or not target.is_dir():
            raise DomainError('WORKSPACE_ESCAPE')
        listing=git(repository,'worktree','list','--porcelain','-z')
        registered=[part[9:] for part in listing.split('\0') if part.startswith('worktree ')]
        if not any(Path(p).resolve()==target for p in registered):
            raise DomainError('WORKSPACE_NOT_REGISTERED_BY_GIT')
        common=Path(git(target,'rev-parse','--git-common-dir'))
        common=(common if common.is_absolute() else target/common).resolve()
        original=Path(git(repository,'rev-parse','--git-common-dir'))
        original=(original if original.is_absolute() else repository/original).resolve()
        if common!=original or Path(git(target,'rev-parse','--show-toplevel')).resolve()!=target:
            raise DomainError('WORKSPACE_REPOSITORY_MISMATCH')
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}',base_sha) or git(repository,'rev-parse',base_sha+'^{commit}')!=base_sha:
            raise DomainError('INVALID_COMMIT')
        file=self.root/(workspace_id+'.json')
        if file.is_symlink() or file.resolve()!=file:
            raise DomainError('WORKSPACE_ESCAPE')
        if file.exists() and file.stat().st_size>1024*1024:
            raise DomainError('WORKSPACE_REGISTRATION_TOO_LARGE')
        raw=file.read_bytes() if file.exists() else b''
        record=dict(id=workspace_id,repository=str(repository),path=str(target),base_sha=base_sha,
                    branch=git(target,'rev-parse','--abbrev-ref','HEAD'))
        evidence=dict(record=record,registration_hash=digest(raw.hex()),head_sha=git(target,'rev-parse','HEAD'),
                      status=git(target,'status','--porcelain=v1'),git_registration_hash=digest(listing))
        return dict(**evidence,fingerprint=digest(evidence))

    def repair(self, repository, workspace_id, base_sha, fingerprint):
        evidence=self.repair_evidence(repository,workspace_id,base_sha)
        if evidence['fingerprint']!=fingerprint:
            raise DomainError('WORKSPACE_REPAIR_EVIDENCE_CHANGED')
        destination=self.root/(workspace_id+'.json')
        if destination.exists():
            backup=self.root/(workspace_id+'.registration-backup-'+uuid.uuid4().hex+'.json')
            backup.write_bytes(destination.read_bytes())
        temporary=self.root/(workspace_id+'.repair-'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(encoded(evidence['record']),encoding='utf-8')
        temporary.replace(destination)
        return self.get(workspace_id)

    def file(self, workspace_id, relative):
        root = Path(self.get(workspace_id)['path'])
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or ':' in relative:
            raise DomainError('PATH_ESCAPE')
        result = (root / path).resolve()
        if not result.is_relative_to(root) or '.git' in path.parts:
            raise DomainError('PATH_ESCAPE')
        return result

    def remove(self, workspace_id, *, stopped=False):
        record = self.get(workspace_id)
        if not stopped:
            raise DomainError('PROCESS_STOP_REQUIRED')
        if git(record['path'], 'status', '--porcelain'):
            raise DomainError('WORKSPACE_DIRTY')
        # No --force; Git is the second guard against deleting user work.
        git(record['repository'], 'worktree', 'remove', record['path'])
        (self.root / (workspace_id + '.json')).unlink()
