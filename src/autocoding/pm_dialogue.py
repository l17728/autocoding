"""Owner-facing PM work dialogue backed by the durable project ledger."""
import json
import re

from .core import DomainError, encoded


class PMDialogue:
    def __init__(self, host):
        self.host, self.core = host, host.core

    def _append(self, message_id, actor, kind, text, *, references=None, state='saved'):
        safe = self.host.redactor.text(text)[:12000]
        request = {'id': message_id, 'project_id': self.host.project_id, 'actor': actor,
                   'kind': kind, 'content': safe, 'references': references or [], 'state': state}

        def action(conn):
            key = 'pm_dialogue:' + message_id
            row = conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            if row:
                return json.loads(row['value'])
            payload = {**request, 'created_at': self.core.clock()}
            conn.execute('INSERT INTO metadata VALUES (?,?)', (key, encoded(payload)))
            task_id = next((r.get('task_id') for r in references or [] if r.get('task_id')), None)
            self.core._event(conn, 'PMDialogueMessageAdded', actor, task=task_id,
                             message_id=message_id, message_kind=kind, state=state)
            return payload
        return self.core._write(actor, 'pm_dialogue_append', message_id, request, action)

    def list(self, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('INVALID_DIALOGUE_PAGE')
        # Newest first makes the bounded default page useful for an ongoing conversation.
        with self.core.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM metadata WHERE key GLOB 'pm_dialogue:*'").fetchone()[0]
            rows = conn.execute("SELECT value FROM metadata WHERE key GLOB 'pm_dialogue:*' "
                                "ORDER BY json_extract(value,'$.created_at') DESC,key DESC LIMIT ? OFFSET ?",
                                (limit, offset)).fetchall()
        items = [json.loads(row['value']) for row in rows]
        return {'items': list(reversed(items)), 'offset': offset, 'limit': limit,
                'has_older': total > offset + len(items)}

    def status(self):
        snap = self.host.dashboard.snapshot()
        batches = snap.get('batches', [])
        batch = batches[-1] if batches else {}
        active_states = {'preparing', 'starting', 'running', 'reviewing', 'verifying'}
        active = [run for run in snap.get('runs', []) if run.get('state') in active_states]
        return {'project_id': self.host.project_id, 'progress': snap.get('pm_progress', {}),
                'task_counts': snap.get('task_counts', {}),
                'total_tasks': snap.get('totals', {}).get('tasks', 0),
                'batch': {k: batch.get(k) for k in
                          ('id', 'state', 'reason', 'owner_next_action', 'active_tasks') if k in batch},
                'active_tasks': [{'task_id': r.get('task_id'), 'provider': r.get('provider'),
                                  'state': r.get('state'), 'phase': r.get('phase')} for r in active],
                'agent_health': snap.get('agent_health', [])}

    @staticmethod
    def _intent(text):
        """Classify only clear commands; questions, negation and quoted examples stay conversational."""
        low = re.sub(r'\s+', ' ', text.casefold()).strip()
        if any(mark in low for mark in ('？', '?', '为什么', '为何', '是否', '能否', '可以吗', '怎么')):
            return 'status' if any(w in low for w in ('进度', '状态', '汇报', '暂停', '卡住', '停工')) else 'discussion'
        if any(w in low for w in ('不要', '别', '暂不', '先不', '无需', '不需要', 'do not', "don't", 'not ')):
            return 'discussion'
        if any(w in low for w in ('暂停派发', '暂停任务', '停止派发', 'pause dispatch', 'pause the project')):
            return 'pause'
        if any(w in low for w in ('进度', '当前状态', '目前状态', '汇报', '进展', 'how is', 'status', 'progress')):
            return 'status'
        if any(w in low for w in ('讨论', '建议', '方案', '优缺点', '风险', 'discuss', 'recommend', 'trade-off')):
            return 'discussion'
        zh_action = re.search(r'(?:请|帮我|帮忙|需要|要|开始|进行|继续|着手|落实|执行|完成).{0,20}'
                              r'(?:实现|开发|修复|完善|改进|添加|增加)|'
                              r'(?:实现|开发|修复|完善|改进|添加|增加)(?:这个|该|上述|此|剩余|新增)?'
                              r'(?:问题|缺陷|错误|功能|需求|项目|任务|能力)', low)
        direct_zh = re.fullmatch(r'(?:请|帮我)?\s*(?:实现|开发|修复|完善|改进|添加|增加)'
                                 r'(?:一下|这个问题|这个功能|这个需求|该问题|该功能)?[。！! ]*', low)
        en_action = re.search(r'\b(?:please\s+)?(?:implement|develop|fix|build|improve|add)\s+'
                              r'(?:this|the|these|a|an)\b', low)
        if zh_action or direct_zh or en_action:
            return 'work'
        if any(w in low for w in ('恢复推进', '继续执行', '继续推进', '恢复执行', 'resume work', 'resume dispatch', '继续')):
            return 'resume'
        return 'record'

    async def submit(self, message_id, text):
        if (not isinstance(message_id, str) or not 8 <= len(message_id) <= 100
                or not isinstance(text, str) or not text.strip()):
            raise DomainError('INVALID_DIALOGUE_MESSAGE')
        message_id = message_id.strip()
        safe = self.host.redactor.text(text.strip())[:12000]
        owner = self._append(message_id, 'owner', 'input', safe)
        intent = self._intent(safe)

        if intent == 'status':
            status = self.status()
            counts, batch = status['task_counts'], status['batch']
            active = '；'.join(f"{r['task_id']}（{r.get('provider') or '未分配'}，{r.get('phase') or r['state']}）"
                               for r in status['active_tasks']) or '当前没有活动任务'
            reply = (f"当前总体进度约 {status['progress'].get('percent', 0)}%；已验证 "
                     f"{counts.get('done', 0)}/{status['total_tasks']} 项。批次状态："
                     f"{batch.get('state', '尚无批次')}。{active}。")
            if batch.get('reason'):
                reply += f"暂停或恢复原因：{batch['reason']}。{batch.get('owner_next_action', '')}"
            refs = [{'type': 'batch', 'id': batch['id']}] if batch.get('id') else []
            pm = self._append('pm_' + message_id, 'pm', 'report', reply, references=refs)
            return {'owner_message': owner, 'reply': pm, 'status': status, 'action': 'answered'}

        if intent == 'pause':
            def pause(conn):
                conn.execute("INSERT INTO metadata VALUES ('paused','true') "
                             "ON CONFLICT(key) DO UPDATE SET value='true'")
                self.core._event(conn, 'ProjectPauseChanged', 'owner', paused=True)
                return {'paused': True}
            self.core._write('owner', 'pm_dialogue_pause', message_id, {'paused': True}, pause)
            self.host.paused = True
            pm = self._append('pm_' + message_id, 'pm', 'report',
                              '已暂停新任务派发；正在执行的任务会按安全边界处理。')
            return {'owner_message': owner, 'reply': pm, 'action': 'paused'}

        if intent == 'resume':
            self.host.paused = False
            with self.core.connection() as conn:
                conn.execute("INSERT INTO metadata VALUES ('paused','false') "
                             "ON CONFLICT(key) DO UPDATE SET value='false'")
            if self.host.coordinator.current is None:
                self.host.coordinator.recover()
            pm = self._append('pm_' + message_id, 'pm', 'recovery',
                              '已收到继续指示，正在从持久化的批次和任务检查点恢复派发；不确定是否已投递的指令不会重发。')
            return {'owner_message': owner, 'reply': pm, 'action': 'resume_requested'}

        if intent == 'discussion':
            status = self.status()
            batch = status['batch']
            reply = (f"我已记录这项讨论。当前批次为 {batch.get('state', '尚无批次')}，"
                     f"总体进度约 {status['progress'].get('percent', 0)}%。我会结合任务和验收证据评估影响；"
                     "目前尚未改动计划或派发开发任务。")
            pm = self._append('pm_' + message_id, 'pm', 'discussion', reply)
            return {'owner_message': owner, 'reply': pm, 'status': status, 'action': 'discussion_recorded'}

        if intent == 'work':
            from .delegation import delegate
            result = await delegate(
                self.host, safe + '\n\nOwner 已在 PM 工作对话中明确要求实施。保留现有项目约束，'
                '不要改动无关业务行为。',
                ['实现 Owner 明确提出的需求，并通过 PM 维护的独立自动验收。'],
                agent='claude', agents=['codex', 'opencode', 'claude'], max_concurrency=3)
            refs = [{'type': 'batch', 'id': result['batch']['id']},
                    *({'type': 'task', 'id': task_id} for task_id in result.get('task_ids', []))]
            pm = self._append('pm_' + message_id, 'pm', 'instruction_accepted',
                              f"已按明确的实施指示启动计划，共 {len(result.get('task_ids', []))} 项任务。"
                              'PM 将按依赖分配 Agent、独立验收并在此汇报。', references=refs)
            return {'owner_message': owner, 'reply': pm, 'action': 'delegated',
                    'batch_id': result['batch']['id']}

        pm = self._append('pm_' + message_id, 'pm', 'acknowledgement',
                          '已记录你的指示并纳入当前项目上下文。我会先核对现有任务、批次和验收条件；'
                          '这条消息尚未触发新的开发任务。')
        return {'owner_message': owner, 'reply': pm, 'action': 'recorded'}
