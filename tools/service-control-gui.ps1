# 开发智能体协作平台 · 桌面服务控制器
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:StartScript = Join-Path $PSScriptRoot 'start-local.ps1'
$script:StopScript = Join-Path $PSScriptRoot 'stop-local.ps1'
$script:ControlDirectory = Join-Path $script:ProjectRoot '.local\service-control'
$script:HomeUrl = 'http://127.0.0.1:8765'
$script:HealthUrl = 'http://127.0.0.1:8765/health'
$script:ActionProcess = $null
$script:Action = ''

$form = New-Object System.Windows.Forms.Form
$form.Text = '开发智能体协作平台 · 服务控制'
$form.StartPosition = 'CenterScreen'
$form.Size = [System.Drawing.Size]::new(600, 335)
$form.MinimumSize = $form.Size
$form.MaximizeBox = $false
$form.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 10)
$form.BackColor = [System.Drawing.Color]::White

$title = New-Object System.Windows.Forms.Label
$title.Text = '开发智能体协作平台 · 本地工作台'
$title.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 16, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = [System.Drawing.Point]::new(28, 22)
$form.Controls.Add($title)

$status = New-Object System.Windows.Forms.Label
$status.Text = '正在检查服务状态…'
$status.Font = [System.Drawing.Font]::new('Microsoft YaHei UI', 11)
$status.AutoSize = $true
$status.Location = [System.Drawing.Point]::new(31, 78)
$form.Controls.Add($status)

$detail = New-Object System.Windows.Forms.Label
$detail.Text = "首页：$script:HomeUrl`r`n日志：$script:ControlDirectory"
$detail.AutoSize = $true
$detail.ForeColor = [System.Drawing.Color]::DimGray
$detail.Location = [System.Drawing.Point]::new(32, 112)
$form.Controls.Add($detail)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = '启动服务'
$startButton.Size = [System.Drawing.Size]::new(125, 44)
$startButton.Location = [System.Drawing.Point]::new(30, 185)
$form.Controls.Add($startButton)

$stopButton = New-Object System.Windows.Forms.Button
$stopButton.Text = '停止服务'
$stopButton.Size = [System.Drawing.Size]::new(125, 44)
$stopButton.Location = [System.Drawing.Point]::new(170, 185)
$form.Controls.Add($stopButton)

$homeButton = New-Object System.Windows.Forms.Button
$homeButton.Text = '打开首页'
$homeButton.Size = [System.Drawing.Size]::new(125, 44)
$homeButton.Location = [System.Drawing.Point]::new(310, 185)
$form.Controls.Add($homeButton)

$logsButton = New-Object System.Windows.Forms.Button
$logsButton.Text = '打开日志'
$logsButton.Size = [System.Drawing.Size]::new(125, 44)
$logsButton.Location = [System.Drawing.Point]::new(450, 185)
$form.Controls.Add($logsButton)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = '关闭此窗口不会停止服务。服务登录令牌保存在项目 .local\workbench\owner-token.txt。'
$hint.AutoSize = $true
$hint.ForeColor = [System.Drawing.Color]::DimGray
$hint.Location = [System.Drawing.Point]::new(32, 255)
$form.Controls.Add($hint)

function Test-LocalHealth {
    try {
        $response = Invoke-WebRequest -Uri $script:HealthUrl -Method Get -TimeoutSec 2 -UseBasicParsing
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch { return $false }
}

function Set-Controls([bool]$running, [bool]$busy) {
    $startButton.Enabled = -not $running -and -not $busy
    $stopButton.Enabled = $running -and -not $busy
    $homeButton.Enabled = $running
    $logsButton.Enabled = -not $busy
}

function Start-ControlAction([ValidateSet('start', 'stop')][string]$action) {
    if ($script:ActionProcess -and -not $script:ActionProcess.HasExited) { return }
    $scriptPath = if ($action -eq 'start') { $script:StartScript } else { $script:StopScript }
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        [System.Windows.Forms.MessageBox]::Show("控制脚本不存在：`r`n$scriptPath", '无法控制服务', 'OK', 'Error') | Out-Null
        return
    }
    New-Item -ItemType Directory -Path $script:ControlDirectory -Force | Out-Null
    $script:Action = $action
    $status.Text = if ($action -eq 'start') { '正在启动服务…' } else { '正在停止服务…' }
    $status.ForeColor = [System.Drawing.Color]::DarkOrange
    Set-Controls $false $true
    $out = Join-Path $script:ControlDirectory "gui-$action.log"
    $err = Join-Path $script:ControlDirectory "gui-$action-error.log"
    try {
        $script:ActionProcess = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath) `
            -WorkingDirectory $script:ProjectRoot -WindowStyle Hidden `
            -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    } catch {
        $script:Action = ''
        $status.Text = '操作无法启动；请打开日志查看。'
        $status.ForeColor = [System.Drawing.Color]::Firebrick
        Set-Controls $false $false
    }
}

$startButton.Add_Click({ Start-ControlAction 'start' })
$stopButton.Add_Click({ Start-ControlAction 'stop' })
$homeButton.Add_Click({ Start-Process $script:HomeUrl })
$logsButton.Add_Click({ New-Item -ItemType Directory -Path $script:ControlDirectory -Force | Out-Null; Start-Process explorer.exe $script:ControlDirectory })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({
    $running = Test-LocalHealth
    $busy = $script:ActionProcess -and -not $script:ActionProcess.HasExited
    if ($running) {
        $status.Text = '服务运行中。'
        $status.ForeColor = [System.Drawing.Color]::ForestGreen
        if ($script:Action -eq 'start') { Start-Process $script:HomeUrl; $script:Action = '' }
    } elseif ($busy) {
        $status.Text = if ($script:Action -eq 'start') { '正在启动服务并等待就绪…' } else { '正在停止服务…' }
        $status.ForeColor = [System.Drawing.Color]::DarkOrange
    } elseif ($script:Action -eq 'stop') {
        $status.Text = '服务已停止。数据和项目文件均保留。'
        $status.ForeColor = [System.Drawing.Color]::DimGray
        $script:Action = ''
    } elseif ($script:Action -eq 'start') {
        $status.Text = '启动失败；请打开日志查看原因。'
        $status.ForeColor = [System.Drawing.Color]::Firebrick
        $script:Action = ''
    } else {
        $status.Text = '服务未运行。'
        $status.ForeColor = [System.Drawing.Color]::DimGray
    }
    Set-Controls $running $busy
})
$form.Add_Shown({ $timer.Start() })
$form.Add_FormClosed({ $timer.Stop() })
[void]$form.ShowDialog()

