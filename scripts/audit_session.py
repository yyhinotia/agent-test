#!/usr/bin/env python
"""会话审计：把「增量知识」的验收标准变成可机械判定的报告。

用法
----
    python scripts/audit_session.py sessions/<session_id>.jsonl
    python scripts/audit_session.py sessions/<id>.jsonl --log logs/runtime.log
    python scripts/audit_session.py sessions/<id>.jsonl --max-context-tokens 2000 --json

判定项（对应 docs/增量知识.md 第 17 节 Validation Criteria）
----
    1. integrity    JSONL 可解析 / 尾部残缺行容忍 / turn 与 step 是否闭环
    2. state_machine approval x execution 组合是否合法；denied|blocked 必须 not_started
    3. batch        同一步内的工具批次规模（并行执行的结构证据）
    4. parallel     bash 子进程 in-flight 并发峰值（来自 logs/runtime.log）
    5. cascade      allowlist 级联：同前缀命令在获批后是否转为 auto
    6. prompt       首条 bash 是否为环境检查；命令是否含 ';'
    7. denial       被 denied/blocked 的命令是否被原样重发（期望：不重发）
    8. compaction   compact/summary 事件；压缩后环境信息（Python 版本）是否保留
    9. location     会话文件是否落在 src/ 下（CLI 未从仓库根启动）
   10. repeat       环境检查是否被重复执行（提示词要求执行过就不再重复）
   11. approval     审批链路健康度：重复 confirm、EOF 降级、疑似跨两次启动

另提供 --digest：按回合打印「用户说了什么 / 模型调了什么工具 / 结果状态」，
便于人工复盘（等价于之前临时写的分析脚本）。

设计说明
----
- 会话事件的 time 是「写入 session 的时刻」，而工具结果由 inbox 延迟到下一步
  的 _flush_step_messages 才落盘，因此批次耗时无法从 JSONL 测得；并行度改从
  logs/runtime.log 的 `bash 执行` / `bash 完成` 配对推算 in-flight 并发峰值。
- 批次归属按 seq 顺序推断（assistant/message 之后紧跟的连续 tool/result 属于
  同一批）；事件里的 step 字段是落盘时刻的步号，存在 +1 偏移，故不作为依据。
- 旧 JSONL（无 approval/execution 字段）自动降级：状态机判定记 SKIP。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# approval x execution 的合法组合（docs/增量知识.md 5.2 组合表）
VALID_COMBOS = {
    ("auto", "success"),
    ("auto", "failed"),
    ("approved", "success"),
    ("approved", "failed"),
    ("denied", "not_started"),
    ("blocked", "not_started"),
}
BLOCKING_DECISIONS = {"denied", "blocked"}

_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
    r" \|\s*(?P<level>[A-Z]+)\s*\| session=(?P<session>\S+)"
    r" turn=(?P<turn>\S+) step=(?P<step>\S+)"
    r" \| (?P<logger>[^|]+) \| (?P<msg>.*)$"
)
_ENV_CHECK_RE = re.compile(r"\b(uname|ver|systeminfo)\b", re.IGNORECASE)
_PY_VERSION_RE = re.compile(r"Python\s+(\d+\.\d+\.\d+)")
_SEMICOLON_RE = re.compile(r";")


@dataclass
class Finding:
    level: str  # PASS / FAIL / WARN / SKIP / INFO
    key: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallRecord:
    call_id: str
    name: str
    args: dict[str, Any]
    order: int
    result: dict[str, Any] | None = None
    decision: str | None = None
    status: str | None = None
    turn: int = 0

    @property
    def command(self) -> str:
        if self.name != "bash":
            return ""
        value = self.args.get("command")
        return value if isinstance(value, str) else ""


@dataclass
class Batch:
    turn: int
    assistant: dict[str, Any]
    calls: list[CallRecord]


# ---------- 载入 ----------


def load_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读 JSONL 事件流；返回 (事件, 无法解析的行数)。尾部残缺行按约定容忍。"""
    events: list[dict[str, Any]] = []
    bad = 0
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(obj, dict) and "type" in obj:
            events.append(obj)
        else:
            bad += 1
    events.sort(key=lambda item: item.get("seq", 0))
    return events, bad


def load_log(path: Path, session_id: str) -> list[tuple[datetime, str]]:
    """读 runtime.log，返回该 session 的 (时间, 消息) 序列。"""
    if not path.exists():
        return []
    rows: list[tuple[datetime, str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _LOG_RE.match(raw)
        if not match:
            continue
        if match.group("session") != session_id:
            continue
        try:
            ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            continue
        rows.append((ts, match.group("msg")))
    return rows


# ---------- 结构还原 ----------


def collect_calls(events: list[dict[str, Any]]) -> list[CallRecord]:
    """从 assistant/message 里抽出工具调用，并按 tool_call_id 挂上结果。"""
    calls: dict[str, CallRecord] = {}
    order = 0
    for event in events:
        if event.get("type") != "assistant/message":
            continue
        data = event.get("data") or {}
        for block in data.get("content") or []:
            if not isinstance(block, dict) or "name" not in block or "args" not in block:
                continue
            try:
                args = json.loads(block.get("args") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            call_id = str(block.get("id", ""))
            calls[call_id] = CallRecord(
                call_id=call_id,
                name=str(block.get("name", "")),
                args=args,
                order=order,
                turn=int(event.get("turn", 0) or 0),
            )
            order += 1
    for event in events:
        if event.get("type") != "tool/result":
            continue
        data = event.get("data") or {}
        record = calls.get(str(data.get("tool_call_id", "")))
        if record is None:
            continue
        record.result = data
        approval = data.get("approval")
        execution = data.get("execution")
        if isinstance(approval, dict):
            record.decision = str(approval.get("decision") or "") or None
        if isinstance(execution, dict):
            record.status = str(execution.get("status") or "") or None
    return sorted(calls.values(), key=lambda item: item.order)


def build_batches(events: list[dict[str, Any]]) -> list[Batch]:
    """按 seq 顺序切批：assistant/message 之后连续的 tool/result 属同一批。"""
    batches: list[Batch] = []
    index = 0
    total = len(events)
    while index < total:
        event = events[index]
        if event.get("type") != "assistant/message":
            index += 1
            continue
        results: list[dict[str, Any]] = []
        scan = index + 1
        while scan < total and events[scan].get("type") == "tool/result":
            results.append(events[scan])
            scan += 1
        if results:
            calls = [
                CallRecord(
                    call_id=str((item.get("data") or {}).get("tool_call_id", "")),
                    name="",
                    args={},
                    order=position,
                )
                for position, item in enumerate(results)
            ]
            batches.append(
                Batch(
                    turn=int(event.get("turn", 0) or 0),
                    assistant=event,
                    calls=calls,
                )
            )
        index = scan
    return batches


def group_batch_calls(calls: list[CallRecord], batches: list[Batch]) -> list[Batch]:
    """把真实 CallRecord 按批次结果集合归位（保持原始顺序）。"""
    index = {call.call_id: call for call in calls}
    out: list[Batch] = []
    for batch in batches:
        resolved = [
            index[item.call_id] for item in batch.calls if item.call_id in index
        ]
        out.append(Batch(turn=batch.turn, assistant=batch.assistant, calls=resolved))
    return out


# ---------- 判定 ----------


def check_integrity(
    events: list[dict[str, Any]], bad_lines: int, calls: list[CallRecord]
) -> list[Finding]:
    findings: list[Finding] = []
    counts = Counter(event.get("type") for event in events)
    if bad_lines:
        findings.append(
            Finding(
                "WARN",
                "integrity.parse",
                f"{bad_lines} 行无法解析（尾部残缺行容忍属预期；非尾部残缺请排查）",
            )
        )
    else:
        findings.append(
            Finding("PASS", "integrity.parse", f"{len(events)} 条事件全部可解析")
        )

    starts = counts.get("turn/start", 0)
    ends = counts.get("turn/end", 0)
    level = "PASS" if starts == ends and starts > 0 else "WARN"
    if starts == 0:
        level = "FAIL"
    findings.append(
        Finding(
            level,
            "integrity.turns",
            f"turn/start={starts} turn/end={ends}（回合必须成对闭合）",
        )
    )

    step_starts = counts.get("step/start", 0)
    step_ends = counts.get("step/end", 0)
    findings.append(
        Finding(
            "PASS" if step_starts == step_ends else "WARN",
            "integrity.steps",
            f"step/start={step_starts} step/end={step_ends}",
        )
    )

    errors = [
        event
        for event in events
        if event.get("type") == "runtime/error"
    ]
    if errors:
        findings.append(
            Finding(
                "WARN",
                "integrity.errors",
                f"{len(errors)} 条 runtime/error 事件",
                {"messages": [str((item.get("data") or {}).get("message", ""))[:120] for item in errors]},
            )
        )
    return findings


def check_state_machine(calls: list[CallRecord]) -> list[Finding]:
    findings: list[Finding] = []
    combos: Counter = Counter()
    legacy = 0
    illegal: list[dict[str, Any]] = []
    for call in calls:
        if call.result is None:
            continue
        if call.decision is None and call.status is None:
            legacy += 1
            continue
        combos[(call.decision, call.status)] += 1
        if (call.decision, call.status) not in VALID_COMBOS:
            illegal.append(
                {"command": call.command, "approval": call.decision, "execution": call.status}
            )
        if call.decision in BLOCKING_DECISIONS and call.status != "not_started":
            illegal.append(
                {
                    "command": call.command,
                    "approval": call.decision,
                    "execution": call.status,
                    "rule": "denied/blocked 必须 not_started",
                }
            )

    if legacy and not combos:
        findings.append(
            Finding(
                "SKIP",
                "state_machine.combos",
                f"{legacy} 条工具结果为旧格式（无 approval/execution），本项跳过",
            )
        )
        return findings

    findings.append(
        Finding(
            "FAIL" if illegal else "PASS",
            "state_machine.combos",
            "非法组合: " + json.dumps(illegal, ensure_ascii=False)
            if illegal
            else "组合分布: "
            + ", ".join(
                f"{key[0]}/{key[1]}x{value}"
                for key, value in sorted(
                    combos.items(), key=lambda item: str(item[0])
                )
            ),
            {"illegal": illegal},
        )
    )
    findings.append(
        Finding(
            "INFO",
            "state_machine.tools",
            f"工具结果 {sum(combos.values())} 条（其中旧格式 {legacy} 条）",
        )
    )
    return findings


def check_batches(batches: list[Batch]) -> list[Finding]:
    findings: list[Finding] = []
    sizes = [len(batch.calls) for batch in batches]
    if not batches:
        findings.append(Finding("SKIP", "batch.size", "会话中没有工具批次"))
        return findings
    multi = [size for size in sizes if size >= 2]
    findings.append(
        Finding(
            "PASS" if multi else "WARN",
            "batch.size",
            f"批次 {len(batches)} 个；规模 {sizes}；"
            + (
                f"其中多工具批次 {multi}（并行执行的结构证据）"
                if multi
                else "未出现多工具批次，无法验证 gather 并行"
            ),
        )
    )
    return findings


def check_parallel(log_rows: list[tuple[datetime, str]]) -> list[Finding]:
    findings: list[Finding] = []
    if not log_rows:
        findings.append(
            Finding("SKIP", "parallel.peak", "未提供/未找到 logs/runtime.log，无法测并发")
        )
        return findings
    inflight = 0
    peak = 0
    starts: deque[datetime] = deque()
    durations: list[float] = []
    approvals = 0
    for ts, message in log_rows:
        if "bash 执行 |" in message:
            inflight += 1
            peak = max(peak, inflight)
            starts.append(ts)
        elif "bash 完成 |" in message:
            inflight = max(0, inflight - 1)
            if starts:
                durations.append((ts - starts.popleft()).total_seconds())
        if "CLI 审批 |" in message:
            approvals += 1
    detail: dict[str, Any] = {"peak_inflight": peak, "approval_calls": approvals}
    if durations:
        detail["bash_durations_s"] = {
            "min": round(min(durations), 3),
            "median": round(statistics.median(durations), 3),
            "max": round(max(durations), 3),
            "n": len(durations),
        }
    findings.append(
        Finding(
            "PASS" if peak >= 2 else "WARN",
            "parallel.peak",
            f"bash 并发峰值={peak}（>=2 表示确实并行执行）",
            detail,
        )
    )
    findings.append(
        Finding(
            "INFO",
            "parallel.approvals",
            f"CLI 审批调用 {approvals} 次",
            {"approval_calls": approvals},
        )
    )
    return findings


def _prefix(command: str) -> tuple[str, ...]:
    tokens = command.strip().split()
    if not tokens:
        return ()
    return tuple(tokens[:2]) if len(tokens) >= 2 else tuple(tokens)


def check_cascade(calls: list[CallRecord]) -> list[Finding]:
    approved_prefixes: dict[tuple[str, ...], str] = {}
    cascade_hits: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for call in calls:
        command = call.command
        if not command:
            continue
        prefix = _prefix(command)
        if call.decision == "approved":
            approved_prefixes.setdefault(prefix, command)
        elif call.decision == "auto" and prefix in approved_prefixes:
            cascade_hits.append(
                {
                    "approved_first": approved_prefixes[prefix],
                    "auto_after": command,
                    "prefix": " ".join(prefix),
                }
            )
        elif call.decision == "denied":
            pending.append({"command": command, "prefix": " ".join(prefix)})
    findings: list[Finding] = []
    if cascade_hits:
        findings.append(
            Finding(
                "PASS",
                "cascade.allowlist",
                "allowlist 级联生效: "
                + "; ".join(
                    f"{item['prefix']}（{item['approved_first']} -> {item['auto_after']} 自动放行）"
                    for item in cascade_hits
                ),
                {"hits": cascade_hits},
            )
        )
    elif approved_prefixes:
        findings.append(
            Finding(
                "WARN",
                "cascade.allowlist",
                "存在 approved 命令但后续没有同前缀 auto 命令，级联未被验证",
                {"approved_prefixes": [" ".join(key) for key in approved_prefixes]},
            )
        )
    else:
        findings.append(
            Finding("SKIP", "cascade.allowlist", "没有 approved 命令，无法验证级联")
        )
    if pending:
        findings.append(
            Finding(
                "INFO",
                "cascade.denied_prefix",
                "存在被拒绝的命令（如需验证「拒绝后不重发」见 denial.resubmit）",
                {"commands": [item["command"] for item in pending]},
            )
        )
    return findings


def _strip_quoted(command: str) -> tuple[str, bool]:
    """剥离引号内内容，返回 (引号外正文, 是否出现过引号片段)。

    `;` 只有出现在**引号之外**时才是命令分隔符；`python -c "import a; b"`
    里的分号是语言字面量（00c6dbaa 会话的误报来源）。
    """
    out: list[str] = []
    quoted = False
    quote: str | None = None
    for char in command:
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            quoted = True
            continue
        out.append(char)
    return "".join(out), quoted


def check_prompt_rules(calls: list[CallRecord]) -> list[Finding]:
    findings: list[Finding] = []
    bash_commands = [call.command for call in calls if call.command]
    if not bash_commands:
        findings.append(Finding("SKIP", "prompt.env_check", "会话中没有 bash 命令"))
        findings.append(Finding("SKIP", "prompt.semicolon", "会话中没有 bash 命令"))
        return findings

    env_index = next(
        (
            position
            for position, command in enumerate(bash_commands)
            if _ENV_CHECK_RE.search(command) and "python" in command.lower()
        ),
        None,
    )
    if env_index is None:
        level, message = "FAIL", "没有任何一条命令符合环境检查（uname/ver + python --version）"
    elif env_index == 0:
        level, message = "PASS", f"首条 bash 即环境检查: {bash_commands[0][:120]}"
    else:
        level, message = (
            "WARN",
            f"环境检查出现在第 {env_index + 1} 条 bash（期望首条）: {bash_commands[env_index][:120]}",
        )
    findings.append(Finding(level, "prompt.env_check", message))

    offenders: list[dict[str, Any]] = []
    quoted_only: list[dict[str, Any]] = []
    for command in bash_commands:
        if not _SEMICOLON_RE.search(command):
            continue
        body, had_quote = _strip_quoted(command)
        entry = {"command": command[:200], "quoted": had_quote}
        if _SEMICOLON_RE.search(body):
            offenders.append(entry)
        else:
            quoted_only.append(entry)
    if offenders:
        findings.append(
            Finding(
                "FAIL",
                "prompt.semicolon",
                f"{len(offenders)} 条命令在引号外含 ';' 分隔符（系统提示词硬性禁令）",
                {"offenders": offenders},
            )
        )
    else:
        findings.append(
            Finding(
                "PASS",
                "prompt.semicolon",
                f"{len(bash_commands)} 条 bash 命令引号外均无 ';'",
            )
        )
    if quoted_only:
        findings.append(
            Finding(
                "INFO",
                "prompt.semicolon.quoted",
                f"{len(quoted_only)} 条命令的 ';' 只出现在引号内（语言字面量，非命令分隔符）",
                {"commands": quoted_only},
            )
        )
    return findings


def check_denial_resubmit(calls: list[CallRecord]) -> list[Finding]:
    blocked: list[tuple[str, CallRecord]] = []
    resubmitted: list[dict[str, str]] = []
    for call in calls:
        command = call.command
        if not command:
            continue
        normalized = " ".join(command.split()).lower()
        for original, source in blocked:
            if normalized == original:
                resubmitted.append(
                    {
                        "command": command,
                        "first_decision": str(source.decision),
                        "later_decision": str(call.decision),
                    }
                )
                break
        if call.decision in BLOCKING_DECISIONS:
            blocked.append((normalized, call))
    if not blocked:
        return [Finding("SKIP", "denial.resubmit", "没有被拒绝/拦截的命令")]
    if resubmitted:
        return [
            Finding(
                "FAIL",
                "denial.resubmit",
                f"{len(resubmitted)} 条被拒命令被原样重发（期望：调整方案而非重试）",
                {"resubmitted": resubmitted},
            )
        ]
    return [
        Finding(
            "PASS",
            "denial.resubmit",
            f"{len(blocked)} 条被拒命令均未原样重发",
            {"blocked": [item[0] for item in blocked]},
        )
    ]


def check_compaction(
    events: list[dict[str, Any]], calls: list[CallRecord]
) -> list[Finding]:
    findings: list[Finding] = []
    compact_events = [event for event in events if event.get("type") == "compact/summary"]
    if not compact_events:
        findings.append(
            Finding(
                "WARN",
                "compaction.summary",
                "没有 compact/summary 事件（需 --max-context-tokens 调小才会触发）",
            )
        )
        return findings

    last_seq = max(int(event.get("seq", 0) or 0) for event in compact_events)
    findings.append(
        Finding(
            "PASS",
            "compaction.summary",
            f"{len(compact_events)} 条 compact/summary（最后一条 seq={last_seq}）",
            {"seqs": [event.get("seq") for event in compact_events]},
        )
    )

    versions: set[str] = set()
    for call in calls:
        if not call.command or call.result is None:
            continue
        if not _ENV_CHECK_RE.search(call.command):
            continue
        for block in call.result.get("content") or []:
            if isinstance(block, dict):
                versions.update(_PY_VERSION_RE.findall(str(block.get("content", ""))))
    if not versions:
        findings.append(
            Finding("SKIP", "compaction.env_retained", "压缩前未捕获到 Python 版本，无法判断")
        )
        return findings

    later_text = "\n".join(
        str(block.get("content", ""))
        for event in events
        if event.get("type") == "assistant/message"
        and int(event.get("seq", 0) or 0) > last_seq
        for block in ((event.get("data") or {}).get("content") or [])
        if isinstance(block, dict) and "content" in block and "name" not in block
    )
    kept = sorted(version for version in versions if version in later_text)
    findings.append(
        Finding(
            "PASS" if kept else "WARN",
            "compaction.env_retained",
            (
                f"压缩后仍保留环境信息（Python {', '.join(kept)}）"
                if kept
                else f"压缩后的助手回复未再出现环境版本（压缩前看到 {sorted(versions)}）"
            ),
            {"versions": sorted(versions), "kept": kept},
        )
    )
    return findings


def check_location(path: Path) -> list[Finding]:
    """会话文件落在 src/ 下 = CLI 不是从仓库根启动（产物污染包目录）。"""
    lowered = {part.lower() for part in path.parts}
    if "src" in lowered:
        return [
            Finding(
                "WARN",
                "integrity.location",
                "会话文件位于 src/ 下：CLI 不是从仓库根启动，"
                "sessions/logs/临时文件会落在包目录里",
            )
        ]
    return [Finding("PASS", "integrity.location", "启动目录正常（未污染包目录）")]


def check_repeat_env_check(calls: list[CallRecord]) -> list[Finding]:
    """环境检查被重复执行（提示词要求「执行过就不再重复检查」）。"""
    env_commands = [
        call.command
        for call in calls
        if call.command
        and _ENV_CHECK_RE.search(call.command)
        and "python" in call.command.lower()
    ]
    if not env_commands:
        return [Finding("SKIP", "prompt.env_repeat", "没有环境检查命令")]
    if len(env_commands) == 1:
        return [
            Finding(
                "PASS",
                "prompt.env_repeat",
                f"环境检查只执行了 1 次: {env_commands[0][:100]}",
            )
        ]
    return [
        Finding(
            "WARN",
            "prompt.env_repeat",
            f"环境检查执行了 {len(env_commands)} 次（提示词要求执行过就不再重复）",
            {"commands": [command[:120] for command in env_commands]},
        )
    ]


def check_approval_flow(calls: list[CallRecord]) -> list[Finding]:
    """审批链路健康度：重复 confirm / EOF 降级 / 疑似跨两次启动。"""
    findings: list[Finding] = []
    confirms = [call for call in calls if call.name == "confirm"]
    approvals = [call for call in calls if call.decision == "approved"]

    if confirms and approvals:
        findings.append(
            Finding(
                "WARN",
                "approval.double_confirm",
                f"既有 {len(confirms)} 次 confirm 工具审批、又有 {len(approvals)} 次策略审批："
                "同一条命令可能被问了两次（提示词要求 bash 命令不要额外调 confirm）",
                {
                    "confirm_actions": [
                        str(call.args.get("action", ""))[:120] for call in confirms
                    ],
                    "approved_commands": [
                        call.command[:120] for call in approvals
                    ],
                },
            )
        )
    else:
        findings.append(
            Finding(
                "PASS",
                "approval.double_confirm",
                f"confirm 工具 {len(confirms)} 次 / 策略审批 {len(approvals)} 次，无重复征询",
            )
        )

    eof_denied = []
    for call in calls:
        if call.result is None:
            continue
        matched = any(
            "输入已结束" in str(block.get("content", ""))
            for block in call.result.get("content") or []
            if isinstance(block, dict)
        )
        if matched:
            eof_denied.append(call)
    if not eof_denied:
        findings.append(
            Finding("PASS", "approval.eof", "没有因输入结束而降级的审批")
        )
        return findings

    last_eof_order = max(call.order for call in eof_denied)
    after = [call for call in calls if call.order > last_eof_order]
    findings.append(
        Finding(
            "WARN",
            "approval.eof",
            f"{len(eof_denied)} 次审批因输入结束（EOF）降级为拒绝"
            "（管道/重定向或 Ctrl-Z 的预期行为，交互式运行不应出现）",
            {"actions": [str(call.args.get("action", ""))[:100] for call in eof_denied]},
        )
    )
    if after:
        findings.append(
            Finding(
                "WARN",
                "session.runs",
                f"EOF 降级之后仍有 {len(after)} 次工具调用：疑似跨两次启动"
                "（第二次用 -r 续聊），单进程内 EOF 之后不应再读入输入",
                {"commands_after_eof": [call.command[:100] for call in after[:5]]},
            )
        )
    else:
        findings.append(
            Finding("PASS", "session.runs", "EOF 之后没有新的输入，单次运行闭合")
        )
    return findings


# ---------- 输出 ----------


def render_digest(
    events: list[dict[str, Any]], calls: list[CallRecord]
) -> None:
    """按回合回放：用户输入 / 模型文本 / 工具调用与审批执行状态。"""
    by_id = {call.call_id: call for call in calls}

    def clip(value: Any, limit: int = 200) -> str:
        flat = " ".join(str(value).split())
        return flat if len(flat) <= limit else flat[:limit] + "..."

    print("")
    print("== 回合回放（--digest） ==")
    for event in events:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "user/message":
            print(
                "\n=== TURN %s 用户: %s"
                % (
                    event.get("turn"),
                    clip(" ".join(b.get("content", "") for b in data.get("content") or []), 220),
                )
            )
        elif kind == "assistant/message":
            for block in data.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if "name" in block and "args" in block:
                    try:
                        args = json.loads(block.get("args") or "{}")
                    except ValueError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    target = (
                        args.get("command")
                        or args.get("action")
                        or args.get("question")
                        or args.get("pattern")
                        or args.get("file_path")
                        or args
                    )
                    call = by_id.get(str(block.get("id", "")))
                    state = "%s/%s" % (
                        call.decision if call and call.decision else "?",
                        call.status if call and call.status else "?",
                    )
                    print(
                        "  step%s 调 %s: %s [%s]"
                        % (event.get("step"), block.get("name"), clip(target, 160), state)
                    )
                elif block.get("content", "").strip():
                    print("  step%s 说: %s" % (event.get("step"), clip(block["content"])))
        elif kind == "runtime/error":
            print("  !! runtime/error: %s" % clip(data, 200))


def render(path: Path, events: list[dict[str, Any]], findings: list[Finding]) -> None:
    counts = Counter(event.get("type") for event in events)
    print(f"== 会话审计: {path.stem} ==")
    print(f"文件: {path}")
    print(
        "事件: "
        + f"{len(events)} 条 | "
        + " ".join(f"{name}={counts[name]}" for name in sorted(counts))
    )
    print("")
    width = max((len(item.key) for item in findings), default=10)
    for item in findings:
        print(f"[{item.level:4}] {item.key.ljust(width)}  {item.message}")
    summary = Counter(item.level for item in findings)
    print("")
    print(
        "汇总: "
        + " ".join(
            f"{level}={summary.get(level, 0)}"
            for level in ("PASS", "FAIL", "WARN", "SKIP", "INFO")
        )
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit_session", description="会话审计（增量知识验收判定）"
    )
    parser.add_argument("session", help="sessions/<session_id>.jsonl 路径")
    parser.add_argument(
        "--log",
        default="logs/runtime.log",
        help="运行时日志路径（默认 logs/runtime.log；传空串跳过）",
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=None,
        help="复现会话时的上下文窗口（仅用于打印阈值提示）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument(
        "--digest",
        action="store_true",
        help="额外按回合打印「用户 / 工具调用 / 结果状态」回放",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.session)
    if not path.exists():
        print(f"[FAIL] 会话文件不存在: {path}", file=sys.stderr)
        return 2

    events, bad_lines = load_events(path)
    calls = collect_calls(events)
    batches = group_batch_calls(calls, build_batches(events))
    log_rows = load_log(Path(args.log), path.stem) if args.log else []

    findings: list[Finding] = []
    findings += check_integrity(events, bad_lines, calls)
    findings += check_location(path)
    findings += check_state_machine(calls)
    findings += check_batches(batches)
    findings += check_parallel(log_rows)
    findings += check_cascade(calls)
    findings += check_prompt_rules(calls)
    findings += check_repeat_env_check(calls)
    findings += check_approval_flow(calls)
    findings += check_denial_resubmit(calls)
    findings += check_compaction(events, calls)
    if args.max_context_tokens:
        findings.append(
            Finding(
                "INFO",
                "compaction.window",
                "上下文窗口={} tokens（TokenMeter 阈值按比例触发压缩）".format(
                    args.max_context_tokens
                ),
            )
        )

    if args.json:
        print(
            json.dumps(
                {
                    "session": path.stem,
                    "events": len(events),
                    "bad_lines": bad_lines,
                    "findings": [
                        {
                            "level": item.level,
                            "key": item.key,
                            "message": item.message,
                            "detail": item.detail,
                        }
                        for item in findings
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        render(path, events, findings)
        if args.digest:
            render_digest(events, calls)
    return 1 if any(item.level == "FAIL" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())