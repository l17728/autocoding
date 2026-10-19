"""Verify a local integration candidate using Owner-provided acceptance commands."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import Core, DomainError
from autocoding.integration import Integration


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--base-sha',required=True)
    parser.add_argument('--task',action='append',required=True)
    parser.add_argument('--checks',type=Path,required=True,help='Owner JSON mapping criterion names to native argv arrays')
    parser.add_argument('--timeout',type=int,default=60)
    args=parser.parse_args()
    try:
        checks=json.loads(args.checks.read_text(encoding='utf-8'))
        result=Integration(Core(args.database),args.project,args.project/'.local/integration',checks).prepare(
            args.base_sha,args.task,timeout=args.timeout)
        print(json.dumps({'state':result['state'],'artifact':result['artifact'],
                          'sha256':result['sha256'],'published':False},ensure_ascii=False))
        return 0 if result['state']=='verified_candidate' else 1
    except DomainError as exc:
        print(json.dumps({'error':exc.code}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
