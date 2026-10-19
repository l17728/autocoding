# 剩余验收推进（2026-10-16）

本记录更新 10 月 15 日的结论，保留此前失败证据。按 Owner 安排，本轮不调用智谱模型，10 月 17 日后再复验；没有创建自动调用任务。

## 已修复的问题

- 终端脱敏将 CSI 最后一个字节 `t/p/c` 当作凭据前缀暂存，导致查询不完整。现在按控制序列边界保留和释放；新增所有分块位置的行为测试，普通凭据前缀仍保持脱敏。
- WebSocket 发送期间产生新帧时，游标曾跳过尚未发送的帧；进程同时退出还会丢失最后输出。游标和关闭状态改为发送前快照，新增可重复触发两个竞态的测试。
- 输出重连会重放能力查询，导致客户端重复写入回复。服务标记历史帧；前端顺序渲染历史并抑制历史查询回复。浏览器测试区分历史和实时查询。
- 输出唤醒使用逐代事件，减少固定轮询延迟；页面渲染队列有容量上限，超限断开并冻结键盘。
- 增加有审计和幂等保护的粘贴 API；固定 bracketed-paste 包装不允许用户注入 ESC，提交使用独立回车。真实 ConPTY 测试验证中文、多行、逐字节包装、恶意控制字符拒绝和重复请求不重发。

## 原生界面证据修正

OpenCode 的空屏不能继续归为供应商启动失败：xterm 会暂停视口外的渲染，旧测试读取了不可见的 DOM。终端打开时现在滚入视口，原生测试也要求屏幕可见。OpenCode 已显示实际 logo 和输入框，启动用例通过（32.3 秒），报告 `.local/native-browser-opencode-1792116041201/report.json`，截图 `startup.png`；模型提示数为 0。Claude 首次主题菜单用例通过（14.6 秒）。这两项不等于完整模型交互矩阵通过。

Codex 协议恢复探针通过，两个真实回合使用相同 provider session 并保留随机上下文：`.local/codex-resume-hsdh8lvq/report.json`。原生网页回合独立验证；协议成功不替代原生成功。

Codex 原生颜色回复兼容：仅对 Codex 忽略可选 OSC 10/11 查询，其他终端照常回复；表单粘贴后延迟 500 ms 再发送一次回车，不重试。对照试验已完成首个真实回答，但续接测试过早在 `Resuming session…` 阶段发送下一条指令，未通过上下文验证：`.local/native-browser-codex-1792117088137/diagnostics.json`。测试改为等待实际恢复就绪；不能据此保证任意机器和负载下的时序。

补充旧终端实例移除检查后，Codex 原生真实上下文恢复复验通过（用例 1.5 分钟，总计 1.7 分钟），报告 `.local/native-browser-codex-1792117929304/report.json`，日志 `.local/native-resume-final-2026-10-16.log`。包含两个真实模型提示、关闭后 `resume --last`、随机标记逆序验证；不以原提示回显判断成功。前述失败证据仍保留，不再把恢复加载阶段的错误测试操作归为已确认的产品挂死。

最终串行合跑又出现原生启动竞态：Codex 已显示权限和模型界面，首次提交却返回 `No active thread is available`，报告 `.local/native-browser-codex-1792118304016/diagnostics.json`。单次成功不等于稳定可用，不能关闭 Codex 原生门禁，也不自动重发该输入。当前 UI 可见状态不能证明供应商内部线程已经就绪；此问题仍需解决。

## 回归证据

第一轮全量 288 项通过（805.550 秒，`.local/regression-terminal-2026-10-16.log`），随后补充粘贴 API 和退出竞态修复。最新后端全量运行 289 项（832.480 秒），288 项通过、1 项真实 ConPTY 颜色回复等待超时，日志 `.local/regression-terminal-final-2026-10-16.log`。未更改代码或放宽时限，终端 8 项原样复验全部通过（41.673 秒）；这不等于最终全量通过。粘贴/API/脱敏/输出流的定向 33 项已通过（49.305 秒）。

最终构建通过，主包 276.26 kB、终端分包 339.61 kB。网络 `tools/check_network.py` DNS/TCP/TLS 1.3 通过，不使用凭据。

浏览器完整合跑 15 项，14 项通过、1 项 Codex 原生恢复失败（6.5 分钟），日志 `.local/browser-final-2026-10-16.log`。OpenCode 原生启动、Claude 首次菜单和其余 12 项通用浏览器用例通过。恢复就绪断言曾被仍在显示的旧终端 DOM 满足，导致过早输入；现补充等待旧终端实例移除，单独复验，不把旧画面作为新会话就绪证据。

最终串行回归：后端 **289 项全部通过，722.365 秒**，日志 `.local/regression-serial-2026-10-16.log`；此前超时的真实 ConPTY 颜色回复用例也在本次全量通过。浏览器 **14 项通过、1 项失败，6.4 分钟**，日志 `.local/browser-serial-2026-10-16.log`，失败为前述 Codex `No active thread`，没有跳过原生用例。页面随后补充这一准确的已知限制提示并通过最终构建；不把文案更新当作故障修复。串行包装命令因浏览器失败返回 1，Python 自身汇总为 OK。

## 真实应用源码探针

新增 `tools/platform_workload_probe.py`：复制平台源码和测试到一次性目录，先证明两个植入缺陷会触发受保护测试，再由真实 Codex 修复并经平台盘点、复审、独立验收。不修改原项目源码或根目录 Git 状态，不复制凭据。

首轮 `.local/platform-workload-hsy8_sl5/report.json` 为 `rework_required`，未完成修复，独立检查正确拒绝完成。该轮限制只能使用文件工具，后续已允许隔离目录中的本地 shell 读写及测试。第二轮 `.local/platform-workload-bdksvw32/report.json` 为 `reconciling`，审计记录 `CommandDeliveryUnknown / CONNECTION_LOST`；未重发未知输入，不能断言公网供应商断连是根因。探针增加脱敏的公开输出诊断留存。

第三轮隔离运行通过：`.local/platform-workload-gdtcovlt/report.json`，340.58 秒，状态 `done`，MCP 授权调用、盘点、专家复审检查点和受保护测试独立验收通过；源项目 HEAD 未改变，智谱调用为 0。这是一次真实应用源码修复样本，不能替代长期真实业务负载。此处“专家复审检查点”是执行 Agent 的完成后复审，不是延期的智谱独立多专家审查。

## 尚未关闭的门禁

- 三供应商完整原生菜单、自定义输入、接管矩阵；Claude/OpenCode 原生上下文恢复；真实操作系统 IME；Codex 原生命令与文件审批。
- 智谱恢复后的真实 PM 自检、专家审查、知识成对评测及 Claude/OpenCode 模型恢复。
- 长期真实业务开发负载和可归因的公网供应商断连恢复；短时样本与 SQLite 故障夹具不能替代。
- 后端全量已通过，但浏览器原生 Codex 仍有启动竞态，整体统一回归门禁未关闭。

## 复验入口

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/platform_workload_probe.py --timeout 600
cd ui
npm.cmd run build
$env:AUTOCODING_NATIVE_TUI='1'
$env:AUTOCODING_NATIVE_CODEX_MODEL='1'
node.exe node_modules/@playwright/test/cli.js test
```

智谱恢复后的顺序和入口沿用 [10 月 15 日验收记录](remaining-acceptance-2026-10-15.md)。不自动重试未知模型输入，不打印凭据。
