"""External watchdog for the local workbench.

Runs outside Uvicorn so it can recover an event-loop stall.  It treats both an
unresponsive health endpoint and a running/reconciling batch with no active
work (while todo work remains) as liveness failures.
"""
from __future__ import annotations

import argparse
import json
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
    token_file=storage/'owner-token.txt'; log=storage/'watchdog.jsonl'
    worker=None; failures=0
    def note(event, **data):
        log.open('a',encoding='utf-8').write(json.dumps({'at':time.time(),'event':event,**data},ensure_ascii=False)+'\n')
    def start(reason):
        nonlocal worker
        if worker and worker.poll() is None: worker.terminate(); worker.wait(timeout=8)
        worker=subprocess.Popen([sys.executable, str(root/'tools/workbench.py'),'--project',str(root),'--port',str(args.port)],cwd=root)
        note('worker_started',reason=reason,pid=worker.pid)
    start('supervisor_start')
    while True:
        time.sleep(3)
        unhealthy=False; reason=''
        try:
            request(f'http://127.0.0.1:{args.port}/health')
            token=token_file.read_text(encoding='utf-8').strip()
            projects=request(f'http://127.0.0.1:{args.port}/api/v1/projects',token).get('items',[])
            for project in projects:
                snap=request(f"http://127.0.0.1:{args.port}/api/v1/projects/{project['id']}/snapshot",token)
                batch=(snap.get('batches') or [{}])[-1]
                stalled=(batch.get('state') in {'running','reconciling'} and not batch.get('active_tasks')
                         and snap.get('task_counts',{}).get('todo',0)>0)
                if stalled: unhealthy=True; reason='BATCH_STALLED:'+project['id']; break
        except Exception as exc:
            unhealthy=True; reason='HTTP_UNRESPONSIVE:'+type(exc).__name__
        failures=failures+1 if unhealthy else 0
        if unhealthy: note('unhealthy',reason=reason,consecutive=failures)
        if failures>=3:
            start(reason); failures=0


if __name__ == '__main__': main()
