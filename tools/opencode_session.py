"""Verify one real OpenCode turn through the platform host and ledger."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autocoding.host import Host

async def main():
    output = Path(tempfile.mkdtemp(prefix='opencode-session-', dir=ROOT / '.local'))
    host = Host(ROOT, output, config=ROOT / '.local/credentials/opencode.json')
    report = {'agent':'opencode','key_printed':False,'task_verified':False}
    try:
        session = await host.start('opencode')
        report.update(session)
        command = await host.send(session['session_id'], 0, 'Reply with exactly CONNECTION_OK.', key='probe')
        async with asyncio.timeout(120):
            while host.core.command(command['command_id'])['state'] in {'queued','dispatching','acknowledged'}:
                await asyncio.sleep(.2)
        report['command_state'] = host.core.command(command['command_id'])['state']
        text = ''.join(e.get('text','') for e in host.output if e['type']=='text')
        report['response_matches'] = text.strip() == 'CONNECTION_OK'
    except Exception as exc:
        report['error'] = getattr(exc, 'code', type(exc).__name__)
    finally:
        await host.close()
    report['events'] = host.core.events()
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'events'}, indent=2))
    print('AUDIT:', output / 'report.json')
    return 0 if report.get('response_matches') and report.get('command_state') == 'completed' else 2

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
