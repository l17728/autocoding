"""Native TUI startup/stop probe; unperformed interaction checks stay pending."""
import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.workspaces import git


async def main(agents):
    output=Path(tempfile.mkdtemp(prefix='terminal-matrix-',dir=ROOT/'.local'))
    repo=output/'project'
    repo.mkdir()
    (repo/'README.md').write_text('Isolated native terminal acceptance fixture.\n',encoding='utf-8')
    git(repo,'init')
    git(repo,'add','README.md')
    git(repo,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-m','baseline')
    host=Host(repo,output/'state',config=ROOT/'.local/credentials/opencode.json')
    report={'model_prompts_sent':0,'scope':'native_process_startup_and_confirmed_local_stop','agents':{},'complete_matrix_passed':False}
    try:
        for agent in agents:
            entry={'process_output':'pending','native_ui':'requires_browser','local_stop':'pending','native_confirmation':'pending','ime':'pending',
                   'control_sequences':'pending','takeover':'pending','context_resume':'pending'}
            report['agents'][agent]=entry
            sid=None
            try:
                record=await host.terminals.start(agent)
                sid=record['id']
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    frames=list(host.terminals.output[sid]['frames'])
                    text=''.join(f['text'] for f in frames)
                    printable=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\)', '', text).strip()
                    if len(printable)>40 or host.terminals.sessions[sid]._closed:
                        break
                    await asyncio.sleep(.25)
                frames=list(host.terminals.output[sid]['frames'])
                text=''.join(f['text'] for f in frames)
                # Output alone may be warnings or capability queries. Never
                # turn it into evidence that the native UI is usable.
                entry.update(process_output='observed' if text and not host.terminals.sessions[sid]._closed else 'inconclusive',
                             control_queries=re.findall(r'\x1b\[[0-?]*[ -/]*[@-~]',text)[:10],
                             output_sha256=hashlib.sha256(text.encode()).hexdigest(),output_bytes=len(text.encode()))
            except Exception as exc:
                entry.update(process_output='failed',reason=getattr(exc,'code','TERMINAL_PROBE_FAILED'))
            finally:
                if sid and sid in host.terminals.sessions:
                    await host.terminals.stop(sid)
                    entry['local_stop']='passed' if host.terminals.records[sid]['stopped_confirmed'] else 'failed'
            print(json.dumps({'agent':agent,**entry}),flush=True)
            (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    finally:
        await host.close()
    print(json.dumps({'report':str(output/'report.json'),'complete_matrix_passed':False}),flush=True)
    return 0 if all(e['process_output']=='observed' and e['local_stop']=='passed' for e in report['agents'].values()) else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents',nargs='+',choices=['codex','claude','opencode'],default=['codex','claude','opencode'])
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.agents)))
