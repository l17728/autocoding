# 本地服务控制 GUI

桌面快捷方式“开发智能体协作平台服务控制”启动 `tools/service-control-gui.ps1`。控制器参考本机 Home Doctor 的本地服务控制方式：独立启动/停止脚本、回环健康检查、状态轮询、后台执行和日志入口。

## 操作

1. 双击桌面快捷方式。
2. 点击“启动服务”。控制器检查项目虚拟环境和已构建前端，确认 8765 端口未被其他程序占用后启动工作台。
3. 健康检查通过后自动打开 `http://127.0.0.1:8765`；也可点击“打开首页”。
4. 点击“停止服务”关闭由本控制器登记的工作台进程。关闭 GUI 窗口本身不会停止服务。

首次登录从 `D:\autocoding\.local\workbench\owner-token.txt` 选择令牌文件。GUI 不读取、显示或写入令牌和模型密钥。

## 文件与故障处理

控制器的运行状态和日志位于 `D:\autocoding\.local\service-control\`：

- `workbench.json`：受控服务的 PID、启动时间、项目目录和端口。
- `workbench.log` 与 `workbench-error.log`：工作台标准输出和错误输出。
- `gui-start*.log`、`gui-stop*.log`：GUI 调用启动和停止脚本的记录。

停止脚本会核对 PID 对应的命令行包含本项目 `tools\workbench.py` 和登记端口，匹配失败时不会停止该进程。启动脚本检测到 8765 被不明程序监听时也拒绝接管；请先处理端口占用，再重新启动。

## 前置条件

需要项目的 `.venv\Scripts\python.exe` 和 `ui\dist\index.html`。若 GUI 提示前端未构建，在项目根目录运行：

```powershell
cd D:\autocoding\ui
npm.cmd run build
```

也可在 PowerShell 中直接执行 `tools\start-local.ps1` 或 `tools\stop-local.ps1`。这两个脚本默认服务根目录为 `D:\autocoding`、端口为 8765；启动脚本仅接受已构建的前端，不自动下载依赖或调用模型。

## 本轮验证

已在本机用 GUI 所调用的同一组脚本完成一次启动、`/health` 就绪确认和停止验证；停止后状态文件已移除，8765 不再监听。PowerShell 脚本也已用 Windows PowerShell 解析器检查。验证没有发送模型请求。
