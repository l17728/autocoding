"""Allowlisted startup diagnostics; never retain exception text or argv/env."""
from .transport import TransportError


class TerminalStartError(TransportError):
    def __init__(self,stage,error,stopped_confirmed=False):
        super().__init__('CONPTY_START_FAILED')
        kind=type(error).__name__
        self.diagnostic={'stage':stage,'kind':kind if kind in {
            'TimeoutError','OSError','PermissionError','FileNotFoundError','EOFError','ValueError','RuntimeError','WinptyError'} else 'Exception',
            'stopped_confirmed':bool(stopped_confirmed)}
        for name in ('winerror','errno'):
            value=getattr(error,name,None)
            if type(value) is int:self.diagnostic[name]=value
        self.stopped_confirmed=bool(stopped_confirmed)
