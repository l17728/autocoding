"""Windows ConPTY host with bounded output, resize, and serialized input leases.

Terminal writes are sent_unconfirmed: screen output never confirms a model
turn and never completes a task. Native terminal rendering is a separate mode.
"""
import asyncio
import json
import os
import re
from pathlib import Path
import sys
import threading
import time
import uuid

from .core import DomainError
from .transport import TransportError

KEYS = {'enter':'\r', 'tab':'\t', 'backspace':'\x7f', 'escape':'\x1b',
        'up':'\x1b[A', 'down':'\x1b[B', 'right':'\x1b[C', 'left':'\x1b[D',
        'home':'\x1b[H', 'end':'\x1b[F', 'delete':'\x1b[3~',
        'page_up':'\x1b[5~', 'page_down':'\x1b[6~', 'ctrl_c':'\x03', 'ctrl_p':'\x10', 'focus_in':'\x1b[I', 'focus_out':'\x1b[O'}

REPLY = re.compile(r'(?:\x1b\[(?:0n|[?>]?[0-9;]{1,32}c|[1-9][0-9]{0,2};[1-9][0-9]{0,2}R|\?[0-9;]{1,32}[$]y|\?[0-9;]{1,32}u|[468];[0-9]{1,5};[0-9]{1,5}t)'
                   r'|\x1b\](?:10|11|12|4;[0-9]{1,3});rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)'
                   r'|\x1bP(?:>\|[A-Za-z0-9 ()._-]{1,64}|!\|[0-9a-fA-F]{8})\x1b\\)')


class TerminalSession:
    def __init__(self, core, session_id, process):
        self.core, self.session_id, self.process = core, session_id, process
        import _winapi
        # Keep a handle to this exact child, avoiding PID reuse on shutdown.
        self._process_handle = _winapi.OpenProcess(0x00100001, False, process.pid)
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=128)
        self._slots = threading.BoundedSemaphore(128)
        self._failure = None
        self._closed = False
        self.user_input_seen = False
        self._lock = asyncio.Lock()
        # Keep WAL open while this interactive session is alive. Closing the
        # last connection after every keystroke otherwise checkpoints/deletes
        # WAL repeatedly (particularly costly on Windows). This connection
        # holds no transaction; each write still commits with FULL durability.
        self._ledger_context = core.connection()
        self._ledger_anchor = self._ledger_context.__enter__()
        self._ledger_anchor.execute('SELECT 1 FROM sessions LIMIT 1').fetchone()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    @classmethod
    async def start(cls, core, session_id, argv, *, cwd, env=None, rows=24, cols=80):
        if os.name != 'nt':
            raise TransportError('CONPTY_REQUIRES_WINDOWS')
        if not argv or not Path(argv[0]).is_absolute() or Path(argv[0]).suffix.lower() in {'.cmd','.bat','.ps1'}:
            raise TransportError('NATIVE_EXECUTABLE_REQUIRED')
        cls._dimensions(rows, cols)
        from winpty import Backend
        from .pty_process import TerminalProcess
        from .terminal_errors import TerminalStartError
        environment = dict(os.environ if env is None else env)
        environment['AUTOCODING_TERMINAL_ARGV'] = json.dumps(argv)
        worker = Path(__file__).with_name('terminal_worker.py')
        # -I omits script directory from imports, so this fixed bootstrap adds
        # only the trusted source directory before executing the worker.
        bootstrap = 'import runpy,sys;sys.path.insert(0,' + repr(str(worker.parent)) + ');runpy.run_path(' + repr(str(worker)) + ',run_name="__main__")'
        try:
            process = await asyncio.to_thread(TerminalProcess.spawn,
                [sys.executable, '-I', '-c', bootstrap], cwd=str(cwd), env=environment,
                dimensions=(rows, cols), backend=Backend.ConPTY)
        except TerminalStartError:
            raise
        except Exception as error:
            raise TerminalStartError('create_unknown',error,False) from None
        instance=cls.__new__(cls)
        try:
            cls.__init__(instance,core, session_id, process)
            return instance
        except Exception as error:
            stopped=False
            try:
                await asyncio.to_thread(process.dispose);stopped=True
            except Exception:pass
            handle=getattr(instance,'_process_handle',None)
            if handle is not None:
                import _winapi
                _winapi.CloseHandle(handle);instance._process_handle=None
            context=getattr(instance,'_ledger_context',None)
            if context is not None:
                try:context.__exit__(None,None,None)
                except Exception:pass
            raise TerminalStartError('session_attach',error,stopped) from None

    @staticmethod
    def _dimensions(rows, cols):
        if type(rows) is not int or type(cols) is not int or not 2 <= rows <= 200 or not 10 <= cols <= 400:
            raise DomainError('INVALID_TERMINAL_SIZE')

    def _offer(self, text):
        modes=getattr(self,'_mode_tail','')+text
        for match in re.finditer(r'\x1b\[\?9001([hl])',modes):
            self.win32_mode=match[1]=='h'
        self._mode_tail=modes[-16:]
        if self._closed:
            self._slots.release()
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            self._failure = 'TERMINAL_OUTPUT_BACKPRESSURE'
            # The reader stops consuming, but does not grow memory unboundedly.

    def _read(self):
        try:
            while not self._closed and not self._failure:
                text = self.process.read(4096)
                if not text:
                    break
                if not self._slots.acquire(timeout=.1):
                    self._failure = 'TERMINAL_OUTPUT_BACKPRESSURE'
                    break
                self._loop.call_soon_threadsafe(self._offer, text)
        except (EOFError, OSError):
            pass
        except Exception:
            self._failure = 'TERMINAL_READ_FAILED'
        finally:
            self._failure = self._failure or 'TERMINAL_CLOSED'

    async def read(self, *, timeout=30):
        async with asyncio.timeout(timeout):
            while self._queue.empty():
                if self._failure:
                    raise TransportError(self._failure)
                await asyncio.sleep(.02)
            result = self._queue.get_nowait()
            self._slots.release()
            return result

    def is_running(self):
        import _winapi
        if self._closed or self._process_handle is None:
            return False
        return _winapi.WaitForSingleObject(self._process_handle,0)!=0

    async def resize(self, rows, cols):
        self._dimensions(rows, cols)
        async with self._lock:
            if self._closed:
                raise TransportError('TERMINAL_CLOSED')
            await asyncio.to_thread(self.process.setwinsize, rows, cols)

    def _freeze(self):
        # One durable transaction, not one FULL fsync per historical keystroke.
        # Retain a separate audit event for every uncertain delivery.
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            commands=conn.execute("SELECT id,generation,epoch FROM commands WHERE session_id=? AND state IN ('dispatching','sent_unconfirmed')",(self.session_id,)).fetchall()
            if commands:
                conn.execute("UPDATE commands SET state='delivery_unknown' WHERE session_id=? AND state IN ('dispatching','sent_unconfirmed')",(self.session_id,))
                conn.execute("UPDATE sessions SET state='reconciling' WHERE id=? AND state!='closed'",(self.session_id,))
                for command in commands:
                    self.core._event(conn,'CommandDeliveryUnknown','runner',session=self.session_id,command=command['id'],
                        generation=command['generation'],epoch=command['epoch'],provider_turn_id=None,reason='TERMINAL_CLOSED')
            conn.commit()

    async def write(self, text, sender, epoch, *, key):
        self.user_input_seen = True
        if not isinstance(text, str) or not text:
            raise DomainError('EMPTY_INPUT')
        if any(ord(ch)<32 and ch not in '\n\r\t' for ch in text) or '\x7f' in text:
            raise DomainError('CONTROL_SEQUENCE_REJECTED')
        wire=text.replace('\r\n','\n').replace('\n','\r')
        if getattr(self,'native_keys',False) and getattr(self,'win32_mode',False):
            from .win32_input import text as encode
            wire=encode(wire)
        return await self._write(json.dumps({'terminal_text':text},ensure_ascii=False),wire,sender,epoch,key=key)

    async def press(self, name, sender, epoch, *, key):
        if name not in {'focus_in','focus_out'}:
            self.user_input_seen = True
        if name not in KEYS:
            raise DomainError('UNSUPPORTED_TERMINAL_KEY')
        wire=KEYS[name]
        if getattr(self,'native_keys',False) and getattr(self,'win32_mode',False):
            from .win32_input import KEYS as native,key as encode
            if name in native:wire=encode(*native[name])
        return await self._write(json.dumps({'terminal_key':name}), wire, sender, epoch, key=key)

    async def paste(self, text, sender, epoch, *, bracketed=False, key):
        self.user_input_seen = True
        if not isinstance(text,str) or not text:
            raise DomainError('EMPTY_INPUT')
        if type(bracketed) is not bool or any(ord(ch)<32 and ch not in '\n\r\t' for ch in text) or '\x7f' in text:
            raise DomainError('CONTROL_SEQUENCE_REJECTED')
        value=text.replace('\r\n','\n').replace('\r','\n')
        if (getattr(self.process,'editor_paste_enabled',False) is True
                and (len(value)>256 or '\n' in value)):
            return await self._write(json.dumps({'terminal_paste':text,'bracketed':bracketed},ensure_ascii=False),
                                     value,sender,epoch,key=key,editor=True)
        wire='\x1b[200~'+value+'\x1b[201~' if bracketed else value.replace('\n','\r')
        if getattr(self,'native_keys',False) and getattr(self,'win32_mode',False):
            from .win32_input import text as encode
            wire=encode(value,paste=True)
        # Only fixed framing is trusted. Input cannot close a paste or inject
        # arbitrary control sequences; submitting is a separate named Enter.
        return await self._write(json.dumps({'terminal_paste':text,'bracketed':bracketed},ensure_ascii=False),
                                 wire,sender,epoch,key=key)

    async def reply(self, text, sender, epoch, *, key):
        if not isinstance(text, str) or not text or len(text)>4096:
            raise DomainError('TERMINAL_RESPONSE_REJECTED')
        offset = count = 0
        while offset < len(text):
            match = REPLY.match(text, offset)
            count += 1
            if not match or count > 32:
                raise DomainError('TERMINAL_RESPONSE_REJECTED')
            offset = match.end()
        return await self._write(json.dumps({'terminal_response':text}), text, sender, epoch, key=key)

    async def _write(self, text, wire, sender, epoch, *, key, editor=False):
        async with self._lock:
            if self._closed or self._failure:
                raise TransportError(self._failure or 'TERMINAL_CLOSED')
            command=self.core.enqueue(self.session_id,sender,epoch,text,key=key)['command_id']
            state=self.core.command(command)['state']
            if state!='queued':
                return {'command_id':command,'state':state,'resent':False}
            def begin(conn):
                session=self.core._get(conn,'sessions',self.session_id)
                if session['epoch']!=epoch or session['owner']!=sender or session['state']!='ready':
                    raise DomainError('STALE_INPUT_EPOCH')
                conn.execute("UPDATE commands SET state='dispatching' WHERE id=?",(command,))
                self.core._event(conn,'CommandDispatching','runner',session=self.session_id,command=command)
                return {'command_id':command}
            self.core._write('runner','terminal_dispatch',command,{'command':command},begin)
            try:
                writer=self.process.paste if editor else self.process.write
                receipt=await asyncio.wait_for(asyncio.to_thread(writer,wire),timeout=130 if editor else 5)
                result=self.core.record_command(command,'sent_unconfirmed',key='terminal-sent:'+command)
                if editor:
                    result={**result,'draft_confirmation':receipt}
                return result
            except BaseException as error:
                self._freeze()
                await self._close_process()
                if isinstance(error,(OSError,TimeoutError)):
                    raise TransportError('TERMINAL_INPUT_UNCONFIRMED') from None
                raise

    async def takeover(self, owner, epoch, *, key):
        async with asyncio.timeout(6):
            async with self._lock:
                result=self.core.takeover(self.session_id,owner,epoch,key=key)
                if result['state']=='reconciling':
                    await self._close_process()
                return result

    async def _close_process(self):
        if not self._closed:
            self._closed=True
            def stop():
                import _winapi
                # pywinpty.isalive mutates .closed, so close() can skip socket
                # cleanup after terminate(). Release the reader and native PTY
                # explicitly; retaining the ConPTY can retain its cwd on Windows.
                handle=self._process_handle
                try:
                    self.process.dispose(handle=handle)
                finally:
                    _winapi.CloseHandle(handle)
                    self._process_handle=None
                self._reader.join(timeout=1)
                if self._reader.is_alive() or self.process._thread.is_alive():
                    raise OSError('TERMINAL_STOP_UNCONFIRMED')
            try:
                await asyncio.wait_for(asyncio.to_thread(stop),timeout=8)
            except (TimeoutError,OSError) as error:
                self._failure='TERMINAL_STOP_UNCONFIRMED'
                code=str(error)
                self.shutdown_error=code if re.fullmatch(r'TERMINAL_[A-Z_]+',code) else type(error).__name__
                import traceback
                self.shutdown_diagnostic={'code':self.shutdown_error,'winerror':getattr(error,'winerror',None),
                    'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(error.__traceback__)]}
            except BaseException:
                # Cancelling the asyncio waiter does not stop its native worker.
                # Never allow a subsequent close/resume to claim confirmed exit.
                self._failure='TERMINAL_STOP_UNCONFIRMED'
                raise
            finally:
                self._ledger_context.__exit__(None,None,None)
                self._ledger_anchor = None

    async def close(self):
        started=time.perf_counter()
        async with self._lock:
            locked=time.perf_counter()
            self._freeze()
            frozen=time.perf_counter()
            await self._close_process()
            ended=time.perf_counter()
            self.shutdown_timings={'lock_ms':round((locked-started)*1000,3),
                'freeze_ms':round((frozen-locked)*1000,3),'process_ms':round((ended-frozen)*1000,3),
                'total_ms':round((ended-started)*1000,3)}
