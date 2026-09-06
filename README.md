# react-openai-agent

基于 **OpenAI Chat Completions** 与 **Pydantic v2** 的 ReAct Agent 脚手架。
代码由 `新建 文本文档.txt` 中按 `---` 分隔的 6 段代码还原而来，还原时修正了原文中的若干 bug（见下文）。

## 项目结构

```
agent-test/
├── main.py              # 演示入口：向 Agent 发消息并执行 ReAct 循环
├── pyproject.toml       # 项目元数据与依赖
├── requirements.txt     # 运行时依赖
├── .env.example         # 环境变量示例（复制为 .env 后填写）
├── conftest.py          # pytest 根目录配置（保证 import src 可用）
├── src/
│   ├── types.py         # Pydantic 数据模型 + LLM 适配器基类
│   ├── session.py       # Session：session_id + {session_id}.jsonl 自动持久化
│   ├── inbox.py         # InBox 消息队列（turn / step 分区，每 Agent 私有）
│   ├── llm_adapter.py   # OpenAI 流式适配器 + LLM_CLIENT 注册表
│   ├── tools.py         # ToolCenter 工具注册/执行 + 内置 read 工具
│   ├── agent.py         # ReactAgent：初始化私有 inbox/session，turn -> step 循环
│   └── utils.py         # get_uuid / get_now
├── tests/
│   └── test_core.py     # 本地冒烟测试（不依赖网络 / API Key）
└── sessions/            # 运行时自动生成：{session_id}.jsonl（会话持久化文件）
```

## 快速开始

```bash
# 1. 创建独立虚拟环境（若已存在可跳过）
python -m venv .venv            # Windows: .venv\Scripts\activate
source .venv/bin/activate       # Linux/macOS

# 2. 安装依赖
pip install -r requirements.txt
pip install pytest               # 仅跑测试需要

# 3. 配置环境变量
cp .env.example .env            # Windows: copy .env.example .env
# 填写 API_KEY / BASE_URI / MODEL_NAME

# 4. 运行（每次运行自动生成 sessions/{session_id}.jsonl）
python main.py "你好，请介绍一下你自己"
python main.py "请读取 E:\\workspace\\agent-test\\README.md 的前 30 行"

# 5. 跑测试
python -m pytest -q
```

## 架构（turn / step 职责契约）

一轮对话（`turn`）拆成多个 `step`，职责严格分离：

- **main**：把用户消息放入 `inbox` 的 `turn` 队列；
- **turn 负责取消息与持久化**：
  1. `turn()` 设置 `phase.stage = "turn"`，进入循环；
  2. 循环体按当前阶段获取对应 `inbox` 队列的消息：`turn` 阶段取 `inbox.turns`，`step` 阶段取 `inbox.steps`；
  3. 把领到的消息按类型（`user/message`、`assistant/message`、`tool/result`）逐条持久化到 `session`；
  4. 调用 `step()`，若模型还需要工具调用则把 `phase.stage` 置为 `"step"` 继续循环，直到 `finish` / `max_token` / `error`；
  5. 最后一轮 step 产生的助手消息补记到 `session`，避免最终回答丢失。
- **step 只负责执行一步**：仅从 `session.derive_messages()` 组装 LLM 输入，调用 `llm_client.stream(...)` 得到 LLM 消息与 func-call，执行工具拿到 func-res，并把 **LLM 消息与工具结果写回 `inbox.step` 队列**，交由 turn 的下一轮循环持久化。

### Agent 实例与全局状态

**每个 `ReactAgent` 初始化时实例化私有的 `InBox` 与 `Session`**，不再共享全局
单例队列/会话，多 Agent、多轮对话互不串扰：

```python
agent = ReactAgent()              # 内部: self.inbox = InBox(); self.session = Session()
agent2 = ReactAgent()             # 各自独立的 inbox 与 session
```

`LLM_CLIENT` 与 `tool_center`（工具注册表）仍为进程内共享单例：LLM 客户端无状态
可复用，工具注册本质是全局能力注册表。

## 会话持久化（Session + JSONL）

Session 是 per-Agent 的会话记录，同时具备**文件持久化**能力：

- `Session(session_id=None, persist_dir="sessions")`：
  - 不传 `session_id` 时自动生成 UUID；
  - `__init__` 自动创建 `{persist_dir}/{session_id}.jsonl` 持久化文件（默认 `sessions/` 目录）；
- `Session.append(event_type, data)`：在内存记录事件的同时，把事件序列化为
  JSON **追加写入文件**（每行一条，UTF-8）；
- `Session.from_file(session_id, persist_dir="sessions")`：从 JSONL 恢复会话，
  事件中的消息会自动还原为对应的 `Message` 模型（`UserMessage` /
  `AssistantMessage` / `ToolResultMessage`）；
- 续聊：恢复 session 后，把新消息放入 `agent.inbox` 的 `turn` 队列再次调用
  `agent.turn()` 即可基于历史继续。

```python
from src.agent import ReactAgent
from src.session import Session

agent = ReactAgent()                      # 自动生成 session_id 与 sessions/{id}.jsonl
print(agent.session.session_id)           # 例如 3f2b...
print(agent.session.file_path)            # sessions/3f2b....jsonl

# 恢复历史会话（消息还原为 Message 模型）
restored = Session.from_file(agent.session.session_id)
for msg in restored.derive_messages():
    print(msg)
```

## 如何注册新工具

```python
from src.tools import tool_center

@tool_center.register(
    desc="计算两个数的和",
    parameters={
        "a": {"type": "integer", "description": "加数"},
        "b": {"type": "integer", "description": "被加数"},
    },
    required=["a", "b"],
)
def add(a: int, b: int) -> int:
    return a + b
```

工具返回值支持任意可 JSON 序列化的对象，`ToolCenter.execute` 会自动序列化为字符串；
同步/异步函数均可（`inspect.iscoroutinefunction` 自动识别）。

## 代码还原说明

txt 内 6 段代码对应文件：

| txt 段 | 还原文件 | 说明 |
| --- | --- | --- |
| 第 1 段 | `src/agent.py` | ReactAgent（去除误加的 `from pyexpat.errors import messages`） |
| 第 2 段 | `src/inbox.py` | InBox 消息队列 |
| 第 3 段 | `src/llm_adapter.py` | OpenAI 流式适配器 |
| 第 4 段 | `src/session.py` | Session 事件记录 |
| 第 5 段 | `src/tools.py` | ToolCenter + read 工具 |
| 第 6 段 | `src/types.py` | Pydantic 数据模型 + LLMBaseAdapter |

还原时修复的问题：

- **循环导入**：`types.py` 原先 `from src.session import ...` 与 `session.py` 互相导入，已移除；
- **错误端点**：`completions.create`（文本补全）→ `chat.completions.create`（对话补全，支持 tools）；
- **流式工具调用累积**：OpenAI 流式返回工具调用按 `index` 分片，原代码每片追加一个不完整的 `ToolCallBlock`，已改为按 index 累积 id/name/arguments；
- **文本内容被覆盖**：原 `assistant_message.content = tool_calls` 无条件覆盖文本，已在无工具调用时保留文本；
- **Schema 构建**：`ToolSchema.to_openai_schema()` 原实现把参数写错层级，并误调 `model_dump()`，已修正为规范 properties 结构；`get_schemas()` 改为调用 `to_openai_schema()`；
- **错误标记**：`execute()` 异常时原代码 `is_error` 恒为 False，已改为 True 并带错误信息；
- **返回类型**：工具返回 dict 时原 `TextBlock(content=dict)` 校验失败，`execute()` 现在自动 JSON 序列化；
- **session 消息写入**：原 `turn()` 把整份消息列表作为事件数据写入，`derive_messages()` 取不到消息，已改为逐条写入；
- **缺失 await**：`turn()` 中 `self._step(...)` 未 `await`（协程永不执行、循环直接结束），已补上；
- **字段重命名**：`ToolCenterSchema.schema` → `tool_schema`（避免与 pydantic `BaseModel.schema` 旧方法重名产生 shadowing 警告）；
- **env 加载**：新增 `python-dotenv` 支持 `.env`；`LLM_CLIENT['openai']` 改为按需构建，避免 import 时因缺环境变量报错。

对接本地模型（llama.cpp）时补充修复：

- **Schema 类型**：工具参数 `"type": "int"` → `"integer"`（`int` 不是合法 JSON Schema 类型，llama.cpp 的 JSON grammar 会直接 400）；
- **最终回答丢失**：最后一轮 step 的助手消息只进 `inbox.step` 未被持久化，turn 结束时补记到 session，`main.py` 才能打印最终回答；
- **事件类型**：工具结果原先被写成 `assistant/message`，现按消息类型写 `tool/result`；
- **流式连接清理**：`stream()` 结束时显式 `await stream.close()`；`main.py` 使用 `asyncio.Runner` + 自定义异常处理器忽略 llama.cpp/OpenAI 流在事件循环关闭时的无害告警。

Per-Agent 会话改造（基于上文架构继续演进）：

- **私有 inbox / session**：`ReactAgent.__init__` 实例化自己的 `InBox()` 与
  `Session()`，移除全局 `inbox` / `session_manager` 单例（inbox.py 不再导出
  `inbox`，session.py 不再导出 `session_manager`）；
- **JSONL 持久化**：`Session.__init__(session_id=None, persist_dir="sessions")`
  自动创建 `{session_id}.jsonl`；`append()` 在记录内存事件的同时把事件 JSON
  追加写入文件；`Session.from_file()` 可从文件恢复并把消息还原为 `Message` 模型。

## 对接本地模型（llama.cpp + Qwen2.5-0.5B）

本项目已用本地模型端到端验证（`E:\hf_home` 为 `HF_HOME`）：

```dotenv
# .env（已就绪）
API_KEY=llama.cpp          # 任意非空即可，llama.cpp 不校验
BASE_URI=http://127.0.0.1:8080/v1
MODEL_NAME=qwen2.5-0.5b-instruct
```

- 下载：`Qwen/Qwen2.5-0.5B-Instruct-GGUF` 的 `qwen2.5-0.5b-instruct-q4_k_m.gguf`（约 485MB），缓存在 `E:\hf_home\hub\models--Qwen--Qwen2.5-0.5B-Instruct-GGUF\snapshots\...`；
- 运行：llama.cpp `llama-server.exe`（CPU，`-ngl 0`，8 线程，ctx 4096）监听 `127.0.0.1:8080`，日志在 `E:\hf_home\runtime\llama-server.log(.err.log)`；
- 验证：`Invoke-RestMethod http://127.0.0.1:8080/v1/models`；
- 停止服务：`Stop-Process -Id <llama-server 的 PID>`（当前 PID 见任务日志）；
- 适用范围：0.5B 模型已成功完成 `read` 工具调用 + 总结的完整 ReAct 循环；复杂工具/推理建议换更大的模型（同样走 OpenAI 兼容接口，仅需改 `MODEL_NAME`）。
