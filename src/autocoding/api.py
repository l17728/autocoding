"""Single-project loopback API with bearer authentication and Origin checking."""
import asyncio
import base64
import sqlite3
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .core import DomainError, digest
from .global_history import GlobalHistory
from .operations import external_once
from .providers import ProviderError
from .transport import TransportError


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class TaskInput(Body):
    id: str = Field(pattern=r'^task_[A-Za-z0-9_-]+$', max_length=100)
    description: str = Field(min_length=1, max_length=10000)
    acceptance: list[str] = Field(min_length=1, max_length=50)
    dependencies: list[str] = Field(default_factory=list, max_length=100)

class SessionInput(Body):
    agent: str
    workspace_id: str | None = None
    tools: bool = False
    resume_from: str | None = None

class TerminalInput(Body):
    agent: str = Field(pattern='^(check|claude|codex|opencode)$')
    resume_from: str | None = Field(default=None, max_length=100)
    startup_attempts: int = Field(default=3,ge=1,le=5)

class TerminalSize(Body):
    rows: int = Field(ge=2, le=200)
    cols: int = Field(ge=10, le=400)

class TerminalKey(Body):
    epoch: int = Field(ge=0)
    name: str = Field(max_length=20)

class QuestionAnswerInput(Body):
    answers: list[list[str]] = Field(min_length=1,max_length=10)

class CommandInput(Body):
    epoch: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=65536)

class TerminalPaste(CommandInput):
    bracketed: bool = False

class TakeoverInput(Body):
    expected_epoch: int = Field(ge=0)
    owner: str = Field(pattern='^(human|pm)$')

class DecisionInput(Body):
    decision: str = Field(pattern='^(approved|denied)$')
    operation_hash: str = Field(pattern='^[a-f0-9]{64}$')
    expected_revision: int = Field(ge=0)

class WorkspaceInput(Body):
    head_sha: str = Field(pattern='^([a-f0-9]{40}|[a-f0-9]{64})$')

class PauseInput(Body):
    paused: bool

class PlanInput(Body):
    goal: str = Field(min_length=1, max_length=10000)
    acceptance: list[str] = Field(min_length=1, max_length=50)

class PMDialogueInput(Body):
    message_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    text: str = Field(min_length=1, max_length=12000)

class PMProfileInput(Body):
    revision: int = Field(ge=0)
    preferences: dict[str,str]

class DelegateInput(PlanInput):
    agent: str = Field(default='codex',pattern='^(claude|codex|opencode)$')
    agents: list[str] | None = Field(default=None,min_length=1,max_length=3)
    max_concurrency: int = Field(default=1,ge=1,le=3)
    max_attempts: int = Field(default=2,ge=1,le=3)

class RunInput(Body):
    agent: str = Field(default='claude', pattern='^(claude|codex|opencode)$')
    base_sha: str | None = Field(default=None, pattern=r'^([a-f0-9]{40}|[a-f0-9]{64})$')
    max_seconds: int = Field(default=1800, ge=1, le=3600)

class RecoveryInput(Body):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
    note: str = Field(min_length=3, max_length=2000)
    stopped: bool
    accept_missing_workspace: bool = False

class AcceptanceInput(Body):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
    config: dict

class RegistrationRepairInput(Body):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
    stopped: bool

class CoverageInput(Body):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
    requirements: list[dict] = Field(max_length=500)

class ExpertSettings(Body):
    enabled: bool

class EvaluationSuiteInput(Body):
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')
    cases: list[dict] = Field(min_length=2,max_length=40)

class HoldoutSceneInput(Body):
    source: str = Field(min_length=1,max_length=1000)
    prompt: str = Field(min_length=1,max_length=4000)
    scope: str = Field(pattern='^(inside|outside)$')

class HoldoutAnswerInput(Body):
    expected: dict

class HoldoutReviewInput(Body):
    revision: int = Field(ge=0)
    decision: str = Field(pattern='^(accepted|rejected|contaminated)$')
    note: str = Field(min_length=10,max_length=2000)

class HoldoutExportInput(Body):
    sample_ids: list[str] = Field(min_length=2,max_length=40)

class HoldoutInstallInput(Body):
    export_id: str = Field(min_length=1,max_length=100)
    fingerprint: str = Field(pattern='^[a-f0-9]{64}$')

class ExpertRunInput(Body):
    batch_id: str = Field(min_length=1,max_length=100)

class ExpertDisposition(Body):
    state: str = Field(pattern='^(open|accepted|dismissed)$')
    note: str = Field(min_length=3,max_length=2000)
    task_id: str | None = Field(default=None,max_length=100)

class BatchInput(Body):
    agents: list[str] | None = Field(default=None,min_length=1,max_length=3)
    max_concurrency: int = Field(default=1,ge=1,le=3)
    agent: str = Field(default='claude', pattern='^(claude|codex|opencode)$')
    task_ids: list[str] = Field(min_length=1, max_length=100)
    max_attempts: int = Field(default=2, ge=1, le=3)

class ProjectSettings(Body):
    name: str = Field(min_length=1,max_length=100)
    github_owner: str = Field(default='',max_length=39)
    github_repository: str = Field(default='',max_length=100)
    permission_mode: str = Field(default='bypass',pattern='^(bypass|review)$')
    revision: int = Field(default=1,ge=1)

class ProjectCreate(ProjectSettings):
    root: str = Field(min_length=1,max_length=2048)
    initialize_git: bool = False

class ProjectDelete(Body):
    confirmation: str = Field(min_length=1,max_length=100)

class DirectoryInput(Body):
    root: str = Field(min_length=1,max_length=2048)


class DesignDocumentInput(Body):
    filename: str = Field(min_length=1, max_length=180)
    content_base64: str = Field(min_length=1, max_length=900000)

class ReviewSettings(Body):
    enabled: bool
    interval_minutes: int = Field(ge=5,le=10080)

class ArchiveReview(Body):
    archived: bool

class KnowledgeResolution(Body):
    selected: str = Field(min_length=24,max_length=24)
    revision: str = Field(min_length=24,max_length=24)


class Tokens:
    def __init__(self, owner_token, project_id):
        self.project_id = project_id
        self.records = {hashlib.sha256(owner_token.encode()).hexdigest(): {'role': 'owner', 'subject': 'owner',
                         'project_id': project_id, 'expires': time.time() + 12 * 3600}}

    def authenticate(self, value):
        if not value or not value.startswith('Bearer '):
            raise DomainError('UNAUTHORIZED')
        digest = hashlib.sha256(value[7:].encode()).hexdigest()
        principal = next((p for token_hash, p in self.records.items() if hmac.compare_digest(digest, token_hash)), None)
        if not principal or principal['expires'] <= time.time():
            raise DomainError('UNAUTHORIZED')
        return principal


def create_app(host, owner_token, *, origin='http://127.0.0.1:8765', ui=None, registry=None, child=False):
    tokens = Tokens(owner_token, host.project_id)
    global_history = GlobalHistory(registry.default if registry else host)
    @asynccontextmanager
    async def lifespan(app):
        from .windows_disconnect import install
        # Keep WAL open without holding a transaction. Avoid checkpointing and
        # deleting the registry WAL after every polling request on Windows.
        with host.core.connection() as anchor:
            anchor.execute('SELECT 1 FROM metadata LIMIT 1').fetchone()
            restore_disconnect_handler = install()
            try:
                if registry:
                    await registry.start()
                else:
                    host.recover()
                    host.start_services()
                yield
            finally:
                try:
                    if registry:
                        await registry.close()
                    else:
                        await host.close()
                finally:
                    restore_disconnect_handler()

    app = FastAPI(title='开发智能体协作平台', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.host, app.state.tokens = host, tokens
    if registry and not child:
        from .projects import ProjectDispatch
        registry.tokens=tokens
        app.add_middleware(ProjectDispatch,registry=registry,token=owner_token,origin=origin,ui=ui)

    def error(code, status=409):
        return JSONResponse({'error': {'code': code, 'retryable': False, 'trace_id': 'tr_' + uuid.uuid4().hex}}, status_code=status)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        status = {'UNAUTHORIZED': 401, 'FORBIDDEN': 403, 'NOT_FOUND': 404, 'INPUT_TOO_LARGE': 413}.get(exc.code, 409)
        return error(exc.code, status)

    @app.exception_handler(TransportError)
    async def transport_error(request, exc):
        return error(exc.code, 502)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return error(str(exc), 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors can include the submitted input; never echo them.
        return error('INVALID_REQUEST', 422)

    @app.middleware('http')
    async def guard(request, call_next):
        request_started=time.monotonic()
        if request.headers.get('host') != origin.split('://', 1)[1]:
            return error('HOST_REJECTED', 403)
        if request.headers.get('origin') not in (None, origin):
            return error('ORIGIN_REJECTED', 403)
        if request.url.path.startswith('/api/'):
            try:
                request.state.principal = tokens.authenticate(request.headers.get('authorization'))
            except DomainError:
                return error('UNAUTHORIZED', 401)
            if request.state.principal['role']!='owner':
                return error('FORBIDDEN',403)
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                key = request.headers.get('idempotency-key', '')
                if not 1 <= len(key) <= 200:
                    return error('IDEMPOTENCY_KEY_REQUIRED', 400)
                if request.headers.get('content-type', '').split(';')[0] != 'application/json':
                    return error('JSON_REQUIRED', 415)
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        return error('REQUEST_TOO_LARGE', 413)
                    chunks.append(chunk)
                request._body = b''.join(chunks)
                try:
                    submitted=json.loads(request._body)
                except (TypeError, ValueError):
                    submitted={'unparseable': True}
                # This is intentionally platform-wide. It preserves the Owner's
                # inputs across projects so the PM can recognize recurring goals
                # and wording; sensitive-looking fields are redacted at source.
                # History ingestion is auxiliary.  Its idempotency namespace
                # must include the submitted body so it cannot pre-empt the
                # endpoint's own, deliberately stricter idempotency conflict.
                history_key='input:'+host.project_id+':'+key+':'+digest({'path':request.url.path,'content':submitted})
                global_history.record(key=history_key, project_id=host.project_id,
                                      path=request.url.path, content=submitted, redactor=host.redactor)
        try:
            response = await call_next(request)
        except sqlite3.Error as exc:
            # Failure does not prove a write was absent. Never auto-retry or
            # expose SQL, filesystem paths, or submitted data in the response.
            code = getattr(exc, 'sqlite_errorcode', 0) & 255
            response = error('STORAGE_BUSY' if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                             else 'STORAGE_UNAVAILABLE', 503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
        elapsed=time.monotonic()-request_started
        if elapsed>=0.5:
            def record_slow_request():
                path=host.storage/'http-slow.jsonl'
                try:
                    if path.exists() and path.stat().st_size>1024*1024:
                        path.replace(path.with_suffix('.jsonl.1'))
                    with path.open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'at':time.time(),'method':request.method,
                            'path':request.url.path,'status':response.status_code,
                            'duration_ms':round(elapsed*1000,1)},ensure_ascii=False)+'\n')
                except OSError:
                    return
            await asyncio.to_thread(record_slow_request)
        return response

    def owner(request: Request, project_id: str):
        principal = request.state.principal
        if project_id != host.project_id or (not registry and principal['project_id'] != project_id) or principal['role'] != 'owner':
            raise DomainError('FORBIDDEN')
        return principal

    prefix = '/api/v1/projects/{project_id}'
    guarded = [Depends(owner)]

    @app.get('/api/v1/input-history')
    async def input_history(q: str='', offset: int=0, limit: int=30):
        return global_history.search(q, offset, limit)

    @app.get('/api/v1/projects')
    async def projects_list():
        return registry.list() if registry else [{'id':host.project_id,'name':host.root.name,'root':str(host.root)}]

    @app.post('/api/v1/projects/inspect')
    async def project_inspect(body: DirectoryInput):
        from .projects import inspect_directory
        info = await asyncio.to_thread(inspect_directory,body.root)
        if registry:
            candidate = str(Path(info['root']).resolve())
            registered = next((record for record in registry.list()
                               if str(Path(record['root']).resolve()) == candidate), None)
            if registered:
                info['registered_project'] = registered
        return info

    @app.post('/api/v1/projects')
    async def project_create(body: ProjectCreate, request: Request):
        if not registry:
            raise DomainError('PROJECT_CATALOGUE_UNAVAILABLE')
        return await external_once(registry.core,'project_register',request.headers['idempotency-key'],body.model_dump(),
                                   lambda:registry.register(body.model_dump()))

    @app.delete(prefix,dependencies=guarded)
    async def project_delete(body: ProjectDelete, request: Request):
        if not registry:
            raise DomainError('PROJECT_CATALOGUE_UNAVAILABLE')
        return await external_once(registry.core,'project_delete:'+host.project_id,request.headers['idempotency-key'],body.model_dump(),
                                   lambda:registry.remove(host.project_id,body.confirmation))

    @app.get(prefix+'/settings',dependencies=guarded)
    async def project_settings():
        if not registry:
            return {'id':host.project_id,'name':host.root.name,'root':str(host.root),'github_owner':'',
                    'github_repository':'','permission_mode':host.permission_mode(),'revision':1}
        return registry.get(host.project_id)

    @app.post(prefix+'/settings',dependencies=guarded)
    async def save_project_settings(body: ProjectSettings, request: Request):
        if not registry:
            raise DomainError('PROJECT_CATALOGUE_UNAVAILABLE')
        return await external_once(registry.core,'project_settings:'+host.project_id,request.headers['idempotency-key'],body.model_dump(),
                                   lambda:registry.update(host.project_id,body.model_dump()))

    @app.get(prefix+'/design-documents', dependencies=guarded)
    async def design_documents():
        directory = host.root / 'docs' / 'imported'
        if not directory.is_dir():
            return []
        return [{'name': item.name, 'bytes': item.stat().st_size}
                for item in sorted(directory.iterdir(), key=lambda item: item.name.casefold())
                if item.is_file() and item.suffix.casefold() in {'.md', '.txt'}]

    @app.post(prefix+'/design-documents', dependencies=guarded)
    async def design_document_upload(body: DesignDocumentInput, request: Request):
        suffix = Path(body.filename).suffix.casefold()
        name = Path(body.filename).name
        if name != body.filename or suffix not in {'.md', '.txt'}:
            raise DomainError('INVALID_DESIGN_DOCUMENT')
        try:
            content = base64.b64decode(body.content_base64, validate=True)
            content.decode('utf-8')
        except (ValueError, UnicodeDecodeError):
            raise DomainError('INVALID_DESIGN_DOCUMENT')
        if not 1 <= len(content) <= 512 * 1024:
            raise DomainError('INVALID_DESIGN_DOCUMENT')
        directory = host.root / 'docs' / 'imported'
        target = directory / name
        async def write_document():
            directory.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            added = subprocess.run(['git', 'add', '--', 'docs/imported/' + name], cwd=host.root,
                                   capture_output=True, text=True, encoding='utf-8', errors='replace')
            if added.returncode:
                raise DomainError('DESIGN_DOCUMENT_STAGE_FAILED')
            committed = subprocess.run(['git', '-c', 'user.name=Project Owner', '-c', 'user.email=owner@localhost',
                                        'commit', '--only', '-m', 'docs: import design document ' + name,
                                        '--', 'docs/imported/' + name], cwd=host.root,
                                       capture_output=True, text=True, encoding='utf-8', errors='replace')
            if committed.returncode:
                raise DomainError('DESIGN_DOCUMENT_COMMIT_FAILED')
            head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=host.root, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace')
            return {'name': name, 'bytes': len(content), 'commit': head.stdout.strip()}
        return await external_once(host.core, 'design_document:'+host.project_id+':'+name,
                                   request.headers['idempotency-key'], {'name': name, 'sha256': hashlib.sha256(content).hexdigest()},
                                   write_document)

    @app.delete(prefix+'/design-documents/{filename}', dependencies=guarded)
    async def design_document_delete(filename: str, request: Request):
        name = Path(filename).name
        if name != filename or Path(name).suffix.casefold() not in {'.md', '.txt'}:
            raise DomainError('INVALID_DESIGN_DOCUMENT')
        directory = host.root / 'docs' / 'imported'
        target = directory / name
        if not target.is_file():
            raise DomainError('NOT_FOUND')
        async def delete_document():
            relative='docs/imported/' + name
            tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', relative], cwd=host.root,
                                     capture_output=True, text=True, encoding='utf-8', errors='replace')
            if tracked.returncode:
                target.unlink()
                return {'name': name, 'deleted': True}
            removed = subprocess.run(['git', 'rm', '--', relative], cwd=host.root,
                                     capture_output=True, text=True, encoding='utf-8', errors='replace')
            if removed.returncode:
                raise DomainError('DESIGN_DOCUMENT_DELETE_FAILED')
            committed = subprocess.run(['git', '-c', 'user.name=Project Owner', '-c', 'user.email=owner@localhost',
                                        'commit', '--only', '-m', 'docs: remove design document ' + name,
                                        '--', relative], cwd=host.root,
                                       capture_output=True, text=True, encoding='utf-8', errors='replace')
            if committed.returncode:
                raise DomainError('DESIGN_DOCUMENT_COMMIT_FAILED')
            return {'name': name, 'deleted': True}
        return await external_once(host.core, 'design_document_delete:'+host.project_id+':'+name,
                                   request.headers['idempotency-key'], {'name': name}, delete_document)

    @app.get(prefix+'/acceptance',dependencies=guarded)
    async def acceptance_settings():
        from .acceptance import Acceptance
        return await asyncio.to_thread(Acceptance(host).snapshot)

    @app.post(prefix+'/acceptance',dependencies=guarded)
    async def save_acceptance(body: AcceptanceInput, request: Request):
        from .acceptance import Acceptance
        return await asyncio.to_thread(Acceptance(host).save, body.fingerprint, body.config,
                                       key=request.headers['idempotency-key'])

    @app.post(prefix+'/acceptance/bootstrap',dependencies=guarded)
    async def bootstrap_acceptance(request: Request):
        from .acceptance import Acceptance
        return await asyncio.to_thread(Acceptance(host).bootstrap,
                                       key=request.headers['idempotency-key'])

    @app.get(prefix+'/learning-evaluations', dependencies=guarded)
    async def learning_evaluations():
        from .learning_eval import LearningEval
        return await asyncio.to_thread(LearningEval(host).snapshot)

    @app.get(prefix+'/learning-evaluations/history-cases', dependencies=guarded)
    async def learning_history_cases():
        from .learning_eval import LearningEval
        return await asyncio.to_thread(LearningEval(host).history_cases)

    @app.get(prefix+'/learning-evaluations/holdouts', dependencies=guarded)
    async def holdouts():
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).snapshot)

    @app.post(prefix+'/learning-evaluations/holdouts', dependencies=guarded)
    async def holdout_create(body: HoldoutSceneInput, request: Request):
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).create,body.source,body.prompt,body.scope,
                                       key=request.headers['idempotency-key'])

    @app.post(prefix+'/learning-evaluations/holdouts/{sample_id}/answer', dependencies=guarded)
    async def holdout_answer(sample_id: str, body: HoldoutAnswerInput, request: Request):
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).answer,sample_id,body.expected,key=request.headers['idempotency-key'])

    @app.post(prefix+'/learning-evaluations/holdouts/{sample_id}/review', dependencies=guarded)
    async def holdout_review(sample_id: str, body: HoldoutReviewInput, request: Request):
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).review,sample_id,body.revision,body.decision,body.note,
                                       key=request.headers['idempotency-key'])

    @app.post(prefix+'/learning-evaluations/holdout-exports', dependencies=guarded)
    async def holdout_export(body: HoldoutExportInput, request: Request):
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).export,body.sample_ids,key=request.headers['idempotency-key'])

    @app.get(prefix+'/learning-evaluations/holdout-exports/{export_id}', dependencies=guarded)
    async def holdout_export_read(export_id: str):
        from .holdouts import Holdouts
        return await asyncio.to_thread(Holdouts(host).exported,export_id)

    @app.post(prefix+'/learning-evaluations/holdout-suite', dependencies=guarded)
    async def holdout_install(body: HoldoutInstallInput, request: Request):
        from .learning_eval import LearningEval
        return await asyncio.to_thread(LearningEval(host).install_holdouts,body.export_id,body.fingerprint,
                                       key=request.headers['idempotency-key'])

    @app.post(prefix+'/learning-evaluations/suite', dependencies=guarded)
    async def learning_suite(body: EvaluationSuiteInput,request: Request):
        from .learning_eval import LearningEval
        return await asyncio.to_thread(LearningEval(host).save_suite,body.fingerprint,body.cases,key=request.headers['idempotency-key'])

    @app.post(prefix+'/learning-evaluations/run', dependencies=guarded)
    async def learning_evaluation_run(request: Request):
        from .learning_eval import LearningEval
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await external_once(host.core,'learning_evaluation',request.headers['idempotency-key'],{},LearningEval(host).run)

    @app.get(prefix+'/expert-reviews', dependencies=guarded)
    async def expert_reviews():
        from .expert_review import ExpertReview
        return await asyncio.to_thread(ExpertReview(host).list)

    @app.post(prefix+'/expert-reviews/settings', dependencies=guarded)
    async def expert_settings(body: ExpertSettings, request: Request):
        from .expert_review import ExpertReview
        return await asyncio.to_thread(ExpertReview(host).configure,body.enabled,key=request.headers['idempotency-key'])

    @app.post(prefix+'/expert-reviews', dependencies=guarded)
    async def expert_run(body: ExpertRunInput, request: Request):
        from .expert_review import ExpertReview
        batch=next((b for b in host.coordinator.list() if b['id']==body.batch_id),None)
        if not batch or batch['state']!='verified_candidate':
            raise DomainError('EXPERT_REVIEW_REQUIRES_VERIFIED_CANDIDATE')
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await external_once(host.core,'expert_review',request.headers['idempotency-key'],body.model_dump(),
                                   lambda:ExpertReview(host).run(batch))

    @app.post(prefix+'/expert-reviews/{review_id}/findings/{finding_id}', dependencies=guarded)
    async def expert_disposition(review_id: str,finding_id: str,body: ExpertDisposition,request: Request):
        from .expert_review import ExpertReview
        return await asyncio.to_thread(ExpertReview(host).disposition,review_id,finding_id,body.state,body.note,body.task_id,
                                       key=request.headers['idempotency-key'])

    @app.get(prefix+'/coverage', dependencies=guarded)
    async def coverage():
        from .coverage import Coverage
        return await asyncio.to_thread(Coverage(host).snapshot)

    @app.post(prefix+'/coverage', dependencies=guarded)
    async def save_coverage(body: CoverageInput, request: Request):
        from .coverage import Coverage
        return await asyncio.to_thread(Coverage(host).save, body.fingerprint, body.requirements,
                                       key=request.headers['idempotency-key'])

    @app.get(prefix+'/directory',dependencies=guarded)
    async def directory_state():
        from .projects import inspect_directory
        return await asyncio.to_thread(inspect_directory,str(host.root))

    @app.get(prefix+'/pm/reviews',dependencies=guarded)
    async def pm_reviews(offset: int=0):
        return await asyncio.to_thread(host.reflection.snapshot,offset)

    @app.get(prefix+'/pm/interactions',dependencies=guarded)
    async def pm_interactions(offset: int=0, limit: int=25):
        return await asyncio.to_thread(host.interactions.snapshot,offset,limit)

    @app.get(prefix+'/pm/dialogue',dependencies=guarded)
    async def pm_dialogue(offset: int=0, limit: int=100):
        return {'conversation':host.pm_dialogue.list(offset,limit),'status':host.pm_dialogue.status()}

    @app.post(prefix+'/pm/dialogue/messages',dependencies=guarded)
    async def pm_dialogue_message(body: PMDialogueInput, request: Request):
        return await external_once(host.core,'pm_dialogue_submit',request.headers['idempotency-key'],
            body.model_dump(),lambda:host.pm_dialogue.submit(body.message_id,body.text))

    @app.get(prefix+'/pm/closures',dependencies=guarded)
    async def pm_closures():
        return await asyncio.to_thread(host.closure.list)

    @app.post(prefix+'/pm/closures',dependencies=guarded)
    async def create_pm_closure(request: Request):
        async def effect():
            batches=host.coordinator.list()
            batch=next((b for b in reversed(batches) if b['state']=='verified_candidate'),None)
            if not batch:
                raise DomainError('PROJECT_CLOSURE_REQUIRES_VERIFIED_INTEGRATION')
            result=await asyncio.to_thread(host.closure.create,batch)
            if result is None:
                raise DomainError('PROJECT_CLOSURE_HAS_UNFINISHED_WORK')
            return result
        return await external_once(host.core,'project_closure',request.headers['idempotency-key'],{},effect)

    @app.post(prefix+'/pm/knowledge/{group_id}/resolve',dependencies=guarded)
    async def resolve_knowledge(group_id: str, body: KnowledgeResolution, request: Request):
        async def effect():
            return await asyncio.to_thread(host.reflection.learning.resolve,group_id,body.selected,body.revision)
        return await external_once(host.core,'pm_knowledge_resolve',request.headers['idempotency-key'],
                                   {'group_id':group_id,**body.model_dump()},effect)

    @app.get(prefix+'/pm/knowledge',dependencies=guarded)
    async def knowledge_page(offset: int=0, limit: int=25):
        return await asyncio.to_thread(host.reflection.learning.page,offset,limit)

    @app.get(prefix+'/pm/profile',dependencies=guarded)
    async def pm_profile():
        return await asyncio.to_thread(host.pm_profile.snapshot)

    @app.post(prefix+'/pm/profile',dependencies=guarded)
    async def save_pm_profile(body: PMProfileInput,request: Request):
        return await asyncio.to_thread(host.pm_profile.save,body.revision,body.preferences,
                                       key=request.headers['idempotency-key'])

    @app.post(prefix+'/pm/reviews',dependencies=guarded)
    async def pm_review(request: Request):
        return await external_once(host.core,'pm_review',request.headers['idempotency-key'],{},lambda:host.reflection.run())

    @app.post(prefix+'/pm/reviews/settings',dependencies=guarded)
    async def pm_review_settings(body: ReviewSettings, request: Request):
        async def effect():
            return host.reflection.configure(body.enabled,body.interval_minutes)
        return await external_once(host.core,'pm_review_settings',request.headers['idempotency-key'],body.model_dump(),effect)

    @app.post(prefix+'/pm/reviews/{review_id}/archive',dependencies=guarded)
    async def pm_review_archive(review_id: str, body: ArchiveReview, request: Request):
        async def effect():
            return host.reflection.archive(review_id,body.archived)
        return await external_once(host.core,'pm_review_archive',request.headers['idempotency-key'],
                                   {'review_id':review_id,**body.model_dump()},effect)

    @app.get(prefix + '/terminals', dependencies=guarded)
    async def terminals():
        return host.terminals.snapshot()

    @app.post(prefix + '/terminals', dependencies=guarded)
    async def terminal_start(body: TerminalInput, request: Request):
        return await external_once(host.core, 'terminal_start', request.headers['idempotency-key'],
                                   body.model_dump(), lambda:host.terminals.start(body.agent,resume_from=body.resume_from,startup_attempts=body.startup_attempts))

    @app.post(prefix + '/terminals/{sid}/input', dependencies=guarded)
    async def terminal_input(sid: str, body: CommandInput, request: Request):
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await host.terminals.get(sid).write(body.text, 'human', body.epoch,
                                                  key=request.headers['idempotency-key'])

    @app.post(prefix + '/terminals/{sid}/response', dependencies=guarded)
    async def terminal_response(sid: str, body: CommandInput, request: Request):
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await host.terminals.get(sid).reply(body.text,'human',body.epoch,key=request.headers['idempotency-key'])

    @app.post(prefix + '/terminals/{sid}/paste', dependencies=guarded)
    async def terminal_paste(sid: str, body: TerminalPaste, request: Request):
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await host.terminals.get(sid).paste(body.text,'human',body.epoch,
            bracketed=body.bracketed,key=request.headers['idempotency-key'])

    @app.post(prefix + '/terminals/{sid}/resize', dependencies=guarded)
    async def terminal_resize(sid: str, body: TerminalSize):
        await host.terminals.get(sid).resize(body.rows, body.cols)
        return {'state':'resized'}

    @app.post(prefix + '/terminals/{sid}/key', dependencies=guarded)
    async def terminal_key(sid: str, body: TerminalKey, request: Request):
        if host.paused:
            raise DomainError('PROJECT_PAUSED')
        return await host.terminals.get(sid).press(body.name, 'human', body.epoch,
                                                  key=request.headers['idempotency-key'])

    @app.post(prefix + '/terminals/{sid}/takeover', dependencies=guarded)
    async def terminal_takeover(sid: str, body: TakeoverInput, request: Request):
        return await host.terminals.get(sid).takeover(body.owner, body.expected_epoch,
                                                     key=request.headers['idempotency-key'])

    @app.post(prefix + '/terminals/{sid}/stop', dependencies=guarded)
    async def terminal_stop(sid: str, request: Request):
        return await external_once(host.core, 'terminal_stop', request.headers['idempotency-key'],
                                   {'session':sid}, lambda:host.terminals.stop(sid))

    @app.websocket(prefix + '/terminals/{sid}/stream')
    async def terminal_stream(socket: WebSocket, project_id: str, sid: str):
        # Browser WebSockets cannot set Authorization. Authenticate in the
        # first bounded frame, never a URL/query that can enter access logs.
        if (socket.headers.get('host') != origin.split('://',1)[1]
                or socket.headers.get('origin') != origin):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=5)
            if len(raw) > 4096:
                raise DomainError('UNAUTHORIZED')
            auth = json.loads(raw)
            principal = tokens.authenticate('Bearer '+auth.get('token',''))
            if principal['role']!='owner' or project_id!=host.project_id or (not registry and principal['project_id']!=project_id):
                raise DomainError('FORBIDDEN')
            host.terminals.get(sid)
            cursor = 0
            while principal['expires'] > time.time():
                entry = host.terminals.output[sid]
                changed = entry['changed']
                frames = list(entry['frames'])
                sent_cursor = frames[-1]['seq'] if frames else cursor
                closed = (host.terminals.sessions[sid]._closed and
                          host.terminals.records[sid].get('startup_state') not in {'starting','retrying'})
                previously_streamed = entry.get('streamed_seq',0)
                # Reconnecting viewers may redraw history, but must not answer
                # old terminal queries again (ESC replies can interrupt turns).
                entry['streamed_seq']=max(previously_streamed,sent_cursor)
                await asyncio.wait_for(socket.send_json({'frames':[{**f,'replay':f['seq']<=previously_streamed} for f in frames if f['seq']>cursor],
                    'gap':bool(frames and cursor<frames[0]['seq']-1),
                    'closed':closed}), timeout=5)
                # Sending yields: the producer may have appended newer frames.
                # Advance only past the captured batch, never unsent output.
                cursor = sent_cursor
                if closed:
                    break
                try:
                    await asyncio.wait_for(changed.wait(),timeout=1)
                except TimeoutError:
                    pass
            await socket.close(code=1000)
        except WebSocketDisconnect:
            pass
        except (DomainError, ValueError, TypeError, AttributeError, TimeoutError):
            await socket.close(code=1008)

    @app.post(prefix + '/pm/proposals', dependencies=guarded)
    async def propose(body: PlanInput, request: Request):
        if any(not value.strip() or len(value) > 1000 for value in body.acceptance):
            raise DomainError('ACCEPTANCE_REQUIRED')
        return await external_once(host.core, 'pm_propose', request.headers['idempotency-key'],
            body.model_dump(), lambda: host.propose(body.goal, body.acceptance))

    @app.post(prefix+'/pm/delegate',dependencies=guarded)
    async def pm_delegate(body: DelegateInput,request: Request):
        from .delegation import delegate
        from .acceptance import Acceptance
        await asyncio.to_thread(Acceptance(host).bootstrap,
                                key='delegate-bootstrap:'+request.headers['idempotency-key'])
        return await external_once(host.core,'pm_delegate',request.headers['idempotency-key'],body.model_dump(),
            lambda:delegate(host,body.goal,body.acceptance,agent=body.agent,agents=body.agents,
                            max_concurrency=body.max_concurrency,max_attempts=body.max_attempts))

    @app.get(prefix+'/pm/delegations',dependencies=guarded)
    async def pm_delegations():
        from .delegation import records
        return await asyncio.to_thread(records,host)

    @app.post(prefix + '/pm/proposals/{proposal_id}/adopt', dependencies=guarded)
    async def adopt_proposal(proposal_id: str, request: Request):
        from .planner import adopt
        async def effect():
            return adopt(host.core, proposal_id, key='adopt:' + request.headers['idempotency-key'])
        return await external_once(host.core, 'pm_adopt', request.headers['idempotency-key'],
                                   {'proposal_id':proposal_id}, effect)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'version': '0.2.0'}

    @app.get('/api/v1/project')
    async def project(request: Request):
        return registry.get(host.project_id) if registry else {'id': host.project_id, 'name': host.root.name, 'root': str(host.root)}

    @app.get(prefix + '/snapshot', dependencies=guarded)
    async def snapshot(task_offset: int = 0):
        from .dashboard import Dashboard
        return await asyncio.to_thread(Dashboard(host).snapshot, task_offset)

    @app.get(prefix + '/liveness', dependencies=guarded)
    async def liveness():
        from .dashboard import Dashboard
        return await asyncio.to_thread(Dashboard(host).liveness)

    @app.get(prefix+'/history/{kind}', dependencies=guarded)
    async def history(kind: str,offset: int = 0,limit: int = 100):
        from .dashboard import Dashboard
        return await asyncio.to_thread(Dashboard(host).history,kind,offset,limit)

    @app.post(prefix + '/tasks', dependencies=guarded)
    async def task(body: TaskInput, request: Request):
        return await asyncio.to_thread(host.core.create_task, body.id, body.description, body.acceptance,
                                       body.dependencies, key=request.headers['idempotency-key'])

    @app.post(prefix + '/tasks/{task_id}/runs', dependencies=guarded)
    async def run_task(task_id: str, body: RunInput, request: Request):
        from .acceptance import Acceptance
        await asyncio.to_thread(Acceptance(host).bootstrap,
                                key='run-bootstrap:'+request.headers['idempotency-key'])
        return await external_once(host.core, 'task_run_start', request.headers['idempotency-key'],
            {'task_id':task_id, **body.model_dump()},
            lambda: host.workflow.start(task_id, base_sha=body.base_sha, max_seconds=body.max_seconds, agent=body.agent))

    @app.get(prefix + '/runs/{run_id}/registration', dependencies=guarded)
    async def registration_evidence(run_id: str):
        from .recovery import Recovery
        return await asyncio.to_thread(Recovery(host).registration,run_id)

    @app.post(prefix + '/runs/{run_id}/registration', dependencies=guarded)
    async def repair_registration(run_id: str, body: RegistrationRepairInput, request: Request):
        from .recovery import Recovery
        return await external_once(host.core,'repair_registration',request.headers['idempotency-key'],
            {'run_id':run_id,**body.model_dump()},
            lambda:asyncio.to_thread(Recovery(host).registration,run_id,body.fingerprint,body.stopped))

    @app.get(prefix + '/runs/{run_id}/recovery', dependencies=guarded)
    async def recovery_evidence(run_id: str):
        from .recovery import Recovery
        return await asyncio.to_thread(Recovery(host).inspect, run_id)

    @app.post(prefix + '/runs/{run_id}/recovery', dependencies=guarded)
    async def recovery_resolve(run_id: str, body: RecoveryInput, request: Request):
        from .recovery import Recovery
        result = await asyncio.to_thread(Recovery(host).resolve, run_id, **body.model_dump(),
                                         key=request.headers['idempotency-key'])
        # Recovery's ledger write is already idempotent. Re-enter the PM loop
        # after every successful/replayed receipt so a crash between that write
        # and this wake-up cannot strand work until the next service restart.
        if result.get('state') == 'todo':
            host.coordinator.resume_after_recovery()
        return result

    @app.post(prefix + '/batches', dependencies=guarded)
    async def batch(body: BatchInput, request: Request):
        from .acceptance import Acceptance
        await asyncio.to_thread(Acceptance(host).bootstrap,
                                key='batch-bootstrap:'+request.headers['idempotency-key'])
        return await external_once(host.core,'batch_start',request.headers['idempotency-key'],body.model_dump(),
            lambda:host.coordinator.start(body.task_ids,max_attempts=body.max_attempts,agent=body.agent,
                                          agents=body.agents,max_concurrency=body.max_concurrency))

    @app.post(prefix + '/sessions', dependencies=guarded)
    async def session(body: SessionInput, request: Request):
        return await external_once(host.core, 'session_start', request.headers['idempotency-key'],
            body.model_dump(), lambda: host.start(body.agent, workspace_id=body.workspace_id, tools=body.tools,resume_from=body.resume_from))

    @app.post(prefix + '/questions/{qid}/answer', dependencies=guarded)
    async def answer_question(qid: str, body: QuestionAnswerInput, request: Request):
        return await external_once(host.core,'question_answer',request.headers['idempotency-key'],
            {'id':qid,**body.model_dump()},lambda:host.questions.answer(qid,body.answers))

    @app.post(prefix + '/sessions/{sid}/commands', dependencies=guarded)
    async def command(sid: str, body: CommandInput, request: Request):
        return await host.send(sid, body.epoch, body.text, key=request.headers['idempotency-key'])

    @app.post(prefix + '/sessions/{sid}/takeover', dependencies=guarded)
    async def takeover(sid: str, body: TakeoverInput, request: Request):
        runtime = host.runtimes.get(sid)
        if not runtime:
            raise DomainError('SESSION_OFFLINE')
        return await runtime.takeover(body.owner, body.expected_epoch, key=request.headers['idempotency-key'])

    @app.post(prefix + '/sessions/{sid}/interrupt', dependencies=guarded)
    async def interrupt(sid: str, request: Request):
        async def effect():
            runtime = host.runtimes.get(sid)
            if not runtime:
                raise DomainError('SESSION_OFFLINE')
            session = next(s for s in host.core.snapshot()['sessions'] if s['id'] == sid)
            return await runtime.takeover('human', session['epoch'],
                key='interrupt:' + request.headers['idempotency-key'])
        return await external_once(host.core, 'session_interrupt', request.headers['idempotency-key'],
                                   {'session_id': sid}, effect)

    @app.get(prefix + '/approvals/{aid}', dependencies=guarded)
    async def approval(aid: str):
        record = next((a for a in host.approvals.list() if a['id'] == aid), None)
        if record is None:
            raise DomainError('NOT_FOUND')
        return {**record, 'operation': host.redactor.value(host.operations.get(aid)),
                'live': aid in host.operations}

    @app.post(prefix + '/approvals/{aid}/decision', dependencies=guarded)
    async def decision(aid: str, body: DecisionInput, request: Request):
        return await host.decide(aid, body.decision, body.operation_hash, body.expected_revision,
                                 key=request.headers['idempotency-key'])

    @app.post(prefix + '/pause', dependencies=guarded)
    async def pause(body: PauseInput, request: Request):
        result = host.core._write('owner', 'pause', request.headers['idempotency-key'], body.model_dump(),
            lambda conn: _pause(conn, body.paused))
        with host.core.connection() as conn:
            host.paused = conn.execute("SELECT value FROM metadata WHERE key='paused'").fetchone()['value'] == 'true'
        if not host.paused:
            for sid in host.runtimes:
                host._background(host._drain(sid))
        return result

    def _pause(conn, paused):
        conn.execute("INSERT OR REPLACE INTO metadata VALUES ('paused',?)", ('true' if paused else 'false',))
        host.core._event(conn, 'ProjectPauseChanged', 'owner', paused=paused)
        return {'paused': paused}

    @app.post(prefix + '/workspaces', dependencies=guarded)
    async def workspace(body: WorkspaceInput, request: Request):
        return await external_once(host.core, 'workspace_create', request.headers['idempotency-key'],
            body.model_dump(), lambda: asyncio.to_thread(host.workspaces.create, host.root, body.head_sha, branch=True))

    @app.get(prefix + '/output', dependencies=guarded)
    async def output(after: int = 0):
        oldest = host.output[0]['seq'] if host.output else host.output_seq + 1
        return {'events': [e for e in host.output if e['seq'] > after], 'latest_seq': host.output_seq,
                'gap': after < oldest - 1, 'persistent': True,
                'retention': {'global_events': 2000, 'per_session_events': 200}}

    @app.get(prefix + '/events', dependencies=guarded)
    async def events(request: Request, after_seq: int = 0, once: bool = False):
        try:
            cursor = int(request.headers.get('last-event-id', after_seq))
        except ValueError:
            return error('INVALID_CURSOR', 400)
        with host.core.connection() as conn:
            latest = conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        if cursor < 0 or cursor > latest:
            return error('CURSOR_REQUIRES_SNAPSHOT', 410)
        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(host.core.events, cursor, 500)
                for row in rows:
                    cursor = row['seq']
                    yield 'id: ' + str(cursor) + '\ndata: ' + json.dumps(host.redactor.value(row), ensure_ascii=False) + '\n\n'
                if once:
                    return
                yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream')

    @app.get(prefix + '/diagnostics', dependencies=guarded)
    async def diagnostics():
        return host.redactor.value({'version': '0.2.0', 'snapshot': host.core.snapshot(),
                                   'events': host.core.events(), 'sessions': host.metadata,
                                   'runs':host.workflow.list(),'batches':host.coordinator.list(),
                                   'questions':host.questions.list()})

    if ui and Path(ui).is_dir():
        app.mount('/assets', StaticFiles(directory=Path(ui) / 'assets'), name='assets')
        @app.get('/')
        async def index():
            return FileResponse(Path(ui) / 'index.html')
    return app
