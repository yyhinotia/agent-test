# agent-test

基于 **OpenAI Chat Completions** 与 **Pydantic v2** 的模块化 ReAct Agent
（uv 管理虚拟环境与依赖）。

> 本项目是从 `deepseek-harness-master` 提炼的 agent-loop 核心复刻脚手架，
> 经模块化重构后用于演练「错误事实进 session、完整堆栈进日志」的可回放
> agent-runtime 设计。

## 模块化结构

```
agent-test/
├── main.py                     # 演示入口：向 Agent 发消息并执行 ReAct 循环
├── pyproject.toml              # 项目元数据、依赖（uv / hatchling）
├── uv.lock                     # uv 生成的锁定文件（由 uv sync 生成）
├── .env.example                # 环境变量示例（复制为 .env 后填写）
├── conftest.py                 # pytest 根配置（测试间隔离运行时日志）
├── src/agent_test/             # 主包：按功能模块组织
│   ├── exceptions/             # 异常体系（AgentBaseError 基类 + 分类异常）
│   │   ├── base.py             #   AgentBaseError（业务异常统一基类）
│   │   ├── session.py          #   SessionEditError / SessionContinuityError
│   │   ├── message.py          #   MessageEditError
│   │   ├── llm.py              #   LlmError（含 retryable 标记）
│   │   └── tools.py            #   ToolExecutionError
│   ├── log/                    # 运行时日志（logs/runtime.log + 控制台）
│   │   └── runtime_log.py      #   RuntimeLog：懒配置 + session/turn/step 上下文
│   ├── types/                  # 数据模型
│   │   ├── messages.py         #   消息（User/Assistant/ToolResult + 内容块）
│   │   ├── events.py           #   EventType/Phase/SessionEvent（事件唯一事实源）
│   │   └── tools.py            #   ToolSchema / ToolCenterSchema
│   ├── core/                   # 核心执行
│   │   ├── agent.py            #   ReactAgent（依赖注入 + 产出即持久化 + 错误统一处理）
│   │   └── inbox.py            #   InBox 消息队列（turn/step 分区，per-Agent 私有）
│   ├── session/                # 会话
│   │   └── session.py          #   Session（持久化游标 + append_error + from_file 校验）
│   ├── llm/                    # LLM 适配
│   │   ├── adapter.py          #   LLMBaseAdapter / OPENAIAdapter（不再吞异常）
│   │   └── registry.py         #   LLM_CLIENT 注册表（懒构建）
│   ├── tools/                  # 工具
│   │   ├── center.py           #   ToolCenter（注册/执行/策略闸门/approver，错误降级为结果）
│   │   ├── builtin.py          #   内置文件工具 + bash + ask_user + tool_center 单例
│   │   ├── bash.py             #   bash 命令执行工具（stdout/stderr/退出码/超时）
│   │   └── ask.py              #   ask_user/confirm 用户交互工具注册（接 AskService）
│   ├── policy/                 # 命令治理（pre_step 拦截，跨工具复用）
│   │   ├── policy.py           #   CommandPolicy：allowlist+规则+越界 -> 决策
│   │   ├── rules.py            #   命令风险静态检测（deny/approve/warn）
│   │   └── cwd.py              #   工作目录解析与工作区越界检测
│   ├── human/                  # 用户交互（Human-in-the-Loop）
│   │   └── service.py          #   AskService / ConsoleAskService（澄清/补充/批准）
│   └── utils.py                # get_uuid / get_now
├── tests/                      # pytest 测试（本地离线）
│   ├── test_core.py            # 核心组件冒烟测试（含 FakeLLM 驱动完整 ReAct 循环）
│   ├── test_exceptions.py      # 异常体系层级/字段/触发/健壮性测试
│   ├── test_runtime_log.py     # 日志文件/上下文/完整堆栈测试
│   ├── test_builtin_tools.py   # 内置 find/edit 工具行为测试
│   ├── test_grep_tool.py       # grep 工具（正则匹配/跨文件分页）测试
│   ├── test_list_write.py      # list_dir / write 工具测试
│   ├── test_session_error_event.py  # 错误事实进 session、堆栈进日志的端到端测试
│   ├── test_ask_user_tool.py        # ask_user/confirm 与批准继续（approver）测试
│   ├── test_policy_pre_step.py      # 命令治理：风险/工作目录/审批与 pre_step 拦截
│   └── test_bash_tool.py            # bash 执行层：回显/退出码/工作目录/超时
├── sessions/                   # 运行时生成：{session_id}.jsonl（会话事件，gitignore）
└── logs/                       # 运行时生成：runtime.log（完整日志，gitignore）
```

## 快速开始（uv）

```bash
# 1. 安装 uv（https://docs.astral.sh/uv/）
# 2. 同步虚拟环境与依赖（自动创建 .venv 并生成 uv.lock）
uv sync

# 3. 配置环境变量
copy .env.example .env
# 填写 API_KEY / BASE_URI / MODEL_NAME

# 4. 运行（每次运行自动生成 sessions/{session_id}.jsonl 与 logs/runtime.log）
uv run python main.py "你好，请介绍一下你自己"
uv run python main.py "请读取 E:\\workspace\\agent-test\\README.md 的前 30 行"

# 5. 跑测试
uv run pytest -v
```

## 日志与异常设计（本次重构核心）

### 错误信息分层

| 载体 | 记录内容 | 目的 |
| --- | --- | --- |
| `logs/runtime.log` | 完整堆栈（traceback）、异常类型、消息、session/turn/step 上下文 | 事后排查 |
| `sessions/{id}.jsonl` | `runtime/error` 事件：`location` + `error_type` + `message` 摘要 | 按时间线回放 |

二者通过 `session_id` 关联：日志行与 session 事件都携带同一会话标识。

### 异常体系（`agent_test.exceptions`）

- `AgentBaseError(Exception)`：业务异常统一基类，提供 `location` 与 `detail`
  结构化字段与 `to_summary()`（可安全写入 session 的摘要）。
  注意：方案中的 `baseException` 落地为 `AgentBaseError(Exception)`，而不是
  直接继承内置 `BaseException` —— 内置 `BaseException` 是系统信号
  （KeyboardInterrupt / SystemExit）的基类，业务异常继承它会绕过
  `finally`/`with` 清理并吞掉 Ctrl+C。
- `SessionEditError` / `SessionContinuityError`：会话编辑失败、回放序号断层；
- `MessageEditError`：消息构造/编辑失败（含工具调用参数 JSON 解析失败）；
- `LlmError`：LLM 调用失败（带 `retryable` 标记）；
- `ToolExecutionError`：工具未注册/不可用/执行失败（execute 降级为
  `{"content": ..., "is_error": True}` 回传给 LLM）。

### 用法

```python
from agent_test import ReactAgent, RuntimeLog

RuntimeLog.configure("logs")
agent = ReactAgent()
tokens = RuntimeLog.bind(session_id=agent.session.session_id, turn=1)
try:
    agent.inbox.append("turn", UserMessage(id="u1", content=[TextBlock(content="hi")]))
    await agent.turn()
finally:
    RuntimeLog.unbind(tokens)
```


## Bash 命令执行工具与命令治理（pre_step 抽象）

### bash 工具（`agent_test.tools.bash`）

- 入参：`command`（必填）、`workdir`、`timeout`、`encoding`、`max_output_chars`；
- 返回：`{command, cwd, exit_code, stdout, stderr, timed_out, truncated}` ——
  `stdout` 为成功回显、`stderr` 为 err 回显，二者分开返回并附退出码；
- 跨平台：Windows 走 `cmd.exe`（COMSPEC）、POSIX 走 `/bin/sh`，工具名叫
  bash、语义是“执行一条命令”；
- 异步执行：`asyncio.create_subprocess_shell`，不阻塞 agent 事件循环，
  超时自动 kill 并标记 `timed_out=True`；
- 审计日志：每次执行把 command / cwd / exit_code / 输出长度写入
  `logs/runtime.log`；参数级错误抛 `ToolExecutionError`，由 ToolCenter 统一
  降级为 `is_error=True` 的工具结果（完整堆栈进日志文件）。

### 审批 / 检测 / 工作目录检测：抽象到 pre_step 阶段？

**结论：应该抽象成跨工具的 pre_step 拦截，而不是写死在 bash 内部。** 理由：

1. **跨工具复用**：权限审批 / 危险命令检测 / 工作目录越界不是 bash 专属，
   write、删除、网络、安装类工具将来同样需要；放进工具内部会重复且易漏；
2. **决策先于副作用**：一步（step）内可能同时请求多个工具调用；只有先对
   **整步所有调用**做一次“全量预检”，才能避免“先执行了允许的、再拦下
   拒绝的”造成的部分副作用；
3. **可观测、可回放**：拒绝 / 需审批不是异常，而是统一以 `is_error=True`
   的 tool/result 回传 LLM 并落 session；决策动作与原因同时写日志，便于审计；
4. **防绕过**：单点硬闸门保证任何执行入口都受管控。

因此治理抽到新包 `agent_test.policy`，并在两层落地：

- `ToolCenter.execute` 内置**策略硬闸门**：`ToolCenter(policy=...)` 后任何
  直接 execute 也先决策；DENY / REQUIRE_APPROVAL 不执行，直接返回
  `{"is_error": True, "blocked": True, "policy_action": ...}`；
- `ReactAgent._step` 新增 **pre_step 阶段**（`_pre_step`）：在副作用前对整步
  工具调用做全量评审，未放行调用直接生成拦截结果，不进 execute。

bash 是第一个接入的高危工具；后续工具只需在 `CommandPolicy.decide_tool`
登记自己的“策略画像”，Agent / ToolCenter 无需改动。

### 决策流程（`CommandPolicy.decide`）

1. **工作目录检测**（`policy/cwd.py`）：解析 workdir（缺省取默认目录）→ 必须
   存在且为目录 → 真实路径必须位于 `allowed_roots` 允许工作区内（越界 DENY）；
2. **已审批前缀 allowlist**：命令头部命中（如 `["git", "push"]`）直接放行；
3. **静态风险检测**（`policy/rules.py`）：正则三档命中 ——
   `deny`（rm -rf / del /s /q / Remove-Item / format / diskpart 等，直接拒绝）、
   `approve`（git push / reset --hard / 安装 / 网络 / 注册表 / 服务变更，需审批）、
   `warn`（提示性）；
4. **工作区外路径检测**：命令涉及允许工作区之外的绝对路径，保守地至少要求审批。

默认单例 `tool_center` 挂载 `CommandPolicy(allowed_roots=[Path.cwd()])`
（进程启动目录即沙箱工作区）；需要更宽松/更严格边界时自行构造
`ToolCenter(policy=CommandPolicy(...))` 传入 Agent。

### 使用示例

```python
from agent_test import ReactAgent
from agent_test.policy import CommandPolicy
from agent_test.tools.bash import register_bash
from agent_test.tools.center import ToolCenter

center = ToolCenter(policy=CommandPolicy(allowed_roots=["./workspace"]))
register_bash(center)
agent = ReactAgent(tools=center, llm_client=...)

# LLM 请求 bash("rm -rf /tmp/x") -> pre_step DENY，工具结果 is_error=True
# LLM 请求 bash("git push ...")  -> REQUIRE_APPROVAL：未配 approver 返回“需审批”；
#   配置 approver 则征求批准，批准后记住前缀并执行（拒绝则原因回传 LLM）
```


## ask_user / 用户交互工具（Human-in-the-Loop）

设计参照主流 Agent 的 HITL 模式（Claude Code AskUserQuestion / canUseTool、
Agno ask_user、Timbal suspend/confirm、LangGraph interrupt、Codex
allow-and-remember），详见 `docs/hitl-tool-design-research.md`。

### ask_user / confirm 工具（`agent_test.tools.ask`）

- `ask_user(question, options?, multi_select?, header?)`：澄清任务 / 收集
  **用户补充消息**。支持 2-4 个候选选项（用户可选项或输入其他内容）与多选；
- `confirm(action, description?)`：请用户**批准继续进行**（如删除/提交/继续
  某方案），返回 `approved` / `denied`（含原因）。
- 二者通过可注入的 `AskService`（`agent_test.human`）呈现与收集回答：
  默认 `ConsoleAskService`（CLI 交互），测试/网页/MCP 自行实现并注入即可；
- 用户的回答作为 tool/result 写入 session，可回放、可回传 LLM 继续决策。

### 批准继续 = 执行边界的闸门（对标 canUseTool / needsApproval）

```python
from agent_test import ReactAgent
from agent_test.human.service import ConsoleAskService
from agent_test.policy import CommandPolicy
from agent_test.tools.bash import register_bash
from agent_test.tools.center import ToolCenter

center = ToolCenter(
    policy=CommandPolicy(allowed_roots=["./workspace"]),
    approver=ConsoleAskService(),   # 可选：配置后 REQUIRE_APPROVAL 自动征求批准
)
register_bash(center)
agent = ReactAgent(tools=center, llm_client=...)
```

- 策略决策 `REQUIRE_APPROVAL`（如 git push）：**批准 -> `add_allowlist_for_command`
  记住前缀（会话级，同类不再打扰）-> 执行**；**拒绝 -> 原因回传 LLM** 调整方案；
- `DENY`（破坏性/越界）仍在 `ReactAgent._pre_step` 硬拦截，决策先于任何副作用；
- 未配置 `approver` 时行为不变：直接返回“需要审批”的 is_error 结果。

## 会话持久化（Session + JSONL）

- `Session(session_id=None, persist_dir="sessions")`：自动创建
  `{persist_dir}/{session_id}.jsonl`；
- `append(event_type, data)`：内存记录 + 同步落盘，seq 使用持久化游标；
- `append_error(location, error_type, message, detail=...)`：写入
  `runtime/error` 事件（错误事实，不含堆栈）；
- `from_file(session_id, persist_dir, strict=False)`：恢复会话并校验事件
  seq 连续性（strict=True 时断层抛 `SessionContinuityError`）；文件缺失或
  某行 JSON 损坏抛 `SessionEditError`（含行号，完整堆栈进日志文件）；
- 续聊：恢复 session 后，把新消息放入 `agent.inbox` 的 `turn` 队列再次
  调用 `agent.turn()` 即可基于历史继续。

```python
from agent_test import ReactAgent, Session

agent = ReactAgent()                      # 自动生成 session_id 与 sessions/{id}.jsonl
print(agent.session.session_id)
print(agent.session.file_path)
restored = Session.from_file(agent.session.session_id)
```

## 架构要点（turn / step）

- **main**：把用户消息放入 `inbox` 的 `turn` 队列；
- **turn**：持久化 turn/start + 用户消息 -> 循环执行 step -> turn/end；
- **step**：仅从 `session.derive_messages()` 组装 LLM 输入 -> 调用
  `llm_client.stream(...)` -> **产出即持久化**（LLM 消息与工具结果立即写
  session）-> 返回 end_reason（'' / finish / max_token / error）；
- **错误统一处理**：`ReactAgent._step` 捕获任何异常 -> 完整堆栈写入日志
  文件 -> location/error_type/message 摘要写入 session 的 runtime/error 事件。

## 对接本地模型（llama.cpp + Qwen2.5-0.5B）

本项目已用本地模型端到端验证（`E:\hf_home` 为 `HF_HOME`）：

```dotenv
# .env（已就绪）
API_KEY=llama.cpp          # 任意非空即可，llama.cpp 不校验
BASE_URI=http://127.0.0.1:8080/v1
MODEL_NAME=qwen2.5-0.5b-instruct
```

- 下载：`Qwen/Qwen2.5-0.5B-Instruct-GGUF` 的 `qwen2.5-0.5b-instruct-q4_k_m.gguf`
  （约 485MB），缓存在 `E:\hf_home\hub\models--Qwen--Qwen2.5-0.5B-Instruct-GGUF\snapshots\...`；
- 运行：llama.cpp `llama-server.exe`（CPU，`-ngl 0`，8 线程，ctx 4096）监听
  `127.0.0.1:8080`，日志在 `E:\hf_home\runtime\llama-server.log(.err.log)`；
- 验证：`Invoke-RestMethod http://127.0.0.1:8080/v1/models`；
- 停止服务：`Stop-Process -Id <llama-server 的 PID>`；
- 适用范围：0.5B 模型已成功完成 `read` 工具调用 + 总结的完整 ReAct 循环；
  复杂工具/推理建议换更大的模型（同样走 OpenAI 兼容接口，仅需改 `MODEL_NAME`）。