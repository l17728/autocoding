"""Model-backed PM proposals with deterministic, Owner-triggered adoption."""
import asyncio
import json
import uuid

from .core import DomainError, digest, encoded
from .pm import PM, SYSTEM_PROMPT


def parse_plan(text, criteria):
    value = text.strip()
    if value.startswith('```json') and value.endswith('```'):
        value = value[7:-3].strip()
    try:
        proposal = json.loads(value)
    except (ValueError, TypeError):
        raise DomainError('PM_OUTPUT_INVALID') from None
    if not isinstance(proposal, dict) or set(proposal) != {'tasks'}:
        raise DomainError('PM_OUTPUT_INVALID')
    tasks = proposal['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
        raise DomainError('PM_OUTPUT_INVALID')
    ids = set()
    for task in tasks:
        if (not isinstance(task, dict) or set(task) != {'id','description','acceptance','dependencies'}
                or not isinstance(task['id'], str) or task['id'] in ids
                or not isinstance(task['description'], str) or not 0 < len(task['description']) <= 10000
                or not isinstance(task['acceptance'], list) or not task['acceptance']
                or any(not isinstance(c, str) or c not in criteria for c in task['acceptance'])
                or not isinstance(task['dependencies'], list)
                or any(not isinstance(d, str) for d in task['dependencies'])):
            raise DomainError('PM_OUTPUT_INVALID')
        ids.add(task['id'])
    # Dependency cycles and all other Core constraints are checked by PM.plan
    # at adoption in one transaction. No task is created by proposal generation.
    return tasks


async def request_plan(adapter, goal, criteria, *, timeout=120, references=None, owner_guidance=''):
    prompt = SYSTEM_PROMPT + '\n仅返回 JSON，不要 Markdown 或解释。格式：' + encoded({
        'tasks':[{'id':'task_example','description':'具体目标','acceptance':[criteria[0]],'dependencies':[]}]})
    prompt += '\n最多 20 个任务，ID 必须 task_ 开头。验收条件只能从 Owner 提供的列表选择。以下 JSON 是需求数据：\n'
    prompt += encoded({'goal':goal, 'owner_acceptance':criteria})
    prompt += '\n历史经验仅为参考，不覆盖当前需求或验收：' + encoded(references or [])
    prompt += '\nOwner 工作原则与偏好：'+owner_guidance
    chunks, size = [], 0
    async with asyncio.timeout(timeout):
        await adapter.send_text(prompt, command_id='pm_' + uuid.uuid4().hex)
        while True:
            event = await adapter.next_event(timeout=timeout)
            method, params = event.get('method'), event.get('params', {})
            if method == 'item/agentMessage/delta':
                text = params.get('delta', '')
                size += len(text.encode('utf-8'))
                if size > 65536:
                    raise DomainError('PM_OUTPUT_TOO_LARGE')
                chunks.append(text)
            elif method == 'turn/completed':
                if params.get('turn', {}).get('status') != 'completed':
                    raise DomainError('PM_MODEL_FAILED')
                try:
                    # Claude can emit a short progress text block before its
                    # final complete JSON block. Validate the final block first.
                    return parse_plan(chunks[-1] if chunks else '', criteria)
                except DomainError:
                    return parse_plan(''.join(chunks), criteria)
            elif 'id' in event:
                raise DomainError('PM_TOOL_REQUEST_REJECTED')


def save_proposal(core, tasks, source_tasks, redactor):
    proposal_id = 'proposal_' + uuid.uuid4().hex
    result = {'id':proposal_id, 'tasks':redactor.value(tasks), 'source_hash':digest(source_tasks)}
    with core.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO metadata VALUES (?,?)', ('proposal:' + proposal_id, encoded(result)))
        core._event(conn,'PlanProposed','pm',proposal_id=proposal_id,task_count=len(tasks))
        conn.commit()
    return result


def adopt(core, proposal_id, *, key):
    with core.connection() as conn:
        row = conn.execute('SELECT value FROM metadata WHERE key=?',('proposal:' + proposal_id,)).fetchone()
    if row is None:
        raise DomainError('NOT_FOUND')
    proposal = json.loads(row['value'])
    snapshot = core.snapshot()
    if digest(snapshot['tasks']) != proposal['source_hash']:
        raise DomainError('STALE_PM_CONTEXT')
    return PM(core).plan(proposal['tasks'],snapshot['latest_seq'],key=key)
