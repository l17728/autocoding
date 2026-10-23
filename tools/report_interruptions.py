"""Summarize durable task interruptions from a project's event ledger."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo('Asia/Shanghai')
INTERRUPTED_STATES = {'failed', 'reconciling', 'reconciled', 'rework_required'}
EXTERNAL_MARKERS = ('RATE_LIMIT', 'QUOTA', 'TOKEN_LIMIT', 'MODEL_UNAVAILABLE',
                    'CONNECTION_LOST', 'NETWORK_UNAVAILABLE', 'DNS_FAILURE', 'TLS_FAILURE')


def _window(day_from: str, day_to: str) -> tuple[float, float]:
    start = datetime.combine(date.fromisoformat(day_from), time.min, LOCAL_TZ)
    end = datetime.combine(date.fromisoformat(day_to), time.min, LOCAL_TZ)
    if end <= start:
        raise ValueError('--to must be later than --from (exclusive)')
    return start.timestamp(), end.timestamp()


def _category(reason: str, error_message: str) -> str:
    evidence = f'{reason} {error_message}'.upper()
    if any(marker in evidence for marker in EXTERNAL_MARKERS):
        return 'external_dependency'
    if any(marker in evidence for marker in ('TIMEOUT', 'CONNECTION', 'NETWORK', 'RATE', 'QUOTA')):
        return 'needs_attribution'
    return 'platform_or_workflow'


def build_report(database: Path, day_from: str, day_to: str) -> dict:
    start, end = _window(day_from, day_to)
    connection = sqlite3.connect(f'file:{database.resolve()}?mode=ro', uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        runs = []
        for row in connection.execute("SELECT value FROM metadata WHERE key GLOB 'run:*'"):
            run = json.loads(row['value'])
            created = run.get('created_at')
            if not isinstance(created, (int, float)) or not start <= created < end:
                continue
            state = run.get('state')
            failed_checks = run.get('failed_checks') or []
            if state not in INTERRUPTED_STATES or not (run.get('reason') or run.get('error_message') or failed_checks):
                continue
            reason = (run.get('reconciliation_reason') or run.get('reason') or
                      ('ACCEPTANCE_FAILED' if failed_checks else 'UNSPECIFIED'))
            error_message = run.get('error_message') or ''
            runs.append({
                'run_id': run.get('id'), 'task_id': run.get('task_id'),
                'provider': run.get('provider'), 'state': state,
                'phase': run.get('error_phase') or run.get('phase'),
                'reason': reason, 'error_type': run.get('error_type'),
                'error_message': error_message[:500],
                'category': _category(reason, error_message),
                'recovery_action': run.get('recovery_action'),
                'created_at': datetime.fromtimestamp(created, LOCAL_TZ).isoformat(timespec='seconds'),
            })
        events = connection.execute(
            'SELECT timestamp,type,payload FROM events WHERE timestamp>=? AND timestamp<?',
            (start, end)).fetchall()
    finally:
        connection.close()

    event_counts = Counter()
    recovered_runs = set()
    stopped_batches = {}
    delivery_reasons = Counter()
    recorded_interruptions = []
    for event in events:
        payload = json.loads(event['payload'] or '{}')
        if event['type'] == 'TaskInterruptionRecorded':
            recorded_interruptions.append({key: payload.get(key) for key in (
                'incident_id', 'task', 'provider', 'state', 'phase', 'reason',
                'error_type', 'recovery_policy')})
        elif event['type'] == 'PMAutoRecoveredAttempt':
            recovered_runs.add(payload.get('run_id') or f"event:{event['timestamp']}")
        elif event['type'] == 'CommandDeliveryUnknown':
            event_counts['delivery_unknown'] += 1
            delivery_reasons[payload.get('reason') or 'UNSPECIFIED'] += 1
        elif event['type'] == 'TaskBatchChanged' and payload.get('state') in {'reconciling', 'rework_required'}:
            stopped_batches[(payload.get('batch_id'), payload.get('state'))] = payload.get('reason')
    by_category = Counter(run['category'] for run in runs)
    by_reason = Counter(run['reason'] for run in runs)
    return {
        'window': {'from': day_from, 'to_exclusive': day_to, 'timezone': 'Asia/Shanghai'},
        'interrupted_runs': len(runs),
        'runs': sorted(runs, key=lambda item: item['created_at']),
        'recorded_interruption_events': len(recorded_interruptions),
        'recorded_recovery_policies': dict(sorted(Counter(
            entry.get('recovery_policy') or 'unspecified' for entry in recorded_interruptions).items())),
        'recent_interruption_events': recorded_interruptions,
        'automatic_recovery_events': len(recovered_runs),
        'delivery_unknown_events': event_counts['delivery_unknown'],
        'delivery_unknown_by_reason': dict(sorted(delivery_reasons.items())),
        'stopped_batches': len(stopped_batches),
        'runs_by_category': dict(sorted(by_category.items())),
        'runs_by_reason': dict(sorted(by_reason.items())),
        'classification_note': 'TIMEOUT and unclassified transport failures require correlation with supervisor/provider logs; they are not assumed to be user-actionable.',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_id')
    parser.add_argument('--from', dest='day_from', required=True, help='Inclusive local date, YYYY-MM-DD')
    parser.add_argument('--to', dest='day_to', required=True, help='Exclusive local date, YYYY-MM-DD')
    parser.add_argument('--storage', type=Path, default=Path('.local/workbench/projects'))
    parser.add_argument('--output', type=Path, help='Optional path to save the JSON report')
    args = parser.parse_args()
    database = args.storage / args.project_id / 'platform.db'
    report = build_report(database, args.day_from, args.day_to)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
