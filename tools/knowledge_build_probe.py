"""Measure first catalogue construction without tracemalloc instrumentation."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import encoded
from autocoding.host import Host


async def main():
    output=Path(tempfile.mkdtemp(prefix='knowledge-build-',dir=ROOT/'.local'))
    host=Host(output,output/'state')
    try:
        with host.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for i in range(5000):
                record={'id':str(i),'state':'completed','created_at':host.core.clock(),
                        'items':[{'category':'lesson','topic':'subject '+str(i),'content':'recommendation '+str(i),
                                  'confidence':'hypothesis','evidence_ids':['synthetic']}]}
                conn.execute('INSERT INTO metadata VALUES (?,?)',('pm_review:'+str(i),encoded(record)))
            conn.commit()
        started=time.perf_counter()
        assert host.reflection.learning.page()['total']==5000
        cold=(time.perf_counter()-started)*1000
        await host.close()
        host=Host(output,output/'state')
        started=time.perf_counter()
        assert host.reflection.learning.page()['total']==5000
        reload=(time.perf_counter()-started)*1000
        report={'records':5000,'model_calls':0,'tracemalloc':False,'first_build_ms':round(cold,2),
                'cached_reload_ms':round(reload,2),'passed':cold<2000 and reload<1000,
                'notice':'Single local synthetic sample; not comparable to tracemalloc-enabled timings.'}
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({**report,'report':str(output/'report.json')}))
        return 0 if report['passed'] else 1
    finally:
        await host.close()


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
