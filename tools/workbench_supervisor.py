"""External watchdog for the local workbench.

Runs outside Uvicorn so it can recover an event-loop stall.  It treats both an
unresponsive health endpoint and a running/reconciling batch with no active
work (while todo work remains) as liveness failures.
"""
from __future__ import annotations

import argparse
import json
import os
import msvcrt
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler

_LOCAL_HTTP = build_opener(ProxyHandler({}))


def stop_process_tree(process):
    """Stop the supervised process and any interpreter child it launched."""
    if process is None or process.poll() is not None:
        return
    if os.name == 'nt':
        result = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0 and process.poll() is None:
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def request(url, token=None, timeout=3):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with _LOCAL_HTTP.open(Request(url, headers=headers), timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    root=args.project.resolve(); storage=root/'.local/workbench'; storage.mkdir(parents=True,exist_ok=True)
    lock=storage/'watchdog.lock'
    lock_handle=open(lock,'a+b')
    if lock_handle.tell() == 0:
        lock_handle.write(b'0'); lock_handle.flush()
    try:
        lock_handle.seek(0); msvcrt.locking(lock_handle.fileno(),msvcrt.LK_NBLCK,1)
    except OSError: raise SystemExit('workbench supervisor already running')
    entry=Path(__file__).with_name('workbench.py')
    token_file=storage/'owner-token.txt'; log=storage/'watchdog.jsonl'
    worker=None; failures=0; started_at=0.0; ready=False
    def note(event, **data):
        log.open('a',encoding='utf-8').write(json.dumps({'at':time.time(),'event':event,**data},ensure_ascii=False)+'\n')
    state_file=root/'.local/service-control/workbench.json'
    def start(reason):
        nonlocal worker, started_at, ready
        if worker and worker.poll() is None:
            stop_process_tree(worker)
        worker=subprocess.Popen([sys.executable, str(entry),'--project',str(root),'--port',str(args.port)],cwd=entry.parent.parent)
        started_at=time.monotonic()
        ready=False
        # start-local writes the initial supervisor record immediately after
        # spawning us. Wait for that atomic registration, then merge our worker
        # PID so the parent cannot overwrite it with stale initial state.
        for _ in range(40):
            try:
                state=json.loads(state_file.read_text(encoding='utf-8-sig'))
                state.update(supervisor_pid=os.getpid(),worker_pid=worker.pid,worker_started=time.time())
                temporary=state_file.with_name(state_file.name+f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(state),encoding='utf-8')
                os.replace(temporary,state_file)
                break
            except (OSError,ValueError):
                time.sleep(.25)
        note('worker_started',reason=reason,pid=worker.pid)
    try:
        start('supervisor_start')
        while True:
            time.sleep(3)
            # A dead child cannot become ready during the cold-start grace period.
            # Restart immediately so transient startup exits do not look like hangs.
            if worker is None or worker.poll() is not None:
                note('worker_exited',pid=worker.pid if worker else None)
                start('WORKER_EXITED')
                failures=0
                continue
            unhealthy=False; reason=''
            try:
                request(f'http://127.0.0.1:{args.port}/health')
                token=token_file.read_text(encoding='utf-8').strip()
                identity=request(f'http://127.0.0.1:{args.port}/api/v1/project',token)
                remote_root=Path(str(identity.get('root',''))).resolve() if identity.get('root') else None
                if remote_root != root:
                    note('port_identity_conflict',expected=str(root),actual=str(remote_root))
                    raise SystemExit('PORT_IDENTITY_CONFLICT')
                if not ready:
                    note('worker_ready',pid=worker.pid,elapsed_seconds=round(time.monotonic()-started_at,2))
                    ready=True
            except Exception as exc:
                reason='HTTP_UNRESPONSIVE:'+type(exc).__name__
                # The app binds only after persistent project/agent recovery.
                # Allow that cold start the same bounded window as the launcher,
                # then supervise every request immediately once it has been ready.
                if not ready and time.monotonic()-started_at < 150:
                    continue
                unhealthy=True
            if not unhealthy:
                probe_stage='project_list'
                try:
                    token=token_file.read_text(encoding='utf-8').strip()
                    projects=request(f'http://127.0.0.1:{args.port}/api/v1/projects',token,timeout=10)
                    if not isinstance(projects, list):
                        raise ValueError('PROJECT_LIST_INVALID')
                    for project in projects:
                        probe_stage='snapshot:'+str(project.get('id','unknown'))
                        snap=request(f"http://127.0.0.1:{args.port}/api/v1/projects/{project['id']}/snapshot",token,timeout=10)
                        batch=(snap.get('batches') or [{}])[-1]
                        # Reconciliation may legitimately await evidence or the Owner.
                        # Only a claimed running batch with no active task is a liveness fault.
                        stalled=(batch.get('state') == 'running' and not batch.get('active_tasks')
                                 and snap.get('task_counts',{}).get('leased',0)>0)
                        if stalled: unhealthy=True; reason='BATCH_STALLED:'+project['id']; break
                except Exception as exc:
                    note('business_probe_failed',stage=probe_stage,reason=type(exc).__name__)
                    # A single project listing/snapshot failure is an application
                    # diagnostic. Restarting the whole service can amplify a bad
                    # project record into a permanent restart loop.
            failures=failures+1 if unhealthy else 0
            if unhealthy: note('unhealthy',reason=reason,consecutive=failures)
            if failures>=3:
                start(reason); failures=0
    finally:
        # If the watchdog exits due to an unexpected exception, do not leave its
        # supervised worker detached and apparently healthy without supervision.
        stop_process_tree(worker)
        try:
            lock_handle.seek(0); msvcrt.locking(lock_handle.fileno(),msvcrt.LK_UNLCK,1)
        except OSError:
            pass
        lock_handle.close()


if __name__ == '__main__': main()
