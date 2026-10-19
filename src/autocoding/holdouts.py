"""Prospective Owner-authored samples, isolated from all PM learning inputs.

Dedicated intake, not retrospective relabeling of operational conversations.
Independence requires human semantic review; capture alone never certifies it.
"""
import json
from .core import DomainError, digest, encoded, identifier
from .interactions import normalized


class Holdouts:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def baseline(self, conn):
        knowledge={r['key']:json.loads(r['value']) for r in conn.execute(
            "SELECT key,value FROM metadata WHERE key GLOB 'pm_review:*' OR key GLOB 'pm_knowledge_resolution:*' ORDER BY key")}
        from .pm_profile import DEFAULTS
        row=conn.execute("SELECT value FROM metadata WHERE key='pm_owner_profile'").fetchone()
        profile=json.loads(row['value']) if row else {'revision':0,'preferences':dict(DEFAULTS),'source':'product_defaults'}
        return {'knowledge':knowledge,'owner_profile':profile}

    def get(self, conn, sample_id):
        row=conn.execute('SELECT value FROM metadata WHERE key=?',('evaluation_holdout:'+sample_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND')
        return json.loads(row['value'])

    def snapshot(self):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            items=[json.loads(r['value']) for r in conn.execute(
                "SELECT value FROM metadata WHERE key GLOB 'evaluation_holdout:*' ORDER BY rowid DESC LIMIT 100")]
            current=digest(self.baseline(conn))
            total=conn.execute("SELECT COUNT(*) FROM metadata WHERE key GLOB 'evaluation_holdout:*'").fetchone()[0]
            exports=[{k:v for k,v in json.loads(r['value']).items() if k in {'id','created_at','content_hash'}}
                     for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'evaluation_holdout_export:*' ORDER BY rowid DESC LIMIT 20")]
        for item in items:
            item['revision']=item.get('revision',0)
            item['review_current']=item.get('review_baseline_hash')==current
        return {'items':items,'total':total,'exports':exports,'certified_holdouts':0,'model_calls':0,
                'notice':'专用隔离采集，不自动读取业务会话或进入知识库。先登记新场景，再填写答案；仍需人工核查来源、语义重叠和范围外反例，不能直接认定为独立样本。'}

    def create(self, source, prompt, scope, *, key):
        if (not isinstance(source,str) or not source.strip() or len(source)>1000
                or not isinstance(prompt,str) or not prompt.strip() or len(prompt)>4000
                or scope not in {'inside','outside'}):
            raise DomainError('INVALID_HOLDOUT_SCENE')
        payload={'source':source,'prompt':prompt,'scope':scope}
        if self.host.redactor.value(payload)!=payload:
            raise DomainError('HOLDOUT_CONTAINS_SECRET')
        def action(conn):
            fingerprint=digest(normalized(prompt))
            if conn.execute("SELECT 1 FROM metadata WHERE key GLOB 'evaluation_holdout:*' AND json_extract(value,'$.prompt_hash')=?",(fingerprint,)).fetchone():
                raise DomainError('DUPLICATE_HOLDOUT_SCENE')
            # The write transaction excludes concurrent knowledge/profile writes.
            baseline=self.baseline(conn)
            knowledge,profile=baseline['knowledge'],baseline['owner_profile']
            baseline_hash=digest(baseline)
            conn.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',
                         ('evaluation_holdout_baseline:'+baseline_hash,encoded(baseline)))
            record={**payload,'id':identifier('holdout'),'created_at':self.core.clock(),
                    'state':'awaiting_answer','revision':0,'prompt_hash':fingerprint,
                    'knowledge_hash':digest(knowledge),'owner_profile_hash':digest(profile),
                    'baseline_hash':baseline_hash,
                    'independence_certified':False}
            conn.execute('INSERT INTO metadata VALUES (?,?)',('evaluation_holdout:'+record['id'],encoded(record)))
            # No prompt/answer/source in events: reflection consumes audit events.
            self.core._event(conn,'EvaluationHoldoutRegistered','human',sample_id=record['id'])
            return record
        return self.core._write('human','holdout_create',key,payload,action)

    def answer(self, sample_id, expected, *, key):
        if not isinstance(expected,dict) or not expected or len(encoded(expected))>2000:
            raise DomainError('INVALID_HOLDOUT_ANSWER')
        if self.host.redactor.value(expected)!=expected:
            raise DomainError('HOLDOUT_CONTAINS_SECRET')
        def action(conn):
            row=conn.execute('SELECT value FROM metadata WHERE key=?',('evaluation_holdout:'+sample_id,)).fetchone()
            if not row:raise DomainError('NOT_FOUND')
            record=json.loads(row['value'])
            if record['state']!='awaiting_answer':raise DomainError('HOLDOUT_ALREADY_ANSWERED')
            record.update(expected=expected,answered_at=self.core.clock(),state='pending_independence_review',revision=record.get('revision',0)+1)
            conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),'evaluation_holdout:'+sample_id))
            self.core._event(conn,'EvaluationHoldoutAnswered','human',sample_id=sample_id)
            return record
        return self.core._write('human','holdout_answer',key,{'id':sample_id,'expected':expected},action)

    def review(self, sample_id, revision, decision, note, *, key):
        if (type(revision) is not int or decision not in {'accepted','rejected','contaminated'}
                or not isinstance(note,str) or not 10<=len(note.strip())<=2000):
            raise DomainError('INVALID_HOLDOUT_REVIEW')
        safe=self.host.redactor.text(note)
        def action(conn):
            record=self.get(conn,sample_id)
            if record.get('revision',0)!=revision:raise DomainError('STALE_HOLDOUT_REVIEW')
            if record['state']=='awaiting_answer':raise DomainError('HOLDOUT_ANSWER_REQUIRED')
            if record['state']=='contaminated':raise DomainError('HOLDOUT_CONTAMINATION_PERMANENT')
            baseline=self.baseline(conn)
            baseline_hash=digest(baseline)
            conn.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)',
                         ('evaluation_holdout_baseline:'+baseline_hash,encoded(baseline)))
            review={'decision':decision,'note':safe,'at':self.core.clock(),'actor':'owner','revision':revision+1,
                    'baseline_hash':baseline_hash}
            record.update(state=decision,revision=revision+1,review_baseline_hash=baseline_hash,
                          reviews=[*record.get('reviews',[]),review])
            conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),'evaluation_holdout:'+sample_id))
            self.core._event(conn,'EvaluationHoldoutReviewed','human',sample_id=sample_id,revision=revision+1,decision=decision)
            return record
        return self.core._write('human','holdout_review',key,
            {'id':sample_id,'revision':revision,'decision':decision,'note':safe},action)

    def validate_sources(self, conn, sources):
        current=digest(self.baseline(conn))
        for source in sources:
            record=self.get(conn,source['id'])
            if (record['state']!='accepted' or record.get('revision',0)!=source['revision']
                    or record.get('review_baseline_hash')!=current or digest(record)!=source['record_hash']):
                raise DomainError('HOLDOUT_REVIEW_INVALIDATED')

    def export(self, sample_ids, *, key):
        if (not isinstance(sample_ids,list) or not 2<=len(sample_ids)<=40
                or any(not isinstance(s,str) for s in sample_ids) or len(set(sample_ids))!=len(sample_ids)):
            raise DomainError('INVALID_HOLDOUT_SELECTION')
        def action(conn):
            records=[self.get(conn,sid) for sid in sample_ids]
            sources=[{'id':r['id'],'revision':r.get('revision',0),'record_hash':digest(r)} for r in records]
            self.validate_sources(conn,sources)
            if {r['scope'] for r in records}!={'inside','outside'}:
                raise DomainError('EVALUATION_COUNTEREXAMPLES_REQUIRED')
            cases=[{'id':r['id'],'scope':r['scope'],'prompt':r['prompt'],'expected':r['expected']} for r in records]
            manifest={'id':identifier('holdout_export'),'format_version':1,'created_at':self.core.clock(),
                      'cases':cases,'sources':sources,'certification':'owner_reviewed_not_independently_proven'}
            manifest['content_hash']=digest({'cases':cases,'sources':sources})
            conn.execute('INSERT INTO metadata VALUES (?,?)',('evaluation_holdout_export:'+manifest['id'],encoded(manifest)))
            self.core._event(conn,'EvaluationHoldoutsExported','human',export_id=manifest['id'],content_hash=manifest['content_hash'])
            return manifest
        return self.core._write('human','holdout_export',key,{'ids':sample_ids},action)

    def exported(self, export_id):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            row=conn.execute('SELECT value FROM metadata WHERE key=?',('evaluation_holdout_export:'+export_id,)).fetchone()
            if not row:raise DomainError('NOT_FOUND')
            manifest=json.loads(row['value'])
            try:self.validate_sources(conn,manifest['sources']);current=True
            except DomainError:current=False
        return {**manifest,'current':current}
