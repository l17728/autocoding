"""Private runner subprocess. Joins a Windows kill-on-close Job before spawning.

Only trusted verifier configuration reaches this executable. Child stdout and
stderr are hashed and counted, never copied into diagnostics or model context.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import subprocess
import sys
import threading


def create_job(name=None):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                    ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                    ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                    ('scheduling', wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ['read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes']]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, name)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error=OSError('JOB_SETUP_FAILED');error.winerror=ctypes.get_last_error()
        if job:
            kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(job)
        raise error
    return job


def own_job():
    if os.name != 'nt':
        os.setsid()
        return None
    job=create_job()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error=OSError('JOB_ASSIGN_FAILED');error.winerror=ctypes.get_last_error()
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(job)
        raise error
    return job  # Kept alive until this supervisor exits; children inherit membership.


def main():
    config = json.loads(sys.stdin.readline())
    try:
        job = own_job()
        process = subprocess.Popen(config['argv'], cwd=config['cwd'], env=config['env'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        print(json.dumps({'status': 'start_failed'}), flush=True)
        return 2
    counts = {}
    def drain(stream, name):
        sha, size, excerpt = hashlib.sha256(), 0, bytearray()
        while chunk := stream.read(8192):
            sha.update(chunk)
            size += len(chunk)
            if len(excerpt) < 16384:
                excerpt.extend(chunk[:16384-len(excerpt)])
        stream.close()
        counts[name] = {'bytes': size, 'sha256': sha.hexdigest(),
                        'excerpt': bytes(excerpt).decode('utf-8', errors='replace'),
                        'excerpt_truncated': size > len(excerpt)}
    readers = [threading.Thread(target=drain, args=(stream, name), daemon=True)
               for stream, name in [(process.stdout, 'stdout'), (process.stderr, 'stderr')]]
    for reader in readers:
        reader.start()
    code = process.wait()
    for reader in readers:
        reader.join(timeout=1)
    print(json.dumps({'status': 'finished', 'exit_code': code, 'streams': counts,
                      'tree_managed': os.name == 'nt', 'streams_complete': len(counts) == 2}), flush=True)
    # os._exit avoids unbounded shutdown caused by a child holding output pipes.
    sys.stdout.flush()
    os._exit(0)


if __name__ == '__main__':
    raise SystemExit(main())
