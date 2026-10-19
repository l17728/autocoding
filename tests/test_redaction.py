import pathlib
import sys
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from autocoding.redaction import Redactor


class RedactionTests(unittest.TestCase):
    def test_terminal_queries_are_complete_at_every_chunk_boundary(self):
        # t/p/c are secret-label or configured-key prefixes, but here they
        # terminate CSI queries. Holding them deadlocks a waiting terminal.
        for query in ('\x1b[18t','\x1b[?2026$p','\x1b[c'):
            for index in range(len(query)+1):
                stream=Redactor(['credential-canary']).terminal_stream()
                result=stream.feed(query[:index])+stream.feed(query[index:])
                self.assertEqual(query,result,(query,index))
                self.assertEqual('',stream.pending)
        stream=Redactor(['credential-canary']).terminal_stream()
        self.assertEqual('prefix ',stream.feed('prefix c'))
        self.assertEqual('[REDACTED] ',stream.feed('redential-canary '))

    def test_terminal_prompts_do_not_wait_for_newline(self):
        stream=Redactor(['PRIVATE_CANARY']).terminal_stream()
        self.assertEqual('Choose an option > ',stream.feed('Choose an option > '))
        self.assertEqual('\x1b[2J',stream.feed('\x1b[2J'))

    def test_terminal_split_secrets_remain_redacted(self):
        raw='prefix PRIVATE_CANARY token=opaque-value\n'
        for index in range(len(raw)+1):
            stream=Redactor(['PRIVATE_CANARY']).terminal_stream()
            result=stream.feed(raw[:index])+stream.feed(raw[index:])
            self.assertEqual('prefix [REDACTED] token=[REDACTED]\n',result)
        stream=Redactor().terminal_stream()
        result=''.join(stream.feed(c) for c in 'Bearer opaque-value\n')
        self.assertEqual('Bearer [REDACTED]\n',result)
        stream=Redactor().terminal_stream()
        self.assertEqual('[OUTPUT_FRAGMENT_DROPPED]',stream.feed('token='+'x'*65537))
        self.assertEqual('',stream.feed('remaining-private-value'))
        self.assertEqual('\nready > ',stream.feed('\nready > '))
    def test_secret_and_auth_split_at_every_boundary(self):
        raw = 'prefix PRIVATE_CANARY and Bearer opaque-secret\n'
        for index in range(len(raw) + 1):
            stream = Redactor(['PRIVATE_CANARY']).stream()
            result = stream.feed(raw[:index]) + stream.feed(raw[index:]) + stream.feed('', final=True)
            self.assertNotIn('PRIVATE_CANARY', result)
            self.assertNotIn('opaque-secret', result)

    def test_nested_diagnostics_and_query_token(self):
        result = Redactor(['private-value']).value({'a':['private-value', 'https://a/?token=abc&x=2']})
        self.assertNotIn('private-value', str(result))
        self.assertNotIn('abc', str(result))

    def test_unbounded_line_is_dropped(self):
        stream = Redactor().stream()
        self.assertEqual('[OUTPUT_LINE_DROPPED]\n', stream.feed('x' * 65537))
        self.assertEqual('', stream.pending)
