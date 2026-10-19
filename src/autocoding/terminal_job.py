"""Parent-owned Windows Job: verify the entire terminal tree has exited."""
import ctypes
from ctypes import wintypes
import time
import uuid


def kernel():
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenJobObjectW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
    api.OpenJobObjectW.restype=wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    api.GetCurrentProcess.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    api.TerminateJobObject.argtypes=[wintypes.HANDLE,wintypes.UINT]
    api.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype=wintypes.HANDLE
    api.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    return api


def join(name):
    api=kernel();handle=api.OpenJobObjectW(1,False,name)
    if not handle:raise OSError('TERMINAL_JOB_OPEN_FAILED')
    try:
        if not api.AssignProcessToJobObject(handle,api.GetCurrentProcess()):
            raise OSError('TERMINAL_JOB_ASSIGN_FAILED')
    finally:
        api.CloseHandle(handle)  # only the parent retains the owning handle


class TerminalJob:
    def __init__(self):
        from .execution_worker import create_job
        self.name='Local\\autocoding-terminal-'+uuid.uuid4().hex
        self.handle=create_job(self.name)

    def active(self):
        class Accounting(ctypes.Structure):
            _fields_=[('times',ctypes.c_int64*4),('faults',wintypes.DWORD),
                      ('total',wintypes.DWORD),('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
        info=Accounting()
        if not kernel().QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None):
            raise OSError('TERMINAL_JOB_QUERY_FAILED')
        return info.active

    def stop(self,timeout=2):
        api=kernel();handles=[]
        class Members(ctypes.Structure):
            _fields_=[('assigned',wintypes.DWORD),('count',wintypes.DWORD),('pids',ctypes.c_size_t*4096)]
        members=Members()
        if not api.QueryInformationJobObject(self.handle,3,ctypes.byref(members),ctypes.sizeof(members),None):
            raise OSError('TERMINAL_JOB_MEMBERS_UNCONFIRMED')
        try:
            for pid in members.pids[:members.count]:
                handle=api.OpenProcess(0x101000,False,pid)
                if not handle:
                    if ctypes.get_last_error()==87:continue  # already exited
                    raise OSError('TERMINAL_JOB_MEMBER_UNCONFIRMED')
                belongs=wintypes.BOOL()
                if not api.IsProcessInJob(handle,self.handle,ctypes.byref(belongs)):
                    api.CloseHandle(handle);raise OSError('TERMINAL_JOB_MEMBER_UNCONFIRMED')
                if belongs.value:handles.append(handle)
                else:api.CloseHandle(handle)  # PID reuse outside this job
            if not api.TerminateJobObject(self.handle,1):raise OSError('TERMINAL_JOB_STOP_FAILED')
            deadline=time.monotonic()+timeout
            # ActiveProcesses can reach zero before process handles signal.
            # Wait retained members as well, rather than trusting the count.
            for handle in handles:
                remaining=max(0,int((deadline-time.monotonic())*1000))
                if api.WaitForSingleObject(handle,remaining)!=0:
                    raise OSError('TERMINAL_JOB_STOP_UNCONFIRMED')
            while self.active():
                if time.monotonic()>=deadline:raise OSError('TERMINAL_JOB_STOP_UNCONFIRMED')
                time.sleep(.01)
        finally:
            for handle in handles:api.CloseHandle(handle)

    def close(self):
        if self.handle:
            kernel().CloseHandle(self.handle);self.handle=None
