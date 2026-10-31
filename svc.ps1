param(
    [Parameter(Position = 0)][string]$Action = 'list',
    [Parameter(Position = 1)][string]$Target = ''
)

<#
================================================================================
 svc —— 服务台的命令行入口（与图形界面共用 services.json）
--------------------------------------------------------------------------------
 用法：
   svc list                      列出全部服务及状态
   svc status <id>               查看某个服务状态
   svc start <id>                启动服务（带端口的会等就绪）
   svc stop  <id>                停止服务
   svc open  <id>                打开服务网页
 供计划任务、脚本、其它工具调用；id 见 svc list 输出。
================================================================================
#>

$ErrorActionPreference = 'Continue'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ConfigFile = Join-Path $AppDir 'services.json'
$LogDir = Join-Path $AppDir 'logs'

function Get-SvcList {
    if (-not (Test-Path $ConfigFile)) { Write-Host "[ERROR] 找不到 $ConfigFile"; return @() }
    try {
        $cfg = (Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8) | ConvertFrom-Json
        return @($cfg.services)
    } catch {
        Write-Host "[ERROR] 配置解析失败：$($_.Exception.Message)"
        return @()
    }
}

function Test-PortOpen {
    param([int]$Port, [int]$TimeoutMs = 700)
    if ($Port -le 0) { return $false }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { $client.EndConnect($async); return $true }
        return $false
    } catch { return $false } finally { try { $client.Close() } catch { } }
}

function Get-State {
    param($Svc)
    if ($Svc.kind -eq 'tool') { return '工具' }
    if ($Svc.kind -eq 'docker') {
        $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
        if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
        try {
            $raw = & $dockerExe inspect -f '{{.State.Running}}' $Svc.container 2>$null
            if ("$raw".Trim() -eq 'true') { return '运行中' }
        } catch { }
        return '已停止'
    }
    if ($Svc.port) {
        if (Test-PortOpen -Port ([int]$Svc.port)) { return '运行中' }
        return '已停止'
    }
    if ($Svc.marker) {
        try {
            $procs = Get-CimInstance Win32_Process -ErrorAction Stop
            foreach ($procItem in $procs) {
                if ($procItem.CommandLine -and $procItem.CommandLine.ToLower().Contains($Svc.marker.ToLower())) { return '运行中' }
            }
        } catch { }
    }
    return '已停止'
}

function Get-Url {
    param($Svc)
    if ($Svc.url) { return $Svc.url }
    return $null
}

$list = Get-SvcList
if (-not $list) { exit 1 }

switch ($Action.ToLower()) {
    'list' {
        "{0,-22} {1,-12} {2,-8} {3}" -f 'ID', '分组', '状态', '说明'
        '-' * 88
        foreach ($svc in $list) {
            "{0,-22} {1,-12} {2,-8} {3}" -f $svc.id, $svc.group, (Get-State -Svc $svc), $svc.desc
        }
        exit 0
    }
    'status' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        "名称 : $($svc.name)"
        "状态 : $(Get-State -Svc $svc)"
        "端口 : $(if ($svc.port) { $svc.port } else { '无' })"
        "地址 : $(if (Get-Url -Svc $svc) { Get-Url -Svc $svc } else { '无' })"
        exit 0
    }
    'start' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        if ($svc.kind -eq 'tool') {
            & $svc.exe @($svc.args) | Out-Null
            Write-Host "已运行工具：$($svc.name)"
            exit 0
        }
        if ((Get-State -Svc $svc) -eq '运行中') { Write-Host "$($svc.name) 已在运行"; exit 0 }
        if ($svc.kind -eq 'docker') {
            $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
            if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
            & $dockerExe start $svc.container | Out-Null
            Write-Host "已启动容器：$($svc.container)"
            exit 0
        }
        if (-not (Test-Path $svc.exe)) { Write-Host "[ERROR] 找不到：$($svc.exe)"; exit 1 }
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        $logFile = Join-Path $LogDir ("{0}-{1}.log" -f $svc.id, (Get-Date).ToString('yyyyMMdd-HHmmss'))
        $argLine = ''
        $argList = @()
        if ($svc.args -is [string]) { $argList = @($svc.args -split '\s+' | Where-Object { $_ }) }
        elseif ($svc.args) { $argList = @($svc.args) }
        foreach ($a in $argList) { if ($a -match '[\s]') { $argLine += '"' + $a + '" ' } else { $argLine += $a + ' ' } }
        $inner = '"' + $svc.exe + '" ' + $argLine.Trim() + ' > "' + $logFile + '" 2>&1'
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'C:\Windows\System32\cmd.exe'
        $psi.Arguments = '/c "' + $inner + '"'
        $psi.WorkingDirectory = $svc.cwd
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        Write-Host "已启动 $($svc.name)  PID=$($proc.Id)"
        if ($svc.port) {
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 500
                if (Test-PortOpen -Port ([int]$svc.port)) { Write-Host "端口 $($svc.port) 已就绪"; break }
            }
        }
        exit 0
    }
    'stop' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        if ($svc.kind -eq 'docker') {
            $dockerExe = (Get-Command docker.exe -ErrorAction SilentlyContinue)
            if (-not $dockerExe) { $dockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' }
            & $dockerExe stop $svc.container | Out-Null
            Write-Host "已停止容器：$($svc.container)"
            exit 0
        }
        if ($svc.marker) {
            try {
                $procs = Get-CimInstance Win32_Process -ErrorAction Stop
                $killed = 0
                foreach ($procItem in $procs) {
                    if ($procItem.CommandLine -and $procItem.CommandLine.ToLower().Contains($svc.marker.ToLower())) {
                        Stop-Process -Id ([int]$procItem.ProcessId) -Force -ErrorAction SilentlyContinue
                        $killed++
                    }
                }
                Write-Host "已结束 $($killed) 个进程：$($svc.name)"
            } catch { Write-Host "[ERROR] $($_.Exception.Message)"; exit 1 }
        } else {
            Write-Host "[WARN] $($svc.name) 没有配置 marker，无法用命令行停止（请在服务台界面停止）"
        }
        exit 0
    }
    'open' {
        if (-not $Target) { Write-Host '[ERROR] 缺少服务 id'; exit 1 }
        $svc = $list | Where-Object { $_.id -eq $Target } | Select-Object -First 1
        if (-not $svc) { Write-Host "[ERROR] 没有 id 为 '$Target' 的服务"; exit 1 }
        $url = Get-Url -Svc $svc
        if (-not $url) { Write-Host "[WARN] $($svc.name) 没有网页入口"; exit 1 }
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $url
        $psi.UseShellExecute = $true
        [void][System.Diagnostics.Process]::Start($psi)
        Write-Host "已打开：$url"
        exit 0
    }
    default {
        Write-Host '用法: svc list | svc status <id> | svc start <id> | svc stop <id> | svc open <id>'
        exit 1
    }
}
