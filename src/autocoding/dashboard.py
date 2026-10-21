"""Bounded browser projections. Internal scheduling keeps its full ledger view."""
import json

from .core import DomainError, encoded
from .textcodec import readable_text

TABLES = {
    'tasks': ('tasks', 'id,description,state,revision,attempt,agent,head_sha,reason', 'id'),
    'sessions': ('sessions', '*', 'rowid DESC'),
    'commands': ('commands', 'id,session_id,sender,epoch,generation,payload_hash,state', 'rowid DESC'),
    'approvals': ('approvals', '*', "(state='pending') DESC,rowid DESC"),
    'events': ('events', '*', 'seq DESC'),
}
RECORDS = {'runs': 'run:', 'batches': 'batch:', 'questions': 'question:', 'closures': 'pm_closure:',
           'expert_reviews': 'expert_review:', 'evaluations': 'learning_eval:'}


def omitted(item):
    result = {k: item[k] for k in ('id', 'seq', 'task_id', 'session_id', 'revision', 'attempt', 'agent', 'head_sha') if k in item}
    result.update(payload_omitted=True, original_state=item.get('state'), state='payload_omitted', questions=[])
    return result


def page(conn, kind, offset=0, limit=100):
    if not isinstance(offset, int) or not 0 <= offset <= 1000000 or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise DomainError('INVALID_HISTORY_PAGE')
    if kind in TABLES:
        table, columns, order = TABLES[kind]
        total = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        items = [dict(r) for r in conn.execute(f'SELECT {columns} FROM {table} ORDER BY {order} LIMIT ? OFFSET ?', (limit, offset))]
        if kind == 'events':
            for item in items:
                item['payload'] = json.loads(item['payload'])
    elif kind in RECORDS:
        pattern = RECORDS[kind] + '*'
        total = conn.execute('SELECT COUNT(*) FROM metadata WHERE key GLOB ?', (pattern,)).fetchone()[0]
        order = "(json_extract(value,'$.state') IN ('pending_pm','awaiting_owner','sending','delivery_unknown')) DESC," if kind == 'questions' else ''
        items = [json.loads(r['value']) for r in conn.execute(
            "SELECT value FROM metadata WHERE key GLOB ? ORDER BY " + order + "COALESCE(json_extract(value,'$.created_at'),0) DESC,key DESC LIMIT ? OFFSET ?",
            (pattern, limit, offset))]
    else:
        raise DomainError('INVALID_HISTORY_KIND')
    # Keep a stable pagination boundary, even when one historical payload is huge.
    size = 0
    for index, item in enumerate(items):
        length = len(encoded(item).encode('utf-8'))
        if length > 65536 or size + length > 512000:
            items[index] = omitted(item)
        else:
            size += length
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit,
            'next_offset': offset + len(items) if offset + len(items) < total else None}


class Dashboard:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def ensure_indexes(self):
        with self.core.connection() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS dashboard_runs_by_task ON metadata "
                         "(json_extract(value,'$.task_id'),COALESCE(json_extract(value,'$.created_at'),0) DESC) "
                         "WHERE key GLOB 'run:*'")

    def history(self, kind, offset=0, limit=100):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            return self.host.redactor.value(page(conn, kind, offset, limit))

    def snapshot(self, task_offset=0):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            pages = {kind: page(conn, kind, task_offset if kind == 'tasks' else 0)
                     for kind in ('tasks', 'sessions', 'commands', 'approvals', 'runs', 'batches', 'questions')}
            # Always associate the latest run with each visible task, including old blocked tasks.
            present = {r.get('task_id') for r in pages['runs']['items']}
            for task in pages['tasks']['items']:
                if task['id'] not in present:
                    row = conn.execute("SELECT value FROM metadata WHERE key GLOB 'run:*' AND json_extract(value,'$.task_id')=? ORDER BY COALESCE(json_extract(value,'$.created_at'),0) DESC LIMIT 1", (task['id'],)).fetchone()
                    if row:
                        pages['runs']['items'].append(json.loads(row['value']))
            result = {kind: list(reversed(p['items'])) if kind in {'runs', 'batches', 'sessions'} else p['items'] for kind, p in pages.items()}
            result['latest_seq'] = conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            result['totals'] = {kind: p['total'] for kind, p in pages.items()}
            result['task_page'] = {k: v for k, v in pages['tasks'].items() if k != 'items'}
            result['task_counts'] = {r['state']: r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM tasks GROUP BY state')}
            result['agent_health'] = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'agent_health:*' ORDER BY key")]
            # Link a task run to its actual PM instruction and public runtime
            # events so the owner need not infer work from an idle session card.
            for run in result['runs']:
                sid=run.get('session_id')
                if not sid:
                    continue
                context=conn.execute('SELECT value FROM metadata WHERE key=?',('session_context:'+sid,)).fetchone()
                instruction=''
                if context:
                    try:
                        instruction=str(json.loads(context['value']).get('initial',''))[:12000]
                    except (TypeError, ValueError):
                        pass
                transcript=conn.execute('SELECT value FROM metadata WHERE key=?',('session_output:'+sid,)).fetchone()
                try:
                    events=json.loads(transcript['value']) if transcript else []
                except (TypeError, ValueError):
                    events=[]
                live=[event for event in self.host.output if event.get('session_id')==sid]
                seen={(event.get('seq'),event.get('type'),event.get('text'),event.get('code')) for event in events}
                events.extend(event for event in live if (event.get('seq'),event.get('type'),event.get('text'),event.get('code')) not in seen)
                for event in events:
                    if isinstance(event,dict) and 'detail' in event:
                        event['detail']=readable_text(event['detail'])
                    if isinstance(event,dict) and 'text' in event:
                        event['text']=readable_text(event['text'])
                run['conversation']={'instruction':instruction,'events':events[-200:]}
        visible = {s['id'] for s in result['sessions']}
        result.update(metadata={sid: data for sid, data in self.host.metadata.items() if sid in visible},
                      paused=self.host.paused, live_ids=[sid for sid, runtime in self.host.runtimes.items() if not runtime._quarantined])
        used, omitted_count = 0, 0
        for kind in ('tasks', 'approvals', 'questions', 'sessions', 'runs', 'batches', 'commands'):
            for index, item in enumerate(result[kind]):
                length = len(encoded(item).encode('utf-8'))
                if length > 65536 or used + length > 2 * 1024 * 1024:
                    result[kind][index] = omitted(item)
                    omitted_count += 1
                else:
                    used += length
                omitted_count += bool(item.get('payload_omitted'))
        if len(encoded(result['metadata']).encode('utf-8')) > 262144:
            result['metadata'] = {}
            omitted_count += 1
        result['payloads_omitted'] = omitted_count
        return self.host.redactor.value(result)
