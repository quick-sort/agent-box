"""Tests for the generic ACP agent driver.

Pure-logic branches are covered with mocks (CI-safe and deterministic).
"""

from acp.schema import (
    DeniedOutcome,
    PermissionOption,
    PermissionOptionKind,
    ToolCallStart,
)

from agent_box.agents.acp import ACPAgent, ACPProvider
from agent_box.models import ProjectInfo

# ── helpers ──


def _opt(option_id: str, kind: PermissionOptionKind, name: str | None = None) -> PermissionOption:
    return PermissionOption(option_id=option_id, name=name or option_id, kind=kind)


def _make_agent(sample_project: ProjectInfo) -> ACPAgent:
    provider = ACPProvider(name="test", command=("test-cli", "acp"))
    return ACPAgent(sample_project, provider)


def _outcome_option_id(resp) -> str | None:
    """Extract the selected option_id from a RequestPermissionResponse."""
    outcome = resp.outcome
    return getattr(outcome, "option_id", None)


# ── _select_permission ──


def test_select_permission_allow_prefers_once():
    """allow=True checks allow_once before allow_always (code order)."""
    opts = [
        _opt("allow_always", "allow_always"),
        _opt("allow_once", "allow_once"),
    ]
    resp = ACPAgent._select_permission(opts, allow=True)
    assert _outcome_option_id(resp) == "allow_once"


def test_select_permission_allow_falls_back_to_always():
    resp = ACPAgent._select_permission([_opt("allow_always", "allow_always")], allow=True)
    assert _outcome_option_id(resp) == "allow_always"


def test_select_permission_deny_prefers_once():
    opts = [
        _opt("reject_always", "reject_always"),
        _opt("reject_once", "reject_once"),
    ]
    resp = ACPAgent._select_permission(opts, allow=False)
    assert _outcome_option_id(resp) == "reject_once"


def test_select_permission_deny_falls_back_to_always():
    resp = ACPAgent._select_permission([_opt("reject_always", "reject_always")], allow=False)
    assert _outcome_option_id(resp) == "reject_always"


def test_select_permission_no_matching_kind_cancels():
    # Options with valid kinds, but none matching what allow=True looks for
    # (allow_once/allow_always) — should fall through to a cancelled denial.
    resp = ACPAgent._select_permission([_opt("reject_once", "reject_once")], allow=True)
    assert isinstance(resp.outcome, DeniedOutcome)
    assert resp.outcome.outcome == "cancelled"


# ── _permission_response ──


def test_permission_response_numeric(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("a", "allow_once"), _opt("b", "allow_once")]
    resp = agent._permission_response(opts, "2")
    assert _outcome_option_id(resp) == "b"


def test_permission_response_numeric_out_of_range_denies(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("a", "allow_once", name="Alpha")]
    resp = agent._permission_response(opts, "99")
    assert isinstance(resp.outcome, DeniedOutcome)


def test_permission_response_by_option_name_case_insensitive(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("x", "allow_once", name="Read File")]
    resp = agent._permission_response(opts, "read file")
    assert _outcome_option_id(resp) == "x"


def test_permission_response_by_option_id(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("op_123", "allow_once", name="Read File")]
    resp = agent._permission_response(opts, "op_123")
    assert _outcome_option_id(resp) == "op_123"


def test_permission_response_approve_word(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("allow_always", "allow_always")]
    resp = agent._permission_response(opts, "同意")
    assert _outcome_option_id(resp) == "allow_always"


def test_permission_response_approve_word_with_trailing_text(sample_project: ProjectInfo):
    """Decision word is split on whitespace/punctuation, trailing text ignored."""
    agent = _make_agent(sample_project)
    opts = [_opt("allow_always", "allow_always")]
    resp = agent._permission_response(opts, "Yes please go ahead")
    assert _outcome_option_id(resp) == "allow_always"


def test_permission_response_unknown_denies(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    opts = [_opt("reject_always", "reject_always")]
    resp = agent._permission_response(opts, "definitely not")
    assert _outcome_option_id(resp) == "reject_always"


# ── _format_tool_call ──


def test_format_tool_call_known_kind_icon(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    update = ToolCallStart(tool_call_id="t1", title="read main.py", kind="read", session_update="tool_call")
    assert agent._format_tool_call(update) == "📖 read main.py"


def test_format_tool_call_unknown_kind_default_icon(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    # kind=None → icon falls back to ⚙️
    update = ToolCallStart(tool_call_id="t1", title="do thing", kind=None, session_update="tool_call")
    assert agent._format_tool_call(update) == "⚙️ do thing"


def test_format_tool_call_strips_project_path(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    title = f"{sample_project.path}/src/main.py"
    update = ToolCallStart(tool_call_id="t1", title=title, kind="read", session_update="tool_call")
    assert agent._format_tool_call(update) == "📖 ./src/main.py"


def test_format_tool_call_truncates_long_title(sample_project: ProjectInfo):
    agent = _make_agent(sample_project)
    update = ToolCallStart(
        tool_call_id="t1", title="x" * 200, kind="execute", session_update="tool_call",
    )
    result = agent._format_tool_call(update)
    assert result.startswith("🔧 ")
    assert result.endswith("...")
    assert len(result) <= 84  # icon + space + 77 chars + "..."
