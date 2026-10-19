"""Managed task execution: claim, isolated worktree, Agent, commit, verification.

PM handles structured questions and approvals. Unknown delivery, takeover and restart
stop automation and keep the task blocked rather than retrying external work.
"""
import asyncio
import fnmatch
import json
from pathlib import Path
import uuid

from .core import DomainError, digest, encoded
from .verifier import Verifier
from .workspaces import git
from .completion import checkpoint

FINAL = {'done', 'rework_required', 'reconciling', 'reconciled', 'failed'}


class Workflow:
    def __init__(self, host):
        self.host, self.core = host, host.core
        self.active = {}
        self.guards = {}

    def list(self):
        with self.core.connection() as conn:
            rows = [json.loads(r['value']) for r in conn.execute(
                "SELECT value FROM metadata WHERE key LIKE 'run:%'")]
        return sorted(rows, key=lambda r: r.get('created_at', 0))

    def save(self, record, state, **values):
        record.update(state=state, **values)
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('run:' + record['id'], encoded(record)))
            self.core._event(conn, 'TaskRunChanged', 'runner', task=record['task_id'],
                             run_id=record['id'], state=state, reason=record.get('reason'))
            conn.commit()

    def block(self, record, reason):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            task = self.core._get(conn, 'tasks', record['task_id'])
            if task['agent'] == record['agent_id'] and task['state'] in {'leased', 'verifying'}:
                conn.execute("UPDATE tasks SET state='blocked',revision=revision+1,reason=? WHERE id=?",
                             (reason, record['task_id']))
                self.core._event(conn, 'TaskBlocked', 'runner', task=record['task_id'], reason=reason)
            conn.commit()
        self.save(record, 'reconciling', reason=reason)

    def recover(self):
        for record in self.list():
            if record['state'] not in FINAL:
                with self.core.connection() as conn:
                    task = self.core._get(conn, 'tasks', record['task_id'])
                if task['state'] == 'done' and task['head_sha'] == record.get('head_sha'):
                    self.save(record, 'done')
                else:
                    self.block(record, 'RUN_RESTART_RECONCILIATION_REQUIRED')

    def configuration(self, task_id):
        try:
            from .acceptance import Acceptance
            config = Acceptance(self.host).load()
            if not isinstance(config, dict) or set(config) - {'checks', 'protected_paths'}:
                raise ValueError()
            checks, protected = config['checks'], config.get('protected_paths', [])
            if not isinstance(checks, dict) or not isinstance(protected, list) or any(
                    not isinstance(p, str) or not p or '\\' in p or p.startswith('/') or '..' in p.split('/') for p in protected):
                raise ValueError()
            with self.core.connection() as conn:
                task = dict(self.core._get(conn, 'tasks', task_id))
            criteria = json.loads(task['acceptance'])
            selected = {name: checks[name] for name in criteria}
            for argv in selected.values():
                if (not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv)
                        or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file()
                        or Path(argv[0]).suffix.lower() in {'.cmd', '.bat', '.ps1'}):
                    raise ValueError()
            return task, selected, protected
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainError('OWNER_CHECK_CONFIGURATION_REQUIRED') from None

    async def start(self, task_id, *, base_sha=None, max_seconds=600, batch_id=None, agent='claude'):
        if agent not in {'claude','codex','opencode'}:
            raise DomainError('UNSUPPORTED_AGENT')
        if self.host.paused:
            raise DomainError('PROJECT_PAUSED')
        with self.core.connection() as conn:
            managed=conn.execute("SELECT 1 FROM metadata WHERE key='catalogue_project'").fetchone()
        if managed:
            from .projects import inspect_directory
            status=await asyncio.to_thread(inspect_directory,str(self.host.root))
            if not status['can_execute']:
                raise DomainError('PROJECT_COMMIT_BASELINE_REQUIRED')
        if not 1 <= max_seconds <= 3600:
            raise DomainError('INVALID_RUN_TIMEOUT')
        if task_id in self.active:
            raise DomainError('TASK_RUN_ACTIVE')
        reserved = self.host.coordinator.reserved.get(task_id)
        if reserved is not None and reserved != batch_id:
            raise DomainError('TASK_RESERVED_BY_BATCH')
        task, checks, protected = self.configuration(task_id)
        if task['state'] != 'todo':
            raise DomainError('TASK_NOT_READY')
        # Reserve in memory before the first await so concurrent starts cannot
        # claim the same task while repository validation runs in a thread.
        self.active[task_id] = None
        try:
            head = base_sha or await asyncio.to_thread(git, self.host.root, 'rev-parse', 'HEAD')
            with self.core.connection() as conn:
                dependencies = [dict(r) for r in conn.execute('SELECT t.id,t.state,t.head_sha FROM dependencies d '
                    'JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=?', (task_id,))]
            if any(d['state'] != 'done' or not d['head_sha'] for d in dependencies):
                raise DomainError('DEPENDENCY_NOT_READY')
            run_id = 'run_' + uuid.uuid4().hex
            record = {'id': run_id, 'task_id': task_id, 'agent_id': 'agent:' + run_id,
                      'created_at': self.core.clock(), 'base_sha': head, 'provider':agent,
                      'checks_hash': digest(checks), 'protected_paths': protected}
            self.save(record, 'preparing')
            self.active[task_id] = self.host._background(self._perform(record, task, checks, dependencies, max_seconds))
            return dict(record)
        except BaseException:
            self.active.pop(task_id, None)
            raise

    @staticmethod
    def protected(relative, patterns):
        return any(fnmatch.fnmatchcase(relative.lower(), pattern.lower()) or
                   relative.lower().startswith(pattern.rstrip('/').lower() + '/') for pattern in patterns)

    def check_policy(self, record):
        _,checks,protected=self.configuration(record['task_id'])
        if digest(checks)!=record['checks_hash'] or protected!=record['protected_paths']:
            raise DomainError('OWNER_CHECK_CONFIGURATION_CHANGED')
        if record.get('session_id') and self.unresolved_interactions(record['session_id']):
            raise DomainError('RUN_INTERACTION_RECONCILIATION_REQUIRED')

    def unresolved_interactions(self, sid):
        with self.core.connection() as conn:
            late=conn.execute('SELECT 1 FROM metadata WHERE key=?',('late_interaction:'+sid,)).fetchone()
            approval=conn.execute("SELECT 1 FROM approvals a LEFT JOIN approval_delivery d ON a.id=d.approval_id "
                "WHERE a.session_id=? AND (a.state='pending' OR d.state IN ('pending','sending','delivery_unknown')) LIMIT 1",(sid,)).fetchone()
        return bool(late or approval) or any(q['session_id']==sid and q['state'] in {
            'pending_pm','awaiting_owner','sending','sent_unconfirmed','delivery_unknown'} for q in self.host.questions.list())

    def guard(self, sid, operation):
        if sid not in self.guards:
            return
        root, patterns = self.guards[sid]
        # Native Codex/OpenCode requests describe commands or permission patterns,
        # not Claude file-tool inputs. PM reviews these operations; protected
        # files are still checked on the sealed worktree before committing.
        if self.host.metadata.get(sid,{}).get('agent') in {'codex','opencode'}:
            return
        tool, body = operation.get('tool_name'), operation.get('input', {})
        if tool not in {'Read', 'Write', 'Edit', 'Glob', 'Grep'}:
            raise DomainError('WORKSPACE_OPERATION_REJECTED')
        raw = body.get('file_path') if tool in {'Read', 'Write', 'Edit'} else body.get('path', '.')
        if not isinstance(raw, str) or not raw:
            raise DomainError('WORKSPACE_OPERATION_REJECTED')
        path = Path(raw)
        target = (path if path.is_absolute() else root / path).resolve()
        if not target.is_relative_to(root):
            raise DomainError('WORKSPACE_OPERATION_REJECTED')
        relative = target.relative_to(root).as_posix()
        if any(p.lower() in {'.git', '.local'} for p in Path(relative).parts):
            raise DomainError('WORKSPACE_OPERATION_REJECTED')
        if tool == 'Glob' and ('..' in Path(body.get('pattern', '')).parts or Path(body.get('pattern', '')).is_absolute()):
            raise DomainError('WORKSPACE_OPERATION_REJECTED')
        if tool in {'Write', 'Edit'} and self.protected(relative, patterns):
            raise DomainError('PROTECTED_ACCEPTANCE_FILE')

    async def _perform(self, record, task, checks, dependencies, max_seconds):
        lease = None
        runtime = None
        try:
            workspace = await asyncio.to_thread(self.host.workspaces.create, self.host.root, record['base_sha'], branch=True)
            root = Path(workspace['path'])
            self.save(record, 'preparing', workspace_id=workspace['id'])
            for dep in dependencies:
                await asyncio.to_thread(git, root, '-c', 'user.name=Autocoding Runner',
                    '-c', 'user.email=runner@localhost', '-c', 'commit.gpgSign=false',
                    'merge', '--no-edit', '--no-ff', '--no-verify', dep['head_sha'])
            base = await asyncio.to_thread(git, root, 'rev-parse', 'HEAD')
            lease = self.core.claim(record['task_id'], record['agent_id'], lease_seconds=120, key=record['id'])
            self.save(record, 'starting', attempt=lease['attempt'], execution_base=base)
            session = await self.host.start(record.get('provider','claude'), workspace_id=workspace['id'], tools=True,
                task_scope={'task_id':record['task_id'], 'agent_id':record['agent_id'], 'attempt':lease['attempt']})
            sid = session['session_id']
            self.save(record, 'starting', session_id=sid)
            runtime = self.host.runtimes[sid]
            self.guards[sid] = (root, record['protected_paths'])
            takeover = await runtime.takeover('pm', 0, key='run-owner:' + record['id'])
            epoch = takeover['epoch']
            prompt = ('完成以下开发任务，仅修改当前工作树内的业务文件。不要提交 Git；平台会提交并运行独立验收。'
                      '不得修改受保护的验收文件。需要澄清时提出结构化问题，由 PM 或用户回答。完成修改后说明结果。\n' +
                      '先调用 autocoding MCP 的 get_instructions 获取任务上下文。MCP 知识仅供参考；完成提交由平台处理。\n' +
                      encoded({'task_id':record['task_id'], 'task':task['description'], 'acceptance':json.loads(task['acceptance']),
                               'protected_paths':record['protected_paths']}))
            previous = next((r for r in reversed(self.list()) if r['task_id']==record['task_id']
                             and r['state']=='rework_required'), None)
            if previous:
                prompt += '\n上次独立验收未通过的检查：' + encoded(previous.get('failed_checks',[]))
            command = await self.host.send(sid, epoch, prompt, key='run-input:' + record['id'], sender='pm')
            self.save(record, 'running', session_id=sid, command_id=command['command_id'],
                      command_ids=[command['command_id']],completion_checks=[],phase='implementation')
            loop = asyncio.get_running_loop()
            deadline, renewed = loop.time() + max_seconds, loop.time()
            while True:
                with self.core.connection() as conn:
                    current = self.core._get(conn, 'sessions', sid)
                    if current['owner'] != 'pm' or current['epoch'] != epoch:
                        raise DomainError('RUN_INPUT_TAKEN_OVER')
                state = self.core.command(command['command_id'])['state']
                if state == 'completed':
                    if any(s==sid for s,_ in self.host.deliveries) or self.unresolved_interactions(sid):
                        raise DomainError('RUN_INTERACTION_RECONCILIATION_REQUIRED')
                    next_check=checkpoint(len(record['completion_checks']))
                    if next_check is None:
                        break
                    if loop.time()>=deadline:
                        raise DomainError('RUN_TIMEOUT_RECONCILIATION_REQUIRED')
                    # Pausing stops new turns, even when the previous one completed.
                    if self.host.paused:
                        if loop.time()-renewed>=20:
                            self.core.heartbeat(record['task_id'],record['agent_id'],lease['lease_token'],
                                lease['attempt'],key='run-heartbeat:'+uuid.uuid4().hex)
                            renewed=loop.time()
                        await asyncio.sleep(.1)
                        continue
                    name,instruction=next_check
                    command=await self.host.send(sid,epoch,instruction+'\n'+encoded({
                        'task_id':record['task_id'],'task':task['description'],
                        'acceptance':json.loads(task['acceptance']),
                        'protected_paths':record['protected_paths']}),
                        key='run-checkpoint:'+record['id']+':'+name,sender='pm')
                    record['completion_checks'].append({'name':name,'command_id':command['command_id']})
                    record['command_ids'].append(command['command_id'])
                    self.save(record,'reviewing',phase=name,command_id=command['command_id'])
                    continue
                if state in {'failed', 'cancelled', 'delivery_unknown'}:
                    raise DomainError('RUN_DELIVERY_RECONCILIATION_REQUIRED')
                if loop.time() >= deadline:
                    raise DomainError('RUN_TIMEOUT_RECONCILIATION_REQUIRED')
                if loop.time() - renewed >= 20:
                    self.core.heartbeat(record['task_id'], record['agent_id'], lease['lease_token'],
                        lease['attempt'], key='run-heartbeat:' + uuid.uuid4().hex)
                    renewed = loop.time()
                await asyncio.sleep(.1)
            # Seal the input arbiter before committing so a late takeover cannot
            # race the task's final filesystem snapshot.
            async with runtime._lock:
                with self.core.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current = self.core._get(conn, 'sessions', sid)
                    if current['owner'] != 'pm' or current['epoch'] != epoch or current['state'] != 'ready':
                        raise DomainError('RUN_INPUT_TAKEN_OVER')
                    conn.execute("UPDATE sessions SET state='closed' WHERE id=?", (sid,))
                    self.core._event(conn, 'SessionSealed', 'runner', session=sid, task=record['task_id'])
                    conn.commit()
                runtime._quarantined = True
                runtime.closed = True
                await runtime.adapter.close()
            # Producer close ends the stream; drain already-read/buffered
            # events before allowing a filesystem snapshot to be accepted.
            consumer=self.host.consumers.get(sid)
            if consumer:
                try:
                    await asyncio.wait_for(asyncio.shield(consumer),5)
                except TimeoutError:
                    raise DomainError('RUN_CONSUMER_RECONCILIATION_REQUIRED') from None
            if self.unresolved_interactions(sid):
                raise DomainError('RUN_INTERACTION_RECONCILIATION_REQUIRED')
            if any(s == sid for s, _ in self.host.deliveries):
                raise DomainError('RUN_APPROVAL_RECONCILIATION_REQUIRED')
            self.core.heartbeat(record['task_id'], record['agent_id'], lease['lease_token'],
                                lease['attempt'], key='run-final-heartbeat:' + record['id'])
            self.save(record, 'committing')
            await asyncio.to_thread(self.check_policy,record)
            if await asyncio.to_thread(git, root, 'rev-parse', 'HEAD') != base:
                raise DomainError('RUN_HEAD_CHANGED')
            changed = await asyncio.to_thread(git, root, 'diff', '--name-only', '--no-renames', '-z', 'HEAD')
            untracked = await asyncio.to_thread(git, root, 'ls-files', '--others', '--exclude-standard', '-z')
            if any(self.protected(p, record['protected_paths']) for p in (changed + '\0' + untracked).split('\0') if p):
                raise DomainError('PROTECTED_ACCEPTANCE_FILE')
            if changed or untracked:
                await asyncio.to_thread(git, root, 'add', '--all')
                await asyncio.to_thread(git, root, '-c', 'user.name=Autocoding Runner',
                    '-c', 'user.email=runner@localhost', '-c', 'commit.gpgSign=false',
                    'commit', '--no-verify', '-m', 'feat: complete ' + record['task_id'])
            head = await asyncio.to_thread(git, root, 'rev-parse', 'HEAD')
            job = self.core.report(record['task_id'], record['agent_id'], lease['lease_token'], lease['attempt'],
                                   head, ['commit:' + head], key='run-report:' + record['id'])
            self.save(record, 'verifying', head_sha=head)
            verifier = Verifier(self.core, self.host.root, self.host.storage / 'verification', checks)
            result = await asyncio.to_thread(verifier.run, job, guard=lambda:self.check_policy(record))
            self.save(record, 'done' if result['state'] == 'done' else 'rework_required',
                      artifact=result['artifact'], artifact_sha256=result['sha256'],
                      failed_checks=[name for name, value in result['checks'].items() if not value['passed']])
        except asyncio.CancelledError:
            self.block(record, 'RUN_CANCELLED_RECONCILIATION_REQUIRED')
            raise
        except Exception as exc:
            code = getattr(exc, 'code', 'RUN_FAILED')
            record['error_type']=type(exc).__name__
            if lease:
                self.block(record, code)
            else:
                self.save(record, 'failed', reason=code)
        finally:
            try:
                if runtime:
                    await runtime.close()
            finally:
                self.active.pop(record['task_id'], None)
