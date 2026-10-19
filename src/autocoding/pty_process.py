"""Project-local bridge for pinned pywinpty 3.0.5; no global vendor patches.

Mirror the pinned native spawn while retaining partially created resources.
The localhost output bridge uses TCP_NODELAY; input has explicit receipts.
Initialization fields match the pinned wrapper's close/read contract.
"""
import os
import codecs
import socket
import threading
import time
import json
import secrets
import tempfile
import hashlib
import subprocess
from pathlib import Path
from winpty import PtyProcess,PTY


def forward_output(address,pty,blocking):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as client:
        client.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        client.connect(address)
        try:
            while True:
                data=pty.read(blocking=blocking)
                if data:
                    client.sendall(data.encode('utf-8'))
                if pty.iseof():
                    return
                if not data:
                    time.sleep(.001)
        except (EOFError,OSError):
            return
        except Exception:
            # The socket closes on native bridge failure; the session reader
            # treats EOF as loss of transport, never as model completion.
            return


class TerminalProcess(PtyProcess):
    @classmethod
    def spawn(cls,argv,**kwargs):
        from .console_input import receive
        supplied=kwargs.pop('env',None)
        env=dict(os.environ if supplied is None else supplied)
        nonce=secrets.token_hex(32)
        draft=tempfile.TemporaryDirectory(prefix='autocoding-console-')
        env['AUTOCODING_DRAFT_DIRECTORY']=draft.name
        from .terminal_job import TerminalJob
        from .terminal_errors import TerminalStartError
        process=pty=job=None
        stage='job_create';spawn_started=False
        with socket.socket() as listener:
            try:
                job=TerminalJob();env['AUTOCODING_TERMINAL_JOB']=job.name
                stage='input_listener'
                listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(10)
                env['AUTOCODING_CONSOLE_CHANNEL']=json.dumps({'port':listener.getsockname()[1],'nonce':nonce})
                dimensions=kwargs.pop('dimensions',(24,80));cwd=kwargs.pop('cwd',None)
                stage='pty_create'
                pty=PTY(dimensions[1],dimensions[0],backend=kwargs.pop('backend',None))
                stage='process_create';spawn_started=True
                pty.spawn(argv[0],cwd=cwd or os.getcwd(),env='\0'.join(k+'='+v for k,v in env.items())+'\0',
                          cmdline=' '+subprocess.list2cmdline(argv[1:]))
                stage='output_bridge'
                process=cls.__new__(cls)
                cls.__init__(process,pty)
                process.argv=list(argv);process.launch_dir=cwd;process._winsize=dimensions
                process.draft_directory=draft;process.job=job
                native_argv=json.loads(env.get('AUTOCODING_TERMINAL_ARGV','[]'))
                process.editor_paste_enabled=bool(native_argv and Path(native_argv[0]).stem.lower()=='codex')
                stage='input_connect'
                channel,_=listener.accept();channel.settimeout(4)
                process.input_channel=channel
                channel.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                stage='input_auth'
                if receive(channel)!={'nonce':nonce}:raise OSError('CONSOLE_CHANNEL_AUTH_FAILED')
                return process
            except Exception as error:
                stopped=False
                try:
                    if process is not None:
                        process.job=job;process.draft_directory=draft
                        process.dispose()
                    else:
                        if job is not None:job.stop()
                        if pty is not None and spawn_started:self_stop(pty.fd)
                        if job is not None:job.close()
                        if pty is not None:pty.cancel_io()
                        draft.cleanup()
                    stopped=True
                except Exception:
                    if job is not None:job.close()
                pty=None
                raise TerminalStartError(stage,error,stopped) from None

    def dispose(self,handle=None):
        """Release this exact tree and both bridges; no PID-based ownership."""
        failure=None
        def attempt(action):
            nonlocal failure
            try:action()
            except Exception as error:
                if failure is None:failure=error
        job=getattr(self,'job',None)
        if job is not None:attempt(job.stop)
        attempt(lambda:self_stop(self.pty.fd if handle is None else handle))
        def cancel_io():
            try:self.pty.cancel_io()
            except OSError:pass
        attempt(cancel_io)
        channel=getattr(self,'input_channel',None)
        if channel is not None:attempt(channel.close)
        sock=getattr(self,'fileobj',None)
        if sock is not None:
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            attempt(sock.close)
        server=getattr(self,'_server',None)
        if server is not None:attempt(server.close)
        thread=getattr(self,'_thread',None)
        if thread is not None:
            attempt(lambda:thread.join(timeout=1))
            if thread.is_alive() and failure is None:failure=OSError('TERMINAL_BRIDGE_STOP_UNCONFIRMED')
        self.closed=True;self.pty=None
        if job is not None:attempt(job.close)
        draft=getattr(self,'draft_directory',None)
        if draft is not None:attempt(draft.cleanup)
        if failure is not None:raise failure

    def paste(self,value):
        from .console_input import send,receive
        request={'id':secrets.token_hex(16),'paste':value}
        self.input_channel.settimeout(125)
        try:
            send(self.input_channel,request)
            expected={'id':request['id'],'sha256':hashlib.sha256(value.encode('utf-8')).hexdigest(),'phase':'verify'}
            result=receive(self.input_channel)
            if result!=expected:
                from .transport import TransportError
                reason=result.get('error') if isinstance(result,dict) else None
                allowed={'EDITOR_HANDOFF_TIMEOUT','EDITOR_DRAFT_REJECTED','EDITOR_READBACK_MISMATCH','CONSOLE_INPUT_FAILED'}
                raise TransportError(reason if reason in allowed else 'EDITOR_INPUT_UNCONFIRMED')
            return {'method':'editor_readback','sha256':expected['sha256']}
        finally:
            self.input_channel.settimeout(4)

    def write(self,text):
        from .console_input import PACKET,records,send,receive
        if PACKET.match(text):
            items=records(text)
            send(self.input_channel,{'wire':text})
            if receive(self.input_channel)!={'count':len(items)}:
                raise OSError('CONSOLE_INPUT_UNCONFIRMED')
            return len(text.encode('utf-8'))
        # Protocol replies and non-Win32 input still pass through the VT parser.
        # pywinpty's asynchronous count is NOT a full-write acknowledgement.
        return super().write(text)

    def __init__(self,pty):
        self.pty,self.pid=pty,pty.pid
        self.argv=self.env=self.launch_dir=None
        self.read_blocking=bool(int(os.environ.get('PYWINPTY_BLOCK',1)))
        self.closed=self.flag_eof=False
        self._decoder=codecs.getincrementaldecoder('utf-8')()
        self.delayafterterminate=self.delayafterclose=.1
        self._server=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self._server.bind(('127.0.0.1',0))
        self._server.listen(1)
        self._server.settimeout(10)
        self._thread=threading.Thread(target=forward_output,
            args=(self._server.getsockname(),pty,self.read_blocking),daemon=True)
        self._thread.start()
        self.fileobj,_=self._server.accept()
        self.fd=self.fileobj.fileno()

    def read(self,size=1024):
        while True:
            data=self.fileobj.recv(size)
            if not data:
                self.flag_eof=True
                tail=self._decoder.decode(b'',final=True)
                if tail:return tail
                raise EOFError('Pty is closed')
            text=self._decoder.decode(data)
            if text:return text


def self_stop(handle):
    import _winapi
    if _winapi.WaitForSingleObject(handle,0)!=0:
        try:_winapi.TerminateProcess(handle,1)
        except PermissionError as error:
            # Job termination may already be in progress. Access denied is
            # not proof of exit: the retained handle must still signal below.
            if getattr(error,'winerror',None)!=5:raise
    if _winapi.WaitForSingleObject(handle,2000)!=0:raise OSError('TERMINAL_STOP_UNCONFIRMED')
