# 开发智能体协作平台

面向 Windows 的本地开发协作平台：PM 制定任务计划，Codex、OpenCode 和 Claude Code 执行开发工作，用户查看公开交互、接管输入并核对独立验证证据。

## 当前版本

2026-10-27 修复在线会话统计口径与 PM 停工告警，区分并修正历史批次重试耗尽分类；修复 Claude 已确认 turn 的迟到 replay 导致 ACK mismatch。Dashboard/Coordinator/Claude/Host 定向回归共 62 项通过；Claude 与 OpenCode 真实隔离回合、三 Agent 终端启动/停止、Codex 进程树崩溃后安全续接、受控 HTTPS 隧道断连恢复及 300 秒三项目合成负载均已补验。真实浏览器页面、OpenCode TUI 和独立知识 holdout 仍未验收。新前后端已安全部署，当前服务健康；详细证据与边界见[实现状态](docs/implementation-status.md)。

2026-10-22 补强 PM 自动闭环、输入历史和本机服务恢复：新增项目级 PM 对话（最新 100 条分页、同幂等编号重试），PM 自动维护验证基线；服务启动加入互斥与项目身份检查，worker 早退立即恢复，业务快照故障不再触发全服务重启。最终测试和现场状态见[实现状态](docs/implementation-status.md)。长时负载、供应商限额和强制故障注入仍需独立验收。

2026-10-19 创建失败新增分段安全诊断及清理确认后的有界重试，与就绪重试共用上限；Windows 关闭改为等待整组进程句柄，补齐目录释放与异常清理。真实交互补验发现并修复 OpenCode 拒绝工具后无法结束回合的问题。Codex 真实模型、Claude/OpenCode 真实 CLI 配合本地模拟供应商的问答、审批、恢复和取消补验通过；最终后端 **362 项**、通用浏览器 **25 项**通过，三种原生终端均在三次内通过，类型检查及构建通过。失败记录及供应商边界见 [创建、清理与交互专项](docs/creation-cleanup-interactions-2026-10-19.md)。以下为历史增量记录。

2026-10-19 超长输入改为完整草稿读回确认，支持超出屏幕或折叠的 Codex 正文。原生 3,597 字符、24 行整例通过，实际文件摘要一致且仅提交一次；浏览器 11 项及读回专项 11 项通过。启动问题复核确认：此前就绪阶段修复仍在，创建阶段失败尚未纳入该重试流程。详见 [超长输入与启动复核](docs/oversized-input-and-startup-audit-2026-10-19.md)。

2026-10-19 高优先级专项：修复 Codex 长文本的代理项编码、emoji 显示宽度与完整回显确认，增加一次性 UTF-8 草稿交接；补齐 Windows 10054 关闭异常后的资源清理。相关后端 58 项、浏览器提交与重放补验 7 项、原生 588 字符长文本整例通过，TypeScript 检查及构建通过。验收范围、旧失败及偶发启动问题见 [长输入与断连专项](docs/terminal-input-and-disconnect-2026-10-19.md)。

2026-10-19 新增隔离评测的复核、驳回、污染淘汰、版本化导出与成对评测接入；知识、偏好或样本状态变化会使旧复核失效。补齐 Codex 子进程隔离、启动错误有界恢复、更新菜单阻塞处理和终端轮询优化。交付证据及未关闭原生验收见 [本轮记录](docs/review-and-recovery-2026-10-19.md)。下方为前轮记录。

新增[启动分段诊断与隔离评测采集](docs/startup-and-holdouts-2026-10-16.md)：OpenCode 本轮原生启动、命令面板和人工接管首次通过，旧空屏失败保留且根因尚未确定。网页可先登记新场景并冻结知识基线，再保存标准答案；不会自动进入 PM 学习，仍须独立性复核。受影响后端 17 项、页面专项 1 项通过，构建通过；下述原生失败属于上一轮记录。

最新故障边界、原生菜单和评测数据隔离工作见 [补验记录](docs/fault-interaction-evaluation-2026-10-16.md)：Codex、Claude 本地交互通过；OpenCode 本轮三次启动验收未通过，旧命令面板断言已收紧。新增 Ctrl+P 支持，完整交互矩阵仍未完成；合成候选共 16 项，独立真实用户保留样本尚未建立。

最新增量与验收见 [剩余工作执行记录](docs/remaining-work-2026-10-16.md)：终端关闭与完整回显修复、六轮真实 Codex 开发、受控网络中断和进程崩溃恢复、离线评测候选集。原生 Codex 最终单轮通过；真实 Windows IME 到 ConPTY 的单场景验收通过，输出背压的调度缺陷已修复。长期真实业务验收已由用户搁置；完整原生交互、学习效果和智谱相关实测仍有未关闭项。

新增一次委托后的 PM 自动规划与执行、持久化用户工作偏好、跨会话恢复的目标与修正、自省推进指标。终端支持有限启动重试：默认 3 次，可设 1～5 次，耗尽后等待手动再次启动；不重发已输入的指令。实现与能力边界见 [PM 替代 Owner 审查](docs/pm-owner-autonomy-2026-10-16.md)。

本轮跨领域代码审查、修复及剩余门禁见 [系统审查记录](docs/system-review-2026-10-15.md)。

最新非智谱工作、测试证据及未关闭门禁见 [本轮验收记录](docs/non-zhipu-acceptance-2026-10-16.md) 和 [当前状态](docs/implementation-status.md)。新增历史用户答案评测草稿、用户偏好变更后的评测失效保护、终端输出桥和重试期间的停止/恢复保护。智谱相关真实模型复验等待 Owner 确认服务就绪，不按日期自动触发。此前 P2 记录见 [P2 交付](docs/p2-delivery-2026-10-15.md)。

0.2.0 开发版，本地多项目、单 Owner 访问。已实现：

- 页面创建或接入本地项目，保存项目名称、绝对目录、GitHub 用户名/组织、仓库名和默认权限策略；每项目独立任务账本。
- PM 定期自检与经验库：记录话术、教训、原则和改进建议，引用审计事件，支持归档并向后续 PM 决策提供参考。
- PM 冷启动：本地用户指令与固定版本规则仓库融合为 18 条带来源的原则和话术，包括同类错误排查与递归收敛；启动时幂等安装，保留归档选择。[抽取与融合记录](docs/cold-start-knowledge.md)。
- PM 推进与收尾：完成声明后自动盘点和复审，再独立验收；记录求助与历史答案、轮换维护冷启动知识，项目完成后保存整体复盘与经验，网页可查询。[循环与收尾机制](docs/pm-loop-and-closure.md)。
- 知识去重、同主题分歧隔离/采用、90 天过期和知识引用审计；独立验收结果趋势对比，样本不足时不宣称改善。该指标不是 PM 能力或经验因果效果评分。
- 知识分页、跨 Host 缓存失效、失败指数退避，以及测试/权限/重放/凭据领域的中英文候选冲突筛选；5,000 条合成知识持续负载与 Host 重建验证通过，仍需完整长期验收。

- SQLite 迁移、任务 DAG、领取租约、输入所有权、幂等请求、事务事件与 Outbox。
- Claude/智谱真实回合；OpenCode 本地服务协议；Codex 完整访问 app-server 会话。
- PM 处理结构化提问、单选/多选/自定义回答及工具审批；工作台支持人工补充和恢复会话。
- 独立工作树、固定提交的验证执行器、隔离集成候选与项目检查。
- 带令牌认证的 HTTP/SSE、本地 React 工作台、三张会话卡、审批详情及脱敏诊断导出。
- PM 任务草案与人工采用、可信本地 stdio MCP、托管任务自动注入与租约绑定、带提交来源的知识记录。
- Claude/Codex/OpenCode 任务执行、续租、提交和独立验证；按依赖并发执行计划、有限返工及集成候选。
- Windows ConPTY 浏览器终端：认证 WebSocket 输出、中文输入、常用控制键、resize、接管、关闭、手动恢复和未知投递冻结。

产品仍在开发：已有任务闭环和供应商连接证据不代表长时间生产负载、所有供应商恢复路径或性能矩阵全部通过。当前具体验收证据和未关闭风险见[实现状态](docs/implementation-status.md)。

## 安装与运行

需要 Windows、Python 3.12+、Node.js 和 Git。使用项目虚拟环境安装工作台依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[workbench]"
cd ui
npm.cmd ci
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe tools/workbench.py
```

打开 http://127.0.0.1:8765，登录时选择 `.local/workbench/owner-token.txt`。服务仅打印令牌文件路径，不打印令牌或模型 key。Owner 令牌在本地持久保存，重启后仍有效；仅显式使用 `--reset-token` 才会轮换。用 `--project D:\your-project --port 8765` 指定项目。工作台存储在所选项目的 `.local/workbench/`。

### 桌面服务控制

Windows 桌面上的“开发智能体协作平台服务控制”可启动、停止本项目工作台并打开首页。它使用回环健康检查确认服务已就绪，启动后自动打开首页；关闭控制窗口不会停止服务。控制器只停止自己登记且命令行核对为本项目工作台的进程，不会按端口强制结束未知程序。日志在 `.local/service-control/`；使用说明和故障处理见[本地服务控制 GUI](docs/service-control-gui.md)。

“与 PM 制定任务计划”可先生成草案，也可通过“委托 PM 规划并执行”让 PM 自动规划、创建验证基线、分派 Agent、复核和验收。PM 工作对话支持进度查询、讨论、暂停、继续和明确任务指令；默认显示最新 100 条并可加载更早消息。断连重试沿用同一消息与幂等编号，避免丢失响应后重复投递。会话卡通过已安装的原生 Agent 程序启动：Claude/OpenCode 使用本项目智谱凭据，Codex 使用已安装账户；发送模型请求消耗供应商额度。

未知投递不会自动重发。协议模式的“回合完成”和终端模式的“已写入”均不表示任务验收通过。协议会话只显示公开模型文本，不显示思考内容；原生终端显示程序本身的屏幕内容。公开输出保留在服务内存，审计与任务账本持久化。

页面底部“本机终端”默认提供不调用模型的自检：启动后发送一行中文，即可核对回显。点击屏幕可直接输入，支持方向键、回车、退格、Tab、Esc、Home/End、Delete、PageUp/PageDown、Ctrl+C 和 Ctrl+P；其他组合键仍可能被拒绝。普通无换行提示即时显示，疑似凭据片段保留到可以脱敏时再输出。选择 Codex、OpenCode 或 Claude 时，需要项目已有 Git 提交，平台从当前 HEAD 创建独立工作树。最多同时运行三个终端，与自动验收工作流分开管理。令牌在 WebSocket 首帧认证，不放入 URL；输入、控制键、缩放、接管和关闭走认证 API。键盘输入串行排队，失败后停止发送，不自动重试。

“恢复终端”仅对已确认停止的终端开放，复用原工作树和原会话存储：Codex 使用 `resume --last --sandbox danger-full-access`（保留工作目录筛选），Claude 使用 `--continue --dangerously-skip-permissions`，OpenCode 使用 `--continue`。Claude/OpenCode 使用各终端独立的配置或数据目录。服务重启会读取终端记录；崩溃时未确认停止的实例禁止自动续接。同一旧实例只能续接一次，后续恢复应选择最新实例；旧输入和未知结果不会重放。自检程序恢复是重新启动回显进程，不包含模型上下文。

## 项目与仓库设置

项目页的“异常对账与恢复”支持托管任务冻结后的人工核查。点击“核对恢复证据”查看旧会话、命令状态、工作树及提交；核查旧执行、子进程和远端操作已停止，并处理未知副作用后，填写结论并明确允许新尝试。系统重新校验证据，原子归档旧尝试、使旧租约和会话输入失效，将任务恢复为待执行。随后从任务入口启动，会创建新会话及独立工作树；不重发旧命令、不自动合并旧修改。旧工作树及对账记录保留，已对账记录可在同一面板查询。

此入口覆盖存在对应托管记录、归属一致的 blocked/reconciling 任务，也支持没有任何运行记录的 blocked 任务。后者明确提示无法自动关联旧会话和工作树，要求核查项目相关执行，且不清理其他会话。旧工作树目录缺失时必须额外确认接受无法自动恢复其中修改，才能从项目基线重新开发；目录存在但注册损坏或路径不匹配时仍拒绝恢复。执行或投递仍活跃时拒绝恢复；证据变化时需要重新核查。停止结论依赖 Owner 的明确核查声明，不能据此宣称平台已经证明远端模型停止。

登录后展开页面顶部“项目与仓库设置”。可以修改当前项目名称、GitHub 用户名或组织、仓库名，以及默认执行权限；保存后配置会持久化。仓库链接由用户名和仓库名生成，此设置不自动创建远程仓库、修改 remote 或推送代码。

在“当前项目”区域维护项目名称、本地目录和 GitHub 仓库关联。修改目录并保存会检查并切换到已登记项目，或接入现有目录；不会覆盖已有文件和提交历史。增量执行需要干净的 Git 提交基线。项目账本保存在工作台存储目录下，不向项目目录写入平台状态。

默认采用完整访问并跳过权限审批：Codex 使用 `danger-full-access` 与 `never`，Claude 使用 `--dangerously-skip-permissions`，OpenCode 使用 `permission.*=allow`。也可选择 PM 审批；权限变更用于新会话，已有供应商会话的授权可能继续沿用原设置。结构化选择题或自定义回答仍交由 PM/Owner 处理。终端恢复分别使用 Codex `resume --last`、Claude/OpenCode `--continue`，协议恢复使用准确的供应商会话 ID。

页面底部“PM 自检与知识库”可设置周期、立即自检及归档经验。默认每 60 分钟检查一次，仅有新活动时调用模型；工作台关闭或派发暂停时不运行。失败总结不进入经验库。详细结构、边界和接口见 [设计说明](docs/pm-learning-projects.md)。目前真实 Claude 探针出现回执超时；2026-10-15 最小消息接口检查返回 HTTP 429，已停止追加模型请求。自检真实模型验收待服务端限制解除后完成。

## Claude 与智谱

默认读取已配置且被忽略提交的 `.local/credentials/opencode.json`，也支持环境变量和显式配置路径。不要提交或分享此文件。凭据仅传给供应商子进程，不修改全局 Claude 设置。

```powershell
python tools/check_network.py
python tools/claude_zhipu.py --check
.\.venv\Scripts\python.exe tools/session_run.py --agent claude --prompt-file .local/claude-session-prompt.txt --timeout 90
```

统一命令行入口默认禁用工具和会话持久化；开发探针使用独立临时仓库和严格限定的文件操作。已验证 Claude → 智谱 glm-5.3 最小连接、一次真实文件修复与独立测试、持久会话恢复、协议中断确认和错误凭据 401。中断确认不等于远端停止证明。

```powershell
.\.venv\Scripts\python.exe tools/claude_development_probe.py
.\.venv\Scripts\python.exe tools/opencode_session.py
.\.venv\Scripts\python.exe tools/pm_probe.py
```

上述命令调用真实模型，结果保存在独立 `.local/` 目录。API 启用 Claude 文件和命令工具需要已注册的 Git 工作树；不会自动初始化当前项目的 Git 仓库。普通 Claude 会话仅启用提问工具，托管开发会话启用文件和命令工具。

## PM 回答与会话恢复

工作台按本项目授权配置启动：Codex 使用 `sandbox: danger-full-access`（app-server 的 `--sandbox danger-full-access` 对应设置）；Claude 使用 `--dangerously-skip-permissions` 并持久化会话；OpenCode 将工具权限设为 `ask`，交由 PM 审核本次操作。

免权限确认参数不会消除需求澄清问题。Claude `AskUserQuestion`、Codex `item/tool/requestUserInput`、OpenCode `/question` 均转换为统一问题，PM 结合当前指令选择单选、多选或填写自定义文字。信息不足或超出授权时保留给用户；工作台“审批与证据”可填写自己的想法。PM 的判断可能出错，问题与回答均保留处理记录。原生 TUI 中没有协议事件的任意弹窗尚不能自动识别。

本机 CLI 的续接参数是 `--continue`，不是 `--conotinue`。Codex CLI 可用 `codex resume --last --sandbox danger-full-access`，`resume --last` 只写一次；Claude 可用 `claude --continue --dangerously-skip-permissions`，OpenCode 可用 `opencode --continue`。平台协议会话使用保存的明确会话 ID 恢复，不依赖全局“最后一次”。协议进程连接异常时最多自动恢复两次；服务重启或达到上限后可点击“恢复会话”。恢复连接不重放结果未知的输入，相关任务仍需对账。原生终端采用上文的独立目录续接方式，仅由用户手动触发。

真实探针（消耗模型额度）：

```powershell
.\.venv\Scripts\python.exe tools/question_probe.py
.\.venv\Scripts\python.exe tools/workflow_probe.py
.\.venv\Scripts\python.exe tools/workflow_probe.py --agent codex
.\.venv\Scripts\python.exe tools/recovery_probe.py --agent codex
.\.venv\Scripts\python.exe tools/workflow_probe.py --agent opencode
.\.venv\Scripts\python.exe tools/workflow_probe.py --agent opencode --glob
.\.venv\Scripts\python.exe tools/resume_probe.py --agent codex
.\.venv\Scripts\python.exe tools/resume_probe.py --agent opencode
.\.venv\Scripts\python.exe tools/concurrency_probe.py
.\.venv\Scripts\python.exe tools/concurrency_probe.py --browser
```

`--browser` 使用已安装的 Edge 和 `ui/node_modules` 中的 Playwright，需先构建 `ui/dist`。它在独立临时项目中从真实页面启动三供应商批次，核对三路运行及会话卡，保存同时在线与集成候选截图、脱敏 JSON 报告。探针为观察短任务重叠，在三个原生会话就绪后同步放行输入；会调用真实模型，不模拟 API 响应，不修改主项目 HEAD。失败批次保留待对账，不重放未知输入。

## 执行任务计划

项目页“验收配置管理”可载入、编辑 JSON 并校验保存独立验收检查和受保护路径，展示版本、生效来源、进行中任务以及缺少检查的任务。检查参数以数组保存，首项必须是本机存在的原生可执行文件绝对路径；保存不会执行命令。版本冲突保留草稿，需重新载入比较后再保存。

首次通过页面保存前仍读取 `.local/acceptance.json`。首次保存后，生效配置转为项目数据库中的版本化记录，原文件保留；此后修改原文件不影响执行，必须在页面更新。配置和审计原子提交。更改正在使用的检查或受保护路径，会在执行器的配置守卫处阻止沿用旧验收结果，相关任务可能进入待对账。配置管理不自动撤销已完成任务。

在已有 Git 项目的 `.local/acceptance.json` 中配置维护者提供的检查，验收名称必须与任务条件一致。例如：

```json
{
  "checks": {"unit": ["C:\\Python312\\python.exe", "test_calculator.py"]},
  "protected_paths": ["test_calculator.py"]
}
```

可执行文件需使用本机存在的绝对路径。创建验收条件为 `unit` 的任务后，在“执行 Agent”选择 Claude、Codex 或 OpenCode，点击任务执行按钮或“执行全部待办”。执行器创建独立工作树、领取并续租、派发给所选 Agent，随后封闭会话、检查受保护文件、提交和独立验证。批次使用所选 Agent 按依赖顺序执行，默认每项最多尝试两次；全部通过后生成经过项目检查的集成候选，供审阅。主分支不会自动发布。中断、接管或未知投递使任务停止并等待对账。

选择“三 Agent 并发”后执行全部待办，可最多同时执行 3 项独立任务，按待办顺序轮流分配 Codex、OpenCode、Claude。批次 API 支持 `agents` 和 `max_concurrency`（1–3）；原有单 Agent 请求仍默认串行。各任务从批次基线及已验证的依赖提交开始，返工保留自身提交。某项达到返工上限或进入待对账后停止派发新任务，已在运行的任务收尾后结束批次。暂停只停止新派发；服务关闭会取消活动任务并保留对账状态。并发探针使用同步放行输入的独立测试仓库，不修改当前项目。

完整访问与免确认模式赋予 Agent 当前操作系统用户的权限；受保护文件检查发生在提交前，不能防止此前的文件或命令副作用。

托管任务启动时自动注入 `autocoding` MCP，恢复时保留原任务及尝试次数。每次调用校验任务归属和有效租约，调用记录进入事件账本。托管会话开放 7 项工具：任务指令、上下文、约定、近期提交、知识查询/写入、协调记录；`report_completion` 由工作流负责，不开放给执行 Agent。知识写入的租约凭据由服务端补齐，不写入 Agent 参数。`ask_pm` 仅记录协调事项；需要即时回答时使用原生结构化提问工具。独立可信本地 MCP 接口仍保留原有 8 项工具。

## 验证与集成

无模型的多项目知识负载与周期性重建检查：

```powershell
.\.venv\Scripts\python.exe tools/multiproject_soak.py --seconds 300 --projects 3 --records 5000
```

报告保存在独立 `.local/multiproject-soak-*/report.json`，包含每项目首次构建与缓存重载时间、P95/P99、重建次数、事件循环延迟和 Python 跟踪内存增长。脚本最多支持 24 小时、10 个项目；短时通过不能替代长期生产负载。知识分组的派生缓存绑定原始知识版本、算法版本及到期时间，损坏时重建；缓存写入失败不影响原始知识读取。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd ui
npm.cmd run build
npx.cmd playwright test
cd ..
```

浏览器测试使用本机 Microsoft Edge，不调用模型。测试入口为 `tests/test_*.py` 和 `ui/e2e/`；协议替身测试与真实供应商探针分开记录。ConPTY 测试仅适用于 Windows。

`tools/integrate.py` 将已验证任务的提交合并进新工作树，在合并提交上执行 Owner 提供的检查，不修改主分支、不推送。检查 JSON 是验收名称到原生可执行文件 argv 数组的映射，必须由项目维护者提供，不能采用 Agent 自报的命令。

```powershell
.\.venv\Scripts\python.exe tools/integrate.py --project D:\your-project --database D:\your-project\.local\workbench\platform.db --base-sha <完整提交SHA> --task task_example --checks D:\your-project\.local\integration-checks.json
```

验证器隔离环境变量、限制进程寿命并保留输出哈希；它不是抵御恶意代码的操作系统沙箱。Git 冲突保留在候选工作树供审阅，不强制删除文件。

## 实际模块布局

新增 P2 功能入口、收尾门禁、复验命令和验收边界见 [P2 实现与验收记录](docs/p2-delivery-2026-10-15.md)。项目页新增独立专家审查、需求覆盖矩阵、知识对照评测和分页历史。自动专家审查须在项目面板开启；登记需求后覆盖证据参与收尾门禁。

- `src/autocoding/core.py`、`migrations.py`、`delivery.py`：事务账本、迁移、Outbox、审批。
- `runtime.py`、`transport.py`、`host.py`：会话通信、输入仲裁、后台派发和公开输出。
- `codex.py`、`claude.py`、`opencode.py`、`providers.py`：供应商协议与配置。
- `api.py`、`ui/`、`tools/workbench.py`：本地 API、React 工作台和启动入口。
- `terminal.py`、`terminal_worker.py`、`terminal_hub.py`：ConPTY、进程托管和浏览器终端管理。
- `workspaces.py`、`verifier.py`、`execution_worker.py`、`integration.py`：工作树、独立验证和集成候选。
- `pm.py`、`planner.py`、`mcp.py`：确定性计划、模型草案和可信本地工具桥接。
- `workflow.py`、`coordinator.py`、`questions.py`：托管任务、依赖批次与 PM/用户回答。
- `expert_review.py`、`coverage.py`、`learning_eval.py`、`dashboard.py`：独立审查、需求证据、知识对照评测与有界页面投影。
- `tools/`：演示、探针及命令行入口；`tests/`：行为测试和协议替身。

Python 使用 4 空格缩进，模块/函数 snake_case、类 PascalCase；前端使用 TypeScript/React/Vite。原始 DOCX 和 AGENTS.md 保留未修改；AGENTS.md 描述初始化状态，实际开发命令以本 README 为准。[设计文档入口](docs/README.md) 保留需求、设计、评审和交付门禁。
