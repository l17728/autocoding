"""Bounded synthetic knowledge load/restart test; never invokes a model."""
import argparse
import asyncio
from collections import deque
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.core import encoded


async def main(seconds, count):
    output=Path(tempfile.mkdtemp(prefix='learning-soak-',dir=ROOT/'.local'))
    tracemalloc.start()
    host=Host(output,output/'state')
    with host.core.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for i in range(count):
            rid='synthetic_'+str(i)
            record={'id':rid,'created_at':host.core.clock(),'state':'completed','archived':False,
                'items':[{'category':'lesson','topic':'subject '+str(i),'content':'recommendation '+str(i),
                'confidence':'hypothesis','evidence_ids':['synthetic_event']}]}
            conn.execute('INSERT INTO metadata VALUES (?,?)',('pm_review:'+rid,encoded(record)))
        conn.commit()
    started=time.monotonic()
    cold=host.reflection.learning.page()
    cold_ms=(time.monotonic()-started)*1000
    assert cold['total']==count
    host.reflection.learning.outcomes()
    baseline=tracemalloc.get_traced_memory()[0]
    timings=deque(maxlen=10000)
    iterations=0;restarts=0;last_restart=time.monotonic();last_report=last_restart
    started=time.monotonic()
    try:
        while time.monotonic()-started<seconds:
            tick=time.monotonic()
            page=host.reflection.learning.page((iterations*25)%count)
            assert len(page['items'])<=25 and page['total']==count
            assert len(host.reflection.references())<=12
            assert host.reflection.learning.outcomes()['status']=='insufficient_samples'
            timings.append((time.monotonic()-tick)*1000)
            iterations+=1
            if time.monotonic()-last_restart>=60:
                await host.close()
                host=Host(output,output/'state')
                assert host.reflection.learning.page()['total']==count
                restarts+=1;last_restart=time.monotonic()
            if time.monotonic()-last_report>=30:
                print(json.dumps({'elapsed_seconds':round(time.monotonic()-started),'iterations':iterations,'restarts':restarts}),flush=True)
                last_report=time.monotonic()
            await asyncio.sleep(.01)
        growth=tracemalloc.get_traced_memory()[0]-baseline
        ordered=sorted(timings)
        p95=ordered[min(len(ordered)-1,int(len(ordered)*.95))]
        report={'synthetic':True,'model_calls':0,'records':count,'elapsed_seconds':round(time.monotonic()-started,2),
            'iterations':iterations,'restarts':restarts,'cold_ms':round(cold_ms,2),'p95_ms':round(p95,2),
            'tracked_growth_bytes':growth,'passed':p95<250 and growth<16*1024*1024}
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({**report,'report':str(output/'report.json')}),flush=True)
        return 0 if report['passed'] else 1
    finally:
        tracemalloc.stop()
        await host.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--records',type=int,default=5000)
    args=parser.parse_args()
    if args.seconds<1 or not 1<=args.records<=100000:
        parser.error('seconds must be positive; records must be 1..100000')
    raise SystemExit(asyncio.run(main(args.seconds,args.records)))
