"""Project-local help history: observable rationale, never hidden reasoning."""
import json
import re
import unicodedata
from .core import DomainError, digest, encoded


def normalized(value):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', value).casefold())


class Interactions:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def record(self, conn, source_id, *, kind, content, state, reason='', answers=None,
               actor=None, context='', requested=False, secret=False):
        key='pm_interaction:'+source_id
        row=conn.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        previous=json.loads(row['value']) if row else {}
        safe_content='[秘密输入：不保存内容]' if secret else self.host.redactor.value(content)
        pattern=digest([kind,normalized(encoded(safe_content))])
        now=self.core.clock()
        record={**previous,'id':source_id,'kind':kind,'content':safe_content,
                'pattern':pattern,'state':state,'updated_at':now,
                'created_at':previous.get('created_at',now),'secret':secret,
                'context':previous.get('context',self.host.redactor.text(context)[:4000]),
                'context_hash':previous.get('context_hash',digest(context)),
                'requested':previous.get('requested',False) or requested}
        if reason:
            record['reason']=self.host.redactor.text(reason)[:2000]
        if requested and not previous.get('requested'):
            record['requested_at']=now
            record['request_reason']=record.get('reason','')
        if answers is not None:
            record['answers']=None if secret else self.host.redactor.value(answers)
            record['answered_by']=actor
            record['answered_at']=previous.get('answered_at',now)
        conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',(key,encoded(record)))
        return record

    def question(self, conn, question):
        # Protocol IDs/options do not define the human question's repeat identity.
        record=self.record(conn,question['id'],kind='question',
            content=[{'question':q['question'],'options':q['options']} for q in question['questions']],
            state=question['state'],reason=question.get('reason',''),answers=question.get('answers'),
            actor=question.get('answered_by'),context=self.host.task_context(question['session_id']),
            requested=question['state']=='awaiting_owner',secret=any(q['secret'] for q in question['questions']))
        record['expires_at']=question['expires_at']
        conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),'pm_interaction:'+question['id']))
        return record

    def rows(self):
        with self.core.connection() as conn:
            rows=[json.loads(r['value']) for r in conn.execute(
                "SELECT value FROM metadata WHERE key LIKE 'pm_interaction:%' ORDER BY json_extract(value,'$.updated_at') DESC, key")]
            escalations={}
            times={}
            reasons={}
            for event in conn.execute("SELECT timestamp,type,payload FROM events WHERE type IN ('PMApprovalEscalated','PMApprovalReason','ApprovalRequested','ApprovalDecided') ORDER BY seq"):
                payload=json.loads(event['payload'])
                aid=payload['approval_id']
                times.setdefault(aid,{'created':event['timestamp']})['updated']=event['timestamp']
                if event['type']=='PMApprovalEscalated':
                    escalations.setdefault(aid,{**payload,'at':event['timestamp']})
                if event['type']=='PMApprovalReason':
                    reasons[aid]=payload['reason']
            for approval in conn.execute('SELECT a.*,d.state AS delivery_state FROM approvals a LEFT JOIN approval_delivery d ON a.id=d.approval_id'):
                escalation=escalations.get(approval['id'])
                requested=escalation is not None
                state=approval['state']
                if state=='pending':
                    state='expired' if approval['expires_at']<=self.core.clock() else 'awaiting_owner' if requested else 'pending_pm'
                rows.append({'id':approval['id'],'kind':'permission','content':{
                    'operation_hash':approval['operation_hash'],'tool':(escalation or {}).get('tool','工具操作')},
                    'pattern':digest(['permission',approval['operation_hash']]),'state':state,
                    'created_at':times.get(approval['id'],{}).get('created',0),
                    'updated_at':times.get(approval['id'],{}).get('updated',0),
                    'requested':requested,'request_reason':(escalation or {}).get('reason',''),
                    'reason':reasons.get(approval['id'],(escalation or {}).get('reason','')),
                    'answers':approval['state'] if approval['decided_by'] else None,
                    'answered_by':approval['decided_by'],'delivery_state':approval['delivery_state'],
                    'secret':False,'context':''})
        for row in rows:
            if row['state'] in {'pending_pm','awaiting_owner'} and row.get('expires_at',float('inf'))<=self.core.clock():
                row['state']='expired'
        return sorted(rows,key=lambda r:(r['updated_at'],r['id']),reverse=True)

    def snapshot(self, offset=0, limit=25):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise DomainError('INVALID_INTERACTION_PAGE')
        # Aggregate in SQLite; decode only the requested page, not every question
        # and permission payload on each dashboard refresh.
        source = """WITH audit AS (
            SELECT seq,timestamp,type,payload,json_extract(payload,'$.approval_id') aid FROM events
            WHERE type IN ('PMApprovalEscalated','PMApprovalReason','ApprovalRequested','ApprovalDecided')
        ), grouped AS (
            SELECT aid,MIN(seq) first_event,MAX(seq) last_event,
              MIN(CASE WHEN type='PMApprovalEscalated' THEN seq END) escalation,
              MAX(CASE WHEN type='PMApprovalReason' THEN seq END) reason FROM audit GROUP BY aid
        ), permissions AS (
            SELECT a.*,d.state delivery_state,f.timestamp created,l.timestamp updated,e.payload escalation,r.payload reason_payload
            FROM approvals a LEFT JOIN approval_delivery d ON a.id=d.approval_id
            LEFT JOIN grouped g ON g.aid=a.id LEFT JOIN events e ON e.seq=g.escalation
            LEFT JOIN events r ON r.seq=g.reason
            LEFT JOIN events f ON f.seq=g.first_event LEFT JOIN events l ON l.seq=g.last_event
        ), raw AS (
            SELECT value record FROM metadata WHERE key GLOB 'pm_interaction:*'
            UNION ALL
            SELECT json_object('id',id,'kind','permission','content',json_object('operation_hash',operation_hash,
              'tool',COALESCE(json_extract(escalation,'$.tool'),'工具操作')),
              'pattern','permission:'||operation_hash,
              'state',CASE WHEN state='pending' THEN CASE WHEN expires_at<=:now THEN 'expired'
                WHEN escalation IS NOT NULL THEN 'awaiting_owner' ELSE 'pending_pm' END ELSE state END,
              'created_at',COALESCE(created,0),'updated_at',COALESCE(updated,0),'requested',escalation IS NOT NULL,
              'request_reason',COALESCE(json_extract(escalation,'$.reason'),''),
              'reason',COALESCE(json_extract(reason_payload,'$.reason'),json_extract(escalation,'$.reason'),''),
              'answers',CASE WHEN decided_by IS NOT NULL AND decided_by!='' THEN state END,'answered_by',decided_by,
              'delivery_state',delivery_state,'secret',json('false'),'context','') FROM permissions
        ), records AS (
            SELECT record,json_extract(record,'$.id') id,json_extract(record,'$.updated_at') updated,
              json_extract(record,'$.pattern') pattern,COALESCE(json_extract(record,'$.requested'),0) requested,
              COALESCE(json_extract(record,'$.secret'),0) secret,json_extract(record,'$.answered_by') answered_by,
              CASE WHEN json_extract(record,'$.state') IN ('pending_pm','awaiting_owner')
                AND COALESCE(json_extract(record,'$.expires_at'),1e99)<=:now THEN 'expired'
                ELSE json_extract(record,'$.state') END state FROM raw
        ), patterns AS (SELECT pattern,COUNT(*) n FROM records WHERE requested AND NOT secret GROUP BY pattern)
        """
        parameters={'now':self.core.clock(),'offset':offset,'limit':limit}
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            counts=dict(conn.execute(source+"""SELECT COUNT(*) total,COALESCE(SUM(requested),0) requests,
                COALESCE(SUM(answered_by='owner'),0) owner_answers,
                COALESCE(SUM(requested AND state='awaiting_owner'),0) pending,
                (SELECT COALESCE(SUM(n-1),0) FROM patterns) repeat_requests FROM records""",parameters).fetchone())
            rows=conn.execute(source+"""SELECT record,state,COALESCE(p.n,0) same_question_requests
                FROM records LEFT JOIN patterns p USING(pattern) ORDER BY updated DESC,id DESC LIMIT :limit OFFSET :offset""",parameters).fetchall()
        items=[]
        for row in rows:
            item=json.loads(row['record'])
            item.update(state=row['state'],requested=bool(item['requested']),same_question_requests=row['same_question_requests'])
            if item['kind']=='permission':
                item['pattern']=digest(['permission',item['content']['operation_hash']])
            items.append(item)
        return {**counts,
                'items':items,
                'offset':offset,'limit':limit,
                'notice':'按独立请求计数，状态更新不重复计数；重复为规范化文本匹配，不代表语义完全相同或求助不必要。'}

    def relevant(self, questions, context):
        text=normalized(' '.join(q['question'] for q in questions))
        grams={text[i:i+2] for i in range(max(0,len(text)-1))}
        matches=[]
        now=self.core.clock()
        # Permission events and unrelated history are not candidates for answer
        # reuse. Stream only eligible question records instead of rebuilding the
        # entire dashboard (including every approval event) for each question.
        for r in self._answered_questions(now):
            if (r['kind']!='question' or r.get('secret') or r.get('answered_by')!='owner'
                    or r['state']!='delivered' or not r.get('answers')
                    or now-r['updated_at']>90*86400 or r['updated_at']>now):
                continue
            candidate=normalized(' '.join(q['question'] for q in r['content']))
            other={candidate[i:i+2] for i in range(max(0,len(candidate)-1))}
            score=1.0 if candidate==text else len(grams & other)/max(1,len(grams | other))
            if score>=0.35:
                matches.append((score,r))
                # Only five records can be reused; keep memory bounded even
                # when years of repeated questions match the same pattern.
                matches.sort(key=lambda pair:(pair[0],pair[1]['updated_at']),reverse=True)
                del matches[5:]
        matches.sort(key=lambda pair:(pair[0],pair[1]['updated_at']),reverse=True)
        result=[]
        for score,r in matches[:5]:
            item={k:r.get(k) for k in ('id','content','answers','context','request_reason','reason')}
            item.update(similarity=round(score,3),same_context=r.get('context_hash')==digest(context))
            if len(encoded(result+[item]))>16000:
                break
            result.append(item)
        return result

    def _answered_questions(self, now):
        with self.core.connection() as conn:
            for row in conn.execute("""SELECT value FROM metadata
                    WHERE key GLOB 'pm_interaction:*'
                      AND json_extract(value,'$.kind')='question'
                      AND json_extract(value,'$.answered_by')='owner'
                      AND json_extract(value,'$.state')='delivered'
                      AND json_extract(value,'$.updated_at') BETWEEN ? AND ?""",
                    (now-90*86400,now)):
                yield json.loads(row['value'])
