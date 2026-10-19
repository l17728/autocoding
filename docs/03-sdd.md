# 开发协作平台软件详细设计 SDD

## 模块与依赖方向

代码按 domain（纯状态与契约）、store（SQLite 事务）、services（用例编排）、adapters（供应商/进程）、transport（HTTP/MCP）、ui、tests 分层。domain 不依赖 FastAPI、终端库或模型 SDK；初始核心可用 Python 标准库测试。所有外部输入经过结构验证，知识文本不得被当成系统指令或自动执行脚本。

## 持久化模型

所有时间存 UTC ISO8601，性能间隔用单调时钟。ID 采用前缀加随机 UUID，不以 PID、标题或路径作为身份。schema_version 由迁移表管理，升级前备份；不支持降级覆盖数据库。

| 表 | 核心字段与约束 |
|---|---|
| projects | id, canonical_root, status, policy_version, created_at；root 唯一 |
| requirements | id, project_id, version, source_ref, title, acceptance_json, status |
| agents | id, project_id, adapter, executable, version, capabilities_json, state |
| sessions | id, agent_id, provider_session_id, mode, cwd, branch, head_sha, generation, state, started_at |
| tasks | id, project_id, requirement_ids, description, acceptance_json, state, revision, attempt, assigned_agent, lease_token, lease_until |
| task_dependencies | task_id, depends_on；复合主键；拒绝自依赖、跨项目与环 |
| commands | id, session_id, task_id, sender, input_epoch, kind, payload_ref, payload_hash, state, idempotency_key, provider_request_id, generation |
| reports | id, task_id, lease_token, attempt, head_sha, evidence_json, accepted_at |
| verifications | id, task_id, attempt, head_sha, checker_identity, checks_json, verdict |
| input_leases | session_id 主键, owner_type, owner_id, epoch, expires_at |
| approvals | id, command_id, provider_request_id, scope_json, state, expires_at, decided_by |
| events | seq 自增主键, event_id 唯一, schema_version, project_id, task_id, session_id, command_id, trace_id, type, actor, payload_json, timestamp |
| outbox | event_seq, destination, state, attempt, next_retry_at；目的地加事件唯一 |
| requests | actor, operation, idempotency_key, request_hash, response_json；前三字段唯一 |
| knowledge | id, project_id, level, source_refs, head_sha, content_ref, status, refreshed_at |
| artifacts | id, project_id, task_id, head_sha, relative_path, sha256, size, kind |
| decisions | id, project_id, issue_id, summary, reasons, supersedes, task_ids, author |

任务与系统对象采用固定 Schema；知识正文可以 JSON/Markdown 扩展。原设计中的 YAML 视图是数据库投影或导出，不是并行修改的事实源。文件用临时文件写入、校验哈希后原子 rename；事务只引用已落盘文件，孤立临时文件启动时清理。

## 状态机与转换守卫

### 任务

`todo → ready → leased → running → verifying → done`。

依赖均 done 才能 ready；派发以 BEGIN IMMEDIATE 加条件更新取得租约。leased 可因启动失败回 ready；running 遇依赖/授权问题转 blocked；verifying 失败回 ready 并增加 attempt；取消转 cancelled，非终态可暂停。blocked 必须附 reason_code 与 unblock_condition，不能只写“卡住了”。

done 需要独立验证证据与本次 attempt、head_sha 一致。租约过期的 leased/running 不直接重分配，先进入 reconciling；确认旧 Runner 已停止或被隔离后才释放。暂停项目不改变历史任务完成状态。基础原型可使用 todo/leased/verifying/done/blocked 子集，其他状态不得在 UI 伪造。

### 会话

`created → starting → ready ↔ busy → stopping → stopped`；任意执行态可进入 awaiting_approval、disconnected、failed、reconciling。恢复后创建新的 generation；旧 generation 的迟到事件保存为诊断但不推进当前状态。

### 输入命令

`queued → dispatching → sent → acknowledged → completed`；queued 可 cancelled，dispatching 之后可能 delivery_unknown。失败区分 rejected（明确未接受）、failed（已执行但失败）、cancelled（取消确认）、delivery_unknown（是否接受不明）。

PTY 的 write 成功仅证明字节交给管道，不能自动升级 acknowledged；屏幕回显也不代表模型接受。缺少协议确认时使用 sent_unconfirmed，并由观察或人工关联回合；任务完成仍独立验证。禁止用出现“done”字符串作为完成事件。

### 输入租约

PM 与人类不能同时持有可写租约。每次接管/归还增加 epoch。命令入队检查 epoch，实际发送前再检查 epoch 与 generation；接管时取消旧 epoch 尚未发送命令。所有输入通过每会话单线程队列串行化，锁持有到发送调用完成。接管若遇发送中，先等待发送结果或标记 unknown，再中断当前回合；不能承诺撤回已经送达字节。

## 核心事务

1. 领取任务：校验身份、能力、项目状态 → 查依赖 → 条件更新任务与唯一有效租约 → 写 TaskLeased 与 Outbox → commit。
2. 发送命令：校验 session/worktree/epoch 与输入长度 → 计算 UTF-8 负载哈希 → 按 actor+operation+幂等键查旧结果 → 创建 queued 命令和事件 → commit。网络发送在事务外。
3. 完成报告：去重 → 校验 task、agent、lease_token、attempt、有效期、head_sha → 存报告 → 转 verifying → 创建验证任务及事件 → commit。同步 API 只返回已接收，不等待 PM 模型推理。
4. 验证：独立验证器读取精确提交，在隔离工作树执行测试，记录命令、退出码与日志哈希 → 校验仍是同一 attempt → 通过后 done；否则返工。验证器无法访问网络时记录未验证，不伪造失败或通过。
5. 事件发布：Outbox 后台读取已提交事件，发送后标记 delivered；发送后标记前崩溃会重复投递，消费者以 event_id 去重。关键事件保留，终端高频块可合并并记录偏移。

SQLite 启用 foreign_keys、WAL、busy_timeout=5000ms。Core 是单机状态写入权威；多线程每操作独立连接、显式事务。跨数据库/外部进程不承诺分布式原子性。租约每 15 秒续约，默认 60 秒；边界以服务端 UTC 判定，重启和时钟跳变触发对账，不能据过期单方面假定进程已死。

## API 契约

默认 /api/v1。Owner 使用本机短期会话令牌；Agent 使用项目限定令牌，日志只记录令牌指纹。所有写接口带 Idempotency-Key；会改变已有资源的接口同时携带 expected_revision。错误统一：

```json
{"error":{"code":"STALE_INPUT_EPOCH","message":"输入控制权已变化","retryable":false,"trace_id":"tr_...","details":{"expected_epoch":4}}}
```

| 方法与路径 | 输入/行为 | 成功与错误 |
|---|---|---|
| POST /projects | canonical_root, name | 201；路径不在授权范围 403 |
| POST /projects/{p}/requirements | source_ref, title, acceptance | 201 |
| POST /projects/{p}/tasks | requirement_ids, description, acceptance, dependencies | 201；DAG 冲突 409 |
| POST /tasks/{t}/claim | agent_id, expected_revision | 200 租约；被占/未就绪 409 |
| POST /tasks/{t}/heartbeat | lease_token, attempt | 200；旧租约 409 |
| POST /tasks/{t}/reports | lease_token, attempt, head_sha, evidence | 202 report_id；旧报告 409 |
| POST /tasks/{t}/verification | attempt, head_sha, checks, verdict | 200；仅 verifier 角色可写 |
| POST /sessions | agent_id, worktree_id, mode | 202；能力不支持 422 |
| POST /sessions/{s}/commands | sender, epoch, text, task_id | 202 command_id；过大 413 |
| POST /sessions/{s}/takeover | expected_epoch | 200 新 epoch |
| POST /sessions/{s}/release | expected_epoch, handoff_summary | 200 新 epoch |
| POST /sessions/{s}/interrupt | turn_id, reason | 202；中断结果异步事件 |
| POST /approvals/{a}/decision | decision, scope, expected_revision | 200；过期/已决定 409 |
| GET /projects/{p}/events | Last-Event-ID 或 after_seq | SSE；旧游标 410 + snapshot URL |
| GET /projects/{p}/snapshot | 当前投影 | 200 + latest_seq |
| WS /sessions/{s}/terminal | auth, generation, epoch；data/resize/control | 只在认证和租约检查后写入 |
| GET /projects/{p}/diagnostics | trace/session/task 过滤 | 脱敏诊断包引用 |

MCP v1 固定 8 个执行 Agent 工具：get_instructions、report_completion、ask_pm、lookup、get_context_for_task、get_conventions、get_recent_changes、update_memory。PM 工具另列 create_task、assign_task、create_coordination_issue、record_decision、update_knowledge、request_verification；禁止 PM 的工具集合包含任意 shell/file-write。MCP 工具参数复用服务层 Schema，不绕过权限。

## Agent Adapter 接口

`probe() → CapabilityReport`；`start(SessionSpec) → SessionHandle`；`send(Command) → DispatchReceipt`；`events(cursor) → EventStream`；`interrupt(turn_id)`；`resume(provider_session_id)`；`close()`。

CapabilityReport 含 executable/version、transport、can_resume/can_interrupt/can_approve/can_attach_tui、probe_evidence、verified_at。不支持的能力返回 CAPABILITY_UNAVAILABLE，不空实现成功。输入 argv 使用列表且 shell=False；Windows .cmd 包装器须解析到可信可执行文件或固定脚本，不把用户消息拼入 cmd /c 字符串。普通文字经 stdin/协议 UTF-8 传递；终端 Escape/控制键单独建模，不允许文字消息偷偷变成终端控制序列。

## Worktree 与验证

项目前置检查 git --version、仓库根、当前改动及分支。新项目由用户选择初始化；已有未提交改动保留并提示隔离策略。执行目录只接受 Worktree 服务返回的路径，拒绝 ..、UNC 越界与重解析点逃逸。每个实例分支 agent/{instance}/{task}，基线提交写入任务上下文。

完成并不自动合并。集成 Agent 在专门工作树合并，通过集成测试后更新需求覆盖。清理只删除平台创建且已停进程的工作树，并核对注册记录、路径与未提交改动；默认保留用户工作，不执行强制清除。

## 恢复与故障策略

启动扫描未完成命令、未确认 Outbox、过期租约、会话 generation。queued 且未发的可重试；dispatching/sent 且无供应商确认的转 delivery_unknown。支持供应商查询时用 request/turn/session ID 对账；否则提示人工确认或在确认旧进程停止后从任务检查点重新规划，绝不原样自动重放可能有副作用的命令。

接口错误退避 1/2/4 秒加抖动，最多 3 次，仅限明确安全的操作；模型超时和输入未知不盲目重试。单任务默认 30 分钟软超时、60 分钟硬预算，长任务可以显式配置。30 秒无输出只显示静默；120 秒无心跳才做健康检查，不能因合法长测试而直接杀进程。断线时先保留日志和进程信息，再做中断与回收。


## 机器可读契约

[contracts.schema.json](contracts.schema.json) 提供任务创建、命令、完成报告和验证结果的字段约束。身份从认证主体取得，不信任请求自行宣称 sender/verifier；输入的 64 KiB 还须按 UTF-8 字节数校验，JSON Schema 的 maxLength 只约束字符数。Schema 验证不替代租约、权限和状态机守卫。

## 租约隔离与验收失效补充

租约 token 是业务栅栏，不是操作系统文件写权限。过期时只阻止旧实例提交新状态，不能证明其停止写工作树。Runner 必须先冻结派发，依据注册的进程标记/启动时间和 Job Object 回收确认旧进程树退出，或隔离并弃用旧工作树；二者都无法证实时保持 reconciling，禁止新实例复用该目录。迟到报告一律不能越过新 attempt。Windows Job Object 管理生命周期，不声称它提供完整安全沙箱。

接管与发送共用每会话输入仲裁器。发送写操作限时 5 秒；超时不能无限持锁，转 delivery_unknown 并冻结输入，接管请求返回“等待对账”。无法取消正在写入的底层调用时关闭该输入通道并终止/隔离会话，确认停止后才恢复发送，不能释放锁后让旧写操作继续和新输入并行。Runner 重启后的未确认命令沿用同样规则。

每个任务验收快照包含 requirement_version、acceptance_hash、dependency_output_manifest（依赖任务 ID/attempt/产物 SHA）、head_sha。done 后若需求或依赖被重开/替换，生成 InvalidationRecorded：尚未派发的后继回 todo；在运行/验证的后继暂停提交判定并进入 reconciling；已 done 的后继标 stale_done，不计入需求完成，生成复验/返工任务。旧证据保留供审计，不能在新基线复用。M1 若尚无需求修改/重开接口，必须拒绝这类操作，而不是悄悄沿用旧 done。

## 专家审阅后的事务与安全细化

### 幂等与版本顺序

所有写入口固定顺序：认证及项目/角色授权 → 按 actor/operation/key 查询 requests → 同 hash 返回已提交的原结果，同 key 异 hash 返回 IDEMPOTENCY_CONFLICT → 仅对新请求检查 revision、租约、epoch、状态等可变守卫 → 事务更新状态、事件和 requests。成功 claim/report 的响应丢失后，重放不能因为当前状态已推进而报旧租约错误。幂等记录保留至少覆盖对应任务生命周期和重试窗口；清理后旧 key 返回 IDEMPOTENCY_WINDOW_EXPIRED，而非当新命令执行。

### 验证与取消

每份验证工作记录 verification_id、task_revision、acceptance_hash、dependency_output_manifest_hash、attempt、head_sha。verifier 写回必须 CAS 匹配这些字段且 task.state=verifying；已取消、重新规划、验收版本修改或已完成均不能被迟到验证覆盖。验证失败事件保留原工作 ID。取消任务同事务增加 revision 并失效未完成验证；M1 可以先支持取消并拒绝修改验收，仍要测试迟到结果被拒绝。

### 审批一致性

approvals 增加 session_id、generation、operation_hash、revision，复合唯一键为 session_id/generation/provider_request_id。审批范围绑定原始操作哈希，不能用相同 request_id 改变操作。用户决定在事务中由 pending CAS 到 approved/denied，事件和 Outbox 一并提交，再异步回传。回传超时标 delivery_unknown，不用新 request_id 重放；供应商对账或确认失效后才继续。过期和跨代际响应一律拒绝。

### 旧进程与远端回合

本地进程退出不证明远端会话停止。重分派前必须同时确认本地进程树停止和已知远端执行回合中断；或将旧工作区隔离且不可复用，并证明旧执行方不再具备产生外部副作用的权限。无法证明则保持 reconciling，M1 不提供绕过该守卫的自动重新派发。Worktree 只隔离代码修改，不是安全沙箱；执行 Agent 和验证器各有环境变量白名单、文件/网络权限及预算，验证器不继承模型 key、发布凭据或用户完整环境。

### 快照与事件接续

snapshot 的所有投影与 latest_seq 在同一 SQLite 读事务中获得，客户端此后只应用 seq > latest_seq 的事件。单项目视图的过滤不改变全局 seq 语义；允许序号间隙但禁止重复应用。保留窗口外游标先返回快照指引，不悄悄从最新开始。用 FT-22 验证并发写入期间的快照与后续事件无漏无重。
