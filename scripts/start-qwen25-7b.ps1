#Requires -Version 5.1
<#
.SYNOPSIS
    agent-test 本地 Qwen2.5-7B-Instruct (Q4_K_M) 一键启动脚本（llama.cpp CUDA + 3060 6GB GPU）。

.DESCRIPTION
    - 定位 HF_HOME 下 CUDA 版 llama.cpp（默认 E:\hf_home\runtime\llama.cpp-cuda，
      需含 ggml-cuda.dll 与 cudart/cublas 运行库）与 GGUF（默认 E:\hf_home\gguf\qwen2.5-7b-instruct-q4_k_m.gguf）；
    - 以 OpenAI 兼容服务方式启动 llama-server 到 127.0.0.1:<Port>，默认全量 GPU offload；
    - KV Cache 使用 Q8_0 以在 6GB 显存上留出更长上下文；
    - 日志写入 <repo>/logs/llama-server-qwen2.5-7b-instruct-<Port>.log(.err.log)，PID 写入同名 .pid；
    - 幂等：端口上已在服务该 alias 时直接复用（除非 -ForceRestart）；
    - -RunDemo "<prompt>"：服务就绪后临时以 BASE_URI/MODEL_NAME 覆盖环境变量，
      运行 `uv run python main.py "<prompt>"`（无需手工改 .env）；
    - -Stop：按 PID 文件停止本脚本启动的服务。

.PARAMETER Port
    服务端口，默认 8080。
.PARAMETER ModelPath
    覆盖 GGUF 路径；缺省自动指向 E:\hf_home\gguf\qwen2.5-7b-instruct-q4_k_m.gguf。
.PARAMETER LlamaDir
    覆盖 llama.cpp CUDA 目录；缺省 $env:HF_HOME\runtime\llama.cpp-cuda。
.PARAMETER CtxSize
    上下文长度（token），默认 8192。
.PARAMETER GpuLayers
    卸载到 GPU 的层数，默认 99（全部；6GB 显存不足时可调小让 CPU 兜底）。
.PARAMETER Threads
    CPU 线程数（调度/兜底），默认 8。
.PARAMETER ForceRestart
    端口已有服务时先停止再重启。
.PARAMETER Stop
    停止指定端口上由本脚本启动的服务。
.PARAMETER RunDemo
    服务就绪后执行一次 agent-test 演示（提示词由此参数传入）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 -RunDemo "请用 read 工具读取 README.md 前 10 行"
    powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 -Stop
#>
[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$ModelPath = "",
    [string]$LlamaDir = "",
    [int]$CtxSize = 8192,
    [int]$GpuLayers = 99,
    [int]$Threads = 8,
    [switch]$ForceRestart,
    [switch]$Stop,
    [string]$RunDemo = ""
)
$ErrorActionPreference = 'Stop'
$alias = 'qwen2.5-7b-instruct'
$logPrefix = $alias -replace '\.', ''
$repoRoot = Split-Path -Parent $PSScriptRoot   # scripts/ 的上一级即仓库根
$logDir = Join-Path $repoRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdoutLog = Join-Path $logDir "llama-server-$logPrefix-$Port.log"
$stderrLog = Join-Path $logDir "llama-server-$logPrefix-$Port.err.log"
$pidFile = Join-Path $logDir "llama-server-$logPrefix-$Port.pid"

function Get-HfHome { if ($env:HF_HOME) { $env:HF_HOME } else { 'E:\hf_home' } }

# ---- 定位 CUDA 版 llama.cpp ----
if (-not $LlamaDir) { $LlamaDir = Join-Path (Get-HfHome) 'runtime\llama.cpp-cuda' }
$serverExe = Join-Path $LlamaDir 'llama-server.exe'
if (-not (Test-Path -LiteralPath $serverExe)) {
    throw "llama-server.exe not found: $serverExe`n请先把 CUDA 版 llama.cpp 解压到该目录。"
}
if (-not (Test-Path -LiteralPath (Join-Path $LlamaDir 'ggml-cuda.dll'))) {
    throw "缺少 ggml-cuda.dll：$LlamaDir 不是 CUDA 版 llama.cpp。`n请使用 b10679 的 bin-win-cuda-12.4 构建，并把 cudart-llama-bin-win-cuda-12.4 里的 dll 一起放入。"
}

# ---- 定位 GGUF ----
if (-not $ModelPath) { $ModelPath = Join-Path (Get-HfHome) "gguf\qwen2.5-7b-instruct-q4_k_m.gguf" }
if (-not (Test-Path -LiteralPath $ModelPath)) {
    $ggufDir = Split-Path -Parent $ModelPath
    $avail = @(Get-ChildItem -Path $ggufDir -Filter '*.gguf' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    throw "model not found: $ModelPath`n$ggufDir 下可用: $($avail -join ', ')"
}
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

function Invoke-Demo([string]$prompt) {
    Write-Host "[demo] 临时环境: BASE_URI=http://127.0.0.1:$Port/v1  MODEL_NAME=$alias"
    Push-Location $repoRoot
    try {
        $env:BASE_URI = "http://127.0.0.1:$Port/v1"
        $env:MODEL_NAME = $alias
        & uv run python main.py $prompt
        if ($LASTEXITCODE -ne 0) { Write-Host "[demo] main.py 退出码=$LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

if ($Stop) { Stop-ServerOnPort $Port; exit 0 }

# ---- 幂等：端口已在服务本 alias ----
$served = Get-ServedModelIds $Port
if ($served -and -not $ForceRestart) {
    Write-Host "[ok] 127.0.0.1:$Port 已在服务: $($served -join ', ')"
    Write-Host "     model: $ModelPath ($modelSizeMB MB)"
    if ($RunDemo) { Invoke-Demo $RunDemo }
    exit 0
}
if ($served -and $ForceRestart) { Stop-ServerOnPort $Port; Start-Sleep -Seconds 1 }

# ---- 启动 llama-server（隐藏窗口 + 日志重定向）----
$argList = @('-m', $ModelPath, '--host', '127.0.0.1', '--port', "$Port",
             '-c', "$CtxSize", '-ngl', "$GpuLayers",
             '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0',
             '-t', "$Threads", '--alias', $alias)
Write-Host "[start] $serverExe"
Write-Host "        model: $ModelPath ($modelSizeMB MB) | port: $Port | ctx: $CtxSize | ngl: $GpuLayers | threads: $Threads"
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
    if ((Get-ServedModelIds $Port) -contains $alias) { $ready = $true; break }
}
if (-not $ready) {
    Write-Error "180s 内服务未就绪，日志: $stderrLog"
    exit 1
}
Write-Host "[ok] 就绪: http://127.0.0.1:$Port/v1  model=$alias (GPU 层数=$GpuLayers)"
Write-Host "     .env 可配置: BASE_URI=http://127.0.0.1:$Port/v1"
Write-Host "                 MODEL_NAME=$alias"
Write-Host "     日志: $stdoutLog"
Write-Host "     停止: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Stop"

if ($RunDemo) { Invoke-Demo $RunDemo }