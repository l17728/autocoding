# 终端与开发智能体接入调研

## 结论与证据等级

可以由本软件启动 Agent 子进程，并通过协议或伪终端发送文字，让用户看见交互。普通 stdin 管道不等同于交互式终端输入框；需要保留 TUI 时应提供 PTY/ConPTY 和终端渲染器。协议模式可以更准确地确认回合、工具和权限事件，但其可视会话不是原生 TUI 的嵌入。

证据等级：L=本机帮助/Schema/缓存核验；P=实际进程或接口探针；E=模型端到端任务；H=候选设计待验证。2026-10-12 官方检索服务连接失败，直连页面返回 WinError 10013；未读取到的网页只列为后续核验入口，不当作已阅读的证据。

| 对象 | 本地事实 | 尚未验证 |
|---|---|---|
| Codex 0.162.1 | L/P：app-server 支持 stdio，能导出 JSON Schema；initialize 已返回 userAgent/codexHome/platformFamily/platformOs；含回合与审批契约 | P：隔离 CODEX_HOME 的 stdio initialize 握手已成功；E：真实任务与原生 TUI 同会话附着仍待验证 |
| OpenCode 1.19.0 | L：serve、attach、run --format json、acp 可用；隔离 XDG 配置后 --help 成功 | P/E：HTTP 路由、attach 同会话和真实输入；原配置目录权限受限 |
| Claude Code 2.1.296 | L/E：本机帮助、stream-json 输入回放与结果关联、智谱真实单回合和账本派发通过 | P/E：审批通道、恢复、中断、连续实机回合与工具执行 |
| Windows ConPTY | L：当前系统存在 CreatePseudoConsole/ResizePseudoConsole/ClosePseudoConsole 导出 | 研究探针未完成预期回传并已停止；正常桌面环境下的进程/中文/resize/焦点/性能仍待复验 |

## 方案比较

| 方式 | 适用 | 用户看到什么 | 约束 |
|---|---|---|---|
| 正式协议/SDK | 自动调度首选 | 公开消息、工具、输入和结果的结构化时间线 | 不保证原生 TUI 同时显示，必须验证会话关联 |
| stdin/stdout 管道 | 支持非交互输入的 CLI | 软件渲染的输出流 | TUI 常读终端设备而非普通管道，不能直接等价 |
| ConPTY + xterm.js | 原生终端体验 | 子进程原本发出的 VT 终端输出 | 需正确处理字符、resize、按键、滚动、断线与背压 |
| 外部窗口嵌入 | 不采用为主方案 | 原外部窗口 | HWND/DPI/焦点/进程回收不稳定，跨权限边界困难 |
| SendInput/自动键鼠 | 最后兼容手段，显式启用 | 用户确实看到外部窗口被输入 | 需激活窗口、验证进程和焦点；容易误发且受权限限制 |

ConPTY 方案中没有“必须激活每个外部窗口”的要求：输入由软件路由到指定会话的管道句柄。UI 焦点只用于确定用户正在操作哪张终端卡，PM 的目标用 session_id 固定。真实 SendInput 则需要 SetForegroundWindow/焦点检查和全局输入互斥，因此不作为并发多 Agent 主控制方式。

ConPTY 启动流程：创建双向管道 → CreatePseudoConsole 指定尺寸 → STARTUPINFOEX 关联 PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE → CreateProcess → 后台连续读取输出 → 输入队列向写端送 UTF-8/控制序列 → resize 时调用 ResizePseudoConsole → ClosePseudoConsole 与 Job Object 回收。必须并发排空输出，不能等待进程退出后才读。终端渲染需处理 VT、宽字符与备用屏幕；普通 textarea 无法替代完整终端。

## Codex 适配

以本机导出的 Schema 为准，不沿用原 DOCX 中未经证实的 --hook。控制流程候选为 initialize/initialized → thread/start 或 thread/resume → turn/start → 通知流 → turn/completed。turn/start 至少需要 threadId 与 input；clientUserMessageId 可用于关联，不直接假定具有服务端幂等保证。

审批请求是反向请求，需要保存 request ID 并单独响应；绝不自动套用“同意所有”。应用公开的回合输出与命令执行事件可直接呈现。多行输入用 JSON 编码而非模拟 Enter。每版 Schema 存哈希与差异摘要，未知事件保留原类型和脱敏 payload，不能导致读取线程退出。

官方核验入口：[Codex app-server](https://developers.openai.com/codex/app-server)。当前证据来自本机 CLI 和导出的 ClientRequest/ServerRequest/ServerNotification，不来自无法访问的网页。

## OpenCode 适配

本机 help 确认 serve 默认 127.0.0.1、attach 可连接运行中 server、run 支持 --format json 和 --session。初期适配可从受控 run 事件流起步；目标采用 server + 同会话 attach 实现协议驱动与真实 TUI 可视。

HTTP 创建会话、发消息、异步 prompt、事件订阅和权限回复等路由必须从该版本 OpenAPI/官方文档核实；文档候选路径不得直接写死当作已测。server 仅回环监听并配置认证，不开启 mdns，attach 必须绑定正确目录和会话。

官方核验入口：[OpenCode server](https://opencode.ai/docs/server/)、[CLI](https://opencode.ai/docs/cli/)。本机默认配置路径读取受限，调研使用项目 .local 下隔离 XDG 目录，仅用于帮助与探测，不表示复用了原登录状态。

## Claude Code 与智谱

2026-10-13 更新：已实现禁用工具的 stream-json 适配器并通过真实单回合；下文保留早期调研记录，当前证据与限制以 [实现状态](implementation-status.md#claude-协议派发2026-10-13) 为准。[Claude 官方非交互文档](https://code.claude.com/docs/en/headless) 说明 JSON/stream-json 输出与运行失败的结果语义；UUID 回放和关联字段另外通过本机 2.1.296 契约探针核验。

Claude 已由用户安装，版本 2.1.296；本机 npm 包装器实际指向 @anthropic-ai/claude-code/bin/claude.exe。适配器调用此 exe，避免把用户文字拼入 .cmd。候选长连接参数为 --print --input-format stream-json --output-format stream-json --verbose --replay-user-messages；本机还确认 --permission-prompts 支持 host/none；确切消息封装、首轮 system 事件、中断和权限控制需契约探针确认后启用。

本机 OpenCode 历史用户消息中的模型配置为 providerID=zhipuai-coding-plan、modelID=glm-5.3、variant=max；本机 models.json 缓存将该 provider 标为 @ai-sdk/openai-compatible，api=https://open.bigmodel.cn/api/coding/paas/v4，凭据环境变量名 ZHIPU_API_KEY。这只证明 OpenCode 侧使用 OpenAI 兼容接口，不能把此 base URL 原样交给 Claude。

Claude 需要 Anthropic Messages 兼容接口。候选国内端点 https://open.bigmodel.cn/api/anthropic 需要官方说明或成功握手证实；国际 Z.AI 与国内智谱不能自动互换。同一 key 是否覆盖该端点、glm-5.3 是否被 Anthropic 通道接受、工具调用/流式/思考参数是否兼容均需实测。max 是 OpenCode 侧 variant，不直接映射成 Claude 参数。

凭据解析顺序：显式项目凭据引用 → 已授权 ZHIPU_API_KEY 环境 → 可读的 OpenCode provider 配置或凭据仓库。支持 {env:NAME} 引用，不输出明文；解析失败返回 CREDENTIAL_UNAVAILABLE。不可读目录不绕过访问控制。仅给 Claude 子进程环境注入所需 Anthropic 变量，不修改全局 settings，不覆盖用户已有供应商配置；敏感变量名称与认证头使用方式须以已核验配置为准。

连接验收：官方说明与版本记录 → TLS/端点校验 → 无工具短问答 → Unicode 流式 → 只读工具 → 受控文件修改 → 续会话 → 权限拒绝与网络断线。不能仅凭 /version 或退出码 0 宣称智谱已接通。

官方待核验入口：[智谱 Coding Plan](https://docs.bigmodel.cn/cn/coding-plan/overview)、[Claude Code 文档](https://code.claude.com/docs/en/overview)、[Claude 非交互使用](https://code.claude.com/docs/en/headless)。当前环境无法连接 npm/官方页面，也不能读取 OpenCode 配置目录，密钥未读取、未复制。

## 发布门禁

三种适配器逐一给出 L/P/E 状态；H 项不用于自动任务。真实终端与协议视图分别验收。没有确认机制的 PTY 会话允许人工操作，自动派发只在明确空闲、持有输入租约且没有待审批时进入，完成仍靠测试证据。网页无法核验不阻断核心数据层开发，但阻断对应真实供应商接入的完成声明。



## 2026-10-13 实施补充（替代上文对应的历史待核验状态）

本节是当前证据状态；上文保留初始调研记录。

- 已读取 [Codex app-server 官方文档](https://developers.openai.com/codex/app-server)，与本机 Schema 的初始化、线程和回合流程一致。transport.py、codex.py、runtime.py 已实现通信和持久化派发。真实 initialize + 只读 thread/start 成功；模型回合仍只用协议替身验证。
- 已读取 [OpenCode server 官方文档](https://opencode.ai/docs/server/)，确认消息与异步 prompt 路由的文档定义；尚未进行本机 server/attach 同会话验收。
- [MetaGLM 官方集成指南](https://github.com/MetaGLM/glm-cookbook/blob/main/vibecoding/glm-4.5-claude-code-integration.md)记载国内端点 https://open.bigmodel.cn/api/anthropic，也示范通过 ANTHROPIC_AUTH_TOKEN 为 Claude 进程配置凭据。启动器已将其设为默认值。指南针对 GLM-4.5，不证明本机 glm-5.3 与当前 key 的兼容性。
- OpenCode 配置目录仍不可读，密钥未读取、未复制；无真实智谱回合。此前“无法读取所有官方网页”的限制已有部分解除。
- 57 项测试通过，包括真实子进程停止读取 stdin 时的超时、中文分片、回包乱序、背压、并发审批回复和接管冻结。关闭直接子进程不等于证明进程树或远端回合已停止。
- 新入口 tools/session_run.py 默认连接模拟供应商的真实子进程，可显示交互并落盘审计元数据。它是协议演示，不是原生终端嵌入；ConPTY 验收状态没有变化。
