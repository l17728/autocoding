"""Import reviewed seeds to the workbench and registered project ledgers."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.projects import Projects
from autocoding.cold_start import install


async def main(project):
    host=Host(project,project/'.local/workbench')
    registry=Projects(host)
    reports=[]
    try:
        for record in registry.list():
            # Import knowledge without recovery of live task/session state.
            target=host if record['id']==host.project_id else Host(
                Path(record['root']),host.storage/'projects'/record['id'])
            registry.hosts[record['id']]=target
            report=install(target.core)
            reports.append({'project_id':record['id'],**report,'active_references':len(target.reflection.references()),
                            'knowledge_groups':target.reflection.learning.page()['total']})
    finally:
        await registry.close()
    output=ROOT/'.local/cold-start/import-report.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(reports,indent=2),encoding='utf-8')
    print(json.dumps(reports))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=ROOT)
    asyncio.run(main(parser.parse_args().project.resolve()))
