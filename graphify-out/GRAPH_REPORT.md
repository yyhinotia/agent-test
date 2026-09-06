# Graph Report - agent-test  (2026-08-29)

## Corpus Check
- 11 files · ~4,424 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 111 nodes · 342 edges · 8 communities detected
- Extraction: 34% EXTRACTED · 66% INFERRED · 0% AMBIGUOUS · INFERRED: 226 edges (avg confidence: 0.62)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 8|Community 8]]

## God Nodes (most connected - your core abstractions)
1. `UserMessage` - 26 edges
2. `AssistantMessage` - 26 edges
3. `TextBlock` - 25 edges
4. `ToolResultMessage` - 21 edges
5. `ReactAgent` - 20 edges
6. `Session` - 20 edges
7. `ToolCallBlock` - 18 edges
8. `InBox` - 15 edges
9. `ToolCenter` - 13 edges
10. `LLMBaseAdapter` - 13 edges

## Surprising Connections (you probably didn't know these)
- `test_tool_call_block_args_dict()` --calls--> `ToolCallBlock`  [INFERRED]
  tests/test_core.py → src/types.py
- `run()` --calls--> `get_uuid()`  [INFERRED]
  main.py → src/utils.py
- `run()` --calls--> `TextBlock`  [INFERRED]
  main.py → src/types.py
- `test_tool_center_register_and_execute()` --calls--> `run()`  [INFERRED]
  tests/test_core.py → main.py
- `test_tool_center_execute_error()` --calls--> `run()`  [INFERRED]
  tests/test_core.py → main.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.19
Nodes (20): 根据消息类型确定写入 session 的事件类型。, 初始化 Agent：实例化本 Agent 私有的 inbox 与 session。          session 会自动创建 {persist_dir}/{, 执行一轮对话。          turn 只负责：设置阶段、获取 inbox 消息、持久化到 session，         并驱动 step 循环直到 L, 执行一步：仅从 session 组装 LLM 输入，调用 LLM 并执行工具。, from_file(), 会话与执行事件记录。  每个 ReactAgent 初始化时实例化一个独立 Session： - __init__ 自动创建 `{persist_dir}/{s, 把 jsonl 中的 data 还原为对应的 Message 模型；非消息数据原样返回。, 会话事件记录，附带 JSONL 文件持久化。 (+12 more)

### Community 1 - "Community 1"
Cohesion: 0.22
Nodes (17): BaseModel, dict, _LLMRegistry, OPENAIAdapter, OpenAI LLM 适配器。  通过 OpenAI Chat Completions 流式调用模型，解析增量返回的 content 与 tool_calls（, LLM 客户端注册表：按需构建，避免 import 时因缺少环境变量而失败。, 流式调用 LLM，返回（助手消息, 结束原因）。          end_reason:             'finish'     模型正常结束（给出, LLMBaseAdapter (+9 more)

### Community 2 - "Community 2"
Cohesion: 0.17
Nodes (12): test_tool_center_execute_error(), test_tool_center_register_and_execute(), test_tool_schema_to_openai_schema(), 获取全部可用工具的 OpenAI function calling schema。, 执行指定工具，返回 {"content": str, "is_error": bool}。, 按行读取文件。      offset: 起始行号（从 0 开始）     limit:  最大读取行数, read(), ToolCenter (+4 more)

### Community 3 - "Community 3"
Cohesion: 0.27
Nodes (11): _event_type_for(), ReactAgent, ensure_env(), main(), _quiet_asyncgen_close(), 演示入口：向 Agent 发送一条消息，执行 ReAct 循环。  用法:     python main.py "你好，请介绍一下你自己"     pytho, 忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。, run() (+3 more)

### Community 4 - "Community 4"
Cohesion: 0.2
Nodes (10): Enum, str, test_agent_constructs_with_env(), test_tool_call_block_args_dict(), test_uuid_and_now(), AgentPhase, EventType, 核心数据类型定义（基于 Pydantic v2）。  消息模型、工具 Schema 与 LLM 适配器基类都定义在这里。 原 txt 中该文件顶部 `from (+2 more)

### Community 5 - "Community 5"
Cohesion: 0.24
Nodes (5): InBox, 消息队列: turn 消息与 step 消息分区存放。  每个 ReactAgent 初始化时实例化一个独立的 InBox，避免全局状态在多轮/多 Agent, inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。, 获取特定阶段（'turn' / 其他）的全部消息并清空。, test_inbox_turn_and_step()

### Community 6 - "Community 6"
Cohesion: 1.0
Nodes (1): src 包：基于 OpenAI + Pydantic 的 ReAct Agent 核心实现。

### Community 8 - "Community 8"
Cohesion: 1.0
Nodes (1): 解析 args JSON 字符串为参数字典。

## Knowledge Gaps
- **11 isolated node(s):** `消息队列: turn 消息与 step 消息分区存放。  每个 ReactAgent 初始化时实例化一个独立的 InBox，避免全局状态在多轮/多 Agent`, `inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。`, `获取特定阶段（'turn' / 其他）的全部消息并清空。`, `核心数据类型定义（基于 Pydantic v2）。  消息模型、工具 Schema 与 LLM 适配器基类都定义在这里。 原 txt 中该文件顶部 `from`, `解析 args JSON 字符串为参数字典。` (+6 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 6`** (2 nodes): `src 包：基于 OpenAI + Pydantic 的 ReAct Agent 核心实现。`, `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 8`** (1 nodes): `解析 args JSON 字符串为参数字典。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TextBlock` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Why does `ReactAgent` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.091) - this node is a cross-community bridge._
- **Why does `UserMessage` connect `Community 3` to `Community 0`, `Community 1`, `Community 4`, `Community 5`?**
  _High betweenness centrality (0.083) - this node is a cross-community bridge._
- **Are the 24 inferred relationships involving `UserMessage` (e.g. with `演示入口：向 Agent 发送一条消息，执行 ReAct 循环。  用法:     python main.py "你好，请介绍一下你自己"     pytho` and `忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。`) actually correct?**
  _`UserMessage` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 24 inferred relationships involving `AssistantMessage` (e.g. with `ReactAgent` and `根据消息类型确定写入 session 的事件类型。`) actually correct?**
  _`AssistantMessage` has 24 INFERRED edges - model-reasoned connections that need verification._
- **Are the 23 inferred relationships involving `TextBlock` (e.g. with `演示入口：向 Agent 发送一条消息，执行 ReAct 循环。  用法:     python main.py "你好，请介绍一下你自己"     pytho` and `忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。`) actually correct?**
  _`TextBlock` has 23 INFERRED edges - model-reasoned connections that need verification._
- **Are the 19 inferred relationships involving `ToolResultMessage` (e.g. with `ReactAgent` and `根据消息类型确定写入 session 的事件类型。`) actually correct?**
  _`ToolResultMessage` has 19 INFERRED edges - model-reasoned connections that need verification._