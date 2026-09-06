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