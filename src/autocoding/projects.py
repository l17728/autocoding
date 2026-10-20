"""Local Owner's persistent project catalogue, with one Host/ledger per root."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from .core import DomainError, digest, encoded
from .global_history import GlobalHistory
from .host import Host
from .workspaces import git


def inspect_directory(value):
    path=Path(value).expanduser()
    if not path.is_absolute():
        raise DomainError('ABSOLUTE_PROJECT_PATH_REQUIRED')
    path=path.resolve()
    if path.exists() and not path.is_dir():
        raise DomainError('PROJECT_DIRECTORY_REQUIRED')
    result={'root':str(path),'exists':path.exists(),'empty':not path.exists() or not any(path.iterdir()),
            'git':False,'head':None,'dirty':False,'repository_root':None}
    if path.exists():
        try:
            top=Path(git(path,'rev-parse','--show-toplevel')).resolve()
            result.update(git=True,repository_root=str(top))
            result['dirty']=any(line!='?? .local/' for line in git(path,'status','--porcelain','--untracked-files=normal').splitlines())
            try:
                result['head']=git(path,'rev-parse','HEAD')
            except DomainError:
                pass
        except DomainError:
            pass
    result['can_execute']=bool(result['head'] and result['repository_root']==str(path) and not result['dirty'])
    return result


class Projects:
    def __init__(self, default):
        self.default=default
        self.core=default.core
        self.hosts={default.project_id:default}
        self.apps={}
        self.global_history=GlobalHistory(default)
        default.global_history=self.global_history
        self.lock=asyncio.Lock()
        self.pm_slots=asyncio.Semaphore(2)
        default.pm_slots=self.pm_slots
        self.started=False
        with self.core.connection() as conn:
            row=conn.execute('SELECT 1 FROM metadata WHERE key=?',('project:'+default.project_id,)).fetchone()
            if not row:
                record={'id':default.project_id,'name':default.root.name,'root':str(default.root),
                        'github_owner':'','github_repository':'','permission_mode':'bypass','revision':1}
                conn.execute('INSERT INTO metadata VALUES (?,?)',('project:'+default.project_id,encoded(record)))

    def list(self):
        with self.core.connection() as conn:
            rows=conn.execute("SELECT value FROM metadata WHERE key LIKE 'project:%' ORDER BY key").fetchall()
        return [json.loads(r['value']) for r in rows]

    def get(self, pid):
        record=next((r for r in self.list() if r['id']==pid),None)
        if record is None:
            raise DomainError('NOT_FOUND')
        return record

    @staticmethod
    def validate(data):
        if not data['name'].strip() or len(data['name'])>100:
            raise DomainError('INVALID_PROJECT_NAME')
        owner,repo=data['github_owner'],data['github_repository']
        if bool(owner)!=bool(repo) or (owner and (not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?',owner)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',repo) or repo in {'.','..'})):
            raise DomainError('INVALID_GITHUB_REPOSITORY')
        if data['permission_mode'] not in {'bypass','review'}:
            raise DomainError('INVALID_PERMISSION_MODE')

    async def start(self):
        self.started=True
        for record in self.list():
            try:
                host=self.host(record['id'])
            except DomainError:
                continue
            if host is self.default:
                host.recover()
            host.start_services()

    def host(self, pid):
        record=self.get(pid)
        if pid not in self.hosts:
            root=Path(record['root'])
            if not root.is_dir() or str(root.resolve())!=record['root']:
                raise DomainError('PROJECT_DIRECTORY_UNAVAILABLE')
            host=Host(root,self.default.storage/'projects'/pid,config=self.default.config,cold_start=self.default.cold_start)
            host.pm_slots=self.pm_slots
            host.global_history=self.global_history
            host.recover()
            self.hosts[pid]=host
            if self.started:
                host.start_services()
        host=self.hosts[pid]
        # Project policy belongs to this project's ledger, never another Host.
        settings={'permission_mode':encoded(record['permission_mode']),'catalogue_project':encoded(record)}
        with host.core.connection() as conn:
            stored={r['key']:r['value'] for r in conn.execute("SELECT key,value FROM metadata WHERE key IN ('permission_mode','catalogue_project')")}
            if stored!=settings:
                conn.execute('BEGIN IMMEDIATE')
                for key,value in settings.items():
                    conn.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value!=excluded.value',(key,value))
                conn.commit()
        return host

    async def register(self, data):
        async with self.lock:
            self.validate(data)
            info=await asyncio.to_thread(inspect_directory,data['root'])
            root=Path(info['root'])
            if root==root.parent:
                raise DomainError('FILESYSTEM_ROOT_NOT_PROJECT')
            pid='project_'+digest(str(root))[:16]
            if any(os.path.normcase(r['root'])==os.path.normcase(str(root)) for r in self.list()):
                raise DomainError('PROJECT_ALREADY_REGISTERED')
            if len(self.list())>=20:
                raise DomainError('PROJECT_LIMIT')
            if info['git'] and info['repository_root']!=str(root):
                raise DomainError('REPOSITORY_ROOT_REQUIRED')
            if data['initialize_git'] and not info['empty']:
                raise DomainError('GIT_INITIALIZATION_REQUIRES_EMPTY_DIRECTORY')
            root.mkdir(parents=True,exist_ok=True)
            if data['initialize_git']:
                await asyncio.to_thread(git,root,'init')
                await asyncio.to_thread(git,root,'-c','user.name=Project Owner','-c','user.email=owner@localhost',
                    '-c','commit.gpgsign=false','commit','--allow-empty','-m','Initialize project')
            record={k:data[k] for k in ('name','github_owner','github_repository','permission_mode')}
            record.update(id=pid,root=str(root),revision=1)
            with self.core.connection() as conn:
                conn.execute('INSERT INTO metadata VALUES (?,?)',('project:'+pid,encoded(record)))
            self.host(pid)
            return record

    async def update(self, pid, data):
        async with self.lock:
            self.validate(data)
            previous=self.get(pid)
            if data['revision']!=previous['revision']:
                raise DomainError('STALE_PROJECT_SETTINGS')
            record={**previous,**{k:data[k] for k in ('name','github_owner','github_repository','permission_mode')},
                    'revision':previous['revision']+1}
            with self.core.connection() as conn:
                conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),'project:'+pid))
            self.host(pid)
            return record

    async def remove(self, pid, confirmation):
        async with self.lock:
            record=self.get(pid)
            if pid==self.default.project_id or confirmation!=record['name']:
                raise DomainError('PROJECT_DELETE_FORBIDDEN')
            root=Path(record['root']).resolve()
            if root==root.parent or not root.is_dir():
                raise DomainError('PROJECT_DIRECTORY_UNAVAILABLE')
            host=self.hosts.get(pid)
            if host and (host.core.snapshot().get('live_ids') or any(t['state'] in {'leased','verifying'} for t in host.core.snapshot()['tasks'])):
                raise DomainError('PROJECT_DELETE_ACTIVE')
            if host:
                try:
                    await asyncio.wait_for(host.close(), timeout=15)
                except TimeoutError:
                    raise DomainError('PROJECT_DELETE_SHUTDOWN_TIMEOUT') from None
            await asyncio.to_thread(self._remove_directory, root, pid)
            with self.core.connection() as conn:
                conn.execute('DELETE FROM metadata WHERE key=?',('project:'+pid,))
            self.hosts.pop(pid,None);self.apps.pop(pid,None)
            return {'id':pid,'deleted':True}

    def _remove_directory(self, root, pid):
        """Remove a stopped project and only its platform-owned Git worktrees."""
        owned_workspace_root=(self.default.storage/'projects'/pid).resolve()
        listing=subprocess.run(['git','-C',str(root),'worktree','list','--porcelain'],capture_output=True,text=True,
                               encoding='utf-8',errors='replace')
        if listing.returncode:
            raise DomainError('PROJECT_DELETE_GIT_WORKTREE_CHECK_FAILED')
        linked=[]
        for line in listing.stdout.splitlines():
            if line.startswith('worktree '):
                candidate=Path(line.removeprefix('worktree ')).resolve()
                if candidate != root:
                    if owned_workspace_root not in candidate.parents:
                        raise DomainError('PROJECT_DELETE_EXTERNAL_WORKTREE')
                    linked.append(candidate)
        for workspace in linked:
            removed=subprocess.run(['git','-C',str(root),'worktree','remove','--force',str(workspace)],capture_output=True,text=True,
                                   encoding='utf-8',errors='replace')
            if removed.returncode:
                raise DomainError('PROJECT_DELETE_WORKTREE_CLEANUP_FAILED')
        subprocess.run(['git','-C',str(root),'worktree','prune'],capture_output=True,text=True,encoding='utf-8',errors='replace')
        try:
            shutil.rmtree(root, onerror=self._clear_readonly_and_retry)
        except OSError:
            raise DomainError('PROJECT_DELETE_DIRECTORY_FAILED') from None
        shutil.rmtree(self.default.storage/'projects'/pid,ignore_errors=True)

    @staticmethod
    def _clear_readonly_and_retry(operation, path, _exc_info):
        """Git may mark loose objects read-only on Windows; clear that bit before retrying."""
        try:
            os.chmod(path, stat.S_IWRITE)
            operation(path)
        except OSError:
            raise

    async def close(self):
        await asyncio.gather(*(h.close() for h in self.hosts.values()))


class ProjectDispatch:
    """Route both HTTP and WS through the selected project's full auth boundary."""
    def __init__(self, app, registry, token, origin, ui):
        self.app,self.registry,self.token,self.origin,self.ui=app,registry,token,origin,ui

    async def __call__(self, scope, receive, send):
        match=re.match(r'^/api/v1/projects/(project_[a-f0-9]{16})(?:/|$)',scope.get('path',''))
        if scope['type'] in {'http','websocket'} and match and match[1]!=self.registry.default.project_id:
            # Do not instantiate a Host or reveal catalogue entries before authentication.
            from .api import create_app
            headers=dict(scope.get('headers',[]))
            known=any(r['id']==match[1] for r in self.registry.list())
            if known:
                if headers.get(b'host',b'').decode()!=self.origin.split('://',1)[1] or headers.get(b'origin',b'').decode() not in ('',self.origin):
                    return await self.app(scope,receive,send)
                if scope['type']=='http':
                    try:
                        self.registry.tokens.authenticate(headers.get(b'authorization',b'').decode())
                    except DomainError:
                        return await self.app(scope,receive,send)
                elif match[1] not in self.registry.hosts:
                    return await self.app(scope,receive,send)
                # WS performs first-frame token authentication inside the project app.
                if match[1] not in self.registry.apps:
                    try:
                        host=self.registry.host(match[1])
                    except DomainError:
                        return await self.app(scope,receive,send)
                    self.registry.apps[match[1]]=create_app(host,self.token,origin=self.origin,ui=self.ui,registry=self.registry,child=True)
                    self.registry.apps[match[1]].state.tokens.records=self.registry.tokens.records
                return await self.registry.apps[match[1]](scope,receive,send)
        return await self.app(scope,receive,send)
