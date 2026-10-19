"""Real SQLite failures on disposable databases; never fill the host disk."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core=Core(Path(self.temp.name)/'fault.db')

    def assert_clean_and_retry(self):
        with self.core.connection() as conn:
            self.assertEqual('ok',conn.execute('PRAGMA quick_check').fetchone()[0])
            for table in ('tasks','events','requests'):
                self.assertEqual(0,conn.execute('SELECT count(*) FROM '+table).fetchone()[0])
        self.core.create_task('task_retry','Recovered',['unit'],key='same-key')
        replay=self.core.create_task('task_retry','Recovered',['unit'],key='same-key')
        self.assertEqual('todo',replay['state'])
        self.assertEqual(1,len(self.core.snapshot()['tasks']))
        self.assertEqual(1,len(self.core.events()))

    def test_real_sqlite_capacity_failure_rolls_back_all_records(self):
        connection=self.core.connection
        @contextmanager
        def bounded():
            with connection() as conn:
                pages=conn.execute('PRAGMA page_count').fetchone()[0]
                conn.execute('PRAGMA max_page_count='+str(pages+1))
                yield conn
        with patch.object(self.core,'connection',bounded):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.core.create_task('task_retry','x'*262144,['unit'],key='same-key')
        self.assertEqual(sqlite3.SQLITE_FULL,caught.exception.sqlite_errorcode)
        self.assert_clean_and_retry()

    def test_real_readonly_database_never_records_partial_task(self):
        @contextmanager
        def readonly():
            conn=sqlite3.connect(self.core.database.as_uri()+'?mode=ro',uri=True,isolation_level=None)
            conn.row_factory=sqlite3.Row
            try: yield conn
            finally: conn.close()
        with patch.object(self.core,'connection',readonly):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.core.create_task('task_retry','Recovered',['unit'],key='same-key')
        self.assertEqual(sqlite3.SQLITE_READONLY,caught.exception.sqlite_errorcode & 255)
        self.assert_clean_and_retry()

    def test_real_writer_contention_keeps_reads_available_and_retry_unique(self):
        connection=self.core.connection
        @contextmanager
        def impatient():
            with connection() as conn:
                conn.execute('PRAGMA busy_timeout=50')
                yield conn
        with connection() as holder:
            holder.execute('BEGIN IMMEDIATE')
            with patch.object(self.core,'connection',impatient):
                self.assertEqual([],self.core.snapshot()['tasks'])
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.core.create_task('task_retry','Recovered',['unit'],key='same-key')
            self.assertEqual(sqlite3.SQLITE_BUSY,caught.exception.sqlite_errorcode & 255)
            holder.rollback()
        self.assert_clean_and_retry()
