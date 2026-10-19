"""Deterministic completion checkpoints; Agent claims are not acceptance."""

CHECKPOINTS = (
    ('inventory', '盘点剩余工作：逐条对照本任务需求和验收项，列出已实现、遗漏、未测试和阻塞项。'
     '继续完成当前授权范围内的遗漏；不要只回复已经完成，不要扩展任务或修改受保护验收文件。'),
    ('expert_review', '进行第二轮审查：分别从架构专家、测试专家和用户体验审查者视角挑战自己的实现。'
     '检查正常、边界、错误、恢复等适用用例，核对测试是否实际执行、证据是否对应当前代码。'
     '举一反三搜索同类缺陷，修复后重新检查受影响链路；不可执行的测试必须明确列出。'
     '报告发现、修复、剩余事项及证据；同一 Agent 的多视角自审不能声称独立专家已验收。'
     '不要提交 Git 或修改受保护验收文件，平台随后执行独立验收。'),
)


def checkpoint(index):
    return CHECKPOINTS[index] if index < len(CHECKPOINTS) else None
