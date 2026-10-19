"""Synthetic long-history page and snapshot budget/latency checks, no models."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import encoded
from autocoding.dashboard import Dashboard
from autocoding.host import Host


async def main():
    output=Path(tempfile.mkdtemp(prefix='dashboard-probe-',dir=ROOT/'.local'))
    host=Host(output,output/'state')
    try:
        for i in range(105):
            host.core.create_task('task_'+str(i).zfill(4),'Synthetic task',['unit'],key=str(i))
        with host.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for i in range(10000):
                value={'id':'r'+str(i),'task_id':'task_'+str(i%105).zfill(4),'state':'done','created_at':i,'summary':'history '*100}
                conn.execute('INSERT INTO metadata VALUES (?,?)',('run:'+value['id'],encoded(value)))
            conn.commit()
        dashboard=Dashboard(host)
        samples=[]
        sizes=[]
        for _ in range(30):
            started=time.monotonic()
            value=dashboard.snapshot()
            samples.append((time.monotonic()-started)*1000)
            sizes.append(len(encoded(value).encode()))
            assert value['totals']['runs']==10000
            assert len(value['tasks'])==100
        samples.sort()
        last=dashboard.history('runs',9980,20)
        assert len(last['items'])==20 and last['next_offset'] is None
        report={'synthetic':True,'model_calls':0,'history_records':10000,'samples':30,
                'snapshot_p95_ms':round(samples[28],2),'snapshot_max_bytes':max(sizes),
                'passed':samples[28]<500 and max(sizes)<3*1024*1024}
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({**report,'report':str(output/'report.json')}))
        return 0 if report['passed'] else 1
    finally:
        await host.close()


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
