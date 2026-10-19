"""Offline leakage audit; never imports history into knowledge or calls a model.

Only metadata and source hashes are exported. Surviving records still require
context, label and semantic-overlap review; they are not certified holdouts.
"""
import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path


def normalize(text):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', text).casefold())


def source_ids(record):
    # Hold out whole source sessions/files, not adjacent lines from one session.
    return {json.dumps(source.get('source_id') if isinstance(source, dict) else source, sort_keys=True, ensure_ascii=False)
            for source in record.get('sources', [])}


def audit(records, exposed):
    exposed_ids = {r['id'] for r in exposed}
    exposed_text = {normalize(r['text']) for r in exposed}
    exposed_sources = set().union(*(source_ids(r) for r in exposed))
    seen = set()
    rejected = []
    candidates = []
    for record in records:
        normalized = normalize(record['text'])
        reason = None
        if record['id'] in exposed_ids or normalized in exposed_text:
            reason = 'cold_start_exposed'
        elif source_ids(record) & exposed_sources:
            reason = 'source_overlap'
        elif not normalized or len(normalized) < 15:
            reason = 'insufficient_context'
        elif normalized in seen:
            reason = 'duplicate'
        elif any(normalized in old or old in normalized for old in exposed_text if len(old) >= 15):
            reason = 'text_overlap'
        seen.add(normalized)
        entry = {'source_id': record['id'], 'text_hash': hashlib.sha256(normalized.encode()).hexdigest()}
        if reason:
            rejected.append({**entry, 'reason': reason})
        else:
            candidates.append({**entry, 'status': 'requires_context_label_and_semantic_review'})
    return {'model_calls': 0, 'certified_holdouts': 0, 'candidates': candidates,
            'rejected': rejected, 'independence_proven': False,
            'notice': 'No expected labels inferred. Do not import candidates into knowledge before evaluation.'}


def read_records(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--exposed', required=True, help='Frozen corpus used to build cold-start knowledge')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output in {Path(args.input).resolve(), Path(args.exposed).resolve()}:
        parser.error('output must not overwrite source corpora')
    report = audit(read_records(args.input), read_records(args.exposed))
    report['exposed_file_sha256'] = hashlib.sha256(Path(args.exposed).read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'candidates': len(report['candidates']), 'rejected': len(report['rejected']),
                      'certified_holdouts': 0, 'model_calls': 0}))


if __name__ == '__main__':
    main()
