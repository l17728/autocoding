"""Trusted ConPTY child bootstrap: join a Job before launching the native CLI."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from execution_worker import own_job

argv = json.loads(os.environ.pop('AUTOCODING_TERMINAL_ARGV'))
from terminal_job import join
join(os.environ.pop('AUTOCODING_TERMINAL_JOB'))
if Path(argv[0]).stem.lower()=='codex':
    editor=subprocess.list2cmdline([sys.executable,'-I',str(Path(__file__).with_name('terminal_editor.py'))])
    os.environ['VISUAL']=os.environ['EDITOR']=editor
job = own_job()
channel=json.loads(os.environ.pop('AUTOCODING_CONSOLE_CHANNEL'))
from console_input import serve
def input_worker():
    try:
        serve(channel['port'],channel['nonce'])
    finally:
        # Losing the private input channel must not leave an unattended CLI
        # running. Exiting this Job owner also terminates its descendants.
        os._exit(1)
threading.Thread(target=input_worker,daemon=True).start()
result = subprocess.run(argv, shell=False)
os._exit(result.returncode)
