# 隔离评测闭环与终端恢复（2026-10-19）

## 实现

隔离样本支持 Owner 复核通过、驳回、永久标记污染，必须填写依据并提交当前样本版本。旧页面的复核拒绝覆盖新状态，所有复核历史保留；污染状态不能恢复为可用样本。标准答案仍不可覆盖。

导出要求 2～40 条复核有效的样本，至少包含范围内和范围外场景。导出清单保存唯一版本 ID、内容摘要、样本版本与记录摘要；支持下载 JSON、重载历史导出，并通过专用接口接入成对评测。样本、答案、复核说明不会写入 PM 学习输入或审计事件正文。

复核会冻结当时知识记录、冲突处理和 Owner 偏好。以上内容变化后，旧复核失效，需重新复核。已导出清单不被覆盖，但不能继续接入评测；已关联报告不再显示为当前有效结果。运行时在每次模型调用前后核查评测集、知识及样本来源，发现变化即停止，不计入该对评分。正在进行的远程请求不可能撤回，但其返回值不会被当作有效验收证据。

人工复核通过仅表示 Owner 完成核查，不等于系统独立证明了样本来源或语义独立性。没有实际真实样本或模型对照结果时，不宣称 PM 已提升。手动编辑 JSON 保存为普通评测集会丢失隔离样本关联，网页明确提示。

新增接口（项目路径下，Owner 鉴权与幂等写入）：

- `POST /learning-evaluations/holdouts/{id}/review`：revision、decision、note。
- `POST /learning-evaluations/holdout-exports`：sample_ids。
- `GET /learning-evaluations/holdout-exports/{id}`：不可变清单及当前有效性。
- `POST /learning-evaluations/holdout-suite`：export_id、当前评测集 fingerprint。

## 终端

就绪识别先去除 ANSI 控制载荷和样式，避免颜色序列将提示语切开，或窗口标题里的文字误判为已就绪。原始输出仍完整交给渲染器。终端轮询只查 sessions，不再扫描整个 tasks/commands 账本，减轻繁忙项目对握手和输入的阻塞；这不是对历史空屏根因的确定归因。

真实 Codex 补验再次遇到 `No active thread is available`。新增启动阶段明确错误识别：仅尚未输入用户指令时进入已有有界重试，先确认旧进程停止，不重放输入。仍沿用最多三次等既定设置，不无限恢复。

进一步修复子进程继承父 `CODEX_DAEMON_SHUTDOWN_FILE` 和 `CODEX_TOOL_CALL_ID` 的隔离遗漏，统一用于原生终端和 app-server；这只是有证据的环境隔离缺陷，尚不能确定它造成了所有线程未就绪错误。第三次失败明确停在 Codex 更新选择菜单。平台子进程追加 `-c check_for_update_on_startup=false`，运行与升级分开管理，用户全局配置不变；更新由单独维护操作执行，不在任务启动时自动选择升级。

原生验收脚本的独立重试与产品内部重试分开：脚本最多三次，每次产品启动上限设为 1，避免三乘三；日常产品默认上限仍为 3。原生审批探针没有完整通过时，不将模型未调用、仅菜单可见或启动失败记为审批通过。

补验发现 Playwright 文件级 `retries:2` 覆盖命令行的零重试设置，新增 `AUTOCODING_NATIVE_SINGLE_ATTEMPT=1` 显式单次开关。旧运行保留实际次数，不将原计划的单次写成实际单次。新版 Codex 页脚使用模型/路径，旧断言等待 `context left` 会产生假失败；修正兼容断言后，仍要求真实审批及文件内容作为该用例的完成条件。

长提示两次只收到部分回显，完整粘贴保护没有追加 Enter；不降低该保护或自动重发。独立的真实 Win32/ConPTY 测试验证 500 字符（含中文和 emoji）完整送达并单独提交 Enter，1 项通过（10.420 秒，`.local/long-native-wire.log`）。这不能证明 Codex 内部输入/显示没有问题，长提示仍需专项定位。

第三次长提示被 Codex 实际执行，工作树中 `native-approval.txt` 内容已独立核对，但没有出现审批菜单，因此审批用例仍失败。文件证据见 `.local/native-browser-codex-1792378353043/command-file-evidence.json`，只能证明实际命令执行/文件效果，不证明交互审批。全权限与 on-request 不意味着每个已允许命令都必须弹框。

短指令单次探针也实际执行并返回 DONE，但没有弹框，不能作为审批通过；日志 `.local/codex-short-approval-2026-10-19.log`。随后在隔离项目添加 `Set-Content` 的明确 `prompt` 执行规则和 `approvals_reviewer="user"`，先通过本机 `execpolicy check` 核对匹配，再运行真实终端。官方 [Rules 说明](https://learn.chatgpt.com/docs/agent-configuration/rules) 规定规则加载及 `prompt` 行为；未添加全局规则。

最终明确规则的单次验收通过（测试用例 2.9 分钟）：实际显示审批菜单，按 Enter 后核对工作树文件内容并等待模型 DONE，随后通过菜单打开、导航、退出及人工接管。日志 `.local/codex-rule-approval-2026-10-19.log`，结构化证据 `.local/native-browser-codex-1792379678515/report.json`。该次只发送 1 条模型提示，不代表所有供应商审批/自由输入交互矩阵已完成。旧失败记录完整保留；该次诊断文件中的 `max_attempts:3` 是运行前缓存的元数据，真实运行由显式单次开关限定，后续脚本已修正为按开关记录。

Codex 审批探针使用隔离测试项目的 review 模式，要求原生审批界面出现，再操作确认并核对工作树中的实际文件内容。它不修改业务项目权限，也不将单纯回显作为成功。若启动失败，审批步骤仍算未验收。

官方配置参考：[审批策略](https://learn.chatgpt.com/docs/config-file/config-reference)、[CLI 参数](https://learn.chatgpt.com/docs/developer-commands?surface=cli)。官方参数说明不构成本地 `No active thread` 根因证据。未修改用户全局 Codex 配置或升级程序。

## 验证

- 全量后端：338 项通过，840.763 秒，`.local/review-cycle-backend-all.log`。
- 通用浏览器：17 项通过、4 项明确跳过，1.8 分钟，`.local/review-cycle-browser-all.log`。跳过项为显式启用的原生验收及有界面的操作系统输入法用例。
- 前端构建通过，16.84 秒，`.local/review-cycle-build-final.log`。
- 真实 500 字符输入专项通过，10.420 秒，`.local/long-native-wire.log`。
- 原生交互合跑：Claude、OpenCode 首次通过；Codex 未通过，保留上述分类证据，`.local/native-recovery-final-2026-10-19.log`。
- Codex 明确规则的原生审批专项：单次通过，测试用例 2.9 分钟，包含实际文件效果、菜单和人工接管；这是后续修正夹具的结果，不覆盖旧失败记录。

智谱仍等待用户确认就绪，长期真实业务负载继续搁置。没有真实独立样本和模型对照结果，不宣称 PM 自学习效果已获实证。

仍需持续验证的边界包括 Codex 长提示部分回显、历史 OpenCode 空屏根因、关闭期间偶发 `WinError 10054` 回调，以及其他供应商的真实审批与自由输入交互。这些未因本轮通过而被标记解决。
