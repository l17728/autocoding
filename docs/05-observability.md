# 开发协作平台日志与故障定位设计

## 日志分层

业务事实写入 SQLite events，与状态更新同事务。运行日志使用结构化 JSONL；终端流按会话和 generation 分片，记录 stream、offset、长度、哈希和时间。Owner 操作、PM 决策、系统自动行为、Agent 工具行为使用不同 actor。日志用于复现公开行为与依据，不保存模型内部推理。

事件统一字段：schema_version、event_id、seq、timestamp_utc、monotonic_elapsed_ms、severity、type、project_id、requirement_id（可空）、task_id、attempt、agent_id、session_id、generation、command_id、trace_id、causation_id、actor、payload。缺省关联字段明确 null，不拿另一个会话 ID 补齐。

## 事件目录

| 编号 | 事件 | 必需信息 |
|---|---|---|
| EV-01 | RequirementImported / TaskCreated | 来源、需求版本、依赖、验收哈希 |
| EV-02 | TaskLeased / LeaseRenewed / LeaseExpired | agent、租约指纹、期限、attempt |
| EV-03 | SessionStarting / Ready / Exited | 适配器版本、脱敏 argv、cwd、PID 与启动时间、退出码 |
| EV-04 | CommandQueued / Dispatching / Sent | 目标、sender、epoch、UTF-8 长度、payload_hash |
| EV-05 | CommandAcknowledged / Completed / DeliveryUnknown | provider request/turn、确认来源、延迟或缺失原因 |
| EV-06 | OutputChunk / OutputGap / TranscriptRotated | 偏移、stream、字节数、分片哈希；不在业务表重复大文本 |
| EV-07 | InputTakenOver / Released / CommandCancelled | 旧新 owner/epoch、操作者、取消范围 |
| EV-08 | ApprovalRequested / Decided / Expired | 操作摘要、授权范围、request_id、决策人与有效期 |
| EV-09 | CompletionReported / VerificationFinished | SHA、attempt、检查项、退出码、日志引用、判定 |
| EV-10 | TaskRequeued / Blocked / Done | 原状态、新状态、reason_code 与解锁条件 |
| EV-11 | CoordinationOpened / DecisionRecorded | 参与者、意见引用、结论依据、落实任务 |
| EV-12 | KnowledgeRefreshed / StaleDetected | 来源 SHA/哈希、新旧版本、更新层级 |
| EV-13 | RecoveryStarted / Reconciled | 待核命令、旧新 generation、对账手段与结论 |
| EV-14 | ProviderProbe / CredentialResolved | 来源类型、model、适配器能力；无 key 值 |
| EV-15 | PolicyRejected / BudgetExceeded / StorageFailed | 策略版本、范围、失败码与恢复动作 |
| EV-16 | AcceptanceRequested / Accepted / Rejected | 需求版本、集成 SHA、验收人、未通过项 |

命令状态与任务状态不同：CommandCompleted 表示本轮交互结束，TaskDone 表示交付物经过验证。UI 不合并两者。

## 错误码

采用稳定 code 而非解析自然语言：ADAPTER_NOT_FOUND、ADAPTER_VERSION_UNVERIFIED、CAPABILITY_UNAVAILABLE、PROVIDER_AUTH_FAILED、PROVIDER_RATE_LIMITED、PROVIDER_UNREACHABLE、PROTOCOL_MALFORMED、PROCESS_EXITED、STALE_LEASE、STALE_INPUT_EPOCH、IDEMPOTENCY_CONFLICT、DEPENDENCY_NOT_READY、APPROVAL_EXPIRED、DELIVERY_UNKNOWN、PATH_OUTSIDE_WORKSPACE、DISK_QUOTA_EXCEEDED、VERIFICATION_FAILED。retryable 与 suggested_action 独立字段，401/403 不自动循环重试。

示例：

```json
{"schema_version":1,"type":"CommandDeliveryUnknown","trace_id":"tr_demo","session_id":"s_demo","generation":2,"command_id":"cmd_demo","actor":"runner","payload":{"code":"DELIVERY_UNKNOWN","phase":"write_completed_ack_missing","retryable":false,"next_action":"reconcile_provider_turn"}}
```

## 脱敏与留存

密钥仅经凭据解析器注入子进程环境，禁止命令行参数、URL query、stdout 和数据库明文。脱敏器先按已知凭据精确匹配，再处理 Bearer、API key 字段与常见 token 格式；输入分块时保留尾部以防密钥跨块泄漏。结构化日志用字段白名单。终端显示可能含用户敏感文本：普通日志默认不保存完整原文，专用会话记录保存在本机授权目录并在 UI 明示记录策略。

事件默认保留 30 天，错误与验收证据 90 天；关联未完成任务的证据不自动删除。终端分片默认 20 MiB，滚动配额 1 GiB/项目；达到 80% 告警，90% 清理已过保留期分片，无法释放时暂停新任务并记录 OutputGap/StorageFailed，不能静默丢关键事实。终端全量记录无法无条件保证，需要明确配额、缺口与保留期。

诊断导出只包含允许字段、必要脱敏片段、版本、事件和校验哈希；不包含环境变量全集、原始历史对话、凭据配置、完整源码或未批准文件。导出前预览清单，默认本地保存，不自动上传。

## 定位手册

1. **指令没进去**：command_id → CommandQueued 是否存在 → input epoch/generation → Dispatching/Sent → provider ack。若只有 Sent，查是否为 PTY 未确认模式，不重复点击发送；核对目标 session 与工作目录。
2. **Agent 显示完成但功能没变**：检查 build ID/实际进程路径 → task attempt 与提交 SHA → 验证器日志 → 是否部署/运行了对应新版本 → 集成分支是否含该提交。
3. **停着不干**：查 ready 任务与依赖 → 租约/输入所有者 → 权限请求 → provider 网络/限额 → PM 预算和健康检查。给出明确阻塞者与解除方法。
4. **输出串到另一窗口**：核对 session_id、generation、订阅 cursor、进程标记；聚合视图必须保留每条输出来源，不能依赖当前选中卡片。
5. **重启后重复操作**：查幂等键、请求 hash、发送边界、对账事件；delivery_unknown 不允许自动退回 queued。
6. **Claude 智谱错误**：只检查协议类型、端点 host、模型 ID、凭据引用是否可解析、HTTP 状态与请求 ID；日志不能输出密钥。

## 可观察性验收

在 FT-01 到 FT-18 中注入故障，要求从一个 task_id 或 command_id 在 5 分钟内定位到错误层、原因及后续动作。通过脚本检查关键转换事件齐全、关联字段存在、凭据金丝雀未出现。诊断包应包含证据缺口，不能为了“完整”伪造未收到的事件。


## 持久化前脱敏补充

凭据脱敏必须发生在 JSONL、events payload、终端分片等所有持久化之前，不只在诊断导出时做。终端审计记录分别保存 raw_stream_offset 和 redacted_stream_offset，脱敏改变字节长度后不可混用。原始流只在受限内存中短暂处理，不另存未脱敏副本；若允许特别诊断模式，必须独立显式授权、加密并限时删除，首版不实现该模式。FT-25 用同一个秘密金丝雀覆盖所有落盘出口和分块边界。
