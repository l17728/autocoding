# 开发智能体协作平台 · 本地工作台启动脚本
# 由服务控制 GUI 调用，也可在 PowerShell 中直接执行。
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$entry = Join-Path $projectRoot 'tools\workbench.py'
$supervisor = Join-Path $projectRoot 'tools\workbench_supervisor.py'
$ui = Join-Path $projectRoot 'ui\dist\index.html'
$controlDirectory = Join-Path $projectRoot '.local\service-control'
$stateFile = Join-Path $controlDirectory 'workbench.json'
$logFile = Join-Path $controlDirectory 'workbench.log'
$errorFile = Join-Path $controlDirectory 'workbench-error.log'
$tokenFile = Join-Path $projectRoot '.local\workbench\owner-token.txt'
$healthUrl = "http://127.0.0.1:$Port/health"

function Test-WorkbenchHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -Method Get -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) { return $false }
        if (-not (Test-Path -LiteralPath $tokenFile)) { return $false }
        $token = (Get-Content -LiteralPath $tokenFile -Raw -Encoding UTF8).Trim()
        $identity = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/project" -Method Get -TimeoutSec 2 `
            -Headers @{ Authorization = "Bearer $token" }
        return [string]::Equals(([string]$identity.root).TrimEnd('\'), $projectRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Get-ListeningOwner {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
}

if (-not (Test-Path -LiteralPath $python)) { throw "找不到项目虚拟环境 Python：$python" }
if (-not (Test-Path -LiteralPath $entry)) { throw "找不到工作台入口：$entry" }
if (-not (Test-Path -LiteralPath $supervisor)) { throw "找不到工作台监督器：$supervisor" }
if (-not (Test-Path -LiteralPath $ui)) { throw "找不到已构建的前端：$ui。请在 ui 目录运行 npm.cmd run build。" }

New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null
    # Serialize the port check, stale-state cleanup and supervisor registration.
    # A named mutex is released by Windows if this launcher exits unexpectedly.
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try { $scope = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-', '') }
    finally { $hash.Dispose() }
    $startMutex = [Threading.Mutex]::new($false, "Local\AutocodingWorkbenchStart_$scope")
    if (-not $startMutex.WaitOne(0)) {
        for ($attempt = 0; $attempt -lt 150; $attempt++) {
            if (Test-WorkbenchHealth) { Write-Output "服务已运行：http://127.0.0.1:$Port"; exit 0 }
            Start-Sleep -Seconds 1
        }
        throw '另一个启动流程尚未完成，且服务仍未就绪。请查看工作台日志。'
    }
if (Test-WorkbenchHealth) {
    Write-Output "服务已运行：$healthUrl"
    exit 0
}

$owner = Get-ListeningOwner
if ($owner) {
    throw "端口 $Port 已被 PID $owner 占用，且不是可确认的工作台服务；为保护其他程序已取消启动。"
}

if (Test-Path -LiteralPath $stateFile) {
    Remove-Item -LiteralPath $stateFile -Force
}

$process = Start-Process -FilePath $python -ArgumentList @($supervisor, '--project', $projectRoot, '--port', "$Port") `
    -WorkingDirectory $projectRoot -RedirectStandardOutput $logFile -RedirectStandardError $errorFile `
    -WindowStyle Hidden -PassThru

$state = [ordered]@{
    pid = $process.Id
    started_utc = $process.StartTime.ToUniversalTime().ToString('o')
    project_root = $projectRoot
    port = $Port
}
$stateTemp = "$stateFile.$($process.Id).tmp"
$state | ConvertTo-Json | Set-Content -LiteralPath $stateTemp -Encoding UTF8
Move-Item -LiteralPath $stateTemp -Destination $stateFile -Force

for ($attempt = 0; $attempt -lt 150; $attempt++) {
    if ($process.HasExited) {
        Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
        throw "工作台启动后退出。请查看：$errorFile"
    }
    if (Test-WorkbenchHealth) {
        Write-Output "服务已启动：http://127.0.0.1:$Port"
        exit 0
    }
    Start-Sleep -Seconds 1
}

if (-not $process.HasExited) { & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null }
Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
throw "工作台未在 150 秒内就绪。请查看：$errorFile"

