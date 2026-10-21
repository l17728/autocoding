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
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)" -ErrorAction SilentlyContinue
    if (-not $process) {
        Write-Output '登记的工作台进程已退出。'
        $removeState = $true
        exit 0
    }

    $expectedEntry = Join-Path $projectRoot 'tools\workbench_supervisor.py'
    $commandLine = [string]$process.CommandLine
    if ($commandLine -notlike "*$expectedEntry*" -or $commandLine -notlike "*--port $($state.port)*") {
        throw "PID $($state.pid) 的命令行与本项目工作台不匹配；为保护其他程序，未停止它。"
    }

    Stop-Process -Id $state.pid -Force -ErrorAction Stop
    Write-Output "已停止工作台服务（PID $($state.pid)）。"
    $removeState = $true
} finally {
    if ($removeState) { Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue }
}

