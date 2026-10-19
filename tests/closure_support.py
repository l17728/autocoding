"""Audited integration artifacts for closure and coordinator contract tests."""
import hashlib
from autocoding.core import encoded


def integration_evidence(host, task_ids, head='c'*40):
    with host.core.connection() as conn:
        tasks=[dict(host.core._get(conn,'tasks',tid)) for tid in task_ids]
    evidence={'state':'verified_candidate','head_sha':head,
              'tasks':[{'id':t['id'],'revision':t['revision'],'head_sha':t['head_sha']} for t in tasks],
              'checks':{'unit':{'passed':True}}}
    raw=encoded(evidence).encode('utf-8')
    sha=hashlib.sha256(raw).hexdigest()
    path=host.storage/(sha+'.json')
    path.write_bytes(raw)
    with host.core.connection() as conn:
        host.core._event(conn,'IntegrationVerified','verifier:local',state='verified_candidate',
                         head_sha=head,task_ids=task_ids,artifact_ref='sha256:'+sha)
    return {**evidence,'artifact':str(path),'sha256':sha}


def persist_batch(host, batch):
    result=integration_evidence(host,batch['task_ids'],batch['candidate_sha'])
    batch.update(artifact=result['artifact'],artifact_sha256=result['sha256'])
    with host.core.connection() as conn:
        conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('batch:'+batch['id'],encoded(batch)))

