"""Bounded streaming removal of known secrets, including split chunks."""
import re


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = tuple(sorted({x for x in secrets if isinstance(x, str) and x}, key=len, reverse=True))

    def text(self, value):
        value = str(value)
        for secret in self.secrets:
            value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)(bearer\s+)[^\s"<>]+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)["\s]*[:=]["\s]*)[^\s"&,}]+', r'\1[REDACTED]', value)
        return value

    def value(self, item):
        if isinstance(item, dict):
            return {k: self.value(v) for k, v in item.items()}
        if isinstance(item, list):
            return [self.value(v) for v in item]
        return self.text(item) if isinstance(item, str) else item

    def stream(self):
        return RedactionStream(self)

    def terminal_stream(self):
        return TerminalRedactionStream(self)


class TerminalRedactionStream:
    """Release prompts without LF, retaining possible split secret prefixes."""
    def __init__(self, redactor):
        self.redactor = redactor
        self.pending = ''
        self.dropping = False

    def feed(self, chunk):
        if self.dropping:
            end = re.search(r'[\s"&,}<>]',chunk)
            if not end:
                return ''
            chunk = chunk[end.start():]
            self.dropping = False
        self.pending += chunk
        for secret in self.redactor.secrets:
            self.pending = self.pending.replace(secret, '[REDACTED]')
        boundary = len(self.pending)
        # CSI final bytes (for example t/p/c) are protocol, not the beginning
        # of token/password/a configured credential. Keep incomplete CSI intact
        # across reads so a final byte cannot be mistaken for printable text.
        complete_csi = re.search(r'\x1b\[[0-?]*[ -/]*[@-~]\Z', self.pending)
        incomplete_csi = re.search(r'\x1b(?:\[[0-?]*[ -/]*)?\Z', self.pending)
        if incomplete_csi:
            boundary = min(boundary,incomplete_csi.start())
        prefixes = (*self.redactor.secrets, 'api_key', 'api-key', 'apikey', 'token', 'password', 'secret', 'bearer')
        for prefix in prefixes:
            for size in range(1, min(len(prefix),len(self.pending))+1):
                if complete_csi and len(self.pending)-size >= complete_csi.start():
                    continue
                if self.pending[-size:].lower() == prefix[:size].lower():
                    boundary = min(boundary,len(self.pending)-size)
        # An unterminated labeled credential must never be emitted in pieces.
        match = re.search(r'(?i)(?:api[_-]?key|token|password|secret)["\s]*(?:[:=]["\s]*[^\s"&,}]*)?\Z|bearer\s+[^\s"<>]*\Z', self.pending)
        if match:
            boundary = min(boundary,match.start())
        ready,self.pending = self.pending[:boundary],self.pending[boundary:]
        if len(self.pending)>65536:
            self.pending = ''
            self.dropping = True
            return self.redactor.text(ready)+'[OUTPUT_FRAGMENT_DROPPED]'
        return self.redactor.text(ready)


class RedactionStream:
    def __init__(self, redactor):
        self.redactor = redactor
        self.pending = ''

    def feed(self, chunk, *, final=False):
        self.pending += chunk
        # Complete lines avoid splitting arbitrary token=... and Bearer values.
        boundary = len(self.pending) if final else self.pending.rfind('\n') + 1
        if boundary == 0:
            if len(self.pending) > 65536:
                self.pending = ''
                return '[OUTPUT_LINE_DROPPED]\n'
            return ''
        # If a configured secret itself contains a newline, retain its prefix.
        if not final:
            for secret in self.redactor.secrets:
                for size in range(1, min(len(secret), boundary + 1)):
                    if self.pending[:boundary].endswith(secret[:size]):
                        boundary -= size
                        break
        ready, self.pending = self.pending[:boundary], self.pending[boundary:]
        return self.redactor.text(ready)
