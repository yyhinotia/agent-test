# agent-test

基于 **OpenAI Chat Completions** 与 **Pydantic v2** 的模块化 ReAct Agent 运行时。
项目覆盖从单轮对话到多节点 DAG 任务的完整链路：

- 两阶段工具循环：**串行审批 → 并行执行**，审批与执行使用两个独立状态机；
- 会话 JSONL 持久化、窗口化续聊、上下文自动压缩、错误事实可回放；
- DAG 调度器：**Plan-Act-Evaluate/Replan（PAE）**、拓扑分层、子图、Layer 级
  Checkpoint、断点恢复；
- 成功节点复用采用**传递依赖闭包 + 输入快照校验**，上游变化会使下游成功节点失效；
- `subagent` 一次性纯内存子 Agent 委托；
- CLI 交互模式、`--task` 任务模式、fixture 回放与离线测试。

当前版本：`agent_test.__version__ == "0.6.0"`。

---

## 1. 快速开始

### 1.1 安装与配置

```bash
# 安装 uv（如未安装）
# https://docs.astral.sh/uv/

uv sync
copy .env.example .env        # Windows
# cp .env.example .env        # POSIX
```

`.env` 需要三个变量：

```dotenv
API_KEY=...
BASE_URI=https://api.openai.com/v1   # 任意 OpenAI 兼容端点
MODEL_NAME=gpt-4o-mini
```

模型端只需兼容 OpenAI Chat Completions，不绑定具体厂商。

### 1.2 单轮演示

```bash
uv run python main.py "你好，请介绍一下你自己"
uv run python main.py "请读取 README.md 的前 30 行"
uv run python main.py --resume <session_id> "接着刚才的问题继续"
```

演示入口默认使用 `cwd/.agent-test`，会话写入
`.agent-test/sessions/{session_id}/session.jsonl`，日志写入
`.agent-test/logs/runtime.log`。

### 1.3 交互式 CLI

```bash
uv run python -m agent_test.app
uv run python -m agent_test.app --session-id demo
uv run python -m agent_test.app --session-id demo --resume
```

- `/exit` 或 `Ctrl-D` 退出；
- 行尾 `\` 续行，一行 = 一个回合；
- 连续重复输入自动去重；
- 主循环 / 审批 / `ask_user` 共用统一的 StdinDispatcher。

### 1.4 任务模式（DAG / PAE）

```bash
uv run python -m agent_test.app \
  --task "分析项目结构并生成报告" \
  --max-concurrency 3 \
  --max-plan-attempt 3 \
  --auto-approve
```

返回码：`0` 表示 DAG 全部节点成功；`1` 表示重规划耗尽后仍未达成。

### 1.5 运行测试

```bash
uv run pytest -q
# 258 passed（普通 shell）
```

---

## 2. 目录与模块

```text
agent-test/
├── main.py                       # 单轮演示入口
├── pyproject.toml / uv.lock      # 依赖与锁文件
├── .env.example                  # 环境变量示例
├── conftest.py                   # pytest 隔离（日志/NO_PROXY）
├── scripts/
│   ├── audit_session.py          # 会话审计（审批/执行/压缩等机械判定）
│   └── start-*.ps1               # 可选：本地 llama.cpp + Qwen 启动脚本
├── src/agent_test/
│   ├── app/                      # CLI 应用 + DAG 调度器
│   ├── core/                     # ReActAgent / InBox / prompts / PlanService
│   ├── session/                  # 会话事件与 JSONL 持久化
│   ├── llm/                      # 适配器 / TokenMeter / Compactor / 注册表
│   ├── tools/                    # ToolCenter、内置工具、bash、ask、subagent
│   ├── policy/                   # 命令治理（工作目录/规则/审批）
│   ├── human/                    # AskService 与 StdinDispatcher
│   ├── types/                    # 消息 / 事件 / 计划 / 工具 Schema
│   ├── exceptions/               # 统一异常体系
│   ├── log/                      # RuntimeLog
│   └── utils.py
├── tests/                        # pytest 测试（离线）
├── tasks/replan-chain/           # 可稳定触发 PAE 重规划的示例任务
└── .agent-test/                  # 运行时工作区（gitignore）
```

### 2.1 模块职责

| 模块 | 职责 |
| --- | --- |
| `app/application.py` | `Application` 交互模式、`--task` 入口、CLI 参数 |
| `app/scheduler.py` | `TaskNode` / `TaskDAG` / `GraphState` / `CheckpointRecord` / `Scheduler` / PAE |
| `app/cli_ask_service.py` | CLI 审批服务：stdin + 审计日志 + EOF 降级 |
| `core/agent.py` | `ReactAgent`：turn/step 循环、`run_task`、`from_agent`、`destroy` |
| `core/inbox.py` | turn/step 两区消息队列，每个 Agent 私有 |
| `core/prompts.py` | 系统提示词、DAG planner prompt、节点收尾 prompt |
| `core/plan_service.py` | `Plan` 创建、DAG 字典规范化 |
| `session/session.py` | JSONL 事件、窗口化 `reload`、压缩打标/摘要、`from_file` |
| `llm/adapter.py` | OpenAI 流式适配，返回 `(message, end_reason, usage)` |
| `llm/token_meter.py` | 按最近一次 usage 判断上下文阈值 |
| `llm/compactor.py` | 按 `(turn, step)` 压缩历史，两级降级 |
| `tools/center.py` | 工具注册/执行/克隆、策略闸门、`pre_decision` 旁路 |
| `tools/builtin.py` | `read/find/edit/grep/list/write` + 全局 `tool_center` 单例 |
| `tools/bash.py` | 异步命令执行、平台感知解码、输出裁剪 |
| `tools/ask.py` | `ask_user` / `confirm` 工具 |
| `tools/subagent.py` | 一次性纯内存子 Agent；`span` 为兼容别名 |
| `policy/policy.py` | 工作目录、allowlist、静态风险、越界审批 |
| `human/service.py` | `AskService` / `ConsoleAskService` / `AutoApproveService` |
| `human/stdin_dispatcher.py` | 后台线程独占 stdin + 队列 + `drain_policy` |

---

## 3. 运行模型

### 3.1 一次 turn 的链路

```text
用户输入
  └─> inbox.turn
      └─> turn/start
          └─> step 循环（直到 end_reason 非空）
              ├─> session.derive_messages() ──> llm.stream()
              │    返回 (assistant, end_reason, usage)
              ├─> TokenMeter 超阈值 ──> Compactor 压缩
              │    旧事件打标 + compact/summary 进入上下文窗口
              ├─> 工具阶段两段分离
              │    ① _pre_step（串行）：policy.decide_tool + approver.confirm
              │       放行 / 拒绝 / 拦截；拒绝的动作 execution=not_started
              │    ② _exec_one（并行）：asyncio.gather 执行已放行动作
              └─> 结果先入 inbox.step，由下一步 claim 或回合收尾写 session
          └─> turn/end(reason)
```

### 3.2 四条不变式

- **审批先于执行**：整步工具调用先完成全量预检与审批，再并行执行；被拒或被拦截
  的动作标记 `execution=not_started`，从未产生副作用。
- **双状态机**：`ApprovalResult`（`auto` / `approved` / `denied` / `blocked`）与
  `ExecutionResult`（`not_started` / `success` / `failed`）分离，避免把“用户拒绝”
  误读成“执行失败”而反复重试。
- **单点闸门**：`ToolCenter.execute` 是任何直接执行入口的安全兜底；Agent 路径通过
  `pre_decision` 跳过重复决策，旁路与独立决策互斥。
- **可回放**：错误事实（`location` / `error_type` / `message`）写入 session 的
  `runtime/error` 事件，完整堆栈写入 `runtime.log`，二者通过 `session_id` 关联。

### 3.3 日志与异常

| 载体 | 内容 |
| --- | --- |
| `.agent-test/logs/runtime.log` | 完整 traceback、异常类型、session/turn/step 上下文 |
| `.agent-test/sessions/.../session.jsonl` | `runtime/error` 事件：位置 + 异常类型 + 摘要 |

异常基类为 `AgentBaseError`，包含 `SessionEditError`、`SessionContinuityError`、
`MessageEditError`、`LlmError`、`ToolExecutionError`。工具内部异常不向上抛，
由 `ToolCenter` 统一降级为 `is_error=True` 的工具结果，回传给 LLM。

---

## 4. 工具与命令治理

### 4.1 内置工具

| 工具 | 作用 |
| --- | --- |
| `read` | 读取文件，支持 `start_idx` / `end_idx` / `has_more` |
| `find` | 按文件名或内容在目录树中查找 |
| `grep` | 正则搜索文件/目录，支持 offset/limit 分页 |
| `list` | 缩进树浏览目录 |
| `write` | 创建或整体覆盖文件（自动建父目录） |
| `edit` | 精确文本替换 |
| `bash` | 执行一条命令，返回退出码 / stdout / stderr / 编码 |
| `ask_user` | 向用户提问，支持 2–4 个候选与多选 |
| `confirm` | 请求用户批准继续 |
| `subagent` | 委托一次性纯内存子 Agent |
| `span` | `subagent` 的兼容别名 |

### 4.2 bash

- 参数：`command`、`workdir`、`timeout`、`encoding`、`max_output_chars`；
- 平台感知解码：Windows 默认按控制台输出代码页（常见 `cp936` / `utf-8`），
  POSIX 默认 `utf-8`；自动模式下解码出现替换字符会按候选兜底；
- 异步执行：`asyncio.create_subprocess_shell`，超时 kill 并标记 `timed_out`；
- 返回：`{command, cwd, exit_code, encoding, stdout, stderr, timed_out, truncated}`。

### 4.3 命令治理

`CommandPolicy.decide()` 的判定顺序：

1. **工作目录检测**：workdir 必须存在且为目录，并位于 `allowed_roots` 内；
   超出允许根属于可审批项，批准后记入允许根；
2. **已审批前缀 allowlist**：命中则直接执行；
3. **静态风险规则**：`deny`（破坏性命令）、`approve`（git push / 安装 / 网络等）、
   `warn`；
4. **越界路径**：涉及允许根之外的绝对路径时至少要求审批。

决策结果：`EXECUTE` / `REQUIRE_APPROVAL` / `DENY`。未配置 `approver` 时，
`REQUIRE_APPROVAL` 直接返回“需要审批”的错误结果；`DENY` 始终被拦截。

### 4.4 HITL 与 subagent

- `ask_user` / `confirm` 通过可注入的 `AskService` 呈现；默认 `ConsoleAskService`，
  无人值守时使用 `AutoApproveService`；
- `subagent`：父 Agent 以 `weakref` 捕获，子 Agent `persist=False`，工具中心克隆并
  排除 `subagent/span/edit`，防止递归与闭包引用；父 Agent 销毁后安全返回错误文本；
- `subagent` 的 LLM 客户端为浅拷贝，避免覆盖父 Agent 的错误事实绑定。

---

## 5. 会话与持久化

### 5.1 目录

```text
<project>/.agent-test/
├── logs/runtime.log
├── sessions/
│   ├── {session_id}/session.jsonl              # 普通对话
│   ├── _planner/planner_att{N}_{uuid}.jsonl    # task planner
│   └── {graph_id}/{graph_id}_{node}_att{N}.jsonl # task 节点 Agent
└── checkpoints/{graph_id}.jsonl                # 每图独立 Layer Checkpoint
```

- `Application` / CLI 默认工作区为 `cwd/.agent-test`；可用 `--workspace-dir` 覆盖，
  或分别用 `--persist-dir` / `--checkpoint-dir` / `--log-dir` 覆盖子目录；
- 直接使用 `Session(...)` 而不传 `session_file` 时，保持兼容的扁平路径
  `{persist_dir}/{session_id}.jsonl`；
- 任务产出的结果文件按任务目标写入指定位置，不受工作区限制。

### 5.2 Session

```python
from agent_test import Session, create_agent

s = Session("scratch", persist=False)   # 纯内存
s.append("user/message", data=msg)      # 只进内存
s.destroy()

agent = create_agent()                  # 新建持久化 Agent（默认 persist_dir="sessions"）
agent2 = create_agent(
    agent.session.session_id,
    str(agent.session.persist_dir),
    resume=True,
)                                       # 窗口化续聊（要显式沿用原 persist_dir）
```

能力：

- `append(event_type, data, *, turn=0, step=0)`：内存记录 + 同步落盘，`seq` 使用自维护游标；
- `append_error(...)`：写入 `runtime/error` 事实事件；
- `from_file(...)`：恢复全量事件并校验 seq 连续性；
- `reload(...)`：从尾部倒序读取，只加载最近一次压缩之后的窗口（摘要 + 近期事件）；
- `mark_compacted` / `insert_after`：压缩打标、插入摘要并全量重写 JSONL；
- `max_turn_step()`：返回全量事件中最大 turn/step，供续聊续接编号；
- `persist=False`：纯内存模式，供 subagent 与 DAG 节点使用。

### 5.3 上下文压缩

- `TokenMeter(max_context_tokens=128000, threshold_ratio=0.8)`：按最近一次 LLM usage
  判断是否超过阈值；`reset()` 在压缩成功后清零；
- `Compactor(session, summarize=None, llm_client=None)`：按 `(turn, step)` 排序，
  以 step 粒度保留最近 `remain_turns` 个 step，把更早历史摘要为
  `compact/summary` 事件；无可压缩块时降级为全量压缩；
- 压缩后内存 `events` 只保留上下文窗口，磁盘 JSONL 保持全量历史（seq 连续）。

---

## 6. DAG 调度与 PAE

### 6.1 TaskNode / TaskDAG

`TaskNode` 字段：

```text
node_id, goal, acceptance_criteria, tool_scope,
depends_on, max_rounds, priority, metadata
```

`TaskDAG`：

- `init_dag()`：校验重复节点 / 悬空依赖 / 环，并生成 `layers`；
- `topo_sort()`：Kahn 分层，同层按 `priority` 降序；
- `subgraph(node_ids, include_upstream=True, include_downstream=False)`：抽取子图闭包。

### 6.2 执行流

```text
plan_and_execute(goal)                      # PAE 主循环
  ├─ PLAN      _plan(goal, replan_context)  # fixture -> 注入 planner -> LLM -> fallback
  ├─ ACT       submit(plan_dict)
  │    ├─ _reconcile_state(new_dag)         # 成功节点复用 / 失效重置
  │    ├─ TaskDAG.topo_sort()               # 分层 + priority
  │    ├─ _execute_layers()
  │    │    ├─ 同层 asyncio.gather + Semaphore
  │    │    ├─ 上游失败 -> 下游 BLOCKED
  │    │    └─ 每层 _save_layer_checkpoint()
  ├─ EVALUATE  _evaluate_global_status()    # 全部 SUCCESS 才成功
  └─ REPLAN    未达成且次数未耗尽 -> attempt+1，回到 PLAN
```

- 最后一层在落盘前先评估全局状态，最终 Checkpoint 记录 success / failed；
- `rounds_used` 表示 `run_task` 的外层回合数（模型未给出合法 StepResult 时的重试次数），
  不是工具 step 数；step 数可从 session 事件观察。

### 6.3 成功节点复用（传递依赖失效）

复用不再只看节点自身签名，而由 `_reusable_successes()` 按拓扑顺序统一判定：

1. 节点签名按 `_same_signature` 兼容；
2. **全部上游都在可复用集合中**（传递闭包）；
3. `NodeState.input_snapshot` 与当前上游产出（goal/status/output/failure_*）一致。

任一上游不可复用，下游 success 会记录
`drop success ... reason=dependency_changed(<dep>)` 并重跑。

`_recover_prior_successes()` 对当前与历史 checkpoint 候选做同样的闭包/快照校验；
不通过的当前 success 会降级为 `PENDING`，避免恢复后继续引用旧上游。

已知边界：`execute_subgraph` 中手工重置 success 后强制重跑的路径，建议后续补
`invalidate_downstream`；当前覆盖的是重规划与断点恢复两条主路径。

### 6.4 Checkpoint 与恢复

- 每个图独立文件：`.agent-test/checkpoints/{graph_id}.jsonl`；
- 每层一条深拷贝快照，包含 `TaskDAG` + `GraphState` + 节点输出/证据/输入快照；
- `rollback_to_checkpoint(steps=1 | target_checkpoint_id=...)`：回退并截断 JSONL；
- `resume_from_jsonl()`：从最新 Layer 的下一层继续；若仍有未达成节点，自动进入 PAE；
- `--resume-checkpoint`：CLI 断点恢复，可用 `--graph-id` 指定图，或自动选最新文件。

### 6.5 节点结果契约

节点 Agent 必须在最后一条 assistant 消息输出：

```json
{
  "status": "success | partial | failed",
  "output": {},
  "evidence": [],
  "self_check": true,
  "failure_type": null,
  "failure_reason": null
}
```

解析支持纯 JSON、Markdown 围栏、嵌入 JSON 块；缺失时保守降级为
`partial + acceptance_failed`（已 finish）或 `failed + round_exhausted`。

> `success` 的判定是“模型自报 + `self_check` 合取”；调度器不会独立执行
> `acceptance_criteria`。需要机器验收时，应在 DAG 中增加独立校验节点或外部验收命令。

### 6.6 子图

- `Scheduler.submit(plan_dict, node_ids=None)`：传 `node_ids` 时保留完整 DAG，
  只执行选中节点 + 上游闭包，未选中节点保持 `PENDING`；
- `Scheduler.execute_subgraph(node_ids)`：在已载入 DAG 上继续执行子图；
- 子图执行完成后按选中节点集合评估全局状态。

---

## 7. CLI 参数

### 7.1 任务模式

```bash
uv run python -m agent_test.app \
  --task "分析项目并生成报告" \
  --max-concurrency 3 \
  --max-plan-attempt 3 \
  --auto-approve
```

| 参数 | 说明 |
| --- | --- |
| `--task` | 提交高层目标，由 Scheduler 规划并执行 |
| `--max-concurrency` | 同层并行节点数上限（默认 3） |
| `--max-plan-attempt` | 最大 PLAN/REPLAN 次数（默认 3） |
| `--max-turns` / `--max-turn` | 节点 `max_rounds` 的下限（只增不减） |
| `--fixture` | 使用 DAG fixture（优先于 LLM planner，便于回放） |
| `--auto-approve` | 自动批准 `REQUIRE_APPROVAL`，`DENY` 仍拦截 |
| `--task-persist` / `--no-task-persist` | 节点会话是否落盘（默认落盘） |
| `--workspace-dir` / `--persist-dir` / `--checkpoint-dir` / `--log-dir` | 工作区覆盖 |
| `--resume-checkpoint` / `--graph-id` | 从最新 Layer Checkpoint 恢复 |

### 7.2 工作区覆盖示例

```bash
uv run python -m agent_test.app --task "..." \
  --workspace-dir ./runs/demo \
  --persist-dir ./runs/demo/sessions \
  --checkpoint-dir ./runs/demo/checkpoints \
  --log-dir ./runs/demo/logs
```

### 7.3 断点恢复

```bash
uv run python -m agent_test.app \
  --resume-checkpoint \
  --workspace-dir .agent-test \
  --graph-id <graph_id> \
  --max-turns 30 \
  --auto-approve
```

---

## 8. 示例任务：`tasks/replan-chain`

该任务用 **4 节点 / 3 层 / 2 版 fixture** 稳定触发一次 PAE 重规划，
并验证传递依赖失效：

```text
attempt 0:
  layer0  baseline  -> success（写 BASELINE-OK）
          prepare   -> success（value.txt = 0）
  layer1  probe     -> success（output.value = "0"）
  layer2  gate      -> failed（只比较 probe.output.value 与 1）

attempt 1:
  layer0  baseline  -> 签名未变，复用
          prepare   -> goal 改为写 1，按签名变化重跑
  layer1  probe     -> 上游已重跑，旧 success 失效，重跑
  layer2  gate      -> 读到 "1"，success
```

运行：

```powershell
pwsh -File tasks/replan-chain/run.ps1
```

期望：最终 `success`，`2 / 2` 尝试，6 条 checkpoint；日志出现
`reuse success ... node=baseline` 与
`drop success ... node=probe reason=dependency_changed(prepare)`；
`baseline` 只有 `att0` 会话，`probe` / `gate` 有 `att0` 与 `att1`。

任务细节与判定证据见 `tasks/replan-chain/README.md`。

---

## 9. 测试

```bash
uv run pytest -q
# 258 passed
```

重点测试：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_scheduler.py` | 拓扑分层、子图、PAE、Checkpoint、传递依赖失效、max_turns、CLI |
| `tests/test_run_task.py` | StepResult 解析、finish 重试、最后回合收尾 |
| `tests/test_workspace_layout.py` | `.agent-test` 路径、graph checkpoint、CLI 工作区参数 |
| `tests/test_subagent_tool.py` | subagent / weakref / span 别名 |
| `tests/test_session_pure_memory.py` | 纯内存 Session / `persist=False` |
| `tests/test_compact.py` | TokenMeter + Compactor 端到端压缩 |
| `tests/test_bash_tool.py` | bash 回显 / 退出码 / 超时 / 编码 |
| `tests/test_approval_parallel.py` | 串行审批 + 并行执行、双状态机 |

`Scheduler` 的 `planner` / `node_executor` 可注入，绝大多数测试不需要真实 LLM。

> 在禁止子进程管道的受限沙箱中，`test_bash_tool.py` 与
> `test_ask_user_tool.py::test_approver_approves_then_remembers` 可能因
> `[WinError 5]` 失败；这是运行环境限制，不是代码回归。

---

## 10. 配置与环境

- 三个必需变量：`API_KEY` / `BASE_URI` / `MODEL_NAME`；
- 任意 OpenAI 兼容端点均可；本地 llama.cpp 也是同一接口，可参考
  `scripts/start-llama-qwen.ps1`、`start-qwen25-3b.ps1`、`start-qwen25-7b.ps1`；
- `llm/adapter.py` 会清理 `NO_PROXY` 中 `[::1]` 这类 httpx 无法解析的条目；
- Windows 控制台输出由 `bash` 工具按代码页自动解码；
- `.agent-test`、`.agent-test-replan`、`.replan-demo`、`.env`、`.tmp` 均已加入
  `.gitignore`。

---

## 11. 设计边界

- **全局成功不是机器验收**：`global_status=success` 等价于所有节点自检通过，
  不保证 `acceptance_criteria` 被独立验证；重要任务应加最终校验节点。
- **REPLAN 不恢复失败节点记忆**：`agent_memory_dump` 当前不注入新 Agent，
  失败节点在重规划时从零开始；成功节点按签名/依赖/快照复用。
- **调度是层屏障**：同层 `gather` 等待最慢节点，未实现依赖就绪队列；
  层内快节点会被慢节点拖住。
- **`--max-turns` 是外层回合下限**：不限制工具 step 数；长节点可能触发上下文压缩。
- **planner 自身回合数固定为 3**：`--max-turns` 只影响 task 节点。
- **bash 依赖子进程管道**：受限沙箱中可能无法创建，属环境限制。

---

## 12. 相关文档

- `tasks/replan-chain/README.md`：重规划示例任务的运行方式与判定证据；
- `scripts/audit_session.py`：会话审计与机械判定工具。
