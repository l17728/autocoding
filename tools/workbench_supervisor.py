"""External watchdog for the local workbench.

Runs outside Uvicorn so it can recover an event-loop stall.  It treats both an
unresponsive health endpoint and a running/reconciling batch with no active
work (while todo work remains) as liveness failures.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def request(url, token=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with urlopen(Request(url, headers=headers), timeout=3) as response:
        return json.loads(response.read().decode('utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    root=args.project.resolve(); storage=root/'.local/workbench'; storage.mkdir(parents=True,exist_ok=True)
    lock=storage/'watchdog.lock'
    try: lock_handle=os.open(lock, os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError: raise SystemExit('workbench supervisor already running')
    entry=Path(__file__).with_name('workbench.py')
    token_file=storage/'owner-token.txt'; log=storage/'watchdog.jsonl'
    worker=None; failures=0; started_at=0.0
    def note(event, **data):
        log.open('a',encoding='utf-8').write(json.dumps({'at':time.time(),'event':event,**data},ensure_ascii=False)+'\n')
    def start(reason):
        nonlocal worker, started_at
        if worker and worker.poll() is None:
            worker.terminate()
            try: worker.wait(timeout=8)
            except subprocess.TimeoutExpired: worker.kill(); worker.wait(timeout=8)
        worker=subprocess.Popen([sys.executable, str(entry),'--project',str(root),'--port',str(args.port)],cwd=entry.parent.parent)
        started_at=time.monotonic()
        note('worker_started',reason=reason,pid=worker.pid)
    start('supervisor_start')
    while True:
        time.sleep(3)
        # Registry recovery can legitimately rebuild several project hosts.
        # Do not mistake that bounded cold-start window for a crash.
        if time.monotonic()-started_at < 45:
            continue
        unhealthy=False; reason=''
        try:
            if worker.poll() is not None:
                raise RuntimeError('WORKER_EXITED')
            request(f'http://127.0.0.1:{args.port}/health')
        except Exception as exc:
            unhealthy=True; reason='HTTP_UNRESPONSIVE:'+type(exc).__name__
        if not unhealthy:
          try:
            token=token_file.read_text(encoding='utf-8').strip()
            projects=request(f'http://127.0.0.1:{args.port}/api/v1/projects',token)
            for project in projects:
                snap=request(f"http://127.0.0.1:{args.port}/api/v1/projects/{project['id']}/snapshot",token)
                batch=(snap.get('batches') or [{}])[-1]
                # Reconciliation may legitimately await evidence or the Owner.
                # Only a claimed running batch with no active task is a liveness fault.
                stalled=(batch.get('state') == 'running' and not batch.get('active_tasks')
                         and snap.get('task_counts',{}).get('leased',0)>0)
                if stalled: unhealthy=True; reason='BATCH_STALLED:'+project['id']; break
          except Exception as exc:
            note('business_probe_failed',reason=type(exc).__name__)
        failures=failures+1 if unhealthy else 0
        if unhealthy: note('unhealthy',reason=reason,consecutive=failures)
        if failures>=3:
            start(reason); failures=0


if __name__ == '__main__': main()
