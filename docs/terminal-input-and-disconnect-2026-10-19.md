# 长输入与 Windows 断连专项（2026-10-19）

## 输入问题与修复

旧长提示不是单一问题。固定 60 秒确认期限无法区分仍在推进和完全停滞；Codex 使用绝对光标位置绘制折行，xterm 的 `isWrapped` 不一定为真。新增当前可见提示匹配，保留行内空格，按匹配进度等待，60 秒无新增匹配或总计 300 秒即停止。完整匹配需持续 500 毫秒，期间重新检查会话、代次、所有权、暂停状态及终端实例。不会因只匹配末尾、出现部分回显或旧历史而追加回车。

首次多行中文/emoji 实测又定位到 UTF-16 事件顺序错误：原编码为高代理项按下/释放、低代理项按下/释放。原生消费者在过滤释放事件前合并代理项，导致 emoji 丢失。现在非 BMP 字符只发送连续高、低代理项各一次；这些是合成文本事件，没有物理按键需要释放。普通控制键仍保持原来的按下/释放流程。参考 [Crossterm Windows 事件解析](https://raw.githubusercontent.com/crossterm-rs/crossterm/master/src/event/sys/windows/parse.rs)。首次失败保留在 `.local/high-priority-native-long.log` 和 `.local/native-browser-codex-1792382130262/`，没有自动补发输入。

后续完整回显进一步定位到显示宽度差异：旧 xterm Unicode 宽度规则把 emoji 算少一列，绝对光标重绘后会产生额外空格。启用官方 `@xterm/addon-unicode11`，不删除真实空格来掩盖差异。增加实际 xterm 渲染用例，验证中文、emoji 后的单空格与文本内部双空格都准确保留。[官方用法](https://raw.githubusercontent.com/xtermjs/xterm.js/master/addons/addon-unicode11/README.md)。

超过 256 字符或含换行的 Codex 文本改走其原生 Ctrl+G 外部编辑器接口，一次性写入 UTF-8 草稿，避免逐键消费和数百次重绘。编辑器只配置于平台子进程，不改变全局设置。每次交接绑定随机 ID、内容摘要和一次性请求文件；已有非空草稿拒绝覆盖。写入后仍等待完整屏幕确认，最后独立发送一次 Enter。启动期间可能忽略 Ctrl+G，因此仅在请求尚未消费时，最多尝试打开本地编辑器三次，交接总等待不超过 60 秒；不重复写入正文或提交模型。异常、摘要不符或超时冻结并停止会话。平台私有交接目录随终端关闭清理，Codex 自身临时草稿由其管理。[Codex 外部编辑器实现](https://raw.githubusercontent.com/openai/codex/main/codex-rs/tui/src/external_editor.rs)。

诊断还发现 pywinpty 异步返回值不能作为完整写入字节数：观察到返回 0，不能据此断言零投递或补发。[上游写入实现](https://raw.githubusercontent.com/andfoy/winpty-rs/main/src/pty/base.rs) 的异步分支没有将每次同步完成的字节数完整加入返回计数。为避免依赖这个回执，平台工作进程新增私有回环连接：随机握手值只用于父子进程，启动原生 CLI 前从环境移除；只接受固定 Win32 事件格式，通过 `WriteConsoleInputW` 批量写入并返回事件数量。回执不完整、超时或断开均进入已有冻结/停止流程，不重试未知写入。输入通道丢失会退出 Job 所有者，终止其子进程。VT 回复和非 Win32 输入仍走原有路径。未修改全局供应商配置或系统剪贴板。

500 字符测试改为核对子进程收到的原始 UTF-8 文件，而不把大段十六进制回显作为唯一证据；回车必须另外提交，文件末尾只能新增一次 CR。保留诊断阶段超时，不能用单次通过抹掉历史失败。

## 断连清理

旧日志明确指向 Python 3.12 `asyncio.proactor_events` 的 `_call_connection_lost`：协议已经收到关闭通知，但 socket 的 `shutdown()` 抛出 `WinError 10054`，跳过后面的 close、server detach 和完成标记。

应用生命周期内安装局部异常处理器，仅在异常类型、Windows 错误码、回调实例、代码及 traceback 中的 shutdown 调用点全部匹配时补齐清理。不会再次调用协议关闭回调；其他读写错误、协议异常、其他代码位置仍交原处理器。关闭应用时恢复旧处理器，不修改 Python 全局文件。该兼容层依赖 CPython 的私有结构，升级 Python 时须复验。

测试包括注入同一 shutdown 错误后释放真实 socket、重复关闭不重复通知、连续真实 TCP reset 后重新连接，以及其他异常继续报告。未将网络重置本身当成可以自动重放业务输入的理由。

## 验收记录

- 浏览器相关回归：6 项通过（1.6 分钟），含实际 Unicode 宽度、终端关闭与恢复，`.local/high-priority-browser-final.log`。
- 最终提交、历史重放和粘贴确认补验：7 项通过（1.1 分钟），`.local/high-priority-submit-replay.log`。部分回显、仅后缀匹配及旧历史不会触发提交，完整匹配后仅一次 Enter。
- 通道、草稿、代理项、冻结、断连及原生输入复查：16 项通过（28.673 秒），`.local/high-priority-recheck-final.log`。
- 最终相关后端回归：58 项全部通过（133.912 秒），含 API、输入、启动、冻结、流、草稿及断连，`.local/high-priority-delivery-backend.log`。
- TypeScript 检查与前端最终构建通过（构建 18.24 秒），`.local/high-priority-delivery-build.log`。
- 原生长提示一次已核对实际文件正文完全一致，但后续等待模型 `DONE` 超过 60 秒，整例仍记失败，见 `.local/high-priority-native-editor-ready.log`。后续验收保存文件摘要与单次提交证据，并采用有界的 180 秒模型收尾等待。
- 最终原生 Codex 长提示整例通过（用例 1.6 分钟），`.local/high-priority-native-completion-retry.log`。588 字符提示包含三行中文、12 个 emoji 和内部双空格；实际文件规范化后 SHA-256 与预期一致，提交计数为 1，模型返回 `DONE`，关闭后恢复入口可用。证据目录 `.local/native-browser-codex-1792384871166/`，含 `long-input-evidence.json`、`report.json`、诊断及截图。本轮只关闭长文本单场景，不代表完整供应商交互矩阵通过。
- 此前一次独立验收在创建终端阶段进入 reconciling，未发送模型提示；保留 `.local/high-priority-native-completion.log`。测试原先在终端画面不存在时等待整例超时，已将该步骤限制为 30 秒。偶发原生创建失败仍需另行定位，不因下一次通过而宣称启动永不失败。

智谱未调用；长期真实业务负载继续按 Owner 要求搁置。

中间一次 55 项回归有 3 项原生夹具等待超时，保留 `.local/high-priority-backend-final.log`，相关项目已包含在上述 16 项复查中。第二次长提示的测试还错误等待已被清空输入框对应的“发送”按钮恢复可用；现改为等待明确操作结果，失败不会继续空等文件。旧日志 `.local/high-priority-native-long-fixed.log` 和未启用新 Unicode 宽度的 `.local/high-priority-native-editor.log` 均保留。

本轮最初对超出可见编辑区域、被供应商折叠或尚不能完整匹配的输入保守停止。后续已增加完整草稿读回校验，更新见 [超长输入修复与启动复核](oversized-input-and-startup-audit-2026-10-19.md)，不根据长度占位符假称完整确认。原生文件验收允许 UTF-8 BOM、Windows 换行及单个末尾换行差异，正文字符和空格必须完全一致。
