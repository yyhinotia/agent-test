"""审计脚本的提示词规则判定（scripts/audit_session.py）。

回归 00c6dbaa 会话的误报：`python -c "import a; b"` 里的分号在引号内，
是语言字面量而非命令分隔符，不该判 FAIL。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def audit():
    spec = importlib.util.spec_from_file_location(
        "audit_session", ROOT / "scripts" / "audit_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses 需要能在 sys.modules 里找到本模块（解析字符串注解）
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _findings(audit, *commands):
    calls = [
        audit.CallRecord(
            call_id=f"c{index}",
            name="bash",
            args={"command": command},
            order=index,
        )
        for index, command in enumerate(commands)
    ]
    return {finding.key: finding for finding in audit.check_prompt_rules(calls)}


def test_strip_quoted_separates_quoted_and_unquoted(audit):
    body, quoted = audit._strip_quoted('python -c "import a; b"')
    assert quoted is True and ";" not in body

    body, quoted = audit._strip_quoted("git status; git log -1")
    assert quoted is False and ";" in body


def test_semicolon_inside_quotes_is_info_not_fail(audit):
    findings = _findings(audit, 'python -c "import pathlib,collections;print(1)"')
    assert findings["prompt.semicolon"].level == "PASS"
    assert findings["prompt.semicolon.quoted"].level == "INFO"
    (entry,) = findings["prompt.semicolon.quoted"].detail["commands"]
    assert entry["quoted"] is True


def test_unquoted_semicolon_still_fails(audit):
    findings = _findings(audit, "git status; git log -1")
    assert findings["prompt.semicolon"].level == "FAIL"
    assert "prompt.semicolon.quoted" not in findings


def test_env_check_rule_still_reports_first_command(audit):
    findings = _findings(
        audit,
        "ver && cd && python --version && git --version",
        'python -c "import os; print(os.name)"',
    )
    assert findings["prompt.env_check"].level == "PASS"


def test_no_bash_command_yields_skip(audit):
    findings = {f.key: f for f in audit.check_prompt_rules([])}
    assert findings["prompt.semicolon"].level == "SKIP"
