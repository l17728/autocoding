"""Real Claude skip-permissions question, PM answer, and exact-session resume."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host


async def wait_command(host,cid):
    async with asyncio.timeout(180):
        while host.core.command(cid)['state'] not in {'completed','failed','cancelled','delivery_unknown'}:
            await asyncio.sleep(.1)
    return host.core.command(cid)['state']


async def main():
    output=Path(tempfile.mkdtemp(prefix='question-probe-',dir=ROOT/'.local'))
    host=Host(ROOT,output,config=ROOT/'.local/credentials/opencode.json')
    report={'key_printed':False,'passed':False}
    try:
        first=await host.start('claude')
        sid=first['session_id']
        prompt=('This is an authorized interaction integration test. You MUST call AskUserQuestion once with exactly two questions, '
                'even though the preferences are already stated. Ask output format with JSON and YAML options, and identifier '
                'prefix with app_ and lib_ options. Free text is allowed. The project requires JSON and the custom prefix team_. '
                'After receiving the answers, briefly repeat the chosen format and prefix. Do not use other tools.')
        command=await host.send(sid,0,prompt,key='question-probe')
        report['first_state']=await wait_command(host,command['command_id'])
        questions=host.questions.list()
        report['question_count']=len(questions)
        report['pm_answered']=bool(questions) and all(q.get('answered_by')=='pm' for q in questions)
        report['custom_answer_used']=any('team_' in str(q.get('answers')) for q in questions)
        resumed=await host.start('claude',resume_from=sid)
        report['same_provider_session']=resumed['provider_session_id']==first['provider_session_id']
        command=await host.send(resumed['session_id'],0,'Without tools, repeat the output format and custom prefix we just selected.',key='resume-probe')
        report['resume_state']=await wait_command(host,command['command_id'])
        text=''.join(e.get('text','') for e in host.output if e['session_id']==resumed['session_id'])
        report['resume_retained_context']='JSON' in text and 'team_' in text
        report['passed']=(report['first_state']=='completed' and report['pm_answered'] and report['custom_answer_used']
                          and report['same_provider_session'] and report['resume_state']=='completed' and report['resume_retained_context'])
    except Exception as exc:
        report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        await host.close()
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
