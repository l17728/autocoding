"""Platform-wide, redacted owner input history for PM recall and reuse."""
import json
import re
import uuid

from .core import DomainError, digest, encoded


SENSITIVE = re.compile(r'(?:api[_-]?key|authorization|credential|password|secret|token|content_base64)', re.I)


def _safe(value, redactor):
    if isinstance(value, dict):
        return {str(key): ('[已脱敏]' if SENSITIVE.search(str(key)) else _safe(item, redactor))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item, redactor) for item in value]
    if isinstance(value, str):
        return redactor.text(value)[:12000]
    return value


class GlobalHistory:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def record(self, *, key, project_id, path, content, redactor):
        if not key:
            raise DomainError('IDEMPOTENCY_OR_IDENTITY_REQUIRED')
        safe = _safe(content, redactor)
        fingerprint=digest({'path':path,'content':safe})
        payload = {'project_id': project_id, 'path': path, 'content': safe, 'fingerprint':fingerprint}

        def action(conn):
            row=conn.execute("SELECT key,value FROM metadata WHERE key GLOB 'global_owner_input:*' AND json_extract(value,'$.fingerprint')=?",
                             (fingerprint,)).fetchone()
            now=self.core.clock()
            if row:
                record=json.loads(row['value'])
                record['occurrences']=record.get('occurrences',1)+1
                record['updated_at']=now
                record['project_ids']=sorted(set(record.get('project_ids',[record['project_id']])+[project_id]))
                conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),row['key']))
                self.core._event(conn, 'GlobalOwnerInputDeduplicated', 'owner', input_id=record['id'], project_id=project_id,
                                 occurrences=record['occurrences'])
                return record
            record = {'id': 'input_' + uuid.uuid4().hex, **payload, 'created_at': now, 'updated_at':now,
                      'occurrences':1,'project_ids':[project_id]}
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('global_owner_input:' + record['id'], encoded(record)))
            self.core._event(conn, 'GlobalOwnerInputRecorded', 'owner', input_id=record['id'], project_id=project_id, path=path)
            return record
        return self.core._write('owner', 'global_owner_input', key, payload, action)

    def search(self, query='', offset=0, limit=30):
        if not isinstance(query, str) or len(query)>500 or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise DomainError('INVALID_INPUT_HISTORY_QUERY')
        with self.core.connection() as conn:
            records=[json.loads(row['value']) for row in conn.execute(
                "SELECT value FROM metadata WHERE key GLOB 'global_owner_input:*' ORDER BY COALESCE(json_extract(value,'$.updated_at'),json_extract(value,'$.created_at')) DESC")]
        needle=query.casefold().strip()
        if needle:
            records=[record for record in records if needle in encoded(record['content']).casefold() or needle in record['path'].casefold()]
        return {'total':len(records),'offset':offset,'limit':limit,'items':records[offset:offset+limit]}
