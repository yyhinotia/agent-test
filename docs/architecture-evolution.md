# Agent-test 架构演进设计 — 可组合式 Agent Runtime / Agent Platform

> 本文档由架构讨论整理而成，描述当前 `react-openai-agent` 项目（OpenAI + Pydantic v2 的 ReAct Agent）如何逐步演进为一个小型的 **Agent Platform / Agent Runtime**，并给出与工业级 Agent Infrastructure 的差距评估与建议路线。

---

## 0. 核心结论

1. **定位升级**：如果把 RAG、搜索、代码分析、数据库访问、浏览器、MCP Server 等能力也纳入“服务注册 + 消息路由 + 权限 + Agent Runtime”体系，本项目不再只是一个 Multi-Agent Demo，而是在逐步形成一个**小型的 Agent Platform / Agent Runtime**。
2. **价值判断**：这是一个非常好的“接近工业级”的练习项目——但是**工业级架构思想的缩小版**，不等同于工业生产系统。
3. **最终评价**：非常值得继续做。真正需要控制的是**范围**——先把消息、任务、执行、状态四个核心语义做扎实，再向 Service / Capability / Policy 扩展；不要用堆组件的方式制造“工业感”。

---

## 1. 架构定位：Composable Agent Runtime / Agent Platform

不要叫 “Multi-Agent Framework”（太窄），更准确的定位是：

> **Composable Agent Runtime / Agent Platform**（或 Composable Multi-Agent Runtime）

核心思想（7 个要素）：

```text
Agent
   +
Message
   +
Workflow
   +
Capability
   +
Service
   +
Policy
   +
Runtime
```

---

## 2. 能力服务化：RAG 等能力独立成 Service

### 2.1 不要（把能力塞进 Agent）

```text
ReactAgent
 ├── LLM
 ├── Tool
 ├── RAG
 ├── VectorDB
 ├── Search
 └── ...
```

### 2.2 而是（Capability as a Service）

```text
                    Agent Runtime
                         │
                    Service Registry
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
   RAG Service      Search Service     Code Service
       │                 │                 │
   Vector DB          Web API          Code Index
```

Agent 只知道能力名，不知道背后实现：

```text
rag.search
search.web
code.search
```

背后可以是：

```text
Qdrant / Milvus / Elasticsearch / OpenSearch / Postgres / GitHub / MCP Server
```

Agent 不需要知道。这与已有的 `IQueue` 抽象思想完全一致（面向端口/接口编程，而不是面向实现）。

---

## 3. 能力总线：Agent 是消费者，Service 是提供者

系统整体可以形成“能力总线”：

```text
                         ┌──────────────┐
                         │   User Task  │
                         └──────┬───────┘
                                │
                         ┌──────▼───────┐
                         │  Supervisor  │
                         └──────┬───────┘
                                │
                        Agent Runtime
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
           Agent A           Agent B           Agent C
              │                 │                 │
              └─────────────────┼─────────────────┘
                                │
                         Capability Bus
                                │
       ┌────────────┬───────────┼────────────┬────────────┐
       ▼            ▼           ▼            ▼            ▼
     RAG          Search      Browser      Code        Database
   Service       Service      Service      Service      Service
```

> **Agent 是消费者，Capability Service 是提供能力的服务**——这是非常重要的架构升级。

---

## 4. MCP 作为能力服务协议（而非“变成 MCP 项目”）

远程 MCP 恰好可以作为其中一种 Service。

各 Service 暴露能力接口（Capability API）：

```text
RAG Service  →  rag.search / rag.retrieve / rag.get_document
Code Service →  code.search / code.read / code.symbol / code.dependency
DB Service   →  db.query / db.schema
```

Service 甚至不需要知道 Agent 是谁，只需要处理标准的请求管线：

```text
Request
   ↓
Authentication
   ↓
Authorization
   ↓
Validation
   ↓
Execution
   ↓
Response
```

因此 MCP 的正确位置是：

```text
Agent
  ↓
Capability Gateway
  ↓
MCP
  ↓
Service
```

而不是：

```text
Agent
  ↓
直接连接所有 MCP
```

### 4.1 统一适配层

```text
                 Capability API
                      │
                 Service Gateway
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Native       HTTP         MCP
       Service      Service      Server
```

同一个能力可以有多种实现，统一映射成 `Capability`，Agent 不关心：

```text
RAG
 ├── Native Python Service
 ├── HTTP
 └── MCP
        ↓（统一映射）
   Capability
```

---

## 5. Service Registry：注册表升级

原有的 `Agent Registry` 可以自然升级为 `Service Registry`：

```python
ServiceDescriptor(
    name="rag-service",
    version="1.2",
    capabilities=[
        "rag.search",
        "rag.retrieve",
    ],
    endpoint="...",
    auth_policy="...",
    owner="knowledge-platform",
)
```

系统启动时的注册内容：

```text
Registry
   │
   ├── researcher-agent
   ├── coder-agent
   ├── reviewer-agent
   │
   ├── rag-service
   ├── search-service
   ├── browser-service
   └── code-service
```

这就已经非常像一个 **Agent Capability Platform**。

---

## 6. 权限模型：LLM 只能请求，不能拥有权限

### 6.1 Agent → Capability 的能力授权

例如 researcher / coder / admin-agent 各自的能力白名单：

```text
researcher
   │
   ├── rag.search       ✅
   ├── search.web       ✅
   ├── code.read        ❌
   └── database.write   ❌

coder
   │
   ├── code.read        ✅
   ├── code.search      ✅
   ├── rag.search       ✅
   └── database.write   ❌

admin
   │
   ├── rag.*            ✅
   ├── code.*           ✅
   └── database.*       ✅
```

调用链路：

```text
Agent
 ↓
Request
 ↓
Authorization
 ↓
Capability
 ↓
Service
```

比简单的 `tools = [...]` 强很多。

### 6.2 多层权限模型（User → Team → Agent → Service → Capability → Resource）

```text
User
  ↓
Team
  ↓
Agent
  ↓
Service
  ↓
Capability
  ↓
Resource
```

示例：

```text
用户 Alice
   ↓
Research Team
   ↓
Research Agent
   ↓
RAG Service
   ↓
rag.search
   ↓
company-public-documents
```

Capability 还可以进一步细化约束：

```text
rag.search
  tenant      = company_a
  namespace   = public
  top_k       <= 20
```

这已经非常接近真实企业 Agent Platform 的权限思路。

### 6.3 关键设计原则：不要让 Agent 自己决定权限

千万不要：

```text
LLM：
“我需要访问 database.write”
       ↓
直接调用
```

应该：

```text
LLM
 ↓
提出 Capability Request
 ↓
Runtime
 ↓
Policy Engine
 ↓
允许 / 拒绝
 ↓
Service
```

> **LLM 可以提出请求，但不能拥有权限。**
> 这是 Agent 系统与普通 Tool Calling 之间一个非常重要的区别。

---

## 7. 双图设计：Agent Graph + Capability Graph

最终可以形成两个图：

### 7.1 Agent Graph（谁可以和谁通信）

```text
Researcher
    ↓
Coder
    ↓
Reviewer
```

### 7.2 Capability Graph（谁可以使用什么能力）

```text
Researcher
    │
    ├── RAG
    ├── Search
    └── Browser

Coder
    │
    ├── RAG
    ├── CodeIndex
    └── Git
```

整个系统因此变成：

```text
                 Agent Platform
                       │
          ┌────────────┴────────────┐
          │                         │
      Agent Graph             Capability Graph
          │                         │
      Agent → Agent             Agent → Service
          │                         │
      Message Bus              Service Bus
```

这是当前架构里**非常值得发展的方向**：状态图既约束“消息投递方向”（Agent → Agent），也约束“能力使用范围”（Agent → Service）。

---

## 8. 目标分层架构（最终演化形态）

```text
┌─────────────────────────────────────────────┐
│                 Application                 │
│       User Task / Agent Application         │
├─────────────────────────────────────────────┤
│               Orchestration                 │
│       Team / Supervisor / StateGraph        │
├─────────────────────────────────────────────┤
│                Agent Runtime                │
│       Turn / Step / LLM / Tool / Task       │
├─────────────────────────────────────────────┤
│              Communication                 │
│      MessageBus / Inbox / Outbox / RPC      │
├─────────────────────────────────────────────┤
│              Capability Layer               │
│     Registry / Discovery / Authorization    │
├─────────────────────────────────────────────┤
│                Service Layer                │
│ RAG / Search / Code / DB / Browser / MCP    │
├─────────────────────────────────────────────┤
│             Infrastructure                 │
│ Redis / DB / VectorDB / EventLog / Trace    │
└─────────────────────────────────────────────┘
```

这已经不是一个简单的 Agent Demo 了。

---

## 9. 工业级差距评估（四个台阶）

### 台阶一：单纯做

```text
Agent
 ↓
LLM
 ↓
Tool
```

**不算接近工业级。**

### 台阶二：加上

```text
Agent
+ Inbox
+ Session
+ Team
+ StateGraph
+ Message Bus
```

已经是**不错的 Agent Runtime 项目**。

### 台阶三：再加上

```text
Registry
+ Service Registry
+ Capability
+ Permission
+ Remote Service
+ MCP Adapter
```

开始进入 **Agent Platform 的架构范畴**。

### 台阶四：再加可靠性 / 可观测性

```text
Retry
ACK
Idempotency
DLQ
Cancellation
Timeout
Recovery
Tracing
Metrics
Audit
Schema Version
```

这时候才可以说：

> **在架构思想上已经相当接近工业级 Agent Infrastructure。**

---

## 10. 为什么是合适的“工业级练习项目”

**这个项目比单独学习 LangGraph、CrewAI、AutoGen 更适合当前阶段。**

因为已经具备基础组件：

```text
ReactAgent
Inbox
Session
Step
Turn
ToolCenter
LLMBase
```

可以亲自把它逐渐演化成：

```text
单 Agent
   ↓
Multi-Agent
   ↓
Agent Runtime
   ↓
Message Bus
   ↓
Service Registry
   ↓
Remote Capability
   ↓
Permission
   ↓
Distributed Runtime
   ↓
Observability
```

每一步都能对应工业系统里的真实问题。最大的收获是：

> **不是在“模仿一个框架的 API”，而是在理解为什么这些框架最终会长成这个样子。**

---

## 11. 演进路线图（建议顺序，共 11 步）

**重要建议：不要现在就同时做 Agent + Redis + RAG + MCP + Registry + Factory + 权限。**

否则很容易变成：

```text
       Redis
      /  |  \
    MCP RAG Agent
     \   |   /
      Registry
         |
      Factory
         |
     StateGraph
```

看起来非常工业，实际上每个模块都只有 30%。

最佳路线：

```text
① ReactAgent
      ↓
② AgentMessage
      ↓
③ AgentTeam
      ↓
④ StateGraph
      ↓
⑤ MessageBus abstraction
      ↓
⑥ Capability abstraction
      ↓
⑦ Service Registry
      ↓
⑧ Permission
      ↓
⑨ Remote Service / MCP
      ↓
⑩ Redis + Reliability
      ↓
⑪ Observability
```

其中第 ⑥～⑧（Capability 抽象 → Service Registry → Permission）是项目真正开始“拉开层次”的地方——此时不再只是实现“多个 Agent 怎么协作”，而是在实现：

> **“Agent 如何作为受控计算主体，发现并使用一个由平台提供的能力生态。”**

这是工业级 Agent Platform 的核心问题。

---

## 12. 范围控制建议

> 先把**消息、任务、执行、状态**四个核心语义做扎实，再向 Service / Capability / Policy 扩展；不要用堆组件的方式制造“工业感”。

对应到当前代码（react-openai-agent）：

| 核心语义 | 当前实现 | 平台化扩展方向 |
| --- | --- | --- |
| 消息 | `UserMessage / AssistantMessage / ToolResultMessage` | `AgentMessage`（多 Agent 消息） |
| 任务与执行 | `Turn / Step` | Task 编排、Supervisor |
| 状态 | `Session` + JSONL 事件流 | Checkpoint / 快照 / Trace |
| 通信 | `Inbox / Outbox` | MessageBus / RPC / Redis 队列 |
| 平台化 | —— | Service Registry / Capability / Permission / MCP |
---

## 2026-09-06：模块化重构 + 日志 / 异常体系落地

### 本次重构交付

1. **uv 化**：`pyproject.toml` 改用 hatchling build backend + PEP 735
   `[dependency-groups]` dev；删除 requirements.txt；`.venv` 与 `uv.lock`
   由 `uv sync` 管理；测试通过 `uv run pytest` 执行。
2. **模块化结构**：扁平 `src/*.py` -> `src/agent_test/` 按功能分包：
   `exceptions / log / types / core / session / llm / tools`。
3. **log 模块**（`agent_test.log.runtime_log.RuntimeLog`）：所有
   agent-runtime 日志在输出控制台的同时额外写入 `logs/runtime.log`；
   异常以 ERROR 级别记录**完整 traceback**；contextvars 自动携带
   session_id / turn / step，支持 async 任务内传播；`capture_exception`
   返回可在 session 中落盘的结构化摘要。
4. **异常体系**（`agent_test.exceptions`）：`AgentBaseError(Exception)`
   统一基类（替代方案中“baseException”命名，避免直接继承内置
   `BaseException` 吞系统信号）；`SessionEditError`、`MessageEditError`、
   `LlmError`（retryable）、`ToolExecutionError`、`SessionContinuityError`。
5. **错误分层落盘**：错误事实（location / error_type / message）写入
   session 的 `runtime/error` 事件；完整堆栈写入日志文件；二者按
   session_id 关联，便于回放与排查。
6. **顺带修复三个基础缺陷**：
   - LLM 异常不再被吞（原 `except: end_reason="error"`）-> 抛 `LlmError`
     并保留原始异常链；
   - step 产出即持久化，消除“先入 inbox 再兜底补记”的最终回答丢失窗口；
   - session seq 改用持久化游标，from_file 恢复后继续追加仍连续，
     并新增 seq 连续性校验（strict 模式抛 SessionContinuityError）。
7. **回归保障**：原有 10 个测试全部迁移到 `agent_test.*` 导入并新增
   异常 / 日志 / 错误事件端到端测试，`uv run pytest -v` 全绿。

### 目录对照（扁平 -> 模块化）

| 旧（src/*.py） | 新（src/agent_test/） |
| --- | --- |
| types.py（消息/事件/Schema/基类） | types/messages.py、types/events.py、types/tools.py；llm/adapter.py（基类） |
| session.py | session/session.py |
| inbox.py | core/inbox.py |
| llm_adapter.py | llm/adapter.py + llm/registry.py |
| tools.py | tools/center.py + tools/builtin.py |
| agent.py | core/agent.py |
| utils.py | utils.py |
| （无） | exceptions/（base/session/message/llm/tools） |
| （无） | log/runtime_log.py |


---

## 2026-09-06（收尾）：完成剩余重构任务

在「模块化重构 + 日志 / 异常体系落地」基础上补齐的收尾项：

1. **MessageEditError 真正落地**：`ToolCallBlock.args_dict` 解析工具调用
   参数 JSON 失败时抛出 `MessageEditError`（location=ToolCallBlock.args_dict，
   detail 含 tool_call_id / name，并保留原始 JSONDecodeError 异常链）——
   session 回放中的 error_type 因此更有语义，完整堆栈仍进日志文件。
2. **日志记录原始异常堆栈**：`Session.append` 在事件类型非法 / 持久化
   写入失败时，日志记录**原始** ValueError / OSError 的真实堆栈（而非
   包装异常的构造帧），对外仍抛统一领域异常 SessionEditError。
3. **Session.from_file 健壮化**：文件打开 / 读取失败（OSError）与某行
   JSON / 校验失败统一包装为 `SessionEditError`（location + 行号 detail），
   完整堆栈写入日志文件；strict 模式的 seq 断层仍抛 SessionContinuityError。
4. **测试补强**：新增 MessageEditError 触发、日志记录原始堆栈、
   from_file 损坏行、端到端错误事件回放测试；
   `uv run pytest -v` 全绿（65 passed）。
5. **文档与图谱同步**：README 目录树 / 内置工具 / 测试清单更新；
   graphify-out 按新模块结构重新生成。


---

## 2026-09-06（续）：Bash 命令执行工具与命令治理（pre_step 抽象）

在模块化重构与日志/异常体系之上新增“命令执行 + 权限治理”能力，回答并落地
“审批/检测是否抽象到 pre_step 阶段”的问题。

1. **bash 工具**（`agent_test/tools/bash.py`）：异步执行一条 shell/cmd 命令
   （Windows cmd.exe / POSIX sh），返回 stdout（成功回显）/ stderr（err 回显）/
   exit_code / cwd / timed_out；输出可截断；每次执行写审计日志；
   参数级错误抛 ToolExecutionError（堆栈进日志、事实进 session）。
2. **治理层独立成包**（`agent_test/policy/`）：cwd.py（工作目录解析 + 允许
   工作区越界检测）、rules.py（deny/approve/warn 三档静态风险规则）、
   policy.py（CommandPolicy：allowlist 前缀 + 规则 + 越界 -> PolicyDecision）。
3. **抽象结论：审批/检测放 pre_step（跨工具拦截层），而非写死在 bash 内**：
   - ToolCenter.execute 内嵌策略硬闸门（任何直接 execute 也受管控，防绕过）；
   - ReactAgent._step 新增 `_pre_step` 阶段：副作用前对整步工具调用全量评审，
     未放行调用直接以 is_error 的 tool/result 进 session（可回放、可回传 LLM）；
   - 拒绝/需审批按 PolicyDecision（EXECUTE / REQUIRE_APPROVAL / DENY）建模，
     均不抛异常，符合“工具错误降级为结果”的既有协议。
4. **默认策略**：全局 `tool_center` 单例挂
   `CommandPolicy(allowed_roots=[Path.cwd()])`；破坏性命令 DENY、影响面大命令
   REQUIRE_APPROVAL、越界路径至少 REQUIRE_APPROVAL、allowlist 前缀直接放行。
5. **测试与图谱**：新增 tests/test_policy_pre_step.py、tests/test_bash_tool.py
   （合计 27 例，全量 92 passed）；graphify-out 重新生成。


---

## 2026-09-06（续二）：ask_user / confirm 用户交互与“批准继续”（HITL）

在 bash + policy 治理之上新增 Human-in-the-Loop 能力，先调研现行优秀 Agent
设计（Claude Code AskUserQuestion / canUseTool、Agno ask_user、Timbal
suspend/confirm、LangGraph interrupt、Codex allow-and-remember，来源与对照见
`docs/hitl-tool-design-research.md`），再落地：

1. **human 包**（`agent_test/human/service.py`）：可注入 `AskService`
   （`ask_user` / `confirm`），默认 `ConsoleAskService` 走 CLI；
   测试/UI 可脚本化回答。
2. **ask_user / confirm 工具**（`agent_test/tools/ask.py`）：LLM 主动调用——
   `ask_user` 澄清/补充消息（选项 + 多选 + 自由输入），`confirm` 批准继续；
   回答作为 tool/result 落 session，可回放可回传 LLM。
3. **批准继续 = 执行边界闸门**：`ToolCenter(approver=AskService)`；策略
   `REQUIRE_APPROVAL` 时征求批准：批准 -> `CommandPolicy.add_allowlist_for_command`
   记住前缀（会话级 remember，对标 approve-and-remember）-> 执行；
   拒绝 -> `deny_by_user` 结果回传 LLM。
4. **pre_step 分工调整**：`ReactAgent._pre_step` 只硬拦截 DENY 与
   “无 approver 的 REQUIRE_APPROVAL”；配置 approver 后审批放行到
   ToolCenter 闸门，保证被批动作在批准前零执行（决策先于副作用）。
5. **cwd 口径统一**：策略存在时 bash 未指定 workdir 默认注入策略允许根，
   与决策 `default_cwd = allowed_roots[0]` 一致。
6. **测试与图谱**：新增 tests/test_ask_user_tool.py（12 例，全量 104 passed）；
   graphify-out 重新生成。


---

## 2026-09-15：上下文压缩知识增量（TokenMeter / Compactor + turn/step/compacted）

按 2026-09-09 增量知识包落地"上下文管理"，解决长对话上下文无限增长问题：

1. **TokenMeter**（`agent_test/llm/token_meter.py`）：覆盖式记录 LLM usage
   （total_tokens 是本次请求完整上下文大小，非增量），`is_over_threshold()`
   在 total_tokens > threshold_tokens 时触发压缩，压缩后 `reset()`；
   窗口与阈值均可配（测试用小窗口 10000 验证）。
2. **Compactor**（`agent_test/llm/compactor.py`）：按 turn 分块、排除最近
   `recent_turns` 个回合；两级压缩——一级保留最近一轮，无可压缩块时降级
   二级全量压缩；摘要生成优先级：注入 summarize > llm_client.stream >
   保守截断（_truncate），保证无 LLM 也不阻塞压缩。
3. **事件模型**：SessionEvent 新增 `turn / step / compacted`（旧 JSONL 兼容
   默认 0/false）；EventType 新增 `TOOL_CALL = "tool/call"`（agent 后续使用）
   与 `COMPACT = "compact/summary"`（Compactor 摘要事件）。
4. **Session 持久化**：新增 `mark_compacted(seq_start, seq_end) -> last_index`
   （区间打标 + _persist_all）、`insert_after(index, type, data, *, turn, step)`
   （插入摘要 + 全量重编号 + _persist_all）、`_persist_all()`（全量重写 JSONL）；
   from_file 恢复校验升级为「seq 集合校验」`sorted(seqs) == range(N)`
   （strict=False 仅告警）。
5. **消息流调整**：LLM 返回与工具结果先入 `inbox.step`，由下一步 pre_step
   claim 写入 session，回合收尾 `_flush_step_messages()` 兜底，保证最终答复
   不丢失。
6. **LLM Adapter**：`stream()` 返回三元组 `(AssistantMessage, str, Dict|None)`；
   启用 `stream_options={"include_usage": True}`，末块提取 usage；服务端缺省时
   `_fetch_usage_fallback` 按字符粗估兜底。
7. **10000 窗口验证（本次目标）**：端到端用例
   `test_agent_context_window_10000_compacts_normally`——注入 GrowingUsageLLM
   （usage 逐轮递增 3500→9500），`max_context_tokens=10000` 阈值 8000，
   第 5 次调用 total=9500 触发压缩；断言压缩确实发生、被压缩的旧回合不再
   进入上下文、摘要（注入 summarize "摘要:" 前缀）作为 UserMessage 进入、
   最近一轮（问题5/回复5）完整保留、持久化 roundtrip 后 seq 连续且
   derive_messages 一致。
8. **测试与统计**：新增 tests/test_token_meter.py（5 例）、tests/test_compact.py
   （4 例，含上述 10000 端到端）；全量 113 例——107 passed；其余 6 例
   （bash 工具子进程类）在本执行环境因创建 cmd 子进程被拒（WinError 5）失败，
   经基线 HEAD（6e2baf0）临时工作树复跑验证为既有环境问题，非本次回归。
9. **内存窗口化（追加优化）**：压缩成功后调用 `Session.cut_to_context_window()`
   ——被压缩（compacted=True）的旧事件从内存 events 移除并转入 `_archived`
   归档，内存只保存上下文窗口（compact 摘要 + 最近保留事件），`events`
   即"上下文窗口"；磁盘 JSONL 始终是全量 0..N-1 历史——压缩流程的
   `_persist_all` 先全量落盘、窗口化只裁剪内存不覆盖磁盘，且 `_persist_all` /
   `_renumber` 均纳入归档副本，保证磁盘 seq 全局连续；from_file 仍恢复全量
   可审计。新增 `test_compactor_memory_window_keeps_disk_full` 覆盖窗口化 /
   磁盘全量 / append 连续性 / 二次压缩稳定 / roundtrip 一致。
