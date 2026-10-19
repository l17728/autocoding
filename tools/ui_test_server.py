"""Start browser tests with a fresh ledger; archive previous test state intact."""
import argparse
import os
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    project=ROOT/'.local/ui-e2e'
    storage=project/'.local/workbench'
    archive=ROOT/'.local/ui-e2e-archives'/uuid.uuid4().hex
    # Exact fixture path only. Never move user projects or follow a redirected root.
    if storage.resolve()!=storage or not storage.resolve().is_relative_to((ROOT/'.local/ui-e2e').resolve()):
        raise RuntimeError('TEST_STORAGE_PATH_INVALID')
    if archive.resolve()!=archive or not archive.resolve().is_relative_to(ROOT/'.local'):
        raise RuntimeError('TEST_ARCHIVE_PATH_INVALID')
    if storage.exists():
        archive.parent.mkdir(parents=True,exist_ok=True)
        storage.rename(archive)
    project.mkdir(parents=True,exist_ok=True)
    sys.argv=[sys.argv[0],'--project',str(project),'--port',str(args.port)]
    from workbench import main as serve
    # Browser regression is offline with respect to the PM provider unless a
    # dedicated supplier acceptance explicitly opts in. Includes new projects.
    if os.environ.get('AUTOCODING_UI_ALLOW_ZHIPU')!='1':
        from autocoding.host import Host
        from autocoding.core import DomainError
        async def disabled(*args,**kwargs):
            raise DomainError('ZHIPU_DISABLED_IN_UI_TESTS')
        Host.pm_text=disabled
        Host.propose=disabled
    serve()


if __name__=='__main__':
    main()
