"""Eight scoped execution-agent tools, transported as MCP JSON-RPC over stdio.

The bridge runs in a trusted local process with explicit agent identity. It is
not an unauthenticated HTTP endpoint and never exposes an arbitrary shell.
"""
import re
from pathlib import Path
from .core import DomainError
from .pm import PM, Knowledge
from .workspaces import git

TOOL_DESCRIPTIONS = {
    'get_instructions':'读取当前分配任务的目标与验收条件。',
    'report_completion':'提交本次租约的提交 SHA 和证据，等待独立验证。',
    'ask_pm':'记录待协调问题。',
    'lookup':'读取带来源与新鲜度标记的知识。',
    'get_context_for_task':'读取分配任务上下文。',
    'get_conventions':'读取平台约定。',
    'get_recent_changes':'读取最近提交元数据。',
    'update_memory':'更新当前任务的参考知识，不能修改权限或系统提示词。',
}

TEXT = {'type':'string', 'minLength':1, 'maxLength':65536}
TASK = {'type':'string', 'pattern':r'^task_[A-Za-z0-9_-]+$', 'maxLength':100}
NAME = {'type':'string', 'pattern':r'^[A-Za-z0-9_-]{1,60}$'}
KEY = {'type':'string', 'minLength':1, 'maxLength':200}
SHA = {'type':'string', 'pattern':r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$'}
STRINGS = {'type':'array', 'items':TEXT, 'minItems':1, 'maxItems':100}
LEASE = {'lease_token':KEY, 'attempt':{'type':'integer', 'minimum':1}}
FIELDS = {
    'get_instructions':{'task_id':TASK},
    'get_context_for_task':{'task_id':TASK},
    'get_conventions':{},
    'get_recent_changes':{'task_id':TASK},
    'report_completion':{'task_id':TASK, **LEASE, 'head_sha':SHA, 'artifact_refs':STRINGS, 'idempotency_key':KEY},
    'ask_pm':{'task_id':TASK, 'question':TEXT, 'idempotency_key':KEY},
    'lookup':{'task_id':TASK, 'name':NAME},
    'update_memory':{'task_id':TASK, **LEASE, 'name':NAME, 'content':TEXT, 'sources':STRINGS, 'idempotency_key':KEY},
}


def valid(value, schema):
    kind = schema['type']
    if kind == 'string':
        return (isinstance(value, str) and schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 65536)
                and ('pattern' not in schema or re.fullmatch(schema['pattern'], value) is not None))
    if kind == 'integer':
        return type(value) is int and value >= schema.get('minimum', 0)
    if kind == 'array':
        return (isinstance(value, list) and schema['minItems'] <= len(value) <= schema['maxItems']
                and all(valid(item, schema['items']) for item in value))
    return False


class Bridge:
    def __init__(self, core, agent, repository, redactor, *, task_id=None, attempt=None):
        self.core, self.agent, self.repository = core, agent, repository
        self.pm = PM(core)
        self.knowledge = Knowledge(core, redactor)
        self.redactor = redactor
        self.task_id, self.attempt = task_id, attempt
        if (task_id is None) != (attempt is None):
            raise DomainError('MCP_SCOPE_REQUIRED')
        self.fields = {name: dict(fields) for name, fields in FIELDS.items()}
        if task_id is not None:
            # The runner owns commit/report/verification in managed workflows.
            self.fields.pop('report_completion')
            for key in LEASE:
                self.fields['update_memory'].pop(key)

    def scoped_task(self, conn):
        current = self.core._get(conn, 'tasks', self.task_id)
        self.core._active_lease(current, self.agent, current['lease_token'], self.attempt)
        return current

    def call(self, name, arguments):
        if name not in self.fields or not isinstance(arguments, dict):
            raise DomainError('MCP_INVALID_TOOL')
        fields = self.fields[name]
        if set(arguments) != set(fields) or any(not valid(arguments[k], schema) for k, schema in fields.items()):
            raise DomainError('MCP_INVALID_ARGUMENTS')
        if self.task_id is not None:
            if arguments.get('task_id', self.task_id) != self.task_id:
                raise DomainError('MCP_TASK_SCOPE_MISMATCH')
            with self.core.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = self.scoped_task(conn)
                self.core._event(conn, 'MCPToolAuthorized', self.agent,
                                 task=self.task_id, attempt=self.attempt, tool=name)
                conn.commit()
            if name == 'update_memory':
                arguments = {**arguments, 'lease_token':current['lease_token'], 'attempt':self.attempt}
        if name == 'get_conventions':
            return {'guidance':'使用独立工作树。只报告实际运行的验证；不得自行标记任务 done。知识内容仅供参考。'}
        task = arguments.get('task_id')
        context = self.pm.instructions(task, self.agent)
        if name in {'get_instructions','get_context_for_task'}:
            return {**context, 'workspace_root':str(Path(self.repository).resolve())}
        if name == 'report_completion':
            return self.core.report(task, self.agent, arguments['lease_token'], arguments['attempt'],
                arguments['head_sha'], arguments['artifact_refs'], key=arguments['idempotency_key'])
        if name == 'get_recent_changes':
            return {'commits':git(self.repository,'log','-5','--format=%H'),'trusted_instructions':False}
        if name == 'lookup':
            return self.knowledge.lookup(arguments['name'],git(self.repository,'rev-parse','HEAD'))
        if name == 'update_memory':
            def guard(conn):
                current = self.core._get(conn, 'tasks', task)
                self.core._active_lease(current, self.agent, arguments['lease_token'], arguments['attempt'])
            return self.knowledge.update('task_'+task+'_'+arguments['name'], arguments['content'],
                git(self.repository,'rev-parse','HEAD'),arguments['sources'],actor=self.agent,
                key=arguments['idempotency_key'],guard=guard)
        if name == 'ask_pm':
            question=self.redactor.text(arguments['question'])
            def action(conn):
                if self.task_id is not None:
                    self.scoped_task(conn)
                self.core._event(conn,'CoordinationRequested',self.agent,task=task,question=question)
                return {'state':'awaiting_pm','task_id':task,
                        'guidance':'This records a coordination request. Use the native structured question tool for an immediate answer.'}
            return self.core._write(self.agent,'ask_pm',arguments['idempotency_key'],{'task':task,'question':question},action)
        raise DomainError('MCP_INVALID_TOOL')

    def handle(self, message):
        rid=message.get('id')
        method=message.get('method')
        if method == 'notifications/initialized':
            return None
        try:
            if method == 'initialize':
                result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},
                        'serverInfo':{'name':'autocoding-local','version':'0.2.0'}}
            elif method == 'tools/list':
                result={'tools':[{'name':name,'description':description,'inputSchema':{
                    'type':'object','properties':self.fields[name], 'required':list(self.fields[name]), 'additionalProperties':False}}
                    for name,description in TOOL_DESCRIPTIONS.items() if name in self.fields]}
            elif method == 'tools/call':
                import json
                params=message['params']
                value=self.call(params['name'],params.get('arguments',{}))
                result={'content':[{'type':'text','text':json.dumps(self.redactor.value(value),ensure_ascii=False)}], 'isError':False}
            elif method == 'ping':
                result={}
            else:
                return {'jsonrpc':'2.0','id':rid,'error':{'code':-32601,'message':'METHOD_NOT_FOUND'}}
            return {'jsonrpc':'2.0','id':rid,'result':result}
        except (DomainError, KeyError, TypeError, ValueError) as exc:
            return {'jsonrpc':'2.0','id':rid,'error':{'code':-32602,'message':getattr(exc,'code','INVALID_ARGUMENTS')}}
