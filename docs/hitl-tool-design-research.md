# HITL 工具设计调研：现行优秀 Agent 的 ask_user / 批准继续模式

> 调研日期：2026-09-06。用途：为 agent-test 的 `ask_user` / `confirm` /
> 审批继续（approver）机制提供设计依据。实现见
> `src/agent_test/human/service.py`、`src/agent_test/tools/ask.py`、
> `src/agent_test/tools/center.py`（approver 闸门）。

## 1. 澄清/补充消息：AskUserQuestion 一族

| 设计 | 形态 | 要点 |
| --- | --- | --- |
| Claude Code（Agent SDK） | `AskUserQuestion` 工具 | 模型自己生成问题与 2-4 个选项；宿主 UI 呈现并回传选择；`canUseTool` 回调在用户回答前一直挂起；也可走 PreToolUse hook 返回 defer 让进程退出、会话持久化后恢复。 |
| Agno `UserFeedbackTools` | `ask_user` 工具 | 一次可问多条问题：`question`（以 ? 结尾）+ `header`（≤12 字符短标签）+ `options`（2-4 个，label 1-5 词 + 可选 description）+ `multi_select`；运行暂停（`is_paused`），用户选择后 `continue_run` 继续。 |
| Timbal | `ask_user` / `ask_user_multi` / `confirm` 工具 | `suspend(payload, kind)` 暂停运行并发出 `InteractionEvent`（含 interaction_id、tool_call_id、response_schema）；恢复时以 `{interaction_id: value}` 续跑，suspend() 返回该值。 |
| opencode（PR #5958） | askquestion 工具 | Plan 模式下让 agent 向用户提问、在选项间选择或给出“解释/替代”。 |

共同点：**问题结构化（question + 少量选项）+ 运行暂停 + 用户回答作为工具结果回传 LLM + 可回放**。

## 2. 批准继续：approval gate 一族

| 设计 | 形态 | 要点 |
| --- | --- | --- |
| Claude Code `canUseTool` | 权限回调（非模型工具） | 工具调用需要批准时暂停执行；宿主返回 `allow(+updated_input)` 或 `deny(message)`。支持“approve and remember”：回显建议的权限规则，下次同类调用跳过提示。 |
| Cloudflare Agents | `needsApproval` 工具标记 | 服务端工具声明需要批准，执行前暂停等待用户 approve。 |
| Timbal | `confirm` / approval gate | 批准就是“resume 一个 bool”的特例；**不可逆动作放在批准门后**——门保证该动作的 handler 代码在批准前零执行。 |
| LangGraph | `interrupt()` / `Command(resume=value)` | 在图内任意点暂停，checkpointer 持久化；恢复时传入值继续；UI 支持 approve / edit / reject / answer 卡片。 |
| Codex（本环境/PR #10584） | `--ask-for-approval` + “Allow and remember” | approval policy（never/on-request）；会话级临时批准，同类调用本会话内不再询问。 |
| Ably AI transport | 请求审批 -> 运行挂起 -> 客户端批准/拒绝 | 持久会话跨设备/跨时间线审批，续跑用同一 runId。 |

共同点：**审批独立于“提问”**（不是模型工具，而是执行边界上的闸门）；批准 = allow（可改输入），拒绝 = deny + message 回传模型调整方案；可“记住”（会话级/持久化规则）减少打扰。

## 3. 对 agent-test 的落地映射

1. **ask_user / confirm 作为工具**（供 LLM 主动调用）：
   - `ask_user(question, options?, multi_select?, header?)` —— 澄清 / 补充消息；
   - `confirm(action, description?)` —— 批准继续；
   - 两者通过可注入 `AskService`（默认 `ConsoleAskService`，测试/UI 可替换）。
2. **审批继续接在执行边界**（对标 canUseTool / needsApproval）：
   - `ToolCenter(policy=..., approver=AskService)`；
   - 策略 `REQUIRE_APPROVAL` -> 征求批准：批准 -> `add_allowlist_for_command`
     记住前缀（会话级，对标 approve-and-remember / allow-and-remember）-> 执行；
     拒绝 -> `deny_by_user` 结果回传 LLM。
3. **pre_step 分工**（对标“决策先于副作用”）：
   - DENY（破坏性/越界）在 `ReactAgent._pre_step` 硬拦截，任何工具都不执行；
   - REQUIRE_APPROVAL 未配 approver 时也在 pre_step 拦截返回“需审批”；
   - 配了 approver 则放行到 ToolCenter 闸门做交互审批，保证被批动作代码在批准前零执行。

## 4. 参考来源

- Claude Code Agent SDK — Handle approvals and user input:
  https://code.claude.com/docs/en/agent-sdk/user-input
- Claude Code (community mirror) — user-input.md（AskUserQuestion + PreToolUse）:
  https://github.com/guess/claude_code/blob/v0.32.0/docs/guides/user-input.md
- Timbal — Suspend & interaction tools:
  https://docs.timbal.ai/human-in-the-loop/suspend
- LangChain LangGraph — Human-in-the-Loop / Interrupts:
  https://docs.langchain.com/oss/python/langchain/frontend/human-in-the-loop
- Agno — User Feedback（UserFeedbackTools / ask_user）:
  https://docs.agno.com/tools/toolkits/others/user-feedback
- Cloudflare Agents — Human in the Loop:
  https://github.com/cloudflare/agents/blob/main/guides/human-in-the-loop/README.md
- opencode — AskQuestion tool（PR #5958）:
  https://github.com/anomalyco/opencode/pull/5958
- OpenAI Codex — sandbox / approval policies:
  https://github.com/openai/codex/blob/main/docs/sandbox.md
- codex-ask-user（MCP server，结构化澄清）:
  https://github.com/blhsing/codex-ask-user
- Ably — Human-in-the-loop（AI transport）:
  https://ably.com/docs/ai-transport/features/human-in-the-loop