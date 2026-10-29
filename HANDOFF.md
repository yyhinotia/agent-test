# HANDOFF — agent-test（2026-10-30）

> 说明：本文件保留上一轮交接的历史记录。`docs/` 下的报告产物已从当前工作区清理，
> 文中相关数字仅作历史参考；当前可运行状态以 `README.md`、`src/`、`tests/`、
> `tasks/replan-chain/` 为准。

> 交接范围：`E:\workspace\agent-test`  
> 当前测试基线：**258 passed**（普通 shell；受限沙箱 247 + 11 管道受限）  
> 最近一次真实 task：`arch-evolution-report`，**6/6 节点 success**，使用 `.agent-test` 新目录结构。

---

## 1. 当前状态 TL;DR

- Scheduler / PAE / 子图 / Checkpoint / 断点恢复已实现；
- `subagent` 工具已实现（`span` 为兼容别名）；
- Application / CLI 默认工作区为 `cwd/.agent-test`；
- 普通对话 session：`.agent-test/sessions/{session_id}/session.jsonl`；
- task 节点 session：`.agent-test/sessions/{graph_id}/{node_session}.jsonl`；
- task Checkpoint：`.agent-test/checkpoints/{graph_id}.jsonl`；
- 最近一次 task 在 2026-10-29 23:59 → 2026-10-30 00:15 完成，最终状态 **success**；
- 新增 `--max-turns`，用于提高 task 节点 Agent 的回合上限；
- 旧版 `sessions/`、`logs/`、`checkpoints/` 目录在本工作区当前不存在；
  如在他机看到，属于历史运行副本，不是当前默认路径。

---

## 2. 最近一次 task 结果（当前有效产物）

### 执行

```bash
uv run python -m agent_test.app --task "生成架构演进报告" --auto-approve
```

- graph_id：`arch-evolution-report`
- 开始：2026-10-29 23:59:56
- 完成：2026-10-30 00:15:34
- DAG：6 节点 / 5 层
  - layer0：`step_1`、`step_2`
  - layer1：`step_3`
  - layer2：`step_4`
  - layer3：`step_5`
  - layer4：`step_6`
- 节点状态：**6/6 success**
- 最终 checkpoint：`arch-evolution-report_v1_att0_L4_c5`
- 最终 `global_status`：**success**（新版 checkpoint 评估顺序修复已验证）

### 当前目录

```text
.agent-test/
├── checkpoints/
│   └── arch-evolution-report.jsonl          # 5 条 checkpoint（c1–c5）
├── logs/
│   └── runtime.log                          # 本次 task 日志
└── sessions/
    ├── _planner/
    │   └── planner_att0_0e53555a.jsonl
    └── arch-evolution-report/
        ├── arch-evolution-report_step_1_att0.jsonl
        ├── arch-evolution-report_step_2_att0.jsonl
        ├── arch-evolution-report_step_3_att0.jsonl
        ├── arch-evolution-report_step_4_att0.jsonl
        ├── arch-evolution-report_step_5_att0.jsonl
        └── arch-evolution-report_step_6_att0.jsonl
```

### 产物一致性

最新主报告：

```text
docs/architecture-evolution-report.md
bytes = 38,089
lines = 379
sha256 = d3689ae7d7e3317177d11740e3fd8ddab68eb88f7cef175b6ac3fd90d00d1f76
```

最新 `docs/report-acceptance.md` 已同步到该指标，并重新列出 10 个产物；此前
“acceptance 指标陈旧 / `_verify_report.py` 缺失”的问题已由这次重跑修复。

`docs/_verify_report.py` 当前存在（12,954 字节 / 284 行）。

---

## 3. 目录与文件约定

### 3.1 `.agent-test` 工作区（默认）

```text
<project>/.agent-test/
├── logs/
│   └── runtime.log
├── sessions/
│   ├── {session_id}/
│   │   └── session.jsonl                    # 普通对话
│   ├── _planner/
│   │   └── planner_att{N}_{uuid}.jsonl      # task planner
│   └── {graph_id}/
│       └── {graph_id}_{node_id}_att{N}.jsonl # task 节点 Agent
└── checkpoints/
    └── {graph_id}.jsonl                     # 每个图独立 checkpoint
```

- `.agent-test/` 已加入 `.gitignore`；
- 结果文件（`docs/*.md`、任务脚本等）不受限制，仍写任务指定位置；
- 直接使用 `Session(...)` 且不传 `session_file` 时，仍支持旧的扁平路径
  `{persist_dir}/{session_id}.jsonl`，用于兼容旧代码。

### 3.2 旧路径（历史数据）

```text
sessions/*.jsonl
logs/runtime.log
checkpoints/dag_checkpoints.jsonl
```

这些是改造前的运行数据，保留未删。旧的 v1/v2/v3 长任务仍有 19 条 checkpoint，
但其中的 c19 是旧代码写入的 `global_status=running`；当前代码已修复最终
checkpoint 评估顺序。**不要用旧目录判断新代码行为。**

---

## 4. 关键代码位置

| 模块 | 文件 | 说明 |
|---|---|---|
| Session | `src/agent_test/session/session.py` | `persist=False` 纯内存、`session_file` 自定义路径、`destroy` |
| Agent | `src/agent_test/core/agent.py` | `run_task`、`from_agent`、`destroy`、StepResult 解析与 retry |
| Scheduler | `src/agent_test/app/scheduler.py` | DAG、拓扑层、子图、PAE、Checkpoint、复用/恢复、max_turns |
| Application | `src/agent_test/app/application.py` | `.agent-test` 工作区、CLI、`run_task_mode` |
| Tools | `src/agent_test/tools/subagent.py` | subagent 委托（weakref + 纯内存） |
| Tools | `src/agent_test/tools/span.py` | span 兼容别名 |
| Tools | `src/agent_test/tools/center.py` | `clone(exclude/include)`、工具注册 |
| Plans | `src/agent_test/core/plan_service.py` | Plan 创建、DAG 字典规范化 |
| Plans | `src/agent_test/types/plans.py` | Plan / PlanStep / 状态枚举 |
| Prompts | `src/agent_test/core/prompts.py` | system prompt、DAG planner、StepResult 收尾 |
| LLM | `src/agent_test/llm/adapter.py` | OpenAI 适配、NO_PROXY 清洗 |
| Human | `src/agent_test/human/service.py` | `AutoApproveService` |

---

## 5. Scheduler / PAE 行为

### 5.1 执行流

```text
plan_and_execute(goal)
  └─ PLAN: fixture -> 注入 planner -> LLM -> fallback
  └─ ACT: submit(plan)
       └─ TaskDAG.topo_sort(): Kahn 分层 + priority 降序
       └─ _execute_layers(): 同层 gather，Semaphore 限流
       └─ 上游失败 -> 下游 BLOCKED
       └─ 每层 _save_layer_checkpoint()
  └─ EVALUATE: 全 SUCCESS 才 success
  └─ REPLAN: failed/partial 节点重置，成功节点按签名复用
```

### 5.2 Checkpoint

- 每图独立文件 `.agent-test/checkpoints/{graph_id}.jsonl`；
- `checkpoint_file=None` 时自动按 graph_id 生成；
- `resume_from_jsonl()` 未指定文件时，从 `checkpoint_dir` 选择最新文件；
- 最后一层在落盘前先评估全局状态，最终 checkpoint 记录 success/failed。

### 5.3 成功节点复用

`_same_signature` 兼容：

- `goal` 必须一致；
- 新 `acceptance_criteria` 为空表示继承旧标准；
- 新依赖是旧依赖的子集；
- 新 `tool_scope` 为空表示全部工具。

恢复时 `_recover_prior_successes()` 会从更早 checkpoint 回收成功节点。

复用判定现在由 `_reusable_successes()` 统一完成，必须同时满足：

- 节点签名按 `_same_signature` 兼容；
- **全部上游都在可复用集合中**（传递闭包）；
- `NodeState.input_snapshot` 与当前上游产出（goal/status/output/failure_*）一致。

任一上游不可复用，下游成功节点会记录
`drop success ... reason=dependency_changed(...)` 并重跑。
`_recover_prior_successes()` 对当前/历史候选做同样的闭包与快照校验；
不一致的当前 success 会降级为 PENDING，避免恢复后继续引用旧上游。

> ✅ 该风险已修复（2026-10-30）。回归任务见 `tasks/replan-chain/`：
> attempt 0 的 `gate` 失败触发重规划，attempt 1 的上游 `prepare` 变化后
> `probe` 被传递失效并重跑，`baseline` 仍正常复用。

---

## 6. CLI 用法

### 普通对话

```bash
uv run python -m agent_test.app
uv run python -m agent_test.app --session-id demo
uv run python -m agent_test.app --session-id demo --resume
```

默认生成：

```text
.agent-test/sessions/demo/session.jsonl
```

### Task 模式

```bash
uv run python -m agent_test.app \
  --task "生成架构演进报告" \
  --auto-approve
```

### 增大节点回合数

```bash
uv run python -m agent_test.app \
  --task "生成架构演进报告" \
  --auto-approve \
  --max-turns 30
```

- `--max-turns` / `--max-turn`：所有 task 节点 `max_rounds` 的下限，只增不减；
- 不改变 planner Agent 自身的固定 `max_turns=3`；
- 也不等于 PAE 重规划次数（后者用 `--max-plan-attempt`）。

### 断点恢复

```bash
uv run python -m agent_test.app \
  --resume-checkpoint \
  --workspace-dir .agent-test \
  --graph-id arch-evolution-report \
  --max-turns 30 \
  --auto-approve
```

旧 checkpoint 恢复：

```bash
uv run python -m agent_test.app \
  --resume-checkpoint \
  --checkpoint-file ./checkpoints/dag_checkpoints.jsonl \
  --max-turns 30 \
  --auto-approve
```

### 工作区覆盖

```bash
uv run python -m agent_test.app --task "..." \
  --workspace-dir ./runs/demo \
  --persist-dir ./runs/demo/sessions \
  --checkpoint-dir ./runs/demo/checkpoints \
  --log-dir ./runs/demo/logs
```

---

## 7. 测试

```bash
uv run pytest -q
# 258 passed（普通 shell）
# DSH 沙箱内：247 passed + 11 个 bash 子进程管道建连失败（环境限制）
```

重点测试：

- `tests/test_scheduler.py`：DAG、子图、PAE、Checkpoint、复用、传递依赖失效、
  max_turns、CLI；新增 4 个用例覆盖“上游变化 -> 下游失效”和恢复时的输入快照校验；
- `tests/test_workspace_layout.py`：`.agent-test` 普通/task 路径、graph checkpoint、
  `edit` 工具事实、CLI 工作区参数；
- `tests/test_run_task.py`：StepResult 解析、finish retry、最后回合收尾提示词；
- `tests/test_subagent_tool.py`：subagent/weakref/span 别名；
- `tests/test_session_pure_memory.py`：纯内存 Session / `persist=False`。

注意：本机 `NO_PROXY` 含 `[::1]` 会导致 httpx 初始化报
`Invalid port: ':1]'`。已做两层兜底：

1. `llm/adapter.py` 的 `_sanitize_no_proxy_env()`；
2. `conftest.py` 的测试日志/环境隔离 + NO_PROXY 清洗。

---

## 8. 已知告警 / 风险

1. **`bash()` unexpected keyword `max_output_tokens`**  
   最近 task 的 `step_3` 中模型误传 `max_output_tokens`，实际 bash 参数是
   `max_output_chars`；ToolCenter 返回错误后节点自行调整并成功。可考虑在
   bash schema 中增加别名或更明确描述。

2. **`工具未注册: edit`**  
   `edit` 工具确实存在且可用；告警来自节点 `tool_scope` 不含 `edit`。
   需要节点使用 `edit` 时，应在 planner 的 `tool_scope` 中加入 `edit`。

3. **聚合节点复用风险（已修复）**  
   见 §5.3。`_reusable_successes` 已加入依赖闭包与 `input_snapshot` 校验；
   回归任务 `tasks/replan-chain/` 可稳定复现“上游变化 -> 下游失效”。

4. **旧目录历史数据**  
   本工作区当前不存在 `sessions/`、`logs/`、`checkpoints/dag_checkpoints.jsonl`；
   如在他机看到，属于历史副本，不要与 `.agent-test/` 当前数据混用。

5. **planner 自身回合数固定**  
   `_llm_plan` 中 planner Agent 固定 `max_turns=3`；`--max-turns` 只影响 task
   节点。若 planner 也需要更多回合，应新增独立参数。

6. **报告换行形态**  
   最新 `report-phases.md` / `evolution-phases.md` 为 CRLF，其他多为 LF；
   已在 acceptance 中作为残余风险登记。

---

## 9. 下一步建议（按优先级）

1. **修复传递依赖复用失效**（已完成，2026-10-30）  
   `_reusable_successes` 已按拓扑闭包 + `input_snapshot` 校验复用；
   `_recover_prior_successes` 同步回收/降级。回归任务见 `tasks/replan-chain/`。

2. **补 planner 回合参数（可选）**  
   增加 `--planner-max-turns`，与节点 `--max-turns` 区分。

3. **清理/迁移旧运行数据（已确认）**  
   本工作区不存在旧 `sessions/`、`logs/`、`checkpoints/`；无需清理。

4. **补充真实 task 的回归验收**（已完成，2026-10-30）  
   `tasks/replan-chain/` 用 fixture 固定两版计划、真实 LLM 执行节点；
   attempt 0 的 `gate` 稳定失败并触发重规划，attempt 1 验证上游变化后
   `probe` 失效重跑、`baseline` 复用。连续两次运行均 2/2 尝试成功。

---

## 10. 交接检查清单

- [x] `uv run pytest -q` → 258 passed（普通 shell）；DSH 沙箱 247 + 11 管道受限
- [x] `.agent-test` 普通/task 路径写入测试
- [x] 每图独立 checkpoint 测试
- [x] 最近真实 task 6/6 success、最终 checkpoint success
- [x] `--max-turns` CLI 可用
- [x] `edit` 工具存在性确认
- [x] 传递依赖失效复用修复（`_reusable_successes` + `_recover_prior_successes`）
- [x] replan-chain 真实 LLM 回归任务（2/2 尝试 success，连续两次）
- [x] 旧运行数据确认（本工作区不存在）
- [ ] planner 独立回合参数（可选）

---

## 11. 2026-10-30 修复与回归记录

### 11.1 传递依赖失效

- `Scheduler._reusable_successes()`：按拓扑顺序计算可复用成功节点集合；
  条件为签名兼容、全部上游可复用、`input_snapshot` 与当前上游产出一致。
- `Scheduler._reconcile_state()`：不再只看节点自身签名；不可复用的 success
  会记录 `drop success ... reason=dependency_changed(<dep>)` 并重置。
- `Scheduler._recover_prior_successes()`：当前 success 优先，历史 checkpoint
  只补齐缺失节点；闭包/快照不通过的当前 success 会降级为 PENDING。
- 新增回归用例（`tests/test_scheduler.py`）：
  - `test_replan_invalidates_downstream_when_upstream_changes`
  - `test_replan_keeps_unrelated_success_nodes`
  - `test_resume_does_not_recover_downstream_when_input_changed`
  - `test_resume_recovers_downstream_when_input_matches`

### 11.2 replan-chain 任务

- 位置：`tasks/replan-chain/`（`fixture.json`、`README.md`、`run.ps1`）。
- 结构：4 节点 / 3 层 / 2 版计划。
  - attempt 0：`baseline` 与 `prepare` 成功，`probe` 成功，`gate` 失败；
  - attempt 1：`prepare` 目标改为写 `1`，`probe` 因上游变化失效重跑，
    `baseline` 复用，`gate` 成功。
- 实测（真实 LLM，DeepSeek flash）：
  - 第 1 次：00:47:07 → 00:47:30，status=success，2/2 尝试，6 条 checkpoint；
  - 第 2 次：00:47:53 → 00:48:04，同样 success；
  - `reuse success ... node=baseline`、
    `drop success ... node=probe reason=dependency_changed(prepare)`；
  - `sessions/replan-chain/` 中 `baseline` 只有 att0，`probe`/`gate` 有 att0/att1。

### 11.3 注释清理

按“只保留功能说明类注释”清理了 `src/` 与 `tests/`：移除知识来源、知识包/
增量知识引用、真实会话复盘编号与产品对标等非功能性说明；保留参数、行为、
兼容规则与安全约束说明。未改动 `docs/` 交付物。
