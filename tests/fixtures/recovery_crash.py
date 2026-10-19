"""Owned subprocess fault injection: abrupt exit around recovery commit."""
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from autocoding.host import Host
from autocoding.recovery import Recovery

root=Path(sys.argv[1])
phase=sys.argv[2]
host=Host(root,root/'.local/workbench')
recovery=Recovery(host)
fingerprint=sys.argv[3]
original=host.core._event
def event(conn, kind, actor, **values):
    original(conn,kind,actor,**values)
    if kind=='OwnerReconciledAttempt' and phase=='before_commit':
        os._exit(71)
host.core._event=event
recovery.resolve('run_a',fingerprint,'Stopped and checked external effects',True,key='crash-recover')
os._exit(72)
