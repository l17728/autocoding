import json
from pathlib import Path
import sys
import tempfile
import unittest
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.api import create_app
from autocoding.host import Host
from autocoding.projects import Projects
from autocoding.workspaces import git


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'.local/workbench')
        self.registry=Projects(self.host)
        self.client=TestClient(create_app(self.host,'owner',registry=self.registry),base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.headers={'Authorization':'Bearer owner','Origin':'http://127.0.0.1:8765'}
        self.counter=0

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def post(self,path,data,key=None):
        self.counter+=1
        return self.client.post(path,json=data,headers={**self.headers,'Idempotency-Key':key or str(self.counter)})

    def test_repeated_project_lookup_does_not_rewrite_unchanged_settings(self):
        self.registry.host(self.host.project_id)
        with self.host.core.connection() as conn:
            version=conn.execute('PRAGMA data_version').fetchone()[0]
            for _ in range(10):
                self.registry.host(self.host.project_id)
            self.assertEqual(version,conn.execute('PRAGMA data_version').fetchone()[0])

    def test_knowledge_resolution_auth_idempotency_and_revision(self):
        reflection=self.host.reflection
        for rid,content in [('first','Run tests'),('second','Skip tests')]:
            reflection._save('pm_review:'+rid,{'id':rid,'created_at':self.host.core.clock(),
                'state':'completed','archived':False,'items':[{'category':'lesson','topic':'tests',
                'content':content,'confidence':'hypothesis','evidence_ids':['fixture']}]})
        group=reflection.learning.catalogue()[0]
        path='/api/v1/projects/'+self.host.project_id+'/pm/knowledge/'+group['id']+'/resolve'
        body={'selected':group['variants'][0]['id'],'revision':group['revision']}
        self.assertEqual(401,self.client.post(path,json=body,headers={'Idempotency-Key':'unauth'}).status_code)
        result=self.post(path,body,key='knowledge')
        self.assertEqual(200,result.status_code,result.text)
        self.assertEqual(result.json(),self.post(path,body,key='knowledge').json())
        self.assertEqual(1,len(reflection.references()))
        self.assertNotEqual(200,self.post(path,{**body,'revision':'0'*24}).status_code)
        events=[e for e in self.host.core.events() if e['type']=='PMReviewKnowledgeResolved']
        self.assertEqual(1,len(events))

    def test_existing_repository_preserved_project_scoping_and_restart_catalogue(self):
        repo=self.root/'existing';repo.mkdir()
        (repo/'app.py').write_text('value=42\n')
        git(repo,'init');git(repo,'add','app.py')
        git(repo,'-c','user.name=Fixture','-c','user.email=test@localhost','commit','-m','existing')
        head=git(repo,'rev-parse','HEAD')
        data={'name':'Existing','root':str(repo),'github_owner':'example-team','github_repository':'app'}
        first=self.post('/api/v1/projects',data,key='register')
        self.assertEqual(200,first.status_code,first.text)
        self.assertEqual(first.json(),self.post('/api/v1/projects',data,key='register').json())
        project=first.json();prefix='/api/v1/projects/'+project['id']
        self.assertEqual('bypass',self.registry.host(project['id']).permission_mode())
        self.assertEqual(head,git(repo,'rev-parse','HEAD'))
        self.assertEqual('',git(repo,'status','--porcelain'))
        self.assertFalse((repo/'.local').exists())
        response=self.post(prefix+'/tasks',{'id':'task_local','description':'Increment','acceptance':['unit']})
        self.assertEqual(200,response.status_code,response.text)
        own=self.client.get(prefix+'/snapshot',headers=self.headers).json()
        self.assertEqual('task_local',own['tasks'][0]['id'])
        self.assertEqual([],self.host.core.snapshot()['tasks'])
        self.assertEqual(401,self.client.get(prefix+'/snapshot').status_code)
        self.assertEqual(403,self.client.get(prefix+'/snapshot',headers={**self.headers,'Origin':'https://evil.invalid'}).status_code)
        saved=self.post(prefix+'/settings',{'name':'Renamed','github_owner':'new-owner','github_repository':'new-repo','permission_mode':'review','revision':1})
        self.assertEqual(200,saved.status_code,saved.text)
        self.assertEqual('review',self.registry.host(project['id']).permission_mode())
        stale=self.post(prefix+'/settings',{'name':'Old','revision':1})
        self.assertEqual('STALE_PROJECT_SETTINGS',stale.json()['error']['code'])
        self.assertEqual('Renamed',Projects(self.host).get(project['id'])['name'])
        (repo/'app.py').write_text('value=43\n')
        directory=self.client.get(prefix+'/directory',headers=self.headers).json()
        self.assertTrue(directory['dirty']);self.assertFalse(directory['can_execute'])

    def test_empty_creation_and_nonempty_initialization_rejected(self):
        target=self.root/'new'
        result=self.post('/api/v1/projects',{'name':'New','root':str(target),'initialize_git':True})
        self.assertEqual(200,result.status_code,result.text)
        self.assertTrue(git(target,'rev-parse','HEAD'))
        occupied=self.root/'occupied';occupied.mkdir();(occupied/'notes.txt').write_text('keep')
        result=self.post('/api/v1/projects',{'name':'Keep','root':str(occupied),'initialize_git':True})
        self.assertEqual('GIT_INITIALIZATION_REQUIRES_EMPTY_DIRECTORY',result.json()['error']['code'])
        self.assertFalse((occupied/'.git').exists())
        self.assertEqual('keep',(occupied/'notes.txt').read_text())
        self.assertEqual(200,self.post('/api/v1/projects',{'name':'Keep','root':str(occupied)}).status_code)

    def test_invalid_metadata_does_not_create_directory(self):
        target=self.root/'bad'
        result=self.post('/api/v1/projects',{'name':'Bad','root':str(target),'github_owner':'owner','github_repository':'../repo'})
        self.assertEqual('INVALID_GITHUB_REPOSITORY',result.json()['error']['code'])
        self.assertFalse(target.exists())
        self.assertEqual('ABSOLUTE_PROJECT_PATH_REQUIRED',self.post('/api/v1/projects/inspect',{'root':'relative'}).json()['error']['code'])
