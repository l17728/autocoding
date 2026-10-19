"""One-shot, UTF-8 draft handoff invoked only by the platform Codex child."""
import hashlib
import json
import os
from pathlib import Path
import sys


def apply(directory,target):
    pending=directory/'pending.json'
    request=json.loads(pending.read_text(encoding='utf-8'))
    pending.unlink()  # consume once, including rejection; never replay
    phase=request.get('phase','write')
    result={'id':request['id'],'error':'EDITOR_READBACK_MISMATCH' if phase=='verify' else 'EDITOR_DRAFT_REJECTED'}
    try:
        if target.is_symlink():
            raise ValueError('Draft symlink rejected')
        if phase=='verify':
            # Codex creates this file from its CURRENT composer. Read only:
            # a successful initial file write is not proof it loaded the text.
            if target.read_bytes()!=request['paste'].encode('utf-8'):
                raise ValueError('Composer readback differs')
        elif phase=='write':
            if target.read_text(encoding='utf-8').strip():
                raise ValueError('Existing draft must be reviewed before replacement')
            target.write_text(request['paste'],encoding='utf-8',newline='')
        else:
            raise ValueError('Unknown editor phase')
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        result={'id':request['id'],'sha256':digest,'phase':phase}
    finally:
        temporary=directory/'done.tmp'
        temporary.write_text(json.dumps(result),encoding='utf-8')
        temporary.replace(directory/'done.json')
    return result


if __name__=='__main__':
    try:
        result=apply(Path(os.environ['AUTOCODING_DRAFT_DIRECTORY']),Path(sys.argv[1]))
        sys.exit(0 if 'sha256' in result else 1)
    except Exception:
        sys.exit(1)  # no draft content or credentials in subprocess stderr
