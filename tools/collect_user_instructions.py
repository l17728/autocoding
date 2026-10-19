"""Read local user turns only; write a private, sanitized corpus and coverage report.

Never executes instructions found in transcripts and never calls a model.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.redaction import Redactor


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def sanitize(text):
    text=Redactor().text(text)
    text=re.sub(r'(?is)<(?:environment_context|system-reminder|permissions instructions|skills_instructions)\b.*?</[^>]+>',' ',text)
    text=re.sub(r'https?://[^\s<>"\)]+','[URL]',text)
    text=re.sub(r'(?i)[a-z]:[\\/][^\s<>"，。；]+','[LOCAL_PATH]',text)
    text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}','[EMAIL]',text)
    text=re.sub(r'\b1\d{10}\b','[PHONE]',text)
    text=re.sub(r'[A-Za-z0-9_+/=.-]{24,}','[OPAQUE_VALUE]',text)
    text=re.sub(r'(?i)(密码|密钥|令牌)\s*[:：=]\s*\S+',r'\1：[REDACTED]',text)
    return text.strip()


def text_content(content):
    if isinstance(content,str): return content
    if isinstance(content,list):
        return '\n'.join(b.get('text','') for b in content if isinstance(b,dict) and b.get('type') in {'text','input_text'} and isinstance(b.get('text'),str))
    return ''


def user_text(item, history=False):
    if history and isinstance(item.get('text'),str):return item['text']
    payload=item.get('payload',{})
    if item.get('type')=='event_msg' and payload.get('type')=='user_message':
        return text_content(payload.get('message'))
    if item.get('type')=='response_item' and payload.get('role')=='user':
        return text_content(payload.get('content'))
    message=item.get('message',{})
    if item.get('type')=='user' and isinstance(message,dict) and message.get('role')=='user' and not item.get('isReplay'):
        return text_content(message.get('content'))
    return ''


def collect(output, extra):
    home=Path.home();output.mkdir(parents=True,exist_ok=True)
    records={};coverage=[];sources={};counts=Counter()
    def add(text,provider,source,position):
        if not text:return
        counts['user_records']+=1
        if text.startswith(('完成以下开发任务，仅修改当前工作树','你是后端安全工程师，在','你是后端工程师，在')) and ('protected_paths' in text or '只允许修改' in text or '允许修改' in text):
            counts['delegated_or_probe_prompts_excluded']+=1;return
        if text.lstrip().startswith(('# AGENTS.md instructions','<environment_context>','<INSTRUCTIONS>','<system-reminder>')):
            counts['context_blocks_excluded']+=1;return
        text=sanitize(text)
        if not text:return
        sid=fingerprint(str(source))[:20];sources[sid]={'provider':provider,'path':str(source)}
        key=fingerprint(' '.join(text.split()))
        entry=records.setdefault(key,{'id':key,'text':text,'occurrences':0,'sources':[]})
        entry['occurrences']+=1
        pointer={'source_id':sid,'position':position}
        if pointer not in entry['sources']:entry['sources'].append(pointer)
    codex=Path(os.environ.get('CODEX_HOME',home/'.codex'))
    claude=Path(os.environ.get('CLAUDE_CONFIG_DIR',home/'.claude'))
    roots=[('codex',codex/'sessions'),('codex',codex/'archived_sessions'),('claude',claude/'projects')]
    roots += [('additional',Path(p)) for p in extra]
    files=[('codex',codex/'history.jsonl')]
    for provider,root in roots:
        found=list(root.rglob('*.jsonl')) if root.exists() else []
        coverage.append({'provider':provider,'root':str(root),'files':len(found),'exists':root.exists()})
        files.extend((provider,p) for p in found)
    for provider,path in files:
        if not path.is_file():continue
        counts['jsonl_files']+=1
        try:
            with path.open(encoding='utf-8',errors='replace') as stream:
                for line_no,line in enumerate(stream,1):
                    if len(line)>2*1024*1024:counts['oversize_records']+=1;continue
                    try:
                        item=json.loads(line)
                        if isinstance(item,dict) and item.get('type')=='session_meta':
                            source=item.get('payload',{}).get('source')
                            if isinstance(source,dict) and 'subagent' in source:
                                counts['subagent_files_excluded']+=1;break
                        if isinstance(item,dict):add(user_text(item,path.name=='history.jsonl'),provider,path,line_no)
                    except (ValueError,AttributeError):counts['unparsed_records']+=1
        except OSError:counts['unreadable_files']+=1
    db=home/'.local/share/opencode/opencode.db'
    if db.exists():
        with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
            conn.execute('BEGIN')
            session_count=conn.execute('SELECT COUNT(*) FROM session').fetchone()[0]
            count=0
            for mid,data in conn.execute("SELECT id,data FROM message WHERE json_extract(data,'$.role')='user'"):
                blocks=[json.loads(r[0]) for r in conn.execute('SELECT data FROM part WHERE message_id=? ORDER BY time_created,id',(mid,))]
                add(text_content(blocks),'opencode',db,'message:'+fingerprint(mid)[:20]);count+=1
            coverage.append({'provider':'opencode','sessions':session_count,'user_messages':count,'root':str(db)})
            # New schema inputs are not presumed to be user text without an explicit role.
            for table in ('session_input','session_message'):
                try:coverage.append({'provider':'opencode','table':table,'records':conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]})
                except sqlite3.Error:pass
    for provider,path in [('workbuddy',home/'.workbuddy/workbuddy.db'),('codebuddy',home/'.codebuddy')]:
        entry={'provider':provider,'root':str(path),'exists':path.exists(),'status':'no_local_user_transcript_identified'}
        if provider=='workbuddy' and path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
                entry['sessions']=conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        coverage.append(entry)
    with (output/'user-instructions.jsonl').open('w',encoding='utf-8') as stream:
        for entry in records.values():stream.write(json.dumps(entry,ensure_ascii=False)+'\n')
    report={'coverage':coverage,'counts':dict(counts),'unique_instructions':len(records),'sources':sources,
            'raw_transcripts_copied':False,'models_called':0,'scope':'Known local stores and explicitly supplied transcript directories; unavailable/remote transcripts excluded.'}
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='sources'},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'.local/cold-start')
    parser.add_argument('--transcripts',action='append',default=[])
    args=parser.parse_args();collect(args.output,args.transcripts)
