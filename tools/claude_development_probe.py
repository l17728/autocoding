"""Real Claude tool/approval/verification probe in a new disposable Git fixture.

The only auto-approved operation writes the exact advertised add function to
the fixture's calculator.py. All other permission requests are denied.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tools')]
from autocoding.claude import ClaudeSession
from autocoding.core import Core, digest
from autocoding.delivery import Approvals
from autocoding.providers import resolve_zhipu, claude_environment
from autocoding.runtime import SessionRuntime
from autocoding.redaction import Redactor
from autocoding.verifier import Verifier
from autocoding.workspaces import git
from probe_agents import executable

CODE = 'def add(a, b):\n    return a + b\n'

async def main():
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='claude-development-', dir=local))
    repository = output / 'project'
    repository.mkdir()
    git(repository, 'init')
    git(repository, 'config', 'user.name', 'Autocoding fixture')
    git(repository, 'config', 'user.email', 'fixture@example.invalid')
    (repository / 'calculator.py').write_text('def add(a, b):\n    raise NotImplementedError\n', encoding='utf-8')
    (repository / 'test_calculator.py').write_text(
        'import unittest\nfrom calculator import add\nclass Tests(unittest.TestCase):\n'
        '    def test_add(self):\n        self.assertEqual(5, add(2, 3))\n'
        '        self.assertEqual(-1, add(-3, 2))\n'
        '        self.assertEqual(0, add(0, 0))\n', encoding='utf-8')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'fixture: failing calculator baseline')
    core = Core(output / 'core.db')
    core.create_task('task_calculator', 'Implement addition', ['unit'], key='task')
    lease = core.claim('task_calculator', 'claude', lease_seconds=600, key='claim')
    config = resolve_zhipu(ROOT / '.local/credentials/opencode.json')
    env = claude_environment(config, 'https://open.bigmodel.cn/api/anthropic')
    env['CLAUDE_CONFIG_DIR'] = str(output / 'claude-config')
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    core.create_session('probe', 'claude', owner='human', simulated=False, key='session')
    approvals = Approvals(core)
    adapter = runtime = None
    report = {'model': config.model, 'key_printed': False, 'approval_count': 0, 'task_verified': False}
    pending = {}
    try:
        adapter = await ClaudeSession.connect(executable('claude'), cwd=repository, env=env,
            model=config.model, timeout=60, tools=('Read', 'Write'), persist=True)
        report['provider_session_id'] = adapter.thread_id
        runtime = SessionRuntime(core, 'probe', adapter)
        prompt = 'Read calculator.py, then use Write to replace it with exactly this code, then say IMPLEMENTED. The fixture change is authorized. Use only Read and Write on calculator.py.\n' + CODE
        command = core.enqueue('probe', 'human', 0, prompt, key='input')['command_id']
        await runtime.send(command, prompt)
        async with asyncio.timeout(150):
            while core.command(command)['state'] == 'acknowledged':
                event = await runtime.next_event()
                params = event.get('params', {})
                if event.get('method') == 'item/agentMessage/delta':
                    report.setdefault('public_output', []).append(Redactor([config.api_key]).text(params.get('delta', '')))
                if event.get('method') == 'tool/started':
                    report.setdefault('tool_names', []).append(params.get('toolName'))
                if event.get('method') == 'tool/requestApproval':
                    operation = params['operation']
                    body = operation.get('input', {})
                    target = Path(body.get('file_path', ''))
                    target = target if target.is_absolute() else repository / target
                    allowed = (target.resolve() == repository / 'calculator.py'
                               and (operation.get('tool_name') == 'Read' or
                                    (operation.get('tool_name') == 'Write'
                                     and body.get('content', '').replace('\r\n', '\n') == CODE)))
                    approval = approvals.register('probe', 1, event['id'], operation, key='request:' + event['id'])
                    approvals.decide(approval['id'], 'approved' if allowed else 'denied',
                                     approval['operation_hash'], 0, key='decision:' + event['id'])
                    delivery = approvals.begin_delivery(approval['id'])
                    sent = await adapter.respond_approval(event['id'], digest(operation), allow=allowed)
                    pending[sent['tool_use_id']] = (approval['id'], delivery['token'])
                    report['approval_count'] += 1
                elif event.get('method') in {'tool/result', 'approval/delivered'} and params['toolUseId'] in pending:
                    aid, token = pending.pop(params['toolUseId'])
                    approvals.finish_delivery(aid, token, confirmed=True)
        report['command_state'] = core.command(command)['state']
        if report['command_state'] != 'completed' or pending:
            raise RuntimeError('PROBE_INCOMPLETE')
        if (repository / 'calculator.py').read_text(encoding='utf-8') != CODE:
            raise RuntimeError('FIXTURE_CONTENT_MISMATCH')
        await runtime.close()
        runtime = None
        git(repository, 'add', 'calculator.py')
        git(repository, 'commit', '-m', 'feat: implement addition through Claude tool')
        head = git(repository, 'rev-parse', 'HEAD')
        job = core.report('task_calculator', 'claude', lease['lease_token'], lease['attempt'],
                          head, ['commit:' + head], key='report')
        verified = Verifier(core, repository, output / 'verification',
                            {'unit': [sys.executable, '-m', 'unittest', 'discover', '-v']}).run(job)
        report.update(task_verified=verified['state'] == 'done', head_sha=head, verification_artifact=verified['artifact'])
    except Exception as exc:
        report['error'] = getattr(exc, 'code', type(exc).__name__)
        report['correlation'] = getattr(adapter, 'last_correlation', None)
        report['control'] = getattr(adapter, 'last_control', None)
        # Adapter diagnostics contain only bounded protocol shape and boolean
        # correlation checks; never persist provider frame or prompt values.
        report['protocol_diagnostic'] = getattr(adapter, 'last_error_diagnostic', None)
    finally:
        if runtime:
            await runtime.close()
        elif adapter:
            await adapter.close()
        for aid, token in pending.values():
            approvals.finish_delivery(aid, token, confirmed=False)
        report['events'] = core.events()
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'events'}, indent=2))
    print('AUDIT:', output / 'report.json')
    return 0 if report['task_verified'] else 2

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
