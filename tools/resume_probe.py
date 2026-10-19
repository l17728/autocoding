"""Verify real provider restart and conversation continuity without printing output."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autocoding.host import Host


async def turn(host, sid, prompt, key):
    command = await host.send(sid, 0, prompt, key=key)
    async with asyncio.timeout(180):
        while host.core.command(command['command_id'])['state'] not in {
                'completed', 'failed', 'cancelled', 'delivery_unknown'}:
            await asyncio.sleep(.1)
    return host.core.command(command['command_id'])['state']


async def main(agent):
    output = Path(tempfile.mkdtemp(prefix=agent+'-resume-', dir=ROOT/'.local'))
    project = output/'project'
    project.mkdir()
    host = Host(project, output/'storage', config=ROOT/'.local/credentials/opencode.json')
    report = {'agent':agent, 'passed':False, 'key_printed':False}
    marker = 'CONTEXT_' + uuid.uuid4().hex
    try:
        first = await host.start(agent)
        report['first_state'] = await turn(host, first['session_id'],
            'Do not use tools or access files. Remember this conversation marker: '+marker+'. Reply only OK.', 'first')
        if report['first_state'] != 'completed':
            raise RuntimeError('FIRST_TURN_FAILED')
        resumed = await host.start(agent, resume_from=first['session_id'])
        report['same_provider_session'] = first['provider_session_id'] == resumed['provider_session_id']
        report['resume_state'] = await turn(host, resumed['session_id'],
            'Without tools or files, reply only with the conversation marker I gave you earlier.', 'resumed')
        text = ''.join(e.get('text','') for e in host.output if e['session_id']==resumed['session_id'])
        report['context_retained'] = marker in text
        report['passed'] = report['same_provider_session'] and report['context_retained'] and report['resume_state']=='completed'
    except Exception as exc:
        report['error'] = getattr(exc, 'code', type(exc).__name__)
    finally:
        await host.close()
    (output/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({**report, 'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=['codex','opencode','claude'], required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().agent)))
