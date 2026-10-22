import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from fastapi.testclient import TestClient
from autocoding.api import create_app
from autocoding.host import Host


class ApiTests(unittest.TestCase):
    def test_holdout_intake_requires_owner_and_never_calls_model(self):
        path=self.prefix+'/learning-evaluations/holdouts'
        self.assertEqual(401,self.client.get(path).status_code)
        body={'source':'new fixture','prompt':'future independent scenario','scope':'outside'}
        self.assertEqual(401,self.client.post(path,json=body).status_code)
        headers={**self.headers,'Idempotency-Key':'holdout-create'}
        first=self.client.post(path,headers=headers,json=body)
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(first.json(),self.client.post(path,headers=headers,json=body).json())
        reply=self.client.post(path+'/'+first.json()['id']+'/answer',
            headers={**self.headers,'Idempotency-Key':'holdout-answer'},json={'expected':{'choice':'verify'}})
        self.assertEqual(200,reply.status_code,reply.text)
        snapshot=self.client.get(path,headers=self.headers).json()
        self.assertEqual(0,snapshot['model_calls'])
        self.assertEqual(0,snapshot['certified_holdouts'])
        self.assertEqual('pending_independence_review',snapshot['items'][0]['state'])
        sample=snapshot['items'][0]
        review_path=path+'/'+sample['id']+'/review'
        review={'revision':sample['revision'],'decision':'accepted','note':'Source and semantic overlap checked independently'}
        self.assertEqual(401,self.client.post(review_path,json=review).status_code)
        response=self.client.post(review_path,headers={**self.headers,'Idempotency-Key':'review'},json=review)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('accepted',response.json()['state'])
        self.assertEqual(409,self.client.post(review_path,headers={**self.headers,'Idempotency-Key':'stale-review'},json=review).status_code)

    def test_delegation_is_authenticated_and_not_repeated(self):
        from autocoding.acceptance import Acceptance
        from autocoding.planner import save_proposal
        acceptance=Acceptance(self.host)
        acceptance.save(acceptance.snapshot()['fingerprint'],{'checks':{'unit':[sys.executable,'-V']}},key='delegate-checks')
        async def propose(goal,criteria):
            return save_proposal(self.host.core,[{'id':'task_delegated','description':goal,'acceptance':criteria,'dependencies':[]}],
                                 self.host.core.snapshot()['tasks'],self.host.redactor)
        self.host.propose=AsyncMock(side_effect=propose)
        self.host.coordinator.start=AsyncMock(return_value={'id':'batch_test','state':'queued'})
        path=self.prefix+'/pm/delegate';body={'goal':'实现目标','acceptance':['unit'],'agent':'codex'}
        self.assertEqual(401,self.client.post(path,json=body).status_code)
        headers={**self.headers,'Idempotency-Key':'delegate-once'}
        first=self.client.post(path,headers=headers,json=body)
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(first.json(),self.client.post(path,headers=headers,json=body).json())
        self.host.propose.assert_awaited_once();self.host.coordinator.start.assert_awaited_once()
        self.assertEqual(first.json()['id'],self.client.get(self.prefix+'/pm/delegations',headers=self.headers).json()[0]['id'])

    def test_owner_profile_auth_revision_and_idempotency(self):
        path=self.prefix+'/pm/profile'
        self.assertEqual(401,self.client.get(path).status_code)
        original=self.client.get(path,headers=self.headers).json()
        body={'revision':original['revision'],'preferences':original['preferences']}
        body['preferences']['reporting_style']='先给结果，再给证据和剩余工作'
        headers={**self.headers,'Idempotency-Key':'profile-save'}
        saved=self.client.post(path,headers=headers,json=body)
        self.assertEqual(200,saved.status_code,saved.text)
        self.assertEqual(saved.json(),self.client.post(path,headers=headers,json=body).json())
        self.assertEqual(409,self.client.post(path,headers={**self.headers,'Idempotency-Key':'stale-profile'},json=body).status_code)
        self.assertEqual(body['preferences'],self.client.get(path,headers=self.headers).json()['preferences'])

    def test_terminal_websocket_rejects_origin_token_and_project(self):
        from starlette.websockets import WebSocketDisconnect
        url=self.prefix+'/terminals/missing/stream'
        for headers,path,token in [({'Origin':'https://evil.invalid'},url,self.token),
                ({'Origin':'http://127.0.0.1:8765'},url,'wrong'),
                ({'Origin':'http://127.0.0.1:8765'},'/api/v1/projects/other/terminals/missing/stream',self.token)]:
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect(path,headers={'Host':'127.0.0.1:8765',**headers}) as socket:
                    socket.send_json({'token':token})
                    socket.receive_json()

    def test_real_terminal_stream_input_resize_and_takeover(self):
        headers={**self.headers,'Idempotency-Key':'terminal-start'}
        first=self.client.post(self.prefix+'/terminals',headers=headers,json={'agent':'check'})
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(first.json(),self.client.post(self.prefix+'/terminals',headers=headers,json={'agent':'check'}).json())
        sid=first.json()['id']
        base=self.prefix+'/terminals/'+sid
        with self.client.websocket_connect(base+'/stream',headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765'}) as socket:
            socket.send_json({'token':self.token})
            text=''
            for _ in range(100):
                text+=''.join(f['text'] for f in socket.receive_json()['frames'])
                if 'TERMINAL_READY' in text:break
            self.assertIn('TERMINAL_READY',text)
            self.assertEqual(200,self.client.post(base+'/resize',headers=self.headers,json={'rows':30,'cols':100}).status_code)
            rejected=self.client.post(base+'/paste',headers=self.headers,json={'epoch':0,'text':'bad\u001b[201~','bracketed':True})
            self.assertEqual(409,rejected.status_code)
            response=self.client.post(base+'/paste',headers=self.headers,json={'epoch':0,'text':'hello','bracketed':False})
            self.assertEqual('sent_unconfirmed',response.json()['state'])
            self.assertEqual(200,self.client.post(base+'/key',headers={**self.headers,'Idempotency-Key':'submit-paste'},
                json={'epoch':0,'name':'enter'}).status_code)
            for _ in range(100):
                text+=''.join(f['text'] for f in socket.receive_json()['frames'])
                if 'ECHO:hello' in text:break
            self.assertIn('ECHO:hello',text)
            result=self.client.post(base+'/takeover',headers=self.headers,json={'owner':'human','expected_epoch':0})
            self.assertEqual('reconciling',result.json()['state'])
            rejected=self.client.post(base+'/input',headers={**self.headers,'Idempotency-Key':'stale'},json={'epoch':0,'text':'again'})
            self.assertNotEqual(200,rejected.status_code)
        self.assertEqual('delivery_unknown',self.host.core.command(response.json()['command_id'])['state'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.host = Host(root, root / 'storage')
        self.token = 'test-owner-token'
        self.app = create_app(self.host, self.token)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.prefix = '/api/v1/projects/' + self.host.project_id
        self.headers = {'Authorization': 'Bearer ' + self.token, 'Idempotency-Key': 'test'}

    def test_authentication_and_project_boundary(self):
        self.assertEqual(401, self.client.get(self.prefix + '/snapshot').status_code)
        self.assertEqual(403, self.client.get('/api/v1/projects/other/snapshot', headers=self.headers).status_code)
        self.assertEqual(200, self.client.get(self.prefix + '/snapshot', headers=self.headers).status_code)

    def test_pm_dialogue_is_authenticated_persistent_and_idempotent(self):
        path=self.prefix+'/pm/dialogue/messages'
        body={'message_id':'message_api_0001','text':'现在项目进度如何？'}
        self.assertEqual(401,self.client.post(path,json=body).status_code)
        headers={**self.headers,'Idempotency-Key':'dialogue-status'}
        first=self.client.post(path,headers=headers,json=body)
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(first.json(),self.client.post(path,headers=headers,json=body).json())
        self.assertEqual(2,len(self.client.get(self.prefix+'/pm/dialogue',headers=self.headers).json()['conversation']['items']))
        conflict=self.client.post(path,headers=headers,json={**body,'text':'different request'})
        self.assertEqual(409,conflict.status_code)

    def test_interaction_history_is_authenticated_and_paginated(self):
        path=self.prefix+'/pm/interactions'
        self.assertEqual(401,self.client.get(path).status_code)
        response=self.client.get(path,headers=self.headers)
        self.assertEqual(200,response.status_code)
        self.assertEqual(0,response.json()['requests'])
        self.assertEqual([],response.json()['items'])
        self.assertNotEqual(200,self.client.get(path+'?offset=-1',headers=self.headers).status_code)
        closures=self.prefix+'/pm/closures'
        self.assertEqual(401,self.client.get(closures).status_code)
        self.assertEqual([],self.client.get(closures,headers=self.headers).json())
        self.assertNotEqual(200,self.client.post(closures,headers=self.headers,json={}).status_code)

    def test_origin_and_host_rebinding_rejected(self):
        for headers in [{'Origin': 'https://evil.invalid'}, {'Host': 'evil.invalid:8765'}]:
            self.assertEqual(403, self.client.get(self.prefix + '/snapshot', headers={**self.headers, **headers}).status_code)

    def test_expired_token_and_agent_role_cannot_write_owner_routes(self):
        record = self.app.state.tokens.records[hashlib.sha256(self.token.encode()).hexdigest()]
        record['expires'] = time.time() - 1
        self.assertEqual(401, self.client.get(self.prefix + '/snapshot', headers=self.headers).status_code)
        record.update(expires=time.time() + 100, role='agent')
        self.assertEqual(403, self.client.get(self.prefix + '/snapshot', headers=self.headers).status_code)

    def test_task_idempotency_conflict_and_validation_do_not_echo_input(self):
        body = {'id': 'task_one', 'description': 'Implement', 'acceptance': ['unit']}
        first = self.client.post(self.prefix + '/tasks', headers=self.headers, json=body)
        second = self.client.post(self.prefix + '/tasks', headers=self.headers, json=body)
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.json(), second.json())
        conflict = self.client.post(self.prefix + '/tasks', headers=self.headers, json={**body, 'description': 'different'})
        self.assertEqual(409, conflict.status_code)
        invalid = self.client.post(self.prefix + '/tasks', headers=self.headers, json={**body, 'sender': 'PRIVATE_CANARY'})
        self.assertEqual(422, invalid.status_code)
        self.assertNotIn('PRIVATE_CANARY', invalid.text)

    def test_write_requires_key_json_and_bounded_body(self):
        response = self.client.post(self.prefix + '/tasks', headers={'Authorization': 'Bearer ' + self.token}, json={})
        self.assertEqual(400, response.status_code)
        response = self.client.post(self.prefix + '/tasks', headers=self.headers, content='plain')
        self.assertEqual(415, response.status_code)
        response = self.client.post(self.prefix + '/tasks', headers=self.headers, json={'data': 'x' * 1100000})
        self.assertEqual(413, response.status_code)

    def test_snapshot_and_event_cursor_replay(self):
        before = self.client.get(self.prefix + '/snapshot', headers=self.headers).json()['latest_seq']
        self.client.post(self.prefix + '/tasks', headers=self.headers,
                         json={'id':'task_one','description':'Test','acceptance':['unit']})
        response = self.client.get(self.prefix + '/events?once=true', headers={**self.headers, 'Last-Event-ID': str(before)})
        self.assertEqual(200, response.status_code)
        self.assertIn('TaskCreated', response.text)
        self.assertNotIn('RecoveryFinished', response.text)
        self.assertEqual(410, self.client.get(self.prefix + '/events?after_seq=999999&once=true', headers=self.headers).status_code)

    def test_pause_replay_does_not_undo_later_decision(self):
        self.client.post(self.prefix + '/pause', headers=self.headers, json={'paused': True})
        self.client.post(self.prefix + '/pause', headers={**self.headers, 'Idempotency-Key':'second'}, json={'paused': False})
        response = self.client.post(self.prefix + '/pause', headers=self.headers, json={'paused': True})
        self.assertEqual({'paused': True}, response.json())
        self.assertFalse(self.host.paused)

    def test_start_session_replay_does_not_start_second_process(self):
        self.host.start = AsyncMock(return_value={'session_id': 's'})
        for _ in range(2):
            response = self.client.post(self.prefix + '/sessions', headers=self.headers, json={'agent':'claude'})
            self.assertEqual(200, response.status_code)
        self.assertEqual(1, self.host.start.await_count)

    def test_public_output_gap_is_explicit(self):
        for n in range(2005):
            self.host._output('s', 'text', text=str(n))
        result = self.client.get(self.prefix + '/output?after=0', headers=self.headers).json()
        self.assertTrue(result['gap'])
        self.assertEqual(2000, len(result['events']))

    def test_workspace_result_survives_app_restart_and_conflicting_retry(self):
        self.host.workspaces.create = Mock(return_value={'id': 'workspace_fixture'})
        body = {'head_sha': 'a' * 40}
        first = self.client.post(self.prefix + '/workspaces', headers=self.headers, json=body)
        self.assertEqual(200, first.status_code)
        restarted=Host(self.host.root,self.host.storage)
        restarted.workspaces.create=Mock(side_effect=AssertionError('Replay must not create another workspace'))
        other = create_app(restarted, self.token)
        with TestClient(other, base_url='http://127.0.0.1:8765') as client:
            replay = client.post(self.prefix + '/workspaces', headers=self.headers, json=body)
            self.assertEqual(first.json(), replay.json())
            conflict = client.post(self.prefix + '/workspaces', headers=self.headers,
                                   json={'head_sha': 'b' * 40})
            self.assertEqual(409, conflict.status_code)
        self.host.workspaces.create.assert_called_once()

    def test_failed_external_operation_is_not_repeated(self):
        from autocoding.core import DomainError
        self.host.start = AsyncMock(side_effect=DomainError('START_UNKNOWN'))
        first = self.client.post(self.prefix + '/sessions', headers=self.headers, json={'agent':'claude'})
        second = self.client.post(self.prefix + '/sessions', headers=self.headers, json={'agent':'claude'})
        self.assertEqual(409, first.status_code)
        self.assertEqual('OPERATION_RECONCILIATION_REQUIRED', second.json()['error']['code'])
        self.assertEqual(1, self.host.start.await_count)

    def test_interrupt_retry_does_not_take_control_twice(self):
        from types import SimpleNamespace
        self.host.core.create_session('s', 'fixture', key='s')
        runtime = SimpleNamespace(takeover=AsyncMock(return_value={'epoch':1}), close=AsyncMock())
        self.host.runtimes['s'] = runtime
        for _ in range(2):
            response = self.client.post(self.prefix + '/sessions/s/interrupt', headers=self.headers, json={})
            self.assertEqual({'epoch':1}, response.json())
        self.assertEqual(1, runtime.takeover.await_count)

    def test_pm_plan_requires_explicit_adoption_and_replays(self):
        from autocoding.planner import save_proposal
        tasks=[{'id':'task_plan','description':'Planned task','acceptance':['unit'],'dependencies':[]}]
        proposal=save_proposal(self.host.core,tasks,[],self.host.redactor)
        self.host.propose=AsyncMock(return_value=proposal)
        response=self.client.post(self.prefix+'/pm/proposals',headers=self.headers,
            json={'goal':'Implement','acceptance':['unit']})
        self.assertEqual(200,response.status_code)
        self.assertEqual([],self.host.core.snapshot()['tasks'])
        for _ in range(2):
            response=self.client.post(self.prefix+'/pm/proposals/'+proposal['id']+'/adopt',
                headers={**self.headers,'Idempotency-Key':'adopt'},json={})
            self.assertEqual(200,response.status_code)
        self.assertEqual(1,len(self.host.core.snapshot()['tasks']))

    def test_task_and_batch_launch_idempotency(self):
        self.host.workflow.start=AsyncMock(return_value={'id':'run_fixture','state':'preparing'})
        self.host.coordinator.start=AsyncMock(return_value={'id':'batch_fixture','state':'queued'})
        for _ in range(2):
            self.assertEqual(200,self.client.post(self.prefix+'/tasks/task_a/runs',headers=self.headers,json={'agent':'codex'}).status_code)
            self.assertEqual(200,self.client.post(self.prefix+'/batches',headers=self.headers,json={'task_ids':['task_a'],'agent':'opencode'}).status_code)
        self.assertEqual(1,self.host.workflow.start.await_count)
        self.assertEqual(1,self.host.coordinator.start.await_count)
        self.assertEqual('codex',self.host.workflow.start.call_args.kwargs['agent'])
        self.assertEqual('opencode',self.host.coordinator.start.call_args.kwargs['agent'])

    def test_question_answer_retry_does_not_send_twice(self):
        self.host.questions.answer=AsyncMock(return_value={'state':'delivered'})
        for _ in range(2):
            response=self.client.post(self.prefix+'/questions/q/answer',headers=self.headers,json={'answers':[['custom text']]})
            self.assertEqual(200,response.status_code)
        self.assertEqual(1,self.host.questions.answer.await_count)


if __name__ == '__main__':
    unittest.main()
