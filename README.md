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
├── scripts/                    # 运维与验收脚本
│   ├── start-llama-qwen.ps1    # 本地 Qwen 小模型一键启动（llama.cpp + OpenAI 兼容）
│   ├── start-qwen25-3b.ps1     # 3B 模型（默认端口 8081，ctx 16384）
│   ├── start-qwen25-7b.ps1     # 7B 模型（3060 6GB 量化）
│   └── audit_session.py        # 会话审计：JSONL + runtime.log -> 可机械判定的报告
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
│   │   ├── events.py           #   EventType/Phase/SessionEvent（turn/step/compacted + COMPACT 摘要事件）
│   │   └── tools.py            #   ToolSchema / ToolCenterSchema
│   ├── core/                   # 核心执行
│   │   ├── agent.py            #   ReactAgent（inbox.step 消息流 + 串行审批 + 并行执行 + 压缩）
│   │   ├── prompts.py          #   结构化系统提示词（平台感知环境检查 + shell 硬性规则 + 状态语义）
│   │   └── inbox.py            #   InBox 消息队列（turn/step 分区，per-Agent 私有）
│   ├── session/                # 会话
│   │   └── session.py          #   Session（持久化游标 + 压缩打标/摘要插入 + from_file 校验）
│   ├── llm/                    # LLM 适配
│   │   ├── adapter.py          #   LLMBaseAdapter / OPENAIAdapter（stream 三元组 + usage）
│   │   ├── token_meter.py      #   TokenMeter：按 LLM usage 阈值检测上下文占用
│   │   ├── compactor.py        #   Compactor：按 turn 分块两级压缩（摘要化历史回合）
│   │   └── registry.py         #   LLM_CLIENT 注册表（懒构建）
│   ├── tools/                  # 工具
│   │   ├── center.py           #   ToolCenter（注册/执行/pre_decision 旁路/审批+执行双状态机）
│   │   ├── builtin.py          #   内置文件工具 + bash + ask_user + tool_center 单例
│   │   ├── bash.py             #   bash 执行（回显/退出码/超时 + stdin 隔离 + 平台感知解码）
│   │   └── ask.py              #   ask_user/confirm 用户交互工具注册（接 AskService）
│   ├── policy/                 # 命令治理（pre_step 拦截，跨工具复用）
│   │   ├── policy.py           #   CommandPolicy：allowlist+规则+越界 -> 决策
│   │   ├── rules.py            #   命令风险静态检测（deny/approve/warn）
│   │   └── cwd.py              #   工作目录解析与工作区越界检测
│   ├── human/                  # 用户交互（Human-in-the-Loop）
│   │   ├── service.py          #   AskService / ConsoleAskService（澄清/补充/批准/统一读取入口）
│   │   └── stdin_dispatcher.py #   StdinDispatcher（后台线程独占 input() + 队列 + drain_policy）
│   ├── app/                    # CLI 应用（交互式入口）
│   │   ├── application.py      #   Application：主循环/多行续行/去重/依赖装配
│   │   ├── cli_ask_service.py  #   CLIAskService：审批 + 审计日志 + EOF 降级
│   │   └── __main__.py         #   python -m agent_test.app 入口
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
│   ├── test_approval_parallel.py     # 串行审批+并行执行/双状态机/pre_decision 旁路
│   ├── test_stdin_dispatcher.py      # 统一 stdin 分发器/排空陈旧输入/EOF 降级
│   ├── test_token_meter.py          # TokenMeter：阈值/覆盖式赋值/reset/窗口 10000
│   ├── test_compact.py              # Compactor：分块/二级降级 + 窗口 10000 端到端压缩
│   ├── test_bash_tool.py            # bash 执行层：回显/退出码/工作目录/超时/平台感知编码
│   ├── test_create_agent.py         # create_agent 生命周期工厂（新建/拒绝覆盖/resume）
│   ├── test_session_reload.py       # 窗口化重载 + turn/step 编号续接
│   ├── test_session_torn_tail.py     # 尾部残行的容错与清理
│   └── test_audit_session.py         # 审计脚本判定（`;` 只在引号外算违规）
├── docs/                       # 设计与验收文档
│   ├── architecture-evolution.md     # 架构演进对照分析
│   └── hitl-tool-design-research.md  # HITL（澄清 / 审批）设计调研
├── sessions/                   # 运行时生成：{session_id}.jsonl（会话事件，gitignore）
└── logs/                       # 运行时生成：runtime.log（完整日志，gitignore）
```


## 架构总览（一次 turn 的完整链路）

```text
用户输入 ──> inbox.turn ──> turn/start
                             │
                             ├─ step 循环（直到 end_reason 非空）
                             │    ├─ session.derive_messages() ──> llm.stream()
                             │    │    三元组：assistant / end_reason / usage
                             │    ├─ TokenMeter 超阈值 ──> Compactor 压缩
                             │    │    旧事件打标 + compact/summary 进上下文窗口
                             │    ├─ 工具阶段两段分离（知识增量 5.1）
                             │    │    ① _pre_step 串行：decide_tool -> 审批
                             │    │      批准 -> 记住前缀 / 越界路径 / 越界工作目录
                             │    │      拒绝或拦截 -> execution=not_started（零副作用）
                             │    │    ② _exec_one 并行：asyncio.gather 执行已放行工具
                             │    └─ 结果先入 inbox.step，由下一步 claim 落 session
                             └─> turn/end(reason)
```

四条不变式（细节见下文各节）：

- **审批先于执行**：整步工具调用先做全量预检 + 审批，再并行执行；被拒 / 被拦截的
  动作标记 `execution=not_started`，从未产生副作用；
- **双状态机**：`ApprovalResult`（auto / approved / denied / blocked）与
  `ExecutionResult`（not_started / success / failed）分离，避免把「用户拒绝」
  误读成「执行失败」而反复重试；
- **单点闸门**：`ToolCenter.execute` 是任何执行入口的安全兜底；`pre_decision`
  旁路与独立决策互斥，既防绕过审批、也防二次审批；
- **可回放**：错误事实（location / error_type / message）进 session，完整堆栈进
  `logs/runtime.log`；会话 JSONL + 日志可由 `scripts/audit_session.py` 机械判定。

平台感知贯穿提示词与工具层：系统提示词按当前平台生成首选的**环境检查命令**，
`bash` 的输出编码按**控制台代码页**自动适配（见下文对应小节）。

## 本地小模型一键启动与测试（Qwen2.5-0.5B-Instruct）

仓库用本地 `Qwen2.5-0.5B-Instruct`（GGUF q4_k_m，约 469MB，HF 缓存位于
`HF_HOME=E:\hf_home`）做低成本端到端测试：通过 `.env` 把模型作为输入
（`BASE_URI` / `MODEL_NAME` 指向本地 llama.cpp）。

```powershell
# 1) 一键启动 llama.cpp 服务（OpenAI 兼容，127.0.0.1:8080；幂等，已在运行则复用）
powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1
#    停止：追加 -Stop；换端口：-Port 8081；强制重启：-ForceRestart

# 2) .env 指向本地模型（本项目当前已切换；还原云端见 .env 顶部注释）
#    BASE_URI=http://127.0.0.1:8080/v1
#    MODEL_NAME=qwen2.5-0.5b-instruct

# 3) 端到端测试：qwen 会自动发起 read 工具调用并基于结果作答
uv run python main.py "请使用 read 工具读取 E:\workspace\agent-test\README.md 的前 15 行，并告诉我第一行标题"

# 4) 或让脚本就绪后自动跑一次 demo
powershell -ExecutionPolicy Bypass -File scripts\start-llama-qwen.ps1 -RunDemo "请用一句话介绍你自己"
```

脚本会自动定位 `HF_HOME` 下的 llama.cpp 运行时与 Qwen GGUF 缓存（快照符号链接
自动解析到 blobs 真实文件），日志写入 `logs/llama-server-<port>.log(.err.log)`。

## 快速开始（uv）

```bash
# 1. 安装 uv（https://docs.astral.sh/uv/）
# 2. 同步虚拟环境与依赖（自动创建 .venv 并生成 uv.lock）
uv sync

# 3. 配置环境变量
copy .env.example .env
# 填写 API_KEY / BASE_URI / MODEL_NAME

# 4. 单轮演示（每次运行自动生成 sessions/{session_id}.jsonl 与 logs/runtime.log）
uv run python main.py "你好，请介绍一下你自己"
uv run python main.py "请读取 E:\\workspace\\agent-test\\README.md 的前 30 行"

# 5. 交互式 CLI（多轮会话 + 串行审批 + 统一 stdin）
uv run python -m agent_test.app
uv run python -m agent_test.app -r --session-id <session_id>   # 续聊

# 6. 跑测试
uv run pytest -v

# 7. 增量验收：逐条跑交互式用例后，用审计脚本机械判定会话（见「增量验收」一节）
uv run python scripts/audit_session.py sessions/<session_id>.jsonl --log logs/runtime.log
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
- **平台感知解码**：`encoding` 缺省按平台自动选——POSIX 用 `utf-8`，Windows 取
  **控制台输出代码页**（`chcp 65001` -> `utf-8`，默认中文控制台 -> `cp936`/`gbk`）；
  自动模式下若首选解码出现替换字符则按候选兜底，避免「GBK 回显按 UTF-8 解码」或
  反过来变成一串 `\ufffd`；显式传 `encoding=` 即以参数为准；
- 返回：`{command, cwd, exit_code, encoding, stdout, stderr, timed_out, truncated}` ——
  `stdout` 为成功回显、`stderr` 为 err 回显，二者分开返回并附退出码与本次实际使用的解码编码；
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
   存在且为目录 → 真实路径必须位于 `allowed_roots` 允许工作区内。不存在 /
   不是目录属**不可审批的参数错误**（DENY）；「超出允许工作区」属**可审批项**
   （`CwdCheck.outside=True` → REQUIRE_APPROVAL），批准后经
   `CommandPolicy.add_allowed_root` 记入允许根，本会话内同一目录不再逐条打扰；
2. **已审批前缀 allowlist**：命令头部命中（如 `["git", "push"]`）直接放行；
3. **静态风险检测**（`policy/rules.py`）：正则三档命中 ——
   `deny`（rm -rf / del /s /q / Remove-Item / format / diskpart 等，直接拒绝）、
   `approve`（git push / reset --hard / 安装 / 网络 / 注册表 / 服务变更，需审批）、
   `warn`（提示性）；
4. **越界检测**：命令涉及允许工作区之外的绝对路径，或 workdir 指向允许工作区
   之外的目录，都保守地至少要求审批（两种越界共用「批准即记忆」语义）。

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

- 策略决策 `REQUIRE_APPROVAL`（如 git push、工作区外路径、越界 workdir）：
  **批准 -> `CommandPolicy.remember_approval` 记住命令前缀 / 越界路径 / 越界工作目录
  （会话级，同类不再打扰）-> 执行**；**拒绝 -> 原因回传 LLM** 调整方案；
- `DENY`（破坏性命令、不存在的 workdir 等不可审批项）仍在 `ReactAgent._pre_step`
  硬拦截，决策先于任何副作用；
- 未配置 `approver` 时行为不变：直接返回“需要审批”的 is_error 结果。

## CLI 应用与审批/执行两阶段（知识增量 2026-09-30）

### 串行审批 + 并行执行（两阶段分离）

`ReactAgent._step` 把工具阶段拆成两步：

1. `_pre_step`（**串行**）：逐个 `policy.decide_tool` + `approver.confirm`。
   allowlist 级联依赖顺序（批准 `git push` 后同类命令不再询问），所以审批
   必须串行；审批结论写入 `decision.detail`（frozen dataclass 的可变 dict
   字段，避免 `FrozenInstanceError`）：`_approved` / `_approval_decision` /
   `_approval_source` / `_denied_reason`；
2. `_exec_one`（**并行**）：`asyncio.gather` 并行执行已放行的工具；被拒/
   被拦截的直接返回 blocked 结果，不进入执行层（没有任何副作用）。

单工具异常不取消其他工具：`_exec_one` 内部 try/except 兜底，降级为
`execution=failed` 的工具结果。

### 双状态机：ApprovalResult / ExecutionResult

| 审批 decision | 执行 status | 语义 |
| --- | --- | --- |
| `auto` / `approved` | `success` / `failed` | 已执行 |
| `denied` | `not_started` | 用户拒绝，**从未执行** |
| `blocked` | `not_started` | 策略拦截，**从未执行** |

- `ToolResultMessage.approval` / `.execution` 为可选字段（旧 JSONL 缺省
  `None`）；`is_error` 保留向后兼容，等价于
  `execution.status != "success"`；
- LLM 适配器把两者渲染成标签行（`[审批:用户拒绝执行](user_denied)`、
  `[执行:未开始(审批未通过)]`），压缩器同样保留该语义 —— 不把“审批被拒”
  压缩成普通“执行失败”。

### `pre_decision` 旁路与安全兜底

`ToolCenter.execute(..., pre_decision=...)`：

- **传入**（Agent 路径）：跳过本层重复决策 —— 与“独立决策”互斥，避免二次审批；
- **不传**（任何直接 execute 入口）：本层自行做完整策略决策，硬闸门始终
  生效，防止绕过。

### 交互式 CLI（`python -m agent_test.app`）

```bash
uv run python -m agent_test.app                        # 新建会话
uv run python -m agent_test.app -r --session-id s-1    # 窗口化重载续聊
uv run python -m agent_test.app --allowed-root E:/workspace
uv run python -m agent_test.app --drain-policy never   # 脚本化运行（管道喂输入）
```

- `/exit` 或 Ctrl-D 退出；行尾 `\` 续行，**一行 = 一个回合**（整块粘贴会被
  拆成多个独立请求），连续重复输入自动去重；
- **统一 stdin**：主循环 / 审批 / `ask_user` 三条读取路径都走
  `StdinDispatcher` —— 一个后台 daemon 线程独占 `input()` + `asyncio.Queue`
  分发，审批与提问前先 `drain_pending()` 排空陈旧缓冲行，解决“审批弹出前
  粘贴的多行文本被当作审批回答”的竞争问题；**排空策略可配**
  `--drain-policy auto|always|never`：`auto`（缺省）只在交互终端排空，
  管道 / 重定向（脚本化运行）下不排空，否则会把脚本自己的行（含 `y`/`n`
  回答）吞掉；后台线程无法优雅停止（`input()` 阻塞），随进程退出；
- 子进程（bash）以 `stdin=asyncio.subprocess.DEVNULL` 启动，不继承父进程
  终端；超时 kill 后再套一层 `wait_for(communicate, timeout=5)`，避免挂死。

### 结构化系统提示词（`core/prompts.py`）

`LLMBaseAdapter` 缺省注入结构化提示词（可用 `system_prompt=` 覆盖）：

- 首轮环境检查（`uname -s && pwd && python --version && git --version`）；
- Git Bash / cmd 语法对照表；
- `;` 硬性禁令 + 3 个错误示例（会话分析显示软性建议无效，必须升级为硬规则）；
- Windows 路径规范（正斜杠 / 引号）；
- 审批/执行标签语义，以及要求把环境与审批信息写入 Critical Context
  （压缩后仍可复用）。

### 增量验收（会话审计脚本）

- 验收方式：逐条跑交互式用例，每个用例一个独立 `--session-id`、提示词**写成单行**
  （CLI 只在行尾 `\` 时续行，整块粘贴会被拆成多个回合），跑完再用审计脚本判定；
- `scripts/audit_session.py`：把会话 JSONL + `logs/runtime.log` 变成可机械判定的报告
  —— 审批 x 执行组合合法性、批次规模与 bash 并发峰值、allowlist 级联、`;` 违规、
  被拒命令是否被重发、压缩与环境信息保留，以及实战新增的启动目录告警、环境检查
  重复、重复 confirm、EOF 降级与疑似跨两次启动；`--digest` 还能按回合回放
  「用户 / 工具调用 / 审批执行状态」。

```powershell
# 必须从仓库根启动，否则 sessions/ logs/ 临时文件会落进 src/agent_test/app/
& .\.venv\Scripts\python.exe -m agent_test.app --session-id t3-states
& .\.venv\Scripts\python.exe scripts\audit_session.py sessions\t3-states.jsonl --log logs\runtime.log --digest
```

## 会话持久化（Session + JSONL）

- `Session(session_id=None, persist_dir="sessions")`：自动创建
  `{persist_dir}/{session_id}.jsonl`；
- `append(event_type, data, *, turn=0, step=0)`：内存记录 + 同步落盘，seq 使用
  持久化游标，事件携带 turn/step 上下文与 compacted 标记；
- `append_error(location, error_type, message, detail=...)`：写入
  `runtime/error` 事件（错误事实，不含堆栈）；
- `from_file(session_id, persist_dir, strict=False)`：恢复会话并校验事件
  seq 连续性（strict=True 时断层抛 `SessionContinuityError`）；文件缺失或
  某行 JSON 损坏抛 `SessionEditError`（含行号，完整堆栈进日志文件）；
- `reload(strict=False)`：**窗口化重载**（实例方法）——从 JSONL 尾部倒序
  读取，遇到第一条 `compact/summary` 事件即停止（含该事件），只把
  「摘要 + 摘要之后的事件」载入 `events`（续聊所需的最小上下文窗口；
  无 compact 事件时退化为加载全量）；被跳过的磁盘前缀不载入内存但
  记账为 `_prefix_events`，之后 `_persist_all` 会按行回填，磁盘仍是全量历史；
  `_seq` 取最后一条事件 seq + 1，重载后继续 `append` 不冲突；
- `mark_compacted(seq_start, seq_end)` / `insert_after(index, type, data, *, turn, step)`：
  压缩时对旧事件打标、插入 compact/summary 摘要事件并全量重写 JSONL（seq 恒为 0..N-1）；
- `max_turn_step()`：返回全量事件（`_archived` 归档 + 内存窗口）中最大的
  turn / step；`ReactAgent` 构造时据此**续接已有编号**——`from_file` 或
  `reload` 恢复后继续对话，turn / step 从历史之后接着编号，不会与既有事件
  重号（step 是全局递增计数器，重号会破坏 Compactor 的 (turn, step) 排序）；
  摘要事件携带被压缩区间的 (turn, step) 上界，所以窗口化重载只读到摘要时
  编号上界也不丢；
- 工厂入口（模块 6）：`create_agent(session_id=None, persist_dir="sessions", *, resume=False)`
  ——新建与续聊的统一构造入口；`resume=True` 走窗口化重载（session-reload）
  并自动续接已有 turn/step 编号与全量 seq。`ReactAgent.resume(...)` /
  `Session.resume(...)` 是对应的类方法快捷方式；`create_agent` 在 session_id 已存在
  且非空时**拒绝新建**（否则会从 seq 0 / turn 1 重编号与磁盘历史冲突），
  并提示改用 resume；演示入口：`uv run python main.py --resume <session_id> "..."`；
- 容错：`from_file` / `reload` 遇到「写一半」的末尾残行（无换行结尾 + 解析
  失败）会告警跳过并清理磁盘残片（中间行损坏仍抛
  `SessionEditError`）；`append` 前也会先清掉残片，避免新事件与残片拼成
  无法解析的一行；
- 续聊：恢复 session 后，把新消息放入 `agent.inbox` 的 `turn` 队列再次
  调用 `agent.turn()` 即可基于历史继续（编号自动续接）。

```python
from agent_test import ReactAgent, Session, create_agent

agent = ReactAgent()                      # 自动生成 session_id 与 sessions/{id}.jsonl
print(agent.session.session_id)
print(agent.session.file_path)
restored = Session.from_file(agent.session.session_id)

# 续聊（session-reload）：一条调用完成「窗口化重载 + 续接编号」
agent2 = create_agent(agent.session.session_id, resume=True)
# 等价写法：ReactAgent.resume(agent.session.session_id)
# 只需 session（窗口）时：Session.resume(agent.session.session_id)
```

## 架构要点（turn / step）

- **main**：把用户消息放入 `inbox` 的 `turn` 队列；
- **turn**：持久化 turn/start + 用户消息 -> 循环执行 step -> turn/end；
  空 inbox 调用 `turn()` 直接返回 False——**不递增回合编号、不写任何事件**
  （先探空再编号，避免白耗 turn 号并留下无内容的 turn/start）；
- **回合日志闭环**：turn/start 一旦写出，本轮必定以 turn/end 收尾，
  `data` 携带 `reason`（finish / max_token / error）；回合内出现意外异常时
  也会尽力补写 turn/end(reason=error)；`phase.stage` 用 `AgentPhase`，
  回合内为 RUNNING、回合结束（含异常）复位 IDLE；
- **step**：仅从 `session.derive_messages()` 组装 LLM 输入 -> 调用
  `llm_client.stream(...)`（返回三元组：消息 / end_reason / usage）-> LLM
  消息与工具结果**先入 `inbox.step` 队列**，由下一步的 pre_step claim 或
  回合收尾统一写 session -> TokenMeter 按 usage 阈值检测，超阈值触发
  Compactor 压缩 -> 返回 end_reason（'' / finish / max_token / error）；
- **工具阶段两段分离**（详见「CLI 应用与审批/执行两阶段」）：`_pre_step` 串行
  完成策略决策与交互审批（allowlist 级联依赖顺序，必须串行），`_exec_one`
  用 `asyncio.gather` 并行执行已放行的工具，单工具异常不取消同批其他工具；
- **错误统一处理**：`ReactAgent._step` 捕获任何异常 -> 完整堆栈写入日志
  文件 -> location/error_type/message 摘要写入 session 的 runtime/error 事件。

## 上下文压缩（TokenMeter + Compactor）

长对话按「用量阈值 -> 摘要压缩 -> 最近一轮保留」策略防止上下文超窗：

- `TokenMeter(max_context_tokens=128000, threshold_ratio=0.8)`：
  覆盖式记录最近一次 LLM usage（total_tokens 是本次请求完整上下文大小，非增量）；
  `is_over_threshold()` 在 total_tokens > threshold_tokens 时触发压缩；
- `Compactor(session, summarize=None, llm_client=None)`：按 turn 分块、
  排除最近一轮，把历史回合压缩为 `compact/summary` 摘要事件；无可压缩块时
  降级二级全量压缩；摘要生成优先级为注入 summarize > LLM 客户端 > 保守截断；
- `derive_messages()` 跳过 compacted 事件、把摘要包装为 UserMessage 重入上下文；
  压缩后事件全量重写 JSONL，seq 连续可回放；
- 内存窗口化：压缩成功后 `Session.cut_to_context_window()` 把被压缩的旧事件
  从内存 events 移除（转入 _archived 归档），events 只作为上下文窗口
  （compact 摘要 + 最近保留事件）；磁盘 JSONL 始终是全量历史（seq 0..N-1
  连续，from_file 可完整恢复），窗口化后继续 append 的 seq 仍全局连续；
- 小窗口验证：`ReactAgent(max_context_tokens=10000)` 端到端测试
  （tests/test_compact.py::test_agent_context_window_10000_compacts_normally）
  验证 10000 窗口下压缩**正常且准确**——旧回合被摘要替代、最近一轮完整保留、
  摘要进入上下文、持久化 roundtrip seq 连续。

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
## 本地 GPU 小模型（3060 6GB）：Qwen2.5-7B / 3B 一键启动

在 0.5B 冒烟之上，3060 6GB 已部署两档可用模型（llama.cpp CUDA 构建 + GGUF
缓存在 `E:\hf_home\gguf\`），均通过真实 agent-loop（read 工具）验证：

| 脚本 | 模型 / 量化 | 权重 | 默认端口 | 默认 ctx | 实测生成速度（3060 Laptop 6GB） |
|---|---|---|---|---|---|
| `scripts\start-qwen25-7b.ps1` | Qwen2.5-7B-Instruct Q4_K_M | 4.36 GB | 8080 | 8192 | ~12 tok/s（显存 ~5.5/6 GB） |
| `scripts\start-qwen25-3b.ps1` | Qwen2.5-3B-Instruct Q8_0 | 3.37 GB | 8081 | 16384 | ~55 tok/s（显存 ~4.6/6 GB） |

- 运行时：`E:\hf_home\runtime\llama.cpp-cuda\llama-server.exe`（b10679 CUDA
  12.4；需把 `cudart-llama-bin` 包里的 `cudart64_12.dll / cublas64_12.dll /
  cublasLt64_12.dll` 放入同目录才能加载 `ggml-cuda.dll`）；
- 启动参数默认全量 GPU offload（`-ngl 99`），KV Cache 用 `q8_0` 在 6GB 显存
  上换取更长上下文；`-RunDemo` 会临时把 `BASE_URI`/`MODEL_NAME` 指向该模型，
  无需手工改 `.env`：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 -RunDemo "请调用 read 工具读取 README.md 前 10 行"
powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 -Stop
# start/stop 位置参数（start 为默认；stop 等价于 -Stop）：
powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 start
powershell -ExecutionPolicy Bypass -File scripts\start-qwen25-7b.ps1 stop
# 3B 同理：scripts\start-qwen25-3b.ps1（默认端口 8081，ctx 16384）
```

- 想用哪个模型跑 agent-test，把 `.env` 的 `BASE_URI`/`MODEL_NAME` 指到对应
  端口/别名即可（7B: `http://127.0.0.1:8080/v1` + `qwen2.5-7b-instruct`；
  3B: `http://127.0.0.1:8081/v1` + `qwen2.5-3b-instruct`）；
- 模型文件下载自 ModelScope：7B 官方 `Qwen/Qwen2.5-7B-Instruct-GGUF` 的
  `q4_k_m` 分片已用 `llama-gguf-split --merge` 合并为单文件；3B 用
  `Qwen/Qwen2.5-3B-Instruct-GGUF` 的 `q8_0` 单文件。7B 若偶发 CUDA OOM，
  调小 `-GpuLayers`（如 60）让 CPU 兜底。