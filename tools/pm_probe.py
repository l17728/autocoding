"""Live PM planning probe; creates tasks only in an isolated fixture ledger."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.planner import adopt


async def main():
    storage=Path(tempfile.mkdtemp(prefix='pm-probe-',dir=ROOT/'.local'))
    host=Host(ROOT,storage)
    try:
        proposal=await host.propose('Plan a tiny Python addition library and its unit tests. Propose exactly two tasks.',
                                   ['unit tests pass'])
        result=adopt(host.core,proposal['id'],key='probe-adopt')
        report={'key_printed':False,'provider':'claude-zhipu','proposal_id':proposal['id'],
                'task_count':len(result['task_ids']),'tasks_created':len(host.core.snapshot()['tasks']),
                'execution_started':False}
        (storage/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({**report,'report':str(storage/'report.json')}))
    finally:
        await host.close()


if __name__=='__main__':
    asyncio.run(main())
