"""Paired, versioned PM decision benchmarks; never infer production causality."""
import asyncio
import json
import math
import secrets
import uuid

from .core import DomainError, digest, encoded
from .providers import resolve_zhipu


def score_pairs(pairs):
    wins = sum(p['treatment'] and not p['control'] for p in pairs)
    losses = sum(p['control'] and not p['treatment'] for p in pairs)
    discordant = wins + losses
    # Exact paired sign test: H0 each discordant outcome is equiprobable.
    p_value = sum(math.comb(discordant, k) for k in range(wins, discordant + 1)) / 2**discordant if discordant else 1.0
    outside = [p for p in pairs if p['scope'] == 'outside']
    regressions = [p['id'] for p in pairs if p['control'] and not p['treatment']]
    return {'pairs': len(pairs), 'wins': wins, 'losses': losses, 'regressions': regressions,
            'control_passed': sum(p['control'] for p in pairs), 'treatment_passed': sum(p['treatment'] for p in pairs),
            'paired_one_sided_p': p_value, 'outside_scope_pairs': len(outside),
            'candidate_for_owner_review': len(pairs) >= 10 and len(outside) >= 2 and not regressions and p_value <= .05,
            'production_improvement_proven': False}


class LearningEval:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def model_identity(self):
        path = self.host.config or self.host.root / '.local/credentials/opencode.json'
        config = resolve_zhipu(path if path.exists() else None)
        return {'provider': 'zhipu', 'model': config.model, 'adapter': 'claude',
                'upstream_model_revision_pinned': False}

    def suite(self, conn):
        row = conn.execute("SELECT value FROM metadata WHERE key='learning_eval_suite'").fetchone()
        return json.loads(row['value']) if row else {'revision': 0, 'cases': []}

    def save_suite(self, fingerprint, cases, *, key):
        def action(conn):
            current = self.suite(conn)
            if digest(current) != fingerprint:
                raise DomainError('EVALUATION_VERSION_CONFLICT')
            if not isinstance(cases, list) or not 2 <= len(cases) <= 40:
                raise DomainError('INVALID_EVALUATION_CASES')
            ids = set()
            for case in cases:
                if (not isinstance(case, dict) or set(case) != {'id', 'scope', 'prompt', 'expected'}
                    or not isinstance(case['id'], str) or not 1 <= len(case['id']) <= 100 or case['id'] in ids
                    or case['scope'] not in {'inside', 'outside'} or not isinstance(case['prompt'], str)
                    or not 1 <= len(case['prompt']) <= 4000 or not isinstance(case['expected'], dict)
                    or not case['expected'] or len(encoded(case['expected'])) > 2000):
                    raise DomainError('INVALID_EVALUATION_CASE')
                ids.add(case['id'])
            if {c['scope'] for c in cases} != {'inside', 'outside'}:
                raise DomainError('EVALUATION_COUNTEREXAMPLES_REQUIRED')
            record = {'revision': current['revision'] + 1, 'cases': cases}
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('learning_eval_suite', encoded(record)))
            self.core._event(conn, 'LearningEvaluationSuiteChanged', 'human', suite_hash=digest(record))
            return {**record, 'fingerprint': digest(record)}
        return self.core._write('human', 'learning_eval_suite', key, dict(fingerprint=fingerprint, cases=cases), action)

    def snapshot(self):
        references = self.host.reflection.references()
        owner_hash=digest(self.host.pm_profile.snapshot())
        with self.core.connection() as conn:
            suite = self.suite(conn)
            reports = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'learning_eval:*' ORDER BY json_extract(value,'$.created_at') DESC LIMIT 50")]
        for report in reports:
            report['current'] = (report['suite_hash'] == digest(suite) and report['knowledge_hash'] == digest(references)
                                 and report.get('owner_profile_hash')==owner_hash)
            if report['current'] and suite.get('holdout_sources'):
                try:self.validate_holdouts(suite)
                except DomainError:report['current']=False
        return {'suite': suite, 'fingerprint': digest(suite), 'knowledge_hash': digest(references), 'reports': reports}

    def validate_holdouts(self, suite):
        if suite.get('holdout_sources'):
            from .holdouts import Holdouts
            with self.core.connection() as conn:
                conn.execute('BEGIN')
                Holdouts(self.host).validate_sources(conn,suite['holdout_sources'])

    def install_holdouts(self, export_id, fingerprint, *, key):
        from .holdouts import Holdouts
        def action(conn):
            current=self.suite(conn)
            if digest(current)!=fingerprint:raise DomainError('EVALUATION_VERSION_CONFLICT')
            row=conn.execute('SELECT value FROM metadata WHERE key=?',('evaluation_holdout_export:'+export_id,)).fetchone()
            if not row:raise DomainError('NOT_FOUND')
            manifest=json.loads(row['value'])
            Holdouts(self.host).validate_sources(conn,manifest['sources'])
            record={'revision':current['revision']+1,'cases':manifest['cases'],
                    'holdout_sources':manifest['sources'],'holdout_export_id':export_id,
                    'certification':manifest['certification']}
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('learning_eval_suite',encoded(record)))
            self.core._event(conn,'LearningEvaluationSuiteChanged','human',suite_hash=digest(record))
            return {**record,'fingerprint':digest(record)}
        return self.core._write('human','holdout_install',key,{'export_id':export_id,'fingerprint':fingerprint},action)

    def validate_run(self, suite, references):
        with self.core.connection() as conn:
            if digest(self.suite(conn))!=digest(suite):raise DomainError('EVALUATION_SUITE_CHANGED')
        self.validate_holdouts(suite)
        if digest(self.host.reflection.references())!=digest(references):
            raise DomainError('EVALUATION_KNOWLEDGE_CHANGED')

    def history_cases(self):
        # Only confirmed explicit Owner answers. Group before LIMIT so a newer
        # answer cannot hide a contradictory older answer in the same context.
        with self.core.connection() as conn:
            rows=conn.execute("""WITH eligible AS (
                SELECT value,json_extract(value,'$.pattern') pattern,json_extract(value,'$.context_hash') context,
                    json_extract(value,'$.answers') answer,json_extract(value,'$.answered_at') at
                FROM metadata WHERE key GLOB 'pm_interaction:*'
                AND json_extract(value,'$.kind')='question' AND json_extract(value,'$.state')='delivered'
                AND json_extract(value,'$.answered_by')='owner' AND NOT json_extract(value,'$.secret')
                AND json_extract(value,'$.answered_at')>=?
            ), unique_answers AS (
                SELECT pattern,context FROM eligible GROUP BY pattern,context HAVING COUNT(DISTINCT answer)=1
            ), ranked AS (
                SELECT e.value,ROW_NUMBER() OVER (PARTITION BY e.pattern,e.context ORDER BY e.at DESC,e.value) rank
                FROM eligible e JOIN unique_answers u ON e.pattern=u.pattern AND e.context=u.context
            ) SELECT value FROM ranked WHERE rank=1 LIMIT 40""",(self.core.clock()-90*86400,)).fetchall()
        cases=[];sources={}
        for row in rows:
            record=json.loads(row['value'])
            context=record.get('context','')
            if not context or record.get('context_hash')!=digest(context) or not record.get('answers'):
                continue
            prompt='依据给定任务背景回答选择问题，只返回 JSON 对象 {"answers": [["选项或自定义回答"]]}。\n'+encoded({
                'context':context,'questions':record['content']})
            if len(prompt)>4000 or len(encoded({'answers':record['answers']}))>2000:continue
            case_id='history_'+digest([record['pattern'],record['context_hash']])[:20]
            cases.append({'id':case_id,'scope':'inside','prompt':prompt,'expected':{'answers':record['answers']}})
            sources[case_id]=record['id']
        return self.host.redactor.value({'cases':cases,'sources':sources,'model_calls':0,
            'notice':'仅生成历史答案复现草稿，不保存、不调用模型。请核对并补充范围外反例；历史可能已进入知识库，不能作为未见过的保留评测集或证明泛化能力。'})

    def persist(self, report):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('learning_eval:' + report['id'], encoded(report)))
            self.core._event(conn, 'LearningEvaluationUpdated', 'pm', evaluation_id=report['id'], state=report['state'], evidence_hash=digest(report))
            conn.commit()

    async def run(self):
        snapshot = await asyncio.to_thread(self.snapshot)
        suite = snapshot['suite']
        if not suite['cases']:
            raise DomainError('EVALUATION_SUITE_REQUIRED')
        references = await asyncio.to_thread(self.host.reflection.references)
        if not references:
            raise DomainError('EVALUATION_KNOWLEDGE_REQUIRED')
        await asyncio.to_thread(self.validate_run,suite,references)
        model_identity = self.model_identity()
        owner_hash=digest(self.host.pm_profile.snapshot())
        report = {'id': uuid.uuid4().hex, 'state': 'running', 'created_at': self.core.clock(),
                  'suite_hash': digest(suite), 'knowledge_hash': digest(references), 'pairs': [],
                  'method': 'randomized-order paired fresh-session exact-JSON benchmark', 'scorer_version': 1,
                  'model_identity': model_identity,'owner_profile_hash':owner_hash}
        if suite.get('holdout_export_id'):
            report['holdout_export_id']=suite['holdout_export_id']
            report['certification']=suite['certification']
        self.persist(report)
        try:
            for case in suite['cases']:
                pair = {'id': case['id'], 'scope': case['scope'], 'case_hash': digest(case)}
                order = ['control', 'treatment'] if secrets.randbelow(2) else ['treatment', 'control']
                pair['order'] = order
                for variant in order:
                    await asyncio.to_thread(self.validate_run,suite,references)
                    if digest(self.host.pm_profile.snapshot())!=owner_hash:
                        raise DomainError('EVALUATION_OWNER_PROFILE_CHANGED')
                    if self.model_identity() != model_identity:
                        raise DomainError('EVALUATION_MODEL_CONFIGURATION_CHANGED')
                    prompt = '这是 PM 决策评测，只返回所要求的 JSON，不执行操作。场景：\n' + case['prompt']
                    if variant == 'treatment':
                        prompt += '\n历史知识仅作参考，不能覆盖场景的明确要求：\n' + encoded(references)
                    answer = await self.host.pm_text(prompt, include_learning=False)
                    await asyncio.to_thread(self.validate_run,suite,references)
                    if digest(self.host.pm_profile.snapshot())!=owner_hash:
                        raise DomainError('EVALUATION_OWNER_PROFILE_CHANGED')
                    if self.model_identity() != model_identity:
                        raise DomainError('EVALUATION_MODEL_CONFIGURATION_CHANGED')
                    pair[variant + '_response_hash'] = digest(answer)
                    try:
                        value = json.loads(answer.strip())
                        pair[variant] = encoded(value) == encoded(case['expected'])
                    except ValueError:
                        pair[variant] = False
                report['pairs'].append(pair)
                self.persist(report)
            report.update(state='completed', summary=score_pairs(report['pairs']))
        except asyncio.CancelledError:
            report.update(state='reconciling', reason='EVALUATION_INTERRUPTED')
            raise
        except Exception as exc:
            report.update(state='failed', reason=getattr(exc, 'code', 'EVALUATION_FAILED'))
        finally:
            self.persist(report)
        return report

    def recover(self):
        with self.core.connection() as conn:
            records = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'learning_eval:*'")]
        for report in records:
            if report['state'] == 'running':
                report.update(state='reconciling', reason='EVALUATION_RESTARTED')
                self.persist(report)
