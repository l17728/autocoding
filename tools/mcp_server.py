"""Launch a project-scoped local stdio MCP bridge for an assigned Agent."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import Core
from autocoding.mcp import Bridge
from autocoding.redaction import Redactor

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--agent',required=True)
    parser.add_argument('--task')
    parser.add_argument('--attempt',type=int)
    args=parser.parse_args()
    bridge=Bridge(Core(args.database),args.agent,args.project.resolve(),Redactor(),
                  task_id=args.task,attempt=args.attempt)
    sys.stdout.reconfigure(encoding='utf-8')
    while line:=sys.stdin.buffer.readline(1024*1024+1):
        if len(line)>1024*1024:
            return 2
        try:
            message=json.loads(line)
            response=bridge.handle(message) if isinstance(message,dict) else None
        except (ValueError,UnicodeError):
            response={'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'PARSE_ERROR'}}
        if response is not None:
            print(json.dumps(response,ensure_ascii=False),flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
