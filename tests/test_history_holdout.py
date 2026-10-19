import unittest
from tools.prepare_history_holdout import audit


class HistoryHoldoutTests(unittest.TestCase):
    def test_exposed_reworded_format_and_source_overlap_are_rejected(self):
        old = {'id': 'old', 'text': 'Keep all original project files and verify every change.', 'sources': [{'source_id': 'session1', 'position': 1}]}
        records = [old,
                   {'id': 'copy', 'text': old['text'].upper(), 'sources': ['turn2']},
                   {'id': 'source', 'text': 'A different passage from the same original source.', 'sources': [{'source_id': 'session1', 'position': 99}]},
                   {'id': 'append', 'text': old['text'] + ' Additional context.', 'sources': ['turn3']}]
        report = audit(records, [old])
        self.assertEqual([], report['candidates'])
        self.assertEqual(['cold_start_exposed', 'cold_start_exposed', 'source_overlap', 'text_overlap'],
                         [r['reason'] for r in report['rejected']])

    def test_new_unique_history_requires_review_and_never_becomes_proven(self):
        records = [{'id': 'a', 'text': 'A sufficiently detailed future decision context.', 'sources': ['new']},
                   {'id': 'b', 'text': 'A sufficiently detailed future decision context!', 'sources': ['copy']}]
        report = audit(records, [])
        self.assertEqual(1, len(report['candidates']))
        self.assertEqual('duplicate', report['rejected'][0]['reason'])
        self.assertEqual(0, report['certified_holdouts'])
        self.assertFalse(report['independence_proven'])
        self.assertNotIn('text', report['candidates'][0])
