"""Send ASCII scan codes only to an exactly identified disposable browser window."""
import argparse
import ctypes as c
from ctypes import wintypes as w
import json
import time


def main(title,action):
    user=c.WinDLL('user32',use_last_error=True);imm=c.WinDLL('imm32',use_last_error=True)
    user.GetForegroundWindow.restype=w.HWND
    user.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)]
    user.SetForegroundWindow.argtypes=[w.HWND]
    user.GetKeyboardLayout.argtypes=[w.DWORD];user.GetKeyboardLayout.restype=w.HANDLE
    user.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,c.c_int]
    user.IsWindowVisible.argtypes=[w.HWND]
    class Info(c.Structure):
        _fields_=[('size',w.DWORD),('flags',w.DWORD)]+[(n,w.HWND) for n in
            ['active','focus','capture','menu','move','caret']]+[('rect',w.RECT)]
    user.GetGUIThreadInfo.argtypes=[w.DWORD,c.POINTER(Info)]
    callback=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    matches=[]
    @callback
    def visit(hwnd,_):
        text=c.create_unicode_buffer(2048);user.GetWindowTextW(hwnd,text,len(text))
        if title in text.value and user.IsWindowVisible(hwnd):matches.append(hwnd)
        return True
    user.EnumWindows(visit,0)
    if len(matches)!=1:raise RuntimeError('TEST_WINDOW_NOT_UNIQUE')
    hwnd=matches[0]
    user.SetForegroundWindow(hwnd);time.sleep(.2)
    if user.GetForegroundWindow()!=hwnd:raise RuntimeError('TEST_WINDOW_NOT_FOREGROUND')
    thread=user.GetWindowThreadProcessId(hwnd,None)
    layout=user.GetKeyboardLayout(thread)
    if not layout or layout&0xffff!=0x0804:raise RuntimeError('CHINESE_LAYOUT_REQUIRED')
    info=Info();info.size=c.sizeof(info)
    if not user.GetGUIThreadInfo(thread,c.byref(info)) or not info.focus:raise RuntimeError('FOCUS_REQUIRED')
    imm.ImmGetContext.argtypes=[w.HWND];imm.ImmGetContext.restype=w.HANDLE
    imm.ImmReleaseContext.argtypes=[w.HWND,w.HANDLE]
    imm.ImmGetOpenStatus.argtypes=[w.HANDLE]
    imm.ImmSetOpenStatus.argtypes=[w.HANDLE,w.BOOL]
    imm.ImmGetConversionStatus.argtypes=[w.HANDLE,c.POINTER(w.DWORD),c.POINTER(w.DWORD)]
    imm.ImmSetConversionStatus.argtypes=[w.HANDLE,w.DWORD,w.DWORD]
    context=imm.ImmGetContext(info.focus)
    original=imm.ImmGetOpenStatus(context) if context else None
    conversion=w.DWORD();sentence=w.DWORD()
    if context and not imm.ImmGetConversionStatus(context,c.byref(conversion),c.byref(sentence)):
        imm.ImmReleaseContext(info.focus,context);context=None
    # If the browser exposes no IMM context, preserve the current input mode.
    # The browser test must observe actual Chinese composition to pass.
    class Keyboard(c.Structure):
        _fields_=[('vk',w.WORD),('scan',w.WORD),('flags',w.DWORD),('time',w.DWORD),('extra',c.c_size_t)]
    class Mouse(c.Structure):
        _fields_=[('x',w.LONG),('y',w.LONG),('data',w.DWORD),('flags',w.DWORD),('time',w.DWORD),('extra',c.c_size_t)]
    class Payload(c.Union):_fields_=[('keyboard',Keyboard),('mouse',Mouse)]
    class Input(c.Structure):_fields_=[('kind',w.DWORD),('payload',Payload)]
    user.SendInput.argtypes=[w.UINT,c.POINTER(Input),c.c_int]
    try:
        if context and (not imm.ImmSetOpenStatus(context,True) or not imm.ImmSetConversionStatus(context,1,0)):
            raise RuntimeError('IME_ENABLE_FAILED')
        if any(user.GetAsyncKeyState(key)&0x8000 for key in [0x10,0x11,0x12,0x5b,0x5c]) or user.GetKeyState(0x14)&1:
            raise RuntimeError('MODIFIER_OR_CAPS_LOCK_ACTIVE')
        keys={'chinese-space':[ord(ch.upper()) for ch in 'zhongwen']+[0x20],
              'composition-enter':[ord(ch.upper()) for ch in 'ceshi']+[0x0d],
              'enter':[0x0d]}[action]
        for key in keys:
            if user.GetForegroundWindow()!=hwnd:raise RuntimeError('FOCUS_CHANGED_NO_MORE_INPUT')
            current=Info();current.size=c.sizeof(current)
            if not user.GetGUIThreadInfo(thread,c.byref(current)) or current.focus!=info.focus:
                raise RuntimeError('FOCUS_CHANGED_NO_MORE_INPUT')
            if any(user.GetAsyncKeyState(modifier)&0x8000 for modifier in [0x10,0x11,0x12,0x5b,0x5c]):
                raise RuntimeError('MODIFIER_ACTIVE_NO_MORE_INPUT')
            scan=user.MapVirtualKeyW(key,0)
            inputs=(Input*2)()
            for index,flags in enumerate([8,10]):
                inputs[index].kind=1;inputs[index].payload.keyboard=Keyboard(0,scan,flags,0,0)
            if user.SendInput(2,inputs,c.sizeof(Input))!=2:raise RuntimeError('SEND_INPUT_FAILED')
            time.sleep(.1)
        time.sleep(.5)
        print(json.dumps({'action':action,'ascii_scan_codes_only':True,'keys':len(keys)}))
    finally:
        if context:
            imm.ImmSetConversionStatus(context,conversion.value,sentence.value)
            imm.ImmSetOpenStatus(context,original)
            imm.ImmReleaseContext(info.focus,context)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--title',required=True)
    parser.add_argument('--action',choices=['chinese-space','composition-enter','enter'],required=True)
    args=parser.parse_args()
    if not args.title.startswith('autocoding-ime-') or len(args.title)<30:parser.error('unique test title required')
    main(args.title,args.action)
