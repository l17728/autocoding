import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from autocoding.process_lock import ProcessLock

lock = ProcessLock(sys.argv[1])
if not lock.acquire():
    raise SystemExit('LOCKED')
print('LOCKED', flush=True)
sys.stdin.readline()
lock.release()
