"""Owner-only terminal sessions, separate from verified development runs."""
import asyncio
from collections import deque
import json
import os
import re
from pathlib import Path
import sys
import time

from .core import DomainError, identifier
from .executables import native_executable
from .providers import resolve_zhipu, claude_environment, opencode_provider
from .opencode_compat import isolate_profile
from .redaction import Redactor
from .terminal import TerminalSession
from .transport import TransportError
from .terminal_errors import TerminalStartError
from .workspaces import git
from .codex_environment import child_environment

STARTUP_TIMEOUT=90
STARTUP_RETRY_DELAY=2


def startup_visible_text(screen):
    # Ignore control payloads (titles/clipboard/etc.), then remove complete
    # formatting/cursor sequences. Keep raw output unchanged for the renderer.
    screen=re.sub(r'\x1b[\]PX^_].*?(?:\x07|\x1b\\)', '', screen, flags=re.S)
    screen=re.sub(r'\x1b[\]PX^_].*$', '', screen, flags=re.S)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', screen)


class TerminalHub:
    def __init__(self, host):
        self.host = host
        self.sessions = {}
        self.records = {}
        self.output = {}
        self.readers = {}
        self.launchers = {}
        self._start_lock = asyncio.Lock()

    def recover(self):
        known = {s['id'] for s in self.host.core.snapshot()['sessions']}
        for path in self.host.storage.glob('terminal_*/terminal.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['id']==path.parent.name and record['id'] in known:
                    self.records[record['id']] = record
                    if record.get('startup_state') in {'starting','retrying'}:
                        record.update(startup_state='reconciling',startup_error='SERVER_RESTART_STOP_UNCONFIRMED')
                        self._save(record['id'])
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, sid):
        path = self.host.storage / sid / 'terminal.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.records[sid]),encoding='utf-8')
        temporary.replace(path)

    async def start(self, agent, *, resume_from=None, startup_attempts=3):
        if type(startup_attempts) is not int or not 1<=startup_attempts<=5:
            raise DomainError('INVALID_STARTUP_ATTEMPTS')
        async with self._start_lock:
            return await self._start(agent, resume_from=resume_from,startup_attempts=startup_attempts)

    async def _start(self, agent, *, resume_from=None,startup_attempts=3):
        preparing=time.perf_counter()
        if self.host.paused:
            raise DomainError('PROJECT_PAUSED')
        if agent not in {'check', 'codex', 'claude', 'opencode'}:
            raise DomainError('UNSUPPORTED_AGENT')
        if sum(not t._closed or self.records[sid].get('startup_state')=='retrying' for sid,t in self.sessions.items()) >= 3:
            raise DomainError('TERMINAL_LIMIT')
        previous = self.records.get(resume_from) if resume_from else None
        previous_reader=self.readers.get(resume_from)
        if resume_from and (not previous or previous['agent']!=agent or not previous.get('stopped_confirmed')
                            or previous.get('resumed_by') or resume_from in self.launchers
                            or (previous_reader is not None and not previous_reader.done())):
            raise DomainError('TERMINAL_RESUME_UNAVAILABLE')
        sid = identifier('terminal')
        private = self.host.storage / sid
        private.mkdir()
        state_directory = Path(previous['state_directory']) if previous else private
        if not state_directory.resolve().is_relative_to(self.host.storage):
            raise DomainError('TERMINAL_STATE_DIRECTORY_INVALID')
        env = dict(os.environ)
        workspace = None
        if agent == 'check':
            cwd = state_directory
            argv = [sys.executable, '-u', '-c',
                    "print('TERMINAL_READY',flush=True)\nwhile True:\n line=input()\n print('ECHO:'+line,flush=True)"]
        else:
            executable = native_executable(agent)
            if not executable:
                raise DomainError('ADAPTER_NOT_FOUND')
            if previous:
                workspace = await asyncio.to_thread(self.host.workspaces.get, previous['workspace_id'])
            else:
                head = await asyncio.to_thread(git, self.host.root, 'rev-parse', 'HEAD')
                workspace = await asyncio.to_thread(self.host.workspaces.create, self.host.root, head, branch=True)
            cwd = Path(workspace['path'])
            argv = [str(executable)]
            if agent == 'codex':
                # A child CLI owns a new lifecycle; parent agent session and
                # shutdown handles must not leak into it. Preserve account/home
                # and explicit provider configuration.
                env=child_environment(env)
                if previous and previous.get('startup_state')!='failed':
                    argv += ['resume', '--last']
                argv += ['--sandbox', 'danger-full-access']
                argv += ['--ask-for-approval', 'never' if self.host.permission_mode()=='bypass' else 'on-request']
                # Platform launches use the installed binary; upgrades are a
                # separate maintenance action, never an unattended TUI choice.
                argv += ['-c','check_for_update_on_startup=false']
            else:
                path = self.host.config or self.host.root / '.local/credentials/opencode.json'
                config = resolve_zhipu(path if path.exists() else None)
                self.host.redactor = Redactor((*self.host.redactor.secrets, config.api_key))
                if agent == 'claude':
                    env = claude_environment(config, 'https://open.bigmodel.cn/api/anthropic')
                    env['CLAUDE_CONFIG_DIR'] = str(state_directory / 'config')
                    if self.host.permission_mode()=='bypass':
                        argv += ['--dangerously-skip-permissions']
                    argv += ['--model', config.model]
                else:
                    env = isolate_profile(env,state_directory)
                    env['OPENCODE_DISABLE_AUTOUPDATE']='true'
                    env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'model':'zhipuai-coding-plan/'+config.model,
                        'enabled_providers':['zhipuai-coding-plan'],
                        'provider':{'zhipuai-coding-plan':opencode_provider(config)},
                        'permission':{'*':'allow' if self.host.permission_mode()=='bypass' else 'ask','question':'allow'}})
                    env['OPENCODE_DISABLE_MODELS_FETCH']='true'
                    for variable,folder in [('XDG_CONFIG_HOME','config'),('XDG_DATA_HOME','data'),
                                            ('XDG_CACHE_HOME','cache'),('XDG_STATE_HOME','state')]:
                        env[variable] = str(state_directory / folder)
                if previous and previous.get('startup_state')!='failed':
                    argv += ['--continue']
        # Test runners and redirected server logs may force contradictory color
        # flags. The native PTY has its own terminal capabilities.
        env.pop('NO_COLOR',None)
        env.pop('FORCE_COLOR',None)
        env['TERM']='xterm-256color'
        env['COLORTERM']='truecolor'
        if previous:
            previous['resumed_by'] = sid
            self._save(resume_from)
        self.host.core.create_session(sid, 'terminal:'+agent, owner='human', key=sid)
        self.records[sid] = {'id':sid,'agent':agent,'cwd':str(cwd),'state_directory':str(state_directory),
            'workspace_id':workspace['id'] if workspace else None,'resumed_from':resume_from,'stopped_confirmed':False,
            'startup_attempt':1,'startup_limit':startup_attempts,'startup_state':'ready' if agent=='check' else 'starting',
            'startup_timings':{'prepare_ms':round((time.perf_counter()-preparing)*1000,3),'attempts':[]}}
        self._save(sid)
        async def launch_once():
            started=time.perf_counter()
            attempt={'attempt':self.records[sid]['startup_attempt']}
            self.records[sid]['startup_timings']['attempts'].append(attempt)
            work=asyncio.create_task(TerminalSession.start(self.host.core,sid,argv,cwd=cwd,env=env))
            try:
                child=await asyncio.shield(work)
                attempt['spawn_ms']=round((time.perf_counter()-started)*1000,3)
                self._save(sid)
                return child
            except asyncio.CancelledError:
                confirmed=False
                try:
                    child=await work
                    await child.close()
                    confirmed=child._failure!='TERMINAL_STOP_UNCONFIRMED'
                except TerminalStartError as error:
                    confirmed=error.stopped_confirmed
                finally:
                    if not confirmed and sid in self.sessions:
                        self.sessions[sid]._failure='TERMINAL_STOP_UNCONFIRMED'
                raise
        async def launch():
            record=self.records[sid]
            while True:
                began=time.perf_counter()
                try:
                    child=await launch_once()
                    record.update(startup_state='ready' if agent=='check' else 'starting',stopped_confirmed=False)
                    self._save(sid)
                    return child
                except TerminalStartError as error:
                    attempt=record['startup_timings']['attempts'][-1]
                    attempt.update(failure_ms=round((time.perf_counter()-began)*1000,3),error=error.code,diagnostic=error.diagnostic)
                    record.update(startup_error=error.code,startup_diagnostic=error.diagnostic,
                                  stopped_confirmed=error.stopped_confirmed)
                    if not error.stopped_confirmed:
                        self._save(sid);raise
                    if record['startup_attempt']>=record['startup_limit'] or self.host.paused or record.get('stop_requested'):
                        record['startup_state']='failed';self._save(sid);raise
                    record['startup_state']='retrying';self._save(sid)
                    await asyncio.sleep(STARTUP_RETRY_DELAY)
                    if self.host.paused or record.get('stop_requested'):
                        record['startup_state']='failed';self._save(sid);raise
                    record['startup_attempt']+=1
        self.launchers[sid]=launch
        try:
            terminal = await launch()
        except BaseException:
            confirmed=self.records[sid].get('stopped_confirmed',False)
            self.records[sid]['startup_state']='failed' if confirmed else 'reconciling'
            self._save(sid)
            self.launchers.pop(sid,None)
            self._state(sid, 'closed' if confirmed else 'reconciling')
            raise
        self.sessions[sid] = terminal
        terminal.native_keys = agent=='codex'
        self.output[sid] = {'seq':0, 'frames':deque(maxlen=256),'changed':asyncio.Event()}
        self.readers[sid] = asyncio.create_task(self._read(sid))
        return dict(self.records[sid])

    def _state(self, sid, state, *, invalidate_input=False):
        with self.host.core.connection() as conn:
            conn.execute('UPDATE sessions SET state=?,epoch=epoch+? WHERE id=?', (state,int(invalidate_input),sid))
            self.host.core._event(conn, 'TerminalStateChanged', 'runner', session=sid, state=state)

    def get(self, sid):
        if sid not in self.sessions:
            raise DomainError('SESSION_OFFLINE')
        return self.sessions[sid]

    def snapshot(self):
        # The terminal UI polls every second. Loading all task/command history
        # here stalls the event loop and delays capability handshakes on busy projects.
        with self.host.core.connection() as conn:
            core={r['id']:dict(r) for r in conn.execute("SELECT * FROM sessions WHERE agent GLOB 'terminal:*'")}
        return [{**r, **core[sid], 'agent':r['agent'],
                 'live':sid in self.sessions and not self.sessions[sid]._closed,
                 'can_stop':bool((sid in self.launchers and not r.get('stop_requested')) or
                     (sid in self.sessions and (not self.sessions[sid]._closed or
                     (sid in self.readers and not self.readers[sid].done())))),
                 'can_resume':bool(r.get('stopped_confirmed') and not r.get('resumed_by') and
                     sid not in self.launchers and
                     (sid not in self.readers or self.readers[sid].done()))}
                for sid,r in self.records.items()]

    async def _read(self, sid):
        stream = self.host.redactor.terminal_stream()
        started=time.perf_counter()
        timing=self.records[sid]['startup_timings']['attempts'][-1]
        deadline=asyncio.get_running_loop().time()+STARTUP_TIMEOUT
        screen=''
        try:
            while True:
                if (self.records[sid].get('startup_state')=='starting'
                        and not getattr(self.sessions[sid],'user_input_seen',False)
                        and asyncio.get_running_loop().time()>=deadline):
                    raise TransportError('TERMINAL_START_TIMEOUT')
                try:
                    waiting=(self.records[sid].get('startup_state')=='starting'
                             and not getattr(self.sessions[sid],'user_input_seen',False))
                    timeout=min(10,max(.001,deadline-asyncio.get_running_loop().time())) if waiting else 10
                    text = await self.sessions[sid].read(timeout=timeout)
                except TimeoutError:
                    running=getattr(self.sessions[sid],'is_running',None)
                    if running is not None and not running():
                        raise TransportError('TERMINAL_PROCESS_EXITED')
                    record=self.records[sid]
                    if (record.get('startup_state')=='starting' and not getattr(self.sessions[sid],'user_input_seen',False)
                            and asyncio.get_running_loop().time()>=deadline):
                        raise TransportError('TERMINAL_START_TIMEOUT')
                    continue
                if 'first_output_ms' not in timing:
                    timing['first_output_ms']=round((time.perf_counter()-started)*1000,3)
                    self._save(sid)
                timing['output_chunks']=timing.get('output_chunks',0)+1
                # Prompt text need not end with LF; secret prefixes stay held.
                text = stream.feed(text)
                if text:
                    screen=(screen+text)[-8192:]
                    record=self.records[sid]
                    visible=startup_visible_text(screen)
                    if (record.get('startup_state')=='starting' and record['agent']=='codex'
                            and not getattr(self.sessions[sid],'user_input_seen',False)
                            and 'No active thread is available' in visible):
                        raise TransportError('TERMINAL_THREAD_NOT_READY')
                    if record.get('startup_state')=='starting' and re.search(
                            r'permissions:|context left|Trust this folder|Folder access|Choose the (?:text style|theme)|Dark mode|Ask anything|What would you like',visible):
                        timing['ready_ms']=round((time.perf_counter()-started)*1000,3)
                        record['startup_state']='ready';self._save(sid)
                        self.launchers.pop(sid,None)
                    entry = self.output[sid]
                    for offset in range(0,len(text),4096):
                        entry['seq'] += 1
                        entry['frames'].append({'seq':entry['seq'], 'text':text[offset:offset+4096]})
                    entry['changed'].set()
                    entry['changed']=asyncio.Event()
        except TransportError as exc:
            if self.records[sid].get('startup_state')=='starting':
                timing['failure_ms']=round((time.perf_counter()-started)*1000,3)
                timing['error']=exc.code
            else:
                timing['exit_ms']=round((time.perf_counter()-started)*1000,3)
                timing['exit_reason']=exc.code
            terminal=self.sessions[sid]
            record=self.records[sid]
            retry=(record.get('startup_state')=='starting' and not getattr(terminal,'user_input_seen',False))
            with self.host.core.connection() as conn:
                was_reconciling=self.host.core._get(conn,'sessions',sid)['state']=='reconciling'
            # Fence input before awaiting process termination. No prior user input
            # is ever replayed, and only a confirmed stopped child can be replaced.
            self._state(sid,'starting',invalidate_input=True)
            await self.sessions[sid].close()
            self.records[sid]['stopped_confirmed'] = self.sessions[sid]._failure != 'TERMINAL_STOP_UNCONFIRMED'
            record['startup_error']=exc.code
            if retry and record['stopped_confirmed'] and not self.host.paused and record['startup_attempt']<record['startup_limit']:
                record['startup_attempt']+=1
                record['startup_state']='retrying';self._save(sid)
                await asyncio.sleep(STARTUP_RETRY_DELAY)
                if not self.host.paused:
                    try:
                        replacement=await self.launchers[sid]()
                    except Exception as error:
                        confirmed=isinstance(error,TerminalStartError) and error.stopped_confirmed
                        record['stopped_confirmed']=confirmed
                        record['startup_state']='failed' if confirmed else 'reconciling';self._save(sid)
                        self._state(sid,'closed' if confirmed else 'reconciling')
                        self.launchers.pop(sid,None)
                        return
                    replacement.native_keys=record['agent']=='codex'
                    self.sessions[sid]=replacement
                    record.update(startup_state='starting',stopped_confirmed=False)
                    self._save(sid);self._state(sid,'ready')
                    entry=self.output[sid];entry['seq']+=1
                    entry['frames'].append({'seq':entry['seq'],'text':'\x1b[2J\x1b[H'})
                    entry['changed'].set();entry['changed']=asyncio.Event()
                    await self._read(sid)
                    return
            record['startup_state']='failed' if retry and record['stopped_confirmed'] else ('reconciling' if not record['stopped_confirmed'] else 'stopped')
            self._save(sid)
            self._state(sid, 'reconciling' if was_reconciling or self.sessions[sid]._failure == 'TERMINAL_STOP_UNCONFIRMED' else 'closed')
            self.launchers.pop(sid,None)
        except asyncio.CancelledError:
            raise

    async def stop(self, sid):
        started=time.perf_counter()
        if sid not in self.sessions and sid in self.records:
            record=self.records[sid];record['stop_requested']=True
            confirmed=record.get('stopped_confirmed',False)
            self._save(sid)
            return {'state':'closed' if confirmed else 'reconciling'}
        terminal = self.get(sid)
        self.readers[sid].cancel()
        await asyncio.gather(self.readers[sid], return_exceptions=True)
        cancelled=time.perf_counter()
        await terminal.close()
        if hasattr(terminal,'shutdown_timings'):
            self.records[sid]['shutdown_timings']={**terminal.shutdown_timings,
                'reader_cancel_ms':round((cancelled-started)*1000,3),
                'hub_total_ms':round((time.perf_counter()-started)*1000,3)}
        self.records[sid]['stopped_confirmed'] = terminal._failure != 'TERMINAL_STOP_UNCONFIRMED'
        if self.records[sid].get('startup_state')!='failed':
            self.records[sid]['startup_state']='stopped' if self.records[sid]['stopped_confirmed'] else 'reconciling'
        self.launchers.pop(sid,None)
        self._save(sid)
        self._state(sid, 'reconciling' if terminal._failure == 'TERMINAL_STOP_UNCONFIRMED' else 'closed')
        return {'state':'closed' if terminal._failure != 'TERMINAL_STOP_UNCONFIRMED' else 'reconciling'}

    async def close(self):
        for sid in list(self.sessions):
            await self.stop(sid)
