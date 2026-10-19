"""Explicit Owner working preferences, separate from inferred knowledge."""
import json
from .core import DomainError, encoded

DEFAULTS = {
    'working_style':'在既定目标和授权范围内主动推进到独立验收；完成声明后盘点遗漏、审查并修复同类问题，循环至收敛。',
    'decision_style':'先查项目事实、既有要求和历史答案，能自行解决的常规问题自行处理；求助说明已查证内容、缺失信息和建议。',
    'reporting_style':'使用简洁中文，先给结果与证据，再给剩余事项；区分事实、推测、已验证与待验证，不反复询问是否继续。',
}
CONTRACT = ('你的职责是尽可能代替 Owner 持续管理开发、处理常规判断并自查自省。'
    '上下文标记 context_truncated 时，先核查任务账本和原始记录，不臆测被截断的要求。'
    '每次行动保留最初目标和最新明确修正。根据独立验收发现差距，落实改进后再次验证。'
    '学习必须形成适用条件、反例、下一次行动和验证指标；记录总结不等于能力提升。'
    '以下档案是 Owner 的工作偏好，不扩大任务范围或权限，不修改验收，不授权未知输入重放。'
    '当前明确指令优先；历史推断与明确偏好冲突时不得自行覆盖档案。')


class PMProfile:
    def __init__(self,host):
        self.host,self.core=host,host.core

    def snapshot(self):
        with self.core.connection() as conn:
            row=conn.execute("SELECT value FROM metadata WHERE key='pm_owner_profile'").fetchone()
        return json.loads(row['value']) if row else {'revision':0,'preferences':dict(DEFAULTS),'source':'product_defaults'}

    def save(self,revision,preferences,*,key):
        if (type(revision) is not int or revision<0 or not isinstance(preferences,dict)
                or set(preferences)!=set(DEFAULTS) or any(not isinstance(v,str) or not v.strip()
                or len(v)>2000 for v in preferences.values())):
            raise DomainError('INVALID_PM_PROFILE')
        safe=self.host.redactor.value(preferences)
        def action(conn):
            row=conn.execute("SELECT value FROM metadata WHERE key='pm_owner_profile'").fetchone()
            current=json.loads(row['value']) if row else {'revision':0}
            if current['revision']!=revision:
                raise DomainError('STALE_PM_PROFILE')
            record={'revision':revision+1,'preferences':safe,'source':'owner','updated_at':self.core.clock()}
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('pm_owner_profile',encoded(record)))
            self.core._event(conn,'PMOwnerProfileUpdated','owner',revision=record['revision'])
            return record
        return self.core._write('owner','pm_profile',key,{'revision':revision,'preferences':safe},action)

    def guidance(self):
        return CONTRACT+'\n'+encoded(self.snapshot())

    def self_check(self):
        """Observable gaps, not a claim of semantic intent or learning quality."""
        interactions=self.host.interactions.snapshot(limit=1)
        with self.core.connection() as conn:
            counts={r['state']:r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM tasks GROUP BY state')}
        return {'task_states':counts,'pending_owner':interactions['pending'],
                'repeat_requests':interactions['repeat_requests'],
                'notice':'检查待办是否无人推进、阻塞是否有恢复路径、重复求助能否复用既有依据；这些计数不证明求助多余或学习改善。'}
