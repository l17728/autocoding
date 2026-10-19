import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
import sqlite3
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from fastapi.testclient import TestClient
from autocoding.api import create_app
from autocoding.host import Host


class QualityApiTests(unittest.TestCase):
    def test_storage_failure_is_bounded_redacted_and_not_auto_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            host=Host(directory,Path(directory)/'state')
            with TestClient(create_app(host,'fixture-token'),base_url='http://127.0.0.1:8765') as client:
                for error_code,expected in [(sqlite3.SQLITE_BUSY,'STORAGE_BUSY'),(sqlite3.SQLITE_FULL,'STORAGE_UNAVAILABLE')]:
                    failure=sqlite3.OperationalError('PRIVATE_SQL_PATH_AND_INPUT')
                    failure.sqlite_errorcode=error_code
                    with patch('autocoding.dashboard.Dashboard.snapshot',side_effect=failure):
                        result=client.get('/api/v1/projects/'+host.project_id+'/snapshot',
                                          headers={'Authorization':'Bearer fixture-token'})
                    self.assertEqual(503,result.status_code)
                    self.assertEqual(expected,result.json()['error']['code'])
                    self.assertFalse(result.json()['error']['retryable'])
                    self.assertNotIn('PRIVATE_SQL',result.text)

    def test_authenticated_quality_settings_and_paged_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            host=Host(root,root/'state')
            host.core.create_task('task_a','Example',['unit'],key='task')
            with TestClient(create_app(host,'fixture-token'),base_url='http://127.0.0.1:8765') as client:
                prefix='/api/v1/projects/'+host.project_id
                headers={'Authorization':'Bearer fixture-token','Idempotency-Key':'coverage'}
                for path in ['/coverage','/expert-reviews','/learning-evaluations','/history/runs']:
                    self.assertEqual(401,client.get(prefix+path).status_code)
                current=client.get(prefix+'/coverage',headers=headers).json()
                response=client.post(prefix+'/coverage',headers=headers,json={'fingerprint':current['fingerprint'],'requirements':[{'id':'R1','description':'Example','cases':[{'task_id':'task_a','criterion':'unit'}]}]})
                self.assertEqual(200,response.status_code,response.text)
                self.assertEqual('unproven',client.get(prefix+'/coverage',headers=headers).json()['matrix'][0]['status'])
                response=client.post(prefix+'/expert-reviews/settings',headers={**headers,'Idempotency-Key':'expert'},json={'enabled':True})
                self.assertEqual(200,response.status_code,response.text)
                self.assertTrue(client.get(prefix+'/expert-reviews',headers=headers).json()['settings']['enabled'])
                self.assertEqual(409,client.get(prefix+'/history/runs?limit=101',headers=headers).status_code)
                snapshot=client.get(prefix+'/snapshot',headers=headers).json()
                self.assertEqual(1,snapshot['totals']['tasks'])
                self.assertNotIn('lease_token',snapshot['tasks'][0])
