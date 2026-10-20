"""Owner delegates one goal; existing verified workflow owns execution."""
import asyncio
import json
import uuid
from .acceptance import Acceptance
from .core import DomainError,encoded
from .planner import adopt

def records(host):
    with host.core.connection() as conn:
        rows=conn.execute("""SELECT d.value,b.value batch FROM metadata d LEFT JOIN metadata b
            ON b.key='batch:'||json_extract(d.value,'$.batch_id')
            WHERE d.key GLOB 'delegation:*' ORDER BY json_extract(d.value,'$.created_at') DESC LIMIT 50""").fetchall()
    return [{**json.loads(row['value']),**({'execution_state':json.loads(row['batch'])['state']} if row['batch'] else {})} for row in rows]

def recover(host):
    with host.core.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute("SELECT key,value FROM metadata WHERE key GLOB 'delegation:*'").fetchall():
            record=json.loads(row['value'])
            if record['state'] in {'planning','planned','adopted'}:
                record.update(state='reconciling',reason='DELEGATION_RESTART_RECONCILIATION_REQUIRED')
                conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),row['key']))
                host.core._event(conn,'PMDelegationChanged','runner',delegation_id=record['id'],state='reconciling')
        conn.commit()


async def delegate(host,goal,criteria,*,agent='codex',agents=None,max_concurrency=1,max_attempts=2):
    if host.paused:raise DomainError('PROJECT_PAUSED')
    if host.coordinator.current is not None:raise DomainError('BATCH_ACTIVE')
    # Owners describe product outcomes. PM owns the executable verification
    # policy and maps every planned task to its maintained automatic check.
    Acceptance(host).bootstrap(key='delegate-policy:'+uuid.uuid4().hex)
    if not criteria or any(not isinstance(item,str) or not item.strip() for item in criteria):
        raise DomainError('ACCEPTANCE_REQUIRED')
    if agent not in {'codex','claude','opencode'} or (agents is not None and (
            not agents or len(agents)>3 or len(set(agents))!=len(agents)
            or any(a not in {'codex','claude','opencode'} for a in agents))):
        raise DomainError('UNSUPPORTED_AGENT')
    if type(max_attempts) is not int or not 1<=max_attempts<=3 or type(max_concurrency) is not int or not 1<=max_concurrency<=3:
        raise DomainError('INVALID_BATCH')
    record={'id':'delegation_'+uuid.uuid4().hex,'goal':host.redactor.text(goal),'acceptance':criteria,
            'created_at':host.core.clock(),'agent':agent,'max_attempts':max_attempts,'max_concurrency':max_concurrency}
    def save(state,**values):
        record.update(state=state,**values)
        with host.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('delegation:'+record['id'],encoded(record)))
            host.core._event(conn,'PMDelegationChanged','pm',delegation_id=record['id'],state=state)
            conn.commit()
    save('planning')
    try:
        planning_goal=goal+'\n\n业务验收标准（PM 必须在任务说明中落实并在复审中逐项核对）：\n'+ '\n'.join('- '+item for item in criteria)
        proposal=await host.propose(planning_goal,['pm_auto'])
        save('planned',proposal_id=proposal['id'])
        if {c for task in proposal['tasks'] for c in task['acceptance']}!={'pm_auto'}:
            raise DomainError('DELEGATION_ACCEPTANCE_GAP')
        if host.paused:raise DomainError('PROJECT_PAUSED')
        adopted=adopt(host.core,proposal['id'],key=record['id'])
        save('adopted',task_ids=adopted['task_ids'])
        batch=await host.coordinator.start(adopted['task_ids'],agent=agent,agents=agents,
            max_concurrency=max_concurrency,max_attempts=max_attempts)
        save('executing',batch_id=batch['id'])
        return {**record,'batch':batch}
    except BaseException as exc:
        save('reconciling' if isinstance(exc,asyncio.CancelledError) else 'stopped',
             reason=getattr(exc,'code','DELEGATION_INTERRUPTED'))
        raise
