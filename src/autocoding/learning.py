"""Deterministic knowledge governance and evidence-based outcome metrics."""
import hashlib
import json
import unicodedata
import copy
import re
import sqlite3
import threading
from .core import DomainError, encoded


def normalized(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def identity(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()[:24]


def canonical_topic(item):
    """Conservative bilingual domain aliases, never a semantic truth judgement."""
    if item.get('origin')=='curated_cold_start':
        # These topics have already been reviewed and merged as separate concerns.
        return 'curated:'+normalized(item['topic'])
    text=normalized((item.get('topic') or '')+' '+item['content'])
    domains={
        'verification':r'测试|验收|验证|\btests?\b|\btesting\b|\bverification\b',
        'permission':r'权限|授权|审批|\bpermissions?\b|\bapprovals?\b|\bauthorization\b',
        'unknown_delivery':r'未知投递|未确认输入|重放|\breplay\b|unknown delivery',
        'credentials':r'密钥|凭据|api.?key|\bcredentials?\b|\bsecrets?\b',
    }
    matches=[name for name,pattern in domains.items() if re.search(pattern,text)]
    # Mixed subjects keep their own explicit scope; don't guess which claim wins.
    if len(matches)==1:
        return 'domain:'+matches[0]
    return normalized(item.get('topic') or item['content'])


class Learning:
    def __init__(self, reflection):
        self.review = reflection
        self.core = reflection.core
        self._cache = None
        self._outcome_cache = None
        self._references_cache = None
        self._persisted = None
        self._released = False
        self._cache_lock = threading.Lock()
        with self.core.connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS learning_revision (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
                INSERT OR IGNORE INTO learning_revision VALUES(1,0);
                CREATE INDEX IF NOT EXISTS learning_events_type_seq ON events(type,seq);
                CREATE TABLE IF NOT EXISTS learning_catalogue_cache (
                    id INTEGER PRIMARY KEY CHECK(id=1), format INTEGER NOT NULL,
                    revision INTEGER NOT NULL, valid_after REAL NOT NULL, valid_until REAL NOT NULL,
                    checksum TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS learning_changed_renamed_key
                    AFTER UPDATE OF key ON metadata
                    WHEN (OLD.key GLOB 'pm_review:*' OR OLD.key GLOB 'pm_knowledge_resolution:*')
                    AND NOT (NEW.key GLOB 'pm_review:*' OR NEW.key GLOB 'pm_knowledge_resolution:*')
                    BEGIN UPDATE learning_revision SET value=value+1 WHERE id=1; END;
            ''')
            for operation in ('INSERT','UPDATE','DELETE'):
                row='OLD' if operation=='DELETE' else 'NEW'
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS learning_changed_{operation.lower()}
                    AFTER {operation} ON metadata
                    WHEN {row}.key GLOB 'pm_review:*' OR {row}.key GLOB 'pm_knowledge_resolution:*'
                    BEGIN UPDATE learning_revision SET value=value+1 WHERE id=1; END''')

    def catalogue(self):
        return copy.deepcopy(self._catalogue())

    def release(self):
        """Release large derived graphs even when a closed Host remains referenced."""
        with self._cache_lock:
            self._released = True
            self._cache = None
            self._persisted = None
            self._outcome_cache = None
            self._references_cache = None

    def _remember(self, name, value):
        with self._cache_lock:
            if not self._released:
                setattr(self,name,value)

    def _catalogue(self):
        groups = {}
        # Keep knowledge independent of the UI's most recent 50 review records.
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            version=conn.execute('SELECT value FROM learning_revision WHERE id=1').fetchone()[0]
            cached=self._cache
            if cached and cached[0]==version and cached[3]<=self.core.clock()<cached[1]:
                return cached[2]
            stored=conn.execute('SELECT * FROM learning_catalogue_cache WHERE id=1 AND format=1 AND revision=? '
                'AND valid_after<=? AND valid_until>? AND length(payload)<=33554432',
                (version,self.core.clock(),self.core.clock())).fetchone()
            if stored and hashlib.sha256(stored['payload'].encode('utf-8')).hexdigest()==stored['checksum']:
                try:
                    cached=json.loads(stored['payload'])
                    if isinstance(cached,list):
                        restored=(version,stored['valid_until'],cached,stored['valid_after'])
                        self._remember('_cache',restored)
                        self._remember('_persisted',restored)
                        return cached
                except (ValueError,TypeError):
                    pass
            rows = conn.execute("SELECT value FROM metadata WHERE key GLOB 'pm_review:*'").fetchall()
            decisions={r['key']:json.loads(r['value']) for r in conn.execute("SELECT key,value FROM metadata WHERE key GLOB 'pm_knowledge_resolution:*'")}
        for row in rows:
            review = json.loads(row['value'])
            if review['state'] != 'completed' or review.get('archived'):
                continue
            for item in review.get('items', []):
                topic = canonical_topic(item)
                key = identity([topic] if topic.startswith('domain:') else [item['category'], topic])
                group = groups.setdefault(key, {'id':key, 'topic':item.get('topic') or item['content'], 'variants':{}})
                vid = identity([key, normalized(item['content'])])
                variant = group['variants'].setdefault(vid, {**item, 'id':vid, 'sources':[], 'updated_at':0})
                variant['sources'].append({'review_id':review['id'], 'evidence_ids':item['evidence_ids']})
                if review['created_at'] >= variant['updated_at']:
                    variant.update(updated_at=review['created_at'], review_id=review['id'])
        result = []
        for group in groups.values():
            variants = sorted(group['variants'].values(), key=lambda v:v['id'])
            revision = identity([[v['id'], sorted(s['review_id'] for s in v['sources'])] for v in variants])
            decision = decisions.get('pm_knowledge_resolution:'+group['id'], {})
            selected = decision.get('selected') if decision.get('revision') == revision else None
            for variant in variants:
                expired = self.core.clock()-variant['updated_at'] >= 90*86400
                variant['state'] = ('expired' if expired else 'superseded' if selected and variant['id'] != selected
                                    else 'conflict' if len(variants)>1 and not selected else 'active')
            result.append({**group, 'variants':variants, 'revision':revision})
        result=sorted(result, key=lambda g:g['id'])
        now=self.core.clock()
        expiries=[v['updated_at']+90*86400 for g in result for v in g['variants'] if v['updated_at']+90*86400>now]
        self._remember('_cache',(version,min(expiries,default=now+3600),result,now))
        return result

    def _persist(self):
        # Only page/reference entry points write derived cache, never catalogue()
        # called while a knowledge-resolution transaction already owns the writer.
        cached=self._cache
        if self._released or cached is None or cached is self._persisted:
            return
        payload=encoded(cached[2])
        if len(payload.encode('utf-8'))>32*1024*1024:
            return
        try:
            with self.core.connection() as conn:
                conn.execute('PRAGMA busy_timeout=50')
                result=conn.execute('INSERT OR REPLACE INTO learning_catalogue_cache '
                    'SELECT 1,1,?,?,?,?,? WHERE (SELECT value FROM learning_revision WHERE id=1)=?',
                    (cached[0],cached[3],cached[1],hashlib.sha256(payload.encode('utf-8')).hexdigest(),payload,cached[0]))
                if result.rowcount:
                    self._remember('_persisted',cached)
        except sqlite3.Error:
            pass  # Derived cache failure must not discard authoritative knowledge.

    def page(self, offset=0, limit=25):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise DomainError('INVALID_KNOWLEDGE_PAGE')
        groups=self._catalogue()
        self._persist()
        return {'items':copy.deepcopy(groups[offset:offset+limit]), 'total':len(groups),'offset':offset,'limit':limit}

    def resolve(self, group_id, selected, revision):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Keep writers excluded between revision validation and decision commit.
            group = next((g for g in self._catalogue() if g['id']==group_id), None)
            if group is None:
                raise DomainError('NOT_FOUND')
            if group['revision'] != revision:
                raise DomainError('STALE_KNOWLEDGE')
            if selected not in {v['id'] for v in group['variants']}:
                raise DomainError('INVALID_KNOWLEDGE_SELECTION')
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                         ('pm_knowledge_resolution:'+group_id, encoded({'selected':selected,'revision':revision})))
            self.core._event(conn, 'PMReviewKnowledgeResolved', 'human', group_id=group_id, selected=selected, revision=revision)
            conn.commit()
        return {'state':'resolved'}

    def references(self):
        groups=self._catalogue()
        self._persist()
        cached=self._references_cache
        if cached and cached[0] is groups:
            return copy.deepcopy(cached[1])
        active = [v for g in groups for v in g['variants'] if v['state']=='active']
        active.sort(key=lambda v:(-v['updated_at'], -v.get('priority',0), v['id']))
        result = [{k:v[k] for k in ('id','category','content','confidence','evidence_ids','review_id')} |
                {'trust':'reference_only'} for v in active[:12]]
        self._remember('_references_cache',(groups,result))
        return copy.deepcopy(result)

    def outcomes(self):
        # One final outcome per run; repeated recovery events cannot inflate the score.
        with self.core.connection() as conn:
            version=conn.execute("SELECT MAX(seq) FROM events WHERE type='TaskRunChanged'").fetchone()[0]
            cached=self._outcome_cache
            if cached and cached[0]==version:
                return copy.deepcopy(cached[1])
            rows = conn.execute("SELECT seq,payload FROM events WHERE type='TaskRunChanged' ORDER BY seq").fetchall()
        runs = {}
        for row in rows:
            payload = json.loads(row['payload'])
            if payload.get('state') in {'done','rework_required','failed','reconciling'} and payload.get('run_id'):
                previous = runs.get(payload['run_id'])
                runs[payload['run_id']] = {'seq':previous['seq'] if previous else row['seq'],
                                           'state':payload['state'], 'run_id':payload['run_id']}
        recent = sorted(runs.values(),key=lambda r:r['seq'])[-40:]
        def window(items):
            verified = sum(r['state']=='done' for r in items)
            return {'samples':len(items), 'verified':verified,
                    'score':round(100*verified/len(items),1) if items else None,
                    'run_ids':[r['run_id'] for r in items]}
        current, previous = window(recent[-20:]), window(recent[:-20])
        enough = min(current['samples'], previous['samples']) >= 10
        result = {'current':current, 'previous':previous, 'status':'comparable' if enough else 'insufficient_samples',
                'delta':round(current['score']-previous['score'],1) if enough else None,
                'interpretation':'独立验收通过的执行次数占比，返工、失败和待对账计入分母；并非 PM 能力评分，也不能证明某条经验导致改善。'}
        self._remember('_outcome_cache',(version,result))
        return copy.deepcopy(result)
