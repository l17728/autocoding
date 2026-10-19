"""Review a copied real project ledger without changing the original project."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host


async def main(source):
    output=Path(tempfile.mkdtemp(prefix='pm-reflection-',dir=ROOT/'.local'))
    storage=output/'state';storage.mkdir()
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as original:
        with sqlite3.connect(storage/'platform.db') as copied:
            original.backup(copied)
    host=Host(output,storage,config=ROOT/'.local/credentials/opencode.json')
    report={'passed':False,'key_printed':False,'source_ledger_unchanged':True}
    try:
        review=await host.reflection.run()
        report.update(state=review['state'],review_id=review.get('id'),items=len(review.get('items',[])))
        if review['state']=='completed':
            answer=await host.pm_text('不要使用工具。根据提供的历史经验，仅返回其中一个 review_id，不要解释。')
            report['retrieval_verified']=review['id'] in answer
            report['passed']=report['retrieval_verified'] and bool(host.reflection.references())
        else:
            report['error']=review.get('error')
    except Exception as exc:
        report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        await host.close()
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db',type=Path,required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().source_db)))
