"""Explicit requirement mappings with current, audited verification evidence."""
import hashlib
import json
from pathlib import Path

from .acceptance import Acceptance
from .core import DomainError, digest, encoded


class Coverage:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def read(self, conn):
        row = conn.execute("SELECT value FROM metadata WHERE key='requirement_coverage'").fetchone()
        return json.loads(row['value']) if row else {'revision': 0, 'requirements': []}

    def identity(self, conn):
        tasks=[dict(r) for r in conn.execute('SELECT id,state,revision,attempt,head_sha,acceptance_hash FROM tasks ORDER BY id')]
        return digest([self.read(conn),Acceptance(self.host).read(conn),tasks])

    def save(self, fingerprint, requirements, *, key):
        def action(conn):
            current = self.read(conn)
            if digest(current) != fingerprint:
                raise DomainError('COVERAGE_VERSION_CONFLICT')
            if not isinstance(requirements, list) or len(requirements) > 500:
                raise DomainError('INVALID_REQUIREMENTS')
            ids = set()
            for requirement in requirements:
                if (not isinstance(requirement, dict) or set(requirement) != {'id', 'description', 'cases'}
                    or not isinstance(requirement['id'], str) or not 1 <= len(requirement['id']) <= 100
                    or requirement['id'] in ids or not isinstance(requirement['description'], str)
                    or not 1 <= len(requirement['description'].strip()) <= 2000
                    or not isinstance(requirement['cases'], list) or len(requirement['cases']) > 100):
                    raise DomainError('INVALID_REQUIREMENT')
                ids.add(requirement['id'])
                seen = set()
                for case in requirement['cases']:
                    if (not isinstance(case, dict) or set(case) != {'task_id', 'criterion'}
                        or any(not isinstance(v, str) for v in case.values())):
                        raise DomainError('INVALID_COVERAGE_CASE')
                    task = self.core._get(conn, 'tasks', case['task_id'])
                    identity = (case['task_id'], case['criterion'])
                    if case['criterion'] not in json.loads(task['acceptance']) or identity in seen:
                        raise DomainError('INVALID_COVERAGE_CASE')
                    seen.add(identity)
            record = {'revision': current['revision'] + 1, 'requirements': requirements}
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('requirement_coverage', encoded(record)))
            self.core._event(conn, 'RequirementCoverageChanged', 'human', revision=record['revision'], content_hash=digest(record))
            return {**record, 'fingerprint': digest(record)}
        return self.core._write('human', 'save_coverage', key,
                                dict(fingerprint=fingerprint, requirements=requirements), action)

    def snapshot(self):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            record = self.read(conn)
            evidence_identity = self.identity(conn)
            policy = Acceptance(self.host).read(conn)
            tasks = {r['id']: dict(r) for r in conn.execute('SELECT * FROM tasks')}
            runs = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'run:*'")] if record['requirements'] else []
            audits = [{**json.loads(r['payload']), 'task_id': r['task_id']} for r in conn.execute("SELECT task_id,payload FROM events WHERE type='VerificationFinished'")] if record['requirements'] else []
        config = policy.get('config') or {}
        checks = config.get('checks', {}) if isinstance(config, dict) else {}
        protected = config.get('protected_paths', []) if isinstance(config, dict) else None
        if not isinstance(checks, dict):
            checks = {}
        cache = {}
        def evidence(task_id):
            if task_id in cache:
                return cache[task_id]
            task = tasks.get(task_id)
            result = None
            if task and task['state'] == 'done':
                for run in reversed(runs):
                    if run.get('task_id') != task_id or run.get('head_sha') != task['head_sha']:
                        continue
                    if run.get('protected_paths') != protected:
                        continue
                    try:
                        path = Path(run['artifact'])
                        if path.stat().st_size > 8 * 1024 * 1024:
                            continue
                        raw = path.read_bytes()
                        sha = hashlib.sha256(raw).hexdigest()
                        artifact = json.loads(raw)
                        job = artifact['job']
                        if sha != run['artifact_sha256'] or any(job.get(k) != task[v] for k, v in
                            [('task_id', 'id'), ('head_sha', 'head_sha'), ('attempt', 'attempt'), ('acceptance_hash', 'acceptance_hash')]):
                            continue
                        if job['task_revision'] + 1 != task['revision']:
                            continue
                        if not any(a.get('task_id') == task_id and a.get('verification_id') == job['verification_id']
                            and a.get('passed') is True and all(c.get('artifact_ref') == 'sha256:' + sha
                            for c in a.get('checks', {}).values()) and a.get('checks') for a in audits):
                            continue
                        result = (artifact['checks'], sha)
                        break
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        continue
            cache[task_id] = result
            return result
        rows = []
        mapped = set()
        mapped_checks = set()
        for requirement in record['requirements']:
            cases = []
            for case in requirement['cases']:
                mapped.add(case['task_id'])
                mapped_checks.add((case['task_id'],case['criterion']))
                verified = evidence(case['task_id'])
                check = verified[0].get(case['criterion'], {}) if verified else {}
                passed = (check.get('passed') is True and case['criterion'] in checks
                          and check.get('argv_hash') == digest(checks[case['criterion']]))
                cases.append({**case, 'status': 'passed' if passed else 'unproven',
                              'artifact_sha256': verified[1] if verified else None})
            rows.append({**requirement, 'cases': cases,
                         'status': 'passed' if cases and all(c['status'] == 'passed' for c in cases) else 'unproven'})
        return {**record, 'fingerprint': digest(record), 'matrix': rows,
                'evidence_identity':evidence_identity,
                'unmapped_tasks': sorted(set(tasks) - mapped),
                'unmapped_checks':[{'task_id':t['id'],'criterion':c} for t in tasks.values() for c in json.loads(t['acceptance']) if (t['id'],c) not in mapped_checks],
                'registered_scope_passed': bool(rows) and all(r['status'] == 'passed' for r in rows),
                'all_product_requirements_proven': False}
