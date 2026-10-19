"""Idempotent, evidence-backed project closure reports, available offline."""
import hashlib
import json
from pathlib import Path
from .core import DomainError, digest, encoded


class Closure:
    def __init__(self, host):
        self.host,self.core=host,host.core

    def list(self):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            snapshot=self.snapshot(conn)
            rows=conn.execute("SELECT value FROM metadata WHERE key GLOB 'pm_closure:*' ORDER BY json_extract(value,'$.created_at') DESC LIMIT 50").fetchall()
        reports=[json.loads(r['value']) for r in rows]
        for report in reports:
            report['task_snapshot_current']=report.get('task_snapshot_hash')==digest(snapshot)
        return reports

    @staticmethod
    def snapshot(conn):
        return [dict(r) for r in conn.execute(
            'SELECT id,revision,attempt,state,head_sha,acceptance_hash FROM tasks ORDER BY id')]

    def integration_evidence(self, conn, batch, snapshot):
        """Bind completion to an audited artifact and the entire current project."""
        row=conn.execute('SELECT value FROM metadata WHERE key=?',('batch:'+batch['id'],)).fetchone()
        if not row:
            raise DomainError('PROJECT_CLOSURE_INTEGRATION_EVIDENCE_REQUIRED')
        stored=json.loads(row['value'])
        fields=('state','candidate_sha','artifact','artifact_sha256','task_ids','integration_task_ids')
        if any(stored.get(k)!=batch.get(k) for k in fields):
            raise DomainError('PROJECT_CLOSURE_STALE_BATCH')
        try:
            path=Path(batch['artifact'])
            if path.stat().st_size>8*1024*1024:
                raise ValueError()
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=batch['artifact_sha256']:
                raise ValueError()
            evidence=json.loads(raw)
            if (evidence['state']!='verified_candidate' or evidence['head_sha']!=batch['candidate_sha']
                    or not evidence['checks'] or not all(c.get('passed') is True for c in evidence['checks'].values())):
                raise ValueError()
            versions={t['id']:t for t in evidence['tasks']}
            if len(versions)!=len(evidence['tasks']) or set(versions)!=set(batch.get('integration_task_ids',batch['task_ids'])):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise DomainError('PROJECT_CLOSURE_INTEGRATION_EVIDENCE_REQUIRED') from None
        audited=False
        for row in conn.execute("SELECT payload FROM events WHERE type='IntegrationVerified'"):
            event=json.loads(row['payload'])
            if (event.get('state')=='verified_candidate' and event.get('head_sha')==batch['candidate_sha']
                    and event.get('artifact_ref')=='sha256:'+batch['artifact_sha256']
                    and set(event.get('task_ids',[]))==set(versions)):
                audited=True
                break
        if not audited:
            raise DomainError('PROJECT_CLOSURE_INTEGRATION_EVIDENCE_REQUIRED')
        # All tasks must be included, even when a prior batch independently passed.
        # Otherwise a final small batch could claim a project whose other heads
        # have never been merged into this candidate.
        if set(versions)!={t['id'] for t in snapshot}:
            return False
        if any(any(versions[t['id']].get(k)!=t[k] for k in ('revision','head_sha')) for t in snapshot):
            return False
        return True

    def create(self, batch):
        if batch['state']!='verified_candidate':
            raise DomainError('PROJECT_CLOSURE_REQUIRES_VERIFIED_INTEGRATION')
        from .coverage import Coverage
        coverage=Coverage(self.host)
        matrix=coverage.snapshot()
        if matrix['requirements'] and (not matrix['registered_scope_passed'] or matrix['unmapped_checks']):
            return None
        key='pm_closure:'+batch['id']
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if matrix['evidence_identity']!=coverage.identity(conn):
                return None
            snapshot=self.snapshot(conn)
            tasks=[dict(r) for r in conn.execute('SELECT id,description,state,head_sha,acceptance FROM tasks ORDER BY id')]
            remaining=[t['id'] for t in tasks if t['state']!='done']
            if remaining:
                return None  # A partial batch is not project completion.
            if not snapshot or not self.integration_evidence(conn,batch,snapshot):
                return None
            from .expert_review import ExpertReview
            if not ExpertReview(self.host).gate(conn,batch):
                return None
            runs=[json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'run:*' ORDER BY json_extract(value,'$.created_at')")]
            questions=[json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'pm_interaction:*'")]
            unresolved=[q['id'] for q in questions if q['state'] in {'sending','sent_unconfirmed','delivery_unknown'}
                        or (q['state'] in {'pending_pm','awaiting_owner'} and q.get('expires_at',0)>self.core.clock())]
            if unresolved:
                return None
            if conn.execute("SELECT 1 FROM metadata WHERE key GLOB 'late_interaction:*' LIMIT 1").fetchone():
                return None
            if conn.execute("SELECT 1 FROM approval_delivery WHERE state IN ('pending','sending','delivery_unknown') LIMIT 1").fetchone():
                return None
            if conn.execute("SELECT 1 FROM approvals WHERE state='pending' AND expires_at>? LIMIT 1",(self.core.clock(),)).fetchone():
                return None
            previous=conn.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
            if previous:
                report=json.loads(previous['value'])
                if report.get('task_snapshot_hash')!=digest(snapshot):
                    return None
                return {**report,'task_snapshot_current':True}
            latest_verified={}
            for run in runs:
                if run['state']=='done':
                    latest_verified[run['task_id']]=max(latest_verified.get(run['task_id'],float('-inf')),run.get('created_at',0))
            failures=[{'run_id':r['id'],'task_id':r['task_id'],'state':r['state'],
                       'failed_checks':r.get('failed_checks',[]),'reason':r.get('reason'),
                       'artifact':r.get('artifact'),'artifact_sha256':r.get('artifact_sha256'),
                       'later_verified':latest_verified.get(r['task_id'],float('-inf'))>r.get('created_at',0)}
                      for r in runs if r['state'] in {'rework_required','failed','reconciling'}]
            pattern_counts={}
            for failure in failures:
                for check in failure['failed_checks'] or [failure['reason'] or failure['state']]:
                    pattern_counts[check]=pattern_counts.get(check,0)+1
            report={'id':'closure_'+batch['id'],'batch_id':batch['id'],'created_at':self.core.clock(),
                    'state':'completed','scope':'project','method':'deterministic_evidence_review',
                    'task_snapshot_hash':digest(snapshot),'task_snapshot_current':True,
                    'summary':f"项目 {len(tasks)} 项登记任务均已验证，最近批次集成候选已验证；尚未发布。",
                    'tasks':tasks,'bugfix_review':failures,'patterns':pattern_counts,
                    'interaction_summary':{'recorded_questions':len(questions),
                        'help_requests':sum(q.get('requested',False) for q in questions),
                        'owner_answers':sum(q.get('answered_by')=='owner' for q in questions)},
                    'candidate_sha':batch['candidate_sha'],'integration_task_ids':batch.get('integration_task_ids',batch['task_ids']),
                    'integration_artifact':batch['artifact'],
                    'integration_sha256':batch['artifact_sha256'],'published':False,
                    'limitations':['通过登记的检查不等于证明所有可能用例完备。',
                        '集成证据仅覆盖所列批次任务，不扩大为所有历史提交已合并。',
                        '返工按检查失败或运行异常统计，不能全部等同于产品缺陷；根因须另外举证。',
                        '多视角自审不是独立专家签字；后续定期自检继续提炼和维护知识。']}
            self.core._event(conn,'ProjectClosureCreated','pm',batch_id=batch['id'],report_id=report['id'],
                             task_count=len(tasks),failure_count=len(failures))
            event=conn.execute('SELECT seq,event_id FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            lessons=[{'category':'lesson','topic':'项目收尾验收边界',
                      'content':"收尾必须区分 Agent 完成声明、独立验收与发布状态；尚未发布不能表述为已上线。项目级收尾要求所有当前任务的版本均已纳入同一个通过登记检查的集成候选。",
                      'confidence':'observed','evidence_ids':[event['event_id']]}]
            if failures:
                lessons.append({'category':'lesson','topic':'返工与问题模式',
                    'content':"复盘同类失败时关联失败检查、后续提交和验证证据，不能只按重试次数或后续通过记录推断根因已经消除。具体次数和问题模式保留在来源收尾报告中。",
                    'confidence':'observed','evidence_ids':[event['event_id']]})
            report['lessons']=lessons
            report['knowledge_review_id']='review_closure_'+batch['id']
            review={'id':report['knowledge_review_id'],'created_at':report['created_at'],'state':'completed',
                    'archived':False,'automatic':True,'model_generated':False,'source_kind':'project_closure',
                    'summary':'项目收尾经验总结','from_seq':event['seq'],'to_seq':event['seq'],
                    'items':lessons,'evidence':{'closure_id':report['id'],'event_id':event['event_id']}}
            conn.execute('INSERT INTO metadata VALUES (?,?)',(key,encoded(self.host.redactor.value(report))))
            conn.execute('INSERT INTO metadata VALUES (?,?)',('pm_review:'+review['id'],encoded(review)))
            conn.commit()
        return self.host.redactor.value(report)
