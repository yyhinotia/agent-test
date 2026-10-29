$ErrorActionPreference = "Stop"
Remove-Item -Recurse -Force .agent-test-replan, .replan-demo -ErrorAction SilentlyContinue

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "未找到 .venv，改用 uv run python"
    uv run python -m agent_test.app `
        --task "验证 PAE 重规划与传递依赖失效（replan-chain）" `
        --fixture tasks/replan-chain/fixture.json `
        --workspace-dir .agent-test-replan `
        --max-plan-attempt 2 `
        --max-concurrency 2 `
        --auto-approve
} else {
    & $py -m agent_test.app `
        --task "验证 PAE 重规划与传递依赖失效（replan-chain）" `
        --fixture tasks/replan-chain/fixture.json `
        --workspace-dir .agent-test-replan `
        --max-plan-attempt 2 `
        --max-concurrency 2 `
        --auto-approve
}
