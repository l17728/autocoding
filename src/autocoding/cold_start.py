"""Install the reviewed, source-bound cold-start package, never raw transcripts."""
import json
from pathlib import Path
from .core import encoded, digest


def install(core):
    path=Path(__file__).parent/'data/cold-start.json'
    package=json.loads(path.read_text(encoding='utf-8'))
    package_hash=digest(package)
    imported=0
    updated=0
    with core.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        created=core.clock()
        for index,entry in enumerate(package['items']):
            rid='review_seed_'+digest([package['version'],entry['id']])[:24]
            existing=conn.execute('SELECT value FROM metadata WHERE key=?',('pm_review:'+rid,)).fetchone()
            previous=json.loads(existing['value']) if existing else None
            entry_hash=digest(entry)
            if previous and previous.get('entry_hash')==entry_hash:
                continue
            core._event(conn,'PMReviewColdStartUpdated' if previous else 'PMReviewColdStartImported','human',seed_id=entry['id'],
                        package_version=package['version'],package_hash=package_hash)
            event=conn.execute('SELECT seq,event_id FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            item={'category':entry['category'],'topic':entry['topic'],'content':entry['content']+'\n可用话术：'+entry['template'],
                  'confidence':'hypothesis','evidence_ids':[event['event_id']], 'origin':'curated_cold_start',
                  'priority':len(package['items'])-index,'sources':entry['sources'],'kind':entry['kind']}
            record={'id':rid,'created_at':created,'state':'completed','archived':False,'automatic':False,
                    'from_seq':event['seq'],'to_seq':event['seq'],'summary':'冷启动知识 · '+entry['topic'],
                    'items':[item],'model_generated':False,'source_kind':'curated_cold_start',
                    'package_version':package['version'],'package_hash':package_hash,
                    'entry_hash':entry_hash,
                    'evidence':{'sources':entry['sources'],'notice':'人工归纳的历史参考；导入事件证明来源，不证明建议效果。'}}
            if previous:
                record['archived']=previous.get('archived',False)
                conn.execute('UPDATE metadata SET value=? WHERE key=?',(encoded(record),'pm_review:'+rid))
                updated+=1
            else:
                conn.execute('INSERT INTO metadata VALUES (?,?)',('pm_review:'+rid,encoded(record)))
                imported+=1
        conn.commit()
    return {'imported':imported,'updated':updated,'total':len(package['items']),'version':package['version'],'package_hash':package_hash}
