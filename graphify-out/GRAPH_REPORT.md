# Graph Report - agent-test  (2026-09-06)

## Corpus Check
- 46 files · ~15,745 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 482 nodes · 1548 edges · 38 communities detected
- Extraction: 31% EXTRACTED · 69% INFERRED · 0% AMBIGUOUS · INFERRED: 1064 edges (avg confidence: 0.61)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]

## God Nodes (most connected - your core abstractions)
1. `ToolCenter` - 68 edges
2. `CommandPolicy` - 63 edges
3. `UserMessage` - 62 edges
4. `TextBlock` - 57 edges
5. `RuntimeLog` - 55 edges
6. `AssistantMessage` - 55 edges
7. `Session` - 53 edges
8. `ToolCallBlock` - 50 edges
9. `ToolResultMessage` - 50 edges
10. `run()` - 44 edges

## Surprising Connections (you probably didn't know these)
- `LlmError` --calls--> `test_llm_error_retryable_flag()`  [INFERRED]
  src/agent_test/exceptions/llm.py → tests/test_exceptions.py
- `RuntimeLog` --uses--> `日志模块测试：文件输出、上下文绑定与异常堆栈记录。`  [INFERRED]
  src/agent_test/log/runtime_log.py → tests/test_runtime_log.py
- `ToolCenter` --uses--> `内置 find / edit 工具的基础行为测试（本地离线）。`  [INFERRED]
  src/agent_test/tools/center.py → tests/test_builtin_tools.py
- `ToolCenter` --uses--> `find/edit 经 ToolCenter.execute 走通 JSON 序列化协议。`  [INFERRED]
  src/agent_test/tools/center.py → tests/test_builtin_tools.py
- `_reset_runtime_log()` --calls--> `reset()`  [INFERRED]
  conftest.py → src/agent_test/log/runtime_log.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.09
Nodes (77): LLMBaseAdapter, OPENAIAdapter, LLM 适配器：基类 + OpenAI 流式实现。  重构后不再“吞异常”：OpenAI 流式调用失败会抛出 LlmError（保留原始 异常链），完整堆栈由, 流式调用 LLM，返回（助手消息, 结束原因）。          end_reason:             'finish'     模型正常结束（给出, LLM 适配器基类：负责把内部 Message 列表组装为 OpenAI 消息格式。, 将内部 Message 列表转换为 OpenAI Chat API 的消息列表。, OpenAI Chat Completions 流式适配器。, 基于 ReAct 循环的对话 Agent。  交互契约： - main 把用户消息放入本 Agent 自带 inbox 的 turn 队列； - turn() (+69 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (67): ABC, 用户交互工具：ask_user（澄清/补充消息）与 confirm（批准继续进行）。  与 bash 工具同构：执行层不持有 UI——具体交互通过注入的 Ask, 把 ask_user / confirm 工具注册到指定 ToolCenter。      service 缺省使用 ConsoleAskService（CLI, register_ask(), AskService, bash(), register_bash(), _truncate() (+59 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (47): CwdCheck, 工作目录检测结果。ok=False 时 error 给出可读原因。, Enum, AgentPhase, 执行事件与阶段定义（session 事件类型的唯一事实源）。, src 包：基于 OpenAI + Pydantic 的 ReAct Agent 核心实现。, CommandPolicy, _is_inside() (+39 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (62): edit(), find(), grep(), list_dir(), 内置工具：文件读取/查找/编辑，以及进程内共享的 tool_center 单例。, 在目录树中查找文件。      两种定位方式（可组合）：     - name_keyword:     文件名包含关键字；     - content, 对文件做精确文本替换（读-改-写）。      规则：     - old_text 必须与文件内容逐字符一致；     - old_text 出现 0, 按正则搜索文件内容，支持 offset/limit 跨文件分页。      约定：     - 结果单位为「命中行」：一行只记一条，匹配内容为 {path (+54 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (37): _event_type_for(), 获取特定阶段（'turn' / 其他）的全部消息并清空。, bind(), capture_exception(), configure(), _ContextFilter, debug(), error() (+29 more)

### Community 5 - "Community 5"
Cohesion: 0.13
Nodes (33): Bash 工具：异步执行一条 shell/cmd 命令，捕获成功/错误回显。  设计 ---- - **执行层保持纯净**：bash() 不做权限判断——审批/, 把 bash 命令执行工具注册到指定 ToolCenter。, 截断过长的输出，返回 (文本, 是否截断)。, 异步执行一条命令并返回回显。      返回 dict：command / cwd / exit_code / stdout / stderr /, 工具中心：注册、管理并执行工具。  工具内部异常不向上抛（要回传给 LLM 做下一步决策）： - 完整堆栈写入日志文件（RuntimeLog.capture_e, 执行指定工具，返回 {"content": str, "is_error": bool}。, 初始化工具中心。          policy: 可选命令治理策略（agent_test.policy.CommandPolicy）。, 初始化工具中心。          policy: 可选命令治理策略（agent_test.policy.CommandPolicy）。 (+25 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (20): AgentBaseError, AgentBaseError, 项目级异常基类。  命名说明 -------- 方案中称为 `baseException`，这里落地为 `AgentBaseError(Exception)`，, agent-test 内所有已知业务异常的统一基类。, 返回可安全写入 session 的错误摘要（不含堆栈细节）。, dict, Exception, LlmError (+12 more)

### Community 7 - "Community 7"
Cohesion: 0.4
Nodes (5): is_within(), 工作目录检测：解析请求的工作目录并校验其位于允许工作区（沙箱根）内。  与具体工具解耦：bash / 未来 write / 删除类工具都可通过本模块做 “工作目, child 是否位于 root 之内（含相等，按真实路径比较）。, 解析命令工作目录。      - requested 为空：返回 default_cwd（调用方/策略默认目录）；     - requested 为相对路径：, resolve_workdir()

### Community 9 - "Community 9"
Cohesion: 0.67
Nodes (3): test_uuid_and_now(), get_now(), get_uuid()

### Community 10 - "Community 10"
Cohesion: 1.0
Nodes (1): 向用户提一个澄清/补充消息的问题，返回其回答文本。          options 非空时用户可选项（label）或输入其他内容；         multi

### Community 11 - "Community 11"
Cohesion: 1.0
Nodes (1): 请用户批准一个动作（如执行某命令）。          返回 (是否批准, 消息)：批准消息可作审计备注；         拒绝消息会回传给 LLM 供其调整方

### Community 13 - "Community 13"
Cohesion: 1.0
Nodes (1): 配置运行时日志，返回日志文件路径。          重复调用会清空旧 handler 并切换目录（幂等，测试可随时重定向）。

### Community 14 - "Community 14"
Cohesion: 1.0
Nodes (1): 清空 handler 与配置状态（测试隔离用）。

### Community 15 - "Community 15"
Cohesion: 1.0
Nodes (1): 当前日志文件路径（未配置时为 None）。

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (1): 绑定运行时上下文，返回用于 unbind 的 token 列表。

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): 必须在 except 块内调用：记录消息 + 当前异常完整堆栈。

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): 把异常完整堆栈写入日志，返回可写入 session 的错误摘要。          返回摘要字段（错误事实）：             location

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): 获取特定阶段（'turn' / 其他）的全部消息并清空。

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): OpenAI LLM 适配器。  通过 OpenAI Chat Completions 流式调用模型，解析增量返回的 content 与 tool_calls（

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): 流式调用 LLM，返回（助手消息, 结束原因）。          end_reason:             'finish'     模型正常结束（给出

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): LLM 客户端注册表：按需构建，避免 import 时因缺少环境变量而失败。

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): 把 jsonl 中的 data 还原为对应的 Message 模型；非消息数据原样返回。

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): 会话事件记录，附带 JSONL 文件持久化。

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): 创建会话，并自动生成 {session_id}.jsonl 持久化文件（默认目录 sessions/）。

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (1): 追加一条执行事件，并同步持久化到 {session_id}.jsonl（一行一条 JSON）。

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (1): 从事件中提取完整的 LLM 历史消息列表。

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): 从持久化文件恢复会话（消息自动还原为 Message 模型）。

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (1): 获取全部可用工具的 OpenAI function calling schema。

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (1): 执行指定工具，返回 {"content": str, "is_error": bool}。

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (1): 按行读取文件。      offset: 起始行号（从 0 开始）     limit:  最大读取行数

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (1): 核心数据类型定义（基于 Pydantic v2）。  消息模型、工具 Schema 与 LLM 适配器基类都定义在这里。 原 txt 中该文件顶部 `from

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (1): 解析 args JSON 字符串为参数字典。

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (1): 转换为 OpenAI function calling 的 JSON Schema。

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (1): 工具中心注册条目。      字段避免命名为 `schema`（与 pydantic BaseModel 的旧方法重名，     v2 会产生 shadowin

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): LLM 适配器基类: 负责把内部 Message 列表组装为 OpenAI 消息格式。

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): 将内部 Message 列表转换为 OpenAI Chat API 的消息列表。

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): 对话阶段计数器。      turn / step: 轮次与步数     stage:       inbox 当前阶段（'turn' / 'step'），由

## Knowledge Gaps
- **71 isolated node(s):** `消息队列: turn 消息与 step 消息分区存放。  每个 ReactAgent 初始化时实例化一个独立的 InBox，避免全局状态在多轮/多 Agent`, `inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。`, `获取特定阶段（'turn' / 其他）的全部消息并清空。`, `项目级异常基类。  命名说明 -------- 方案中称为 `baseException`，这里落地为 `AgentBaseError(Exception)`，`, `agent-test 内所有已知业务异常的统一基类。` (+66 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 10`** (1 nodes): `向用户提一个澄清/补充消息的问题，返回其回答文本。          options 非空时用户可选项（label）或输入其他内容；         multi`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 11`** (1 nodes): `请用户批准一个动作（如执行某命令）。          返回 (是否批准, 消息)：批准消息可作审计备注；         拒绝消息会回传给 LLM 供其调整方`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 13`** (1 nodes): `配置运行时日志，返回日志文件路径。          重复调用会清空旧 handler 并切换目录（幂等，测试可随时重定向）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 14`** (1 nodes): `清空 handler 与配置状态（测试隔离用）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 15`** (1 nodes): `当前日志文件路径（未配置时为 None）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (1 nodes): `绑定运行时上下文，返回用于 unbind 的 token 列表。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (1 nodes): `必须在 except 块内调用：记录消息 + 当前异常完整堆栈。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `把异常完整堆栈写入日志，返回可写入 session 的错误摘要。          返回摘要字段（错误事实）：             location`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `inbox 追加待处理消息（'turn' 进 turn 队列，其余进 step 队列）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `获取特定阶段（'turn' / 其他）的全部消息并清空。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `OpenAI LLM 适配器。  通过 OpenAI Chat Completions 流式调用模型，解析增量返回的 content 与 tool_calls（`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `流式调用 LLM，返回（助手消息, 结束原因）。          end_reason:             'finish'     模型正常结束（给出`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `LLM 客户端注册表：按需构建，避免 import 时因缺少环境变量而失败。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `把 jsonl 中的 data 还原为对应的 Message 模型；非消息数据原样返回。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `会话事件记录，附带 JSONL 文件持久化。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `创建会话，并自动生成 {session_id}.jsonl 持久化文件（默认目录 sessions/）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `追加一条执行事件，并同步持久化到 {session_id}.jsonl（一行一条 JSON）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `从事件中提取完整的 LLM 历史消息列表。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `从持久化文件恢复会话（消息自动还原为 Message 模型）。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `获取全部可用工具的 OpenAI function calling schema。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `执行指定工具，返回 {"content": str, "is_error": bool}。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `按行读取文件。      offset: 起始行号（从 0 开始）     limit:  最大读取行数`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `核心数据类型定义（基于 Pydantic v2）。  消息模型、工具 Schema 与 LLM 适配器基类都定义在这里。 原 txt 中该文件顶部 `from`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `解析 args JSON 字符串为参数字典。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `转换为 OpenAI function calling 的 JSON Schema。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `工具中心注册条目。      字段避免命名为 `schema`（与 pydantic BaseModel 的旧方法重名，     v2 会产生 shadowin`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `LLM 适配器基类: 负责把内部 Message 列表组装为 OpenAI 消息格式。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `将内部 Message 列表转换为 OpenAI Chat API 的消息列表。`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `对话阶段计数器。      turn / step: 轮次与步数     stage:       inbox 当前阶段（'turn' / 'step'），由`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `src 包：基于 OpenAI + Pydantic 的 ReAct Agent 核心实现。` connect `Community 2` to `Community 0`, `Community 1`, `Community 5`, `Community 6`?**
  _High betweenness centrality (0.140) - this node is a cross-community bridge._
- **Why does `ToolCenter` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 5`?**
  _High betweenness centrality (0.122) - this node is a cross-community bridge._
- **Why does `CommandPolicy` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 5`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Are the 65 inferred relationships involving `str` (e.g. with `get_uuid()` and `.to_summary()`) actually correct?**
  _`str` has 65 INFERRED edges - model-reasoned connections that need verification._
- **Are the 62 inferred relationships involving `ToolCenter` (e.g. with `用户交互工具：ask_user（澄清/补充消息）与 confirm（批准继续进行）。  与 bash 工具同构：执行层不持有 UI——具体交互通过注入的 Ask` and `把 ask_user / confirm 工具注册到指定 ToolCenter。      service 缺省使用 ConsoleAskService（CLI`) actually correct?**
  _`ToolCenter` has 62 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `CommandPolicy` (e.g. with `src 包：基于 OpenAI + Pydantic 的 ReAct Agent 核心实现。` and `CwdCheck`) actually correct?**
  _`CommandPolicy` has 53 INFERRED edges - model-reasoned connections that need verification._
- **Are the 60 inferred relationships involving `UserMessage` (e.g. with `演示入口：向 Agent 发送一条消息，执行 ReAct 循环。  用法:     python main.py "你好，请介绍一下你自己"     pytho` and `忽略 llama.cpp/OpenAI 流式连接在事件循环关闭时的无害告警。`) actually correct?**
  _`UserMessage` has 60 INFERRED edges - model-reasoned connections that need verification._