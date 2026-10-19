"""Bounded dependency-aware execution of an Owner-selected task plan."""
import asyncio
import json
import uuid

from .core import DomainError, digest, encoded
from .integration import Integration
from .workspaces import git

FINAL = {'verified_candidate', 'rework_required', 'reconciling', 'failed'}


class Coordinator:
    def __init__(self, host):
        self.host, self.core = host, host.core
        self.current = None
        self.reserved = {}

    def list(self):
        with self.core.connection() as conn:
            rows = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key LIKE 'batch:%'")]
        return sorted(rows, key=lambda r:r['created_at'])

    def save(self, record, state, **values):
        record.update(state=state, **values)
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('batch:' + record['id'], encoded(record)))
            self.core._event(conn,'TaskBatchChanged','pm',batch_id=record['id'],state=state,task_ids=record['task_ids'])
            conn.commit()

    def recover(self):
        for record in self.list():
            if record['state'] not in FINAL:
                self.save(record,'reconciling',reason='BATCH_RESTART_RECONCILIATION_REQUIRED')

    async def start(self, task_ids, *, max_attempts=2, agent='claude', agents=None, max_concurrency=1):
        agents = agents if agents is not None else [agent]
        if (not isinstance(agents,list) or not agents or len(agents)>3 or
                any(a not in {'claude','codex','opencode'} for a in agents) or len(set(agents))!=len(agents)
                or type(max_concurrency) is not int or not 1 <= max_concurrency <= 3):
            raise DomainError('INVALID_BATCH_CONCURRENCY')
        if agent not in {'claude','codex','opencode'}:
            raise DomainError('UNSUPPORTED_AGENT')
        if self.host.paused:
            raise DomainError('PROJECT_PAUSED')
        if self.current is not None:
            raise DomainError('BATCH_ACTIVE')
        if (not isinstance(task_ids,list) or not 1 <= len(task_ids) <= 100 or len(set(task_ids)) != len(task_ids)
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3):
            raise DomainError('INVALID_BATCH')
        tasks, checks, policies = {}, {}, {}
        for tid in task_ids:
            if tid in self.host.workflow.active:
                raise DomainError('TASK_RUN_ACTIVE')
            task, selected, protected = self.host.workflow.configuration(tid)
            if task['state'] not in {'todo','done'}:
                raise DomainError('TASK_NOT_READY')
            tasks[tid] = task
            checks.update(selected)
            policies[tid] = digest({'checks':selected,'protected_paths':protected})
        with self.core.connection() as conn:
            for tid in task_ids:
                for dep in conn.execute('SELECT p.id,p.state FROM dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=?',(tid,)):
                    if dep['id'] not in tasks and dep['state'] != 'done':
                        raise DomainError('DEPENDENCY_NOT_SELECTED')
        bid = 'batch_' + uuid.uuid4().hex
        self.current = bid
        self.reserved = dict.fromkeys(task_ids,bid)
        try:
            base = await asyncio.to_thread(git,self.host.root,'rev-parse','HEAD')
            record = {'id':bid,'task_ids':task_ids,'created_at':self.core.clock(),'base_sha':base,'provider':agent,
                      'max_attempts':max_attempts,'run_ids':[],'attempts':{},
                      'agents':agents,'max_concurrency':max_concurrency,
                      'assignments':{tid:agents[i%len(agents)] for i,tid in enumerate(task_ids)}}
            self.save(record,'queued')
            self.host._background(self._perform(record,policies,checks))
            return dict(record)
        except BaseException:
            self.current, self.reserved = None, {}
            raise

    async def _perform(self, record, policies, checks):
        running = {}
        retry_heads = {}
        stopped = None
        try:
            while True:
                with self.core.connection() as conn:
                    tasks = {tid:dict(self.core._get(conn,'tasks',tid)) for tid in record['task_ids']}
                    pending = [t for t in tasks.values() if t['state'] != 'done']
                    ready = [t for t in pending if t['id'] not in running and t['state']=='todo' and not conn.execute(
                        "SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=? AND p.state!='done'",
                        (t['id'],)).fetchone()]
                if not pending and not running:
                    break
                if stopped and not running:
                    self.save(record,stopped[0],reason=stopped[1],active_tasks=[])
                    return
                if not ready and not running and not self.host.paused:
                    raise DomainError('BATCH_TASKS_BLOCKED')
                if not self.host.paused and not stopped:
                    for task in ready:
                        if len(running)>=record.get('max_concurrency',1):
                            break
                        tid=task['id']
                        _, selected, protected = self.host.workflow.configuration(tid)
                        if digest({'checks':selected,'protected_paths':protected}) != policies[tid]:
                            raise DomainError('OWNER_CHECK_CONFIGURATION_CHANGED')
                        run=await self.host.workflow.start(tid,base_sha=retry_heads.get(tid,record['base_sha']),
                            batch_id=record['id'],agent=record['assignments'][tid])
                        record['run_ids'].append(run['id'])
                        record['attempts'][tid]=record['attempts'].get(tid,0)+1
                        running[tid]=(run['id'],self.host.workflow.active[tid])
                state='paused' if self.host.paused else 'running'
                if record['state']!=state or record.get('active_tasks')!=list(running):
                    self.save(record,state,active_tasks=list(running))
                if not running:
                    await asyncio.sleep(.2)
                    continue
                finished,_=await asyncio.wait([work for _,work in running.values()],timeout=.2,
                                              return_when=asyncio.FIRST_COMPLETED)
                for tid,(rid,work) in list(running.items()):
                    if work not in finished:
                        continue
                    await work
                    del running[tid]
                    completed=next(r for r in self.host.workflow.list() if r['id']==rid)
                    if completed['state']=='rework_required':
                        if record['attempts'][tid]>=record['max_attempts']:
                            stopped=stopped or ('rework_required','BATCH_ATTEMPT_LIMIT')
                        else:
                            retry_heads[tid]=completed['head_sha']
                    elif completed['state']!='done':
                        stopped=('reconciling',completed.get('reason','RUN_RECONCILIATION_REQUIRED'))
            self.save(record,'integrating',active_tasks=[])
            # A final incremental batch must integrate earlier verified task
            # heads as well. Do not execute any task outside the selected batch.
            with self.core.connection() as conn:
                project_tasks=[dict(r) for r in conn.execute('SELECT id,state FROM tasks ORDER BY id')]
            integration_ids=record['task_ids']
            if all(t['state']=='done' for t in project_tasks):
                integration_ids=[t['id'] for t in project_tasks]
                for tid in integration_ids:
                    if tid not in policies:
                        _,selected,protected=self.host.workflow.configuration(tid)
                        checks.update(selected)
                        policies[tid]=digest({'checks':selected,'protected_paths':protected})
            result = await asyncio.to_thread(Integration(self.core,self.host.root,self.host.storage/'integration',checks).prepare,
                                             record['base_sha'],integration_ids)
            # Re-inventory after integration; a provider's completion text is
            # never the exit predicate for the PM loop.
            with self.core.connection() as conn:
                remaining=[r['id'] for r in conn.execute("SELECT id FROM tasks WHERE state!='done' ORDER BY id")]
            selected_remaining=[tid for tid in remaining if tid in record['task_ids']]
            for tid in integration_ids:
                _,selected,protected=self.host.workflow.configuration(tid)
                if digest({'checks':selected,'protected_paths':protected})!=policies[tid]:
                    raise DomainError('OWNER_CHECK_CONFIGURATION_CHANGED')
            if selected_remaining:
                raise DomainError('BATCH_TASKS_CHANGED_DURING_INTEGRATION')
            self.save(record,result['state'] if result['state']=='verified_candidate' else 'rework_required',
                      candidate_sha=result.get('head_sha'),artifact=result['artifact'],artifact_sha256=result['sha256'],
                      integration_task_ids=integration_ids,
                      integration_state=result['state'],published=False,
                      completion_inventory={'selected_remaining':selected_remaining,
                          'outside_scope_remaining':[tid for tid in remaining if tid not in record['task_ids']],
                          'checked_criteria':sorted(checks),
                          'exit_reason':'ALL_SELECTED_TASKS_AND_INTEGRATION_VERIFIED' if result['state']=='verified_candidate' else 'INTEGRATION_REWORK_REQUIRED',
                          'all_use_cases_proven':False})
            if record['state']=='verified_candidate':
                try:
                    from .expert_review import ExpertReview
                    experts=ExpertReview(self.host)
                    with self.core.connection() as conn:
                        required=experts.settings(conn)['enabled']
                    if required:
                        review=await experts.run(record)
                        self.save(record,record['state'],expert_review_id=review['id'])
                    report=await asyncio.to_thread(self.host.closure.create,record)
                    self.save(record,record['state'],closure_state='completed' if report else 'deferred')
                except Exception:
                    self.save(record,record['state'],closure_state='failed',closure_error='PROJECT_CLOSURE_FAILED')
        except asyncio.CancelledError:
            self.save(record,'reconciling',reason='BATCH_CANCELLED_RECONCILIATION_REQUIRED')
            raise
        except Exception as exc:
            self.save(record,'failed',reason=getattr(exc,'code','BATCH_FAILED'))
        finally:
            for _,work in running.values():
                if not work.done():
                    work.cancel()
            await asyncio.gather(*(work for _,work in running.values()),return_exceptions=True)
            self.current, self.reserved = None, {}
