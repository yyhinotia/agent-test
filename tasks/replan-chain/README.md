# replan-chain：稳定触发 PAE 重规划的小型主任务

## 目的

用一个 **4 节点 / 3 层 / 2 版计划** 的任务，稳定触发一次 PAE `REPLAN`，并验证：

1. attempt 0 至少有一个节点失败（`gate`）；
2. attempt 1 全局成功；
3. 与失败链路无关的成功节点（`baseline`）被复用；
4. 上游 `prepare` 目标变化后，旧成功节点 `probe` **必须失效重跑**（传递依赖失效）；
5. `probe` 重跑后 `gate` 才能通过。

计划由 `fixture.json` 固定为两版（attempt 0 / attempt 1），避免 LLM planner 的不确定性；
**节点执行使用真实 LLM**。任务只读写 `.replan-demo/` 下两个小文件，不调用 bash，
通常 10–30 秒完成。

## 任务结构

```text
attempt 0:
  layer0  baseline (写 BASELINE-OK)   prepare (写 value.txt = 0)
  layer1  probe (读取 value.txt -> output.value = "0")
  layer2  gate  (只比较 probe.output.value 与 1 -> failed)

attempt 1:
  layer0  baseline (签名未变 -> 复用，不重跑)
          prepare  (goal 改为写 1 -> 重跑)
  layer1  probe    (签名未变，但上游 prepare 已重跑 -> 必须失效重跑)
  layer2  gate     (attempt 0 失败 -> 重跑；读到 "1" -> success)
```

`gate` 的 `tool_scope` 为不存在的工具名，工具中心为空；它只能依据注入的上游
`probe.output.value` 判断，无法读取文件来“绕过”旧值。

## 运行

```powershell
pwsh -File tasks/replan-chain/run.ps1
```

或手动执行：

```powershell
Remove-Item -Recurse -Force .agent-test-replan, .replan-demo -ErrorAction SilentlyContinue
uv run python -m agent_test.app `
  --task "验证 PAE 重规划与传递依赖失效（replan-chain）" `
  --fixture tasks/replan-chain/fixture.json `
  --workspace-dir .agent-test-replan `
  --max-plan-attempt 2 `
  --max-concurrency 2 `
  --auto-approve
```

如果本机 `uv run` 不可用（例如缓存目录受限），直接使用项目虚拟环境：

```powershell
.venv\Scripts\python.exe -m agent_test.app --task "验证 PAE 重规划与传递依赖失效（replan-chain）" --fixture tasks/replan-chain/fixture.json --workspace-dir .agent-test-replan --max-plan-attempt 2 --max-concurrency 2 --auto-approve
```

## 预期结果

CLI 末行应为：

```text
[scheduler] 最终状态: success
[scheduler] 尝试次数: 2 / 2
```

`baseline`、`probe`、`gate` 最终均为 `success`，`probe` 在 attempt 1 被重跑。

### 判定证据

`.agent-test-replan/checkpoints/replan-chain.jsonl`：

- 共 6 条 checkpoint（attempt 0 有 3 层，attempt 1 有 3 层）；
- attempt 0 的最后一条 `global_status` 为 `failed_fatal`（或 `failed_recoverable`），
  `gate` 为 `failed`；
- attempt 1 的最后一条 `global_status` 为 `success`，4 个节点均为 `success`。

`.agent-test-replan/logs/runtime.log`：

```text
[scheduler] node end attempt=0 ... node=gate status=failed ...
[scheduler] plan source=fixture attempt=1 ...
[scheduler] reuse success attempt=1 node=baseline ...
[scheduler] drop success attempt=1 node=probe reason=dependency_changed(prepare) ...
```

`.agent-test-replan/sessions/replan-chain/`：

- `..._baseline_att0.jsonl` 存在，**没有** `..._baseline_att1.jsonl`（复用未重跑）；
- `..._prepare_att0/att1.jsonl`、`..._probe_att0/att1.jsonl`、
  `..._gate_att0/att1.jsonl` 均存在。

`.replan-demo/value.txt` 最终内容为 `1`。

### 反向验证（可选）

临时把 `src/agent_test/app/scheduler.py` 中 `_reusable_successes` 的依赖闭包判断去掉，
再跑一次，应观察到 `probe` 在 attempt 1 被复用（没有 `probe_att1` 会话），
`gate` 再次因 `actual=0` 失败，最终 `failed` 或重规划耗尽。这正是本任务要防住的回归。

## 清理

```powershell
Remove-Item -Recurse -Force .agent-test-replan, .replan-demo -ErrorAction SilentlyContinue
```

两个目录均已加入 `.gitignore`。
