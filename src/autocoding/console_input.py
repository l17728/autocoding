"""Private worker channel for acknowledged Win32 console key-event batches."""
import ctypes
from ctypes import wintypes
import json
import re
import socket
import struct
import os
import time
import hashlib
from pathlib import Path

PACKET=re.compile(r'\x1b\[(\d+);(\d+);(\d+);([01]);(\d+);(\d+)_')
LIMIT=8*1024*1024


def receive(sock):
    def exact(size):
        result=bytearray()
        while len(result)<size:
            part=sock.recv(size-len(result))
            if not part:raise EOFError('CONSOLE_CHANNEL_CLOSED')
            result.extend(part)
        return bytes(result)
    size=struct.unpack('!I',exact(4))[0]
    if size>LIMIT:raise ValueError('CONSOLE_FRAME_TOO_LARGE')
    return json.loads(exact(size))


def send(sock,value):
    data=json.dumps(value,ensure_ascii=True).encode('ascii')
    if len(data)>LIMIT:raise ValueError('CONSOLE_FRAME_TOO_LARGE')
    sock.sendall(struct.pack('!I',len(data))+data)


def records(wire):
    result=[];offset=0
    for match in PACKET.finditer(wire):
        if match.start()!=offset:raise ValueError('INVALID_CONSOLE_PACKET')
        vk,scan,uc,down,mod,repeat=map(int,match.groups())
        if max(vk,scan,uc,repeat)>65535 or mod>0xffffffff or repeat<1:
            raise ValueError('INVALID_CONSOLE_PACKET')
        result.append((vk,scan,uc,down,mod,repeat));offset=match.end()
    if offset!=len(wire) or not result:raise ValueError('INVALID_CONSOLE_PACKET')
    return result


def write_records(items):
    class Key(ctypes.Structure):
        _fields_=[('down',wintypes.BOOL),('repeat',wintypes.WORD),('vk',wintypes.WORD),
                  ('scan',wintypes.WORD),('char',wintypes.WCHAR),('mod',wintypes.DWORD)]
    class Record(ctypes.Structure):
        _fields_=[('kind',wintypes.WORD),('key',Key)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetStdHandle.argtypes=[wintypes.DWORD];kernel.GetStdHandle.restype=wintypes.HANDLE
    kernel.WriteConsoleInputW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Record),wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    kernel.WriteConsoleInputW.restype=wintypes.BOOL
    handle=kernel.GetStdHandle(-10 & 0xffffffff)
    total=0
    for start in range(0,len(items),256):
        chunk=items[start:start+256]
        data=(Record*len(chunk))(*(Record(1,Key(down,repeat,vk,scan,chr(uc),mod)) for vk,scan,uc,down,mod,repeat in chunk))
        written=wintypes.DWORD()
        if not kernel.WriteConsoleInputW(handle,data,len(chunk),ctypes.byref(written)):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value!=len(chunk):raise OSError('CONSOLE_SHORT_WRITE')
        total+=written.value
    return total


def editor_exchange(directory,message):
    """Write once, then reopen Codex's composer and verify without rewriting."""
    expected=hashlib.sha256(message['paste'].encode('utf-8')).hexdigest()
    for phase in ('write','verify'):
        done=directory/'done.json';done.unlink(missing_ok=True)
        pending=directory/'pending.json'
        pending.write_text(json.dumps({**message,'phase':phase}),encoding='utf-8')
        write_records([(71,0,7,1,8,1),(71,0,7,0,8,1)])
        deadline=time.monotonic()+60
        next_open=time.monotonic()+10;opens=1
        while not done.exists():
            if time.monotonic()>=deadline:raise TimeoutError('EDITOR_HANDOFF_TIMEOUT')
            # Only retry OPENING an unconsumed local editor request. No text
            # or Enter is replayed; the verify phase never writes the draft.
            if pending.exists() and opens<3 and time.monotonic()>=next_open:
                write_records([(71,0,7,1,8,1),(71,0,7,0,8,1)])
                opens+=1;next_open=time.monotonic()+10
            time.sleep(.02)
        result=json.loads(done.read_text(encoding='utf-8'));done.unlink()
        if result!={'id':message['id'],'sha256':expected,'phase':phase}:
            return {'id':message['id'],'error':'EDITOR_READBACK_MISMATCH' if phase=='verify' else 'EDITOR_DRAFT_REJECTED'}
    return {'id':message['id'],'sha256':expected,'phase':'verify'}


def serve(address,nonce):
    with socket.create_connection(('127.0.0.1',address),timeout=10) as sock:
        sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        send(sock,{'nonce':nonce});sock.settimeout(None)
        while True:
            try:
                message=receive(sock)
                if 'paste' in message:
                    directory=Path(os.environ['AUTOCODING_DRAFT_DIRECTORY'])
                    send(sock,editor_exchange(directory,message))
                    continue
                items=records(message['wire'])
                count=write_records(items)
                send(sock,{'count':count})
            except EOFError:
                return
            except Exception as error:
                # A partial native write is uncertain: do not continue or echo
                # input. Parent freezes the command and stops this Job.
                reason='EDITOR_HANDOFF_TIMEOUT' if isinstance(error,TimeoutError) else 'CONSOLE_INPUT_FAILED'
                try:send(sock,{'error':reason})
                except OSError:pass
                return
