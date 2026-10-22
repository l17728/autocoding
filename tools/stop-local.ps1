# 开发智能体协作平台 · 本地工作台停止脚本
# 仅停止由 start-local.ps1 登记且命令行可核对的本项目工作台进程。
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $projectRoot '.local\service-control\workbench.json'

if (-not (Test-Path -LiteralPath $stateFile)) {
    Write-Output '没有登记的工作台服务。'
    exit 0
}

$removeState = $false
try {
    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $expectedEntry = Join-Path $projectRoot 'tools\workbench_supervisor.py'
    $targets = @(@{ pid=[int]$state.pid; marker=$expectedEntry })
    if ($state.worker_pid) { $targets += @{ pid=[int]$state.worker_pid; marker=(Join-Path $projectRoot 'tools\workbench.py') } }
    $supervisor = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)" -ErrorAction SilentlyContinue
    $listener = Get-NetTCPConnection -LocalPort ([int]$state.port) -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        # Recover the earlier direct-launch layout and a detached worker left
        # behind after its supervisor died. Match the full project entry and
        # port before adding the listener to the controlled process list.
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        $commandLine = [string]$process.CommandLine
        $workerMarker = Join-Path $projectRoot 'tools\workbench.py'
        if (-not $process -or $commandLine -notlike "*$workerMarker*" -or
            $commandLine -notlike "*--project $projectRoot*" -or
            $commandLine -notlike "*--port $($state.port)*") {
            throw "端口 $($state.port) 的监听进程无法确认为本项目工作台；为保护其他程序，未停止它。"
        }
        if (-not ($targets | Where-Object { $_.pid -eq [int]$listener.OwningProcess })) {
            $targets += @{ pid=[int]$listener.OwningProcess; marker=$workerMarker }
        }
    }
    foreach ($target in $targets) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.pid)" -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $commandLine = [string]$process.CommandLine
        if ($commandLine -notlike "*$($target.marker)*" -or $commandLine -notlike "*--port $($state.port)*") {
            throw "PID $($target.pid) 的命令行与登记的工作台不匹配；为保护其他程序，未停止它。"
        }
        & "$env:SystemRoot\System32\taskkill.exe" /PID $target.pid /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.pid)" -ErrorAction SilentlyContinue
            if ($remaining) { throw "无法停止受控服务进程树（PID $($target.pid)）。" }
        }
    }
    $remainingListener = Get-NetTCPConnection -LocalPort ([int]$state.port) -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($remainingListener) { throw "端口 $($state.port) 仍在监听（PID $($remainingListener.OwningProcess)）；保留服务登记以便继续恢复。" }
    Write-Output '已停止工作台监督器和服务进程树。'
    $removeState = $true
} finally {
    if ($removeState) { Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue }
}

