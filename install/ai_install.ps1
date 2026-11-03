#requires -Version 5.1
<#
  RVC 翻唱站 · AI 无人值守安装器（给 AI Agent / 批处理用）
  ------------------------------------------------------------------
  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File ai_install.ps1 -Dir "D:\RVC" -Gpu auto
  参数：
    -Dir    安装目录（默认 %LOCALAPPDATA%\RVC-Cover-Station；不能是盘根）
    -Gpu    auto | gpu | cpu（默认 auto：有 NVIDIA 卡就装 CUDA 版）
    -Tag    锁定版本，例如 v1.1.0（默认空 = main 分支最新，长效）
    -Start  装完自动启动面板（默认不启动，适合无人值守）
    -DryRun 只验证下载和参数，不真装

  输出：最后一行是 JSON（ok / exit_code / dir / version / missing / error），
        退出码 0 = 成功，1 = 失败。
  说明：本脚本只负责「下载最新的 一键安装.bat → 用环境变量喂参数 → 无人值守跑完 →
        校验目录与版本」。安装细节（测速、装便携 Python、两个 venv、依赖、基础模型）
        全在那个 bat 里，别在这里重复实现。
#>
[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:LOCALAPPDATA 'RVC-Cover-Station'),
    [ValidateSet('auto', 'gpu', 'cpu')][string]$Gpu = 'auto',
    [string]$Tag = '',
    [string]$Installer = '',
    [switch]$Start,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

$Repo     = 'panxian-AI/rvc-cover-station'
$RawBase  = 'https://raw.githubusercontent.com/' + $Repo + '/main/'
$ZipBase  = 'https://codeload.github.com/' + $Repo + '/zip/refs/heads/main'
$Prefixes = @('', 'https://ghfast.top/', 'https://ghproxy.net/', 'https://gh.llkk.cc/',
              'https://gh-proxy.com/', 'https://gh.kkgithub.com/', 'https://ghproxy.cc/',
              'https://ghproxy.link/')
$BatLeaf  = [uri]::EscapeDataString('一键安装.bat')
$Out      = [ordered]@{ ok = $false; exit_code = -1; dir = ''; gpu = $Gpu; version = ''
                        missing = @(); panel = 'http://127.0.0.1:8899'; error = '' }
$Stage    = Join-Path $env:TEMP ('rvc-ai-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:UsedPrefix = ''

function Say($m) { Write-Host ('[AI] ' + $m) -ForegroundColor Cyan }

# GitHub 上拿到的 .bat 偶尔是 LF（仓库 core.autocrlf 归一过），cmd 里 goto/括号块会出怪事
# （实测报 "'AI' 不是内部或外部命令"）。落盘前统一按 GBK 读、CRLF 写，稳定。
function Repair-Bat([string]$path) {
    $enc = [Text.Encoding]::GetEncoding(936)
    $t = [IO.File]::ReadAllText($path, $enc)
    if ($t -match "`r`n") { return }
    $t = $t -replace "`r?`n", "`r`n"
    [IO.File]::WriteAllText($path, $t, $enc)
    Say '安装器原来是 LF 换行，已修正为 CRLF'
}

# 输出纯 ASCII 的 JSON：控制台代码页是 GBK 时，中文直接打印会被 AI/日志吃成乱码；
# 转成 \uXXXX 转义后，任何解析器都能还原中文。
function To-AsciiJson($obj) {
    $j = $obj | ConvertTo-Json -Compress -Depth 4
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $j.ToCharArray()) {
        if ([int]$ch -lt 128) { [void]$sb.Append($ch) }
        else { [void]$sb.Append(('\u{0:x4}' -f [int]$ch)) }
    }
    return $sb.ToString()
}

function Get-Installer([string]$dest) {
    $errs = @()
    foreach ($p in $Prefixes) {
        $name = if ($p) { $p } else { 'direct' }
        try {
            Invoke-WebRequest -Uri ($p + $RawBase + $BatLeaf) -OutFile $dest -UseBasicParsing -TimeoutSec 25
            if ((Get-Item $dest).Length -gt 3000) {
                Repair-Bat $dest
                $script:UsedPrefix = $p
                Say ('安装器来自线路 ' + $name)
                return $true
            }
            $errs += ($name + ': 内容不完整')
        } catch { $errs += ($name + ': ' + $_.Exception.Message) }
    }
    Say 'raw 线路都不通，改用源码包里的安装器…'
    foreach ($p in $Prefixes) {
        $name = if ($p) { $p } else { 'direct' }
        $zip = Join-Path $Stage 'repo.zip'
        try {
            Invoke-WebRequest -Uri ($p + $ZipBase) -OutFile $zip -UseBasicParsing -TimeoutSec 90
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $z = [IO.Compression.ZipFile]::OpenRead($zip)
            try {
                $hit = $z.Entries | Where-Object { $_.FullName -like '*/一键安装.bat' } | Select-Object -First 1
                if ($hit) {
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($hit, $dest, $true)
                    if ((Get-Item $dest).Length -gt 3000) {
                        Repair-Bat $dest
                        Say ('安装器来自源码包（' + $name + '）')
                        return $true
                    }
                }
            } finally { $z.Dispose() }
        } catch { $errs += ('zip ' + $name + ': ' + $_.Exception.Message) }
    }
    throw ('下载安装器失败，所有线路都不通：' + ($errs -join ' | '))
}

try {
    # ---- 1. 目录 ----
    $Dir = $Dir.Trim().Trim('"')
    if ($Dir -notmatch '^[A-Za-z]:[\\/]' -and $Dir -notmatch '^\\\\') {
        $Dir = [IO.Path]::GetFullPath($Dir)
    }
    $Dir = $Dir -replace '/', '\'
    $Dir = $Dir.TrimEnd('\')
    if ($Dir -match '^[A-Za-z]:$' -or $Dir.Length -lt 4) { throw ('安装目录不能是盘根，请给一个具体文件夹：' + $Dir) }
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $Out.dir = $Dir

    # ---- 2. 显卡模式 ----
    if ($Gpu -eq 'auto') {
        $nv = $false
        try { $nv = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue) } catch { }
        if (-not $nv) {
            try {
                $nv = [bool](Get-CimInstance Win32_VideoController -ErrorAction Stop |
                             Where-Object { $_.Name -match 'NVIDIA' })
            } catch { }
        }
        $Gpu = if ($nv) { 'gpu' } else { 'cpu' }
        $Out.gpu = $Gpu
        Say ('检测到' + $(if ($nv) { ' NVIDIA 显卡 → CUDA 版' } else { '没有 NVIDIA 显卡 → CPU 版' }))
    }

    # ---- 3. 下载安装器 ----
    if ($Installer) {
        if (-not (Test-Path $Installer)) { throw ('指定的安装器不存在：' + $Installer) }
        New-Item -ItemType Directory -Force -Path $Stage | Out-Null
        $bat = Join-Path $Stage 'local.bat'
        Copy-Item -LiteralPath $Installer -Destination $bat -Force
        Repair-Bat $bat
        Say ('用本地安装器：' + $Installer)
    } else {
        New-Item -ItemType Directory -Force -Path $Stage | Out-Null
        $bat = Join-Path $Stage 'setup.bat'
        Say '下载安装器（逐个线路试）…'
        Get-Installer $bat | Out-Null
    }

    # ---- 4. 无人值守运行 ----
    $env:RVC_UNATTENDED = '1'
    $env:RVC_DEFDIR     = $Dir
    $env:RVC_GPU        = $Gpu
    $env:RVC_NO_START   = if ($Start) { '0' } else { '1' }
    # 已经探到能用的加速线路就告诉 bat，省得它自己测速全挂时直接放弃
    $env:RVC_GH_PREFIX  = $script:UsedPrefix
    if ($Tag) { $env:RVC_TAG = $Tag } else { $env:RVC_TAG = '' }
    if ($DryRun) { $env:RVC_DRYRUN = '1' } else { $env:RVC_DRYRUN = '' }

    Say ('开始安装：' + $Dir + ' / ' + $Gpu + $(if ($Tag) { ' / ' + $Tag } else { ' / main 最新' }))
    if (-not $DryRun) { Say '依赖下载可能要十几分钟到半小时，别关窗口…' }
    & cmd.exe /c ('"' + $bat + '"')
    $code = $LASTEXITCODE
    $Out.exit_code = $code

    if ($DryRun) {
        $Out.ok = ($code -eq 0)
        if (-not $Out.ok) { $Out.error = 'dry-run 失败，看上面 bat 的输出' }
        return
    }

    # ---- 5. 校验 ----
    $stack = Join-Path $Dir 'RVC-Data'
    $checks = [ordered]@{
        webui    = Join-Path $stack 'RVC-WebUI\webui.py'
        py_web   = Join-Path $stack 'RVC-WebUI\.venv\Scripts\python.exe'
        py_api   = Join-Path $stack 'RVCSVC\apienv\Scripts\python.exe'
        panel    = Join-Path $stack 'RVCSVC\dashboard\panel.py'
        launcher = Join-Path $Dir '启动RVC.bat'
    }
    $missing = @()
    foreach ($k in $checks.Keys) { if (-not (Test-Path $checks[$k])) { $missing += $k } }
    $Out.missing = $missing
    $vf = Join-Path $stack 'RVCSVC\VERSION'
    if (Test-Path $vf) { $Out.version = ((Get-Content $vf -TotalCount 1) -replace '\s', '') }
    $Out.ok = ($code -eq 0 -and $missing.Count -eq 0)
    if (-not $Out.ok) {
        $failLog = Join-Path $Dir '_install_failed.txt'
        if (Test-Path $failLog) { $Out.error = (Get-Content $failLog -Raw).Trim() }
        elseif ($missing.Count) { $Out.error = '缺少文件：' + ($missing -join ', ') }
        else { $Out.error = '安装器退出码 ' + $code }
    }
}
catch {
    $Out.ok = $false
    $Out.error = $_.Exception.Message
}
finally {
    [Console]::Out.WriteLine((To-AsciiJson $Out))
    try { if (Test-Path $Stage) { [IO.Directory]::Delete($Stage, $true) } } catch { }
}
if ($Out.ok) { exit 0 } else { exit 1 }
