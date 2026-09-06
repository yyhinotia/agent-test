#Requires -Version 5.1
<#
.SYNOPSIS
    agent-test 本地小模型一键启动脚本（Qwen2.5-0.5B-Instruct GGUF + llama.cpp server）。

.DESCRIPTION
    - 从 HF_HOME（默认 E:\hf_home）自动定位 llama.cpp 运行时与 Qwen GGUF 缓存，
      以 OpenAI 兼容服务方式启动 llama-server 到 127.0.0.1:<Port>；
    - 日志写入 <repo>/logs/llama-server-<Port>.log(.err.log)，PID 写入同名 .pid；
    - 幂等：端口上已在服务 qwen 时直接复用（除非 -ForceRestart）；
    - -RunDemo "<prompt>"：服务就绪后自动跑 `uv run python main.py "<prompt>"`，
      项目通过 .env（BASE_URI/MODEL_NAME）消费该模型；
    - -Stop：按 PID 文件停止本脚本启动的服务。

.PARAMETER Port
    服务端口，默认 8080（需与 .env 的 BASE_URI 一致）。
.PARAMETER ModelPath
    覆盖 GGUF 路径；缺省自动在 HF_HOME Qwen 缓存中探测。
.PARAMETER LlamaDir
    覆盖 llama.cpp 目录；缺省 $env:HF_HOME\runtime\llama.cpp。
.PARAMETER CtxSize
    上下文长度（token），默认 4096。
.PARAMETER Threads
    CPU 线程数，默认 8。
.PARAMETER ForceRestart
    端口已有服务时先停止再重启。
.PARAMETER Stop
    停止指定端口上由本脚本启动的服务。
.PARAMETER RunDemo
    服务就绪后执行一次 agent-test 演示（提示词由此参数传入）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1 -Port 8081
    powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1 -Stop
    powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1 -RunDemo "请用 read 工具读取 README.md 前 15 行"
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$ModelPath = "",
    [string]$LlamaDir = "",
    [int]$CtxSize = 4096,
    [int]$Threads = 8,
    [switch]$ForceRestart,
    [switch]$Stop,
    [string]$RunDemo = ""
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级即仓库根
$logDir = Join-Path $repoRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdoutLog = Join-Path $logDir "llama-server-$Port.log"
$stderrLog = Join-Path $logDir "llama-server-$Port.err.log"
$pidFile = Join-Path $logDir "llama-server-$Port.pid"

function Get-HfHome { if ($env:HF_HOME) { $env:HF_HOME } else { 'E:\hf_home' } }

# ---- 定位 llama.cpp 可执行文件 ----
if (-not $LlamaDir) { $LlamaDir = Join-Path (Get-HfHome) 'runtime\llama.cpp' }
$serverExe = Join-Path $LlamaDir 'llama-server.exe'
if (-not (Test-Path -LiteralPath $serverExe)) { throw "llama-server.exe not found: $serverExe" }

# ---- 定位 Qwen GGUF（快照符号链接 -> blobs 真实文件）----
if (-not $ModelPath) {
    $cache = Join-Path (Get-HfHome) 'hub\models--Qwen--Qwen2.5-0.5B-Instruct-GGUF'
    $snapDir = Join-Path $cache 'snapshots'
    if (-not (Test-Path $snapDir)) { throw "Qwen cache not found under $cache" }
    $gguf = Get-ChildItem -Path $snapDir -Recurse -Filter '*.gguf' -File -ErrorAction Stop | Select-Object -First 1
    if (-not $gguf) { throw "no .gguf found under $snapDir" }
    if ($gguf.LinkType) {
        # HF Hub 在 Windows 用符号链接指向 blobs\<hash>，取其真实目标文件
        $target = @($gguf.Target)[0]
        $ModelPath = if ($target) { Join-Path $gguf.DirectoryName $target } else { $gguf.FullName }
    } else {
        $ModelPath = $gguf.FullName
    }
}
if (-not (Test-Path -LiteralPath $ModelPath)) { throw "model file not found: $ModelPath" }
$modelSizeMB = [math]::Round((Get-Item -LiteralPath $ModelPath).Length / 1MB, 1)

function Get-ServedModelIds([int]$p) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$p/v1/models" -TimeoutSec 3
        return @($r.data | ForEach-Object { $_.id })
    } catch { return @() }
}

function Stop-ServerOnPort([int]$p) {
    if (Test-Path -LiteralPath $pidFile) {
        $pidNum = [int]((Get-Content -LiteralPath $pidFile -Raw).Trim())
        $proc = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
        if ($proc) { Stop-Process -Id $pidNum -Force; Write-Host "[stop] pid=$pidNum (port $p)" }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        return
    }
    Write-Host "[warn] 端口 $p 没有本脚本的 PID 文件，未自动停止；请手动停止 llama-server。"
}

if ($Stop) { Stop-ServerOnPort $Port; exit 0 }

# ---- 幂等：端口已在服务 qwen ----
$served = Get-ServedModelIds $Port
if ($served -and -not $ForceRestart) {
    Write-Host "[ok] 127.0.0.1:$Port 已在服务: $($served -join ', ')"
    Write-Host "     model: $ModelPath ($modelSizeMB MB)"
    if ($RunDemo) {
        Push-Location $repoRoot
        try { & uv run python main.py $RunDemo } finally { Pop-Location }
    }
    exit 0
}
if ($served -and $ForceRestart) { Stop-ServerOnPort $Port; Start-Sleep -Seconds 1 }

# ---- 启动 llama-server（隐藏窗口 + 日志重定向）----
$argList = @('-m', $ModelPath, '--host', '127.0.0.1', '--port', "$Port",
             '-c', "$CtxSize", '-t', "$Threads", '-ngl', '0',
             '--alias', 'qwen2.5-0.5b-instruct')
Write-Host "[start] $serverExe"
Write-Host "        model: $ModelPath ($modelSizeMB MB) | port: $Port | ctx: $CtxSize | threads: $Threads"
$proc = Start-Process -FilePath $serverExe -ArgumentList $argList -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ascii
Write-Host "[wait] pid=$($proc.Id) 等待 /v1/models 就绪 ..."

$deadline = (Get-Date).AddSeconds(180)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 1500
    if ($proc.HasExited) {
        Write-Error "llama-server 提前退出 (code=$($proc.ExitCode))，日志: $stderrLog"
        exit 1
    }
    if ((Get-ServedModelIds $Port) -contains 'qwen2.5-0.5b-instruct') { $ready = $true; break }
}
if (-not $ready) {
    Write-Error "180s 内服务未就绪，日志: $stderrLog"
    exit 1
}
Write-Host "[ok] 就绪: http://127.0.0.1:$Port/v1  model=qwen2.5-0.5b-instruct"
Write-Host "     .env 应配置: BASE_URI=http://127.0.0.1:$Port/v1"
Write-Host "                 MODEL_NAME=qwen2.5-0.5b-instruct"
Write-Host "     日志: $stdoutLog"

if ($RunDemo) {
    Write-Host "[demo] uv run python main.py `"$RunDemo`""
    Push-Location $repoRoot
    try { & uv run python main.py $RunDemo } finally { Pop-Location }
}