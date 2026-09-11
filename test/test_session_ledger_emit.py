"""Unit tests for the append-only session ledger emitter."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import session_ledger_emit as emit
from kiro_crew.ledger import ledger_path, ledger_root

SESSION = "acp-sess-0001"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _ledger_path(session_id: str = SESSION) -> Path:
    """Ask the storage library where it puts things; never pin its layout."""
    return ledger_path("session", session_id)


def _store_root() -> Path:
    return ledger_root("session")


def _entries(session_id: str = SESSION) -> list[dict]:
    """Every line, header first, exactly as the emitter left it."""
    path = _ledger_path(session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    """The entries after the header line."""
    return _entries(session_id)[1:]


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


# --- creation and header ---------------------------------------------------


def test_first_open_creates_the_ledger_file():
    assert not _ledger_path().exists()
    _open_session()
    assert _ledger_path().is_file()


def test_the_header_is_line_one_and_carries_owner_agent_slot_cwd():
    _open_session()
    header = _entries()[0]
    assert header["type"] == "session"
    assert header["id"] == SESSION
    assert header["owner"] == "default"
    assert header["agent"] == "kirocrew"
    assert header["slot"] == "chat-7"
    assert header["cwd"] == "/home/dev/project"
    assert isinstance(header["createdAt"], int)


def test_opened_entry_echoes_the_header_and_adds_the_model():
    _open_session()
    opened = [e for e in _body() if e["type"] == "session/opened"]
    assert len(opened) == 1
    entry = opened[0]
    assert entry["src"] == "gateway"
    assert isinstance(entry["time"], int)
    data = entry["data"]
    assert data["agent"] == "kirocrew"
    assert data["slot"] == "chat-7"
    assert data["cwd"] == "/home/dev/project"
    assert data["owner"] == "default"
    assert data["resumed"] is False
    # The storage header has no model field, so the entry carries it.
    assert data["model"] == "claude-opus-5"


def test_an_agentless_call_site_still_produces_a_valid_header():
    emit.on_session_opened(SESSION, slot="chat-1")
    assert _entries()[0]["agent"] == "kirocrew"


def test_reopening_appends_instead_of_truncating():
    _open_session()
    first = len(_entries())
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", slot="chat-7", resumed=True)
    entries = _entries()
    assert len(entries) == first + 1
    assert entries[-1]["data"]["resumed"] is True
    assert sum(1 for e in entries if e["type"] == "session") == 1


def test_a_warm_reuse_of_the_same_session_adds_no_second_opened_entry():
    """The claim runs every turn; only a create or a re-attach is worth an entry."""
    _open_session()
    _open_session()
    _open_session()
    assert sum(1 for e in _body() if e["type"] == "session/opened") == 1


def test_a_warm_reuse_still_clears_the_stale_turn_anchor():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    _open_session()
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-1")
    assert "thread" not in _body()[-1]


def test_owner_is_written_once_and_a_reopen_cannot_change_it():
    emit.on_session_opened(SESSION, agent="kirocrew", owner="raymond")
    emit.reset_caches()
    emit.on_session_opened(SESSION, agent="kirocrew", owner="somebody-else")
    assert _entries()[0]["owner"] == "raymond"


# --- a whole turn ----------------------------------------------------------


def test_one_full_turn_produces_contiguous_seq():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(
        SESSION,
        4,
        input_tokens=1200,
        output_tokens=340,
        cache_read_tokens=9000,
        cache_write_tokens=15,
        credits=0.42,
        duration_ms=8123,
        stop_reason="end_turn",
        model="claude-opus-5",
        provider="kiro",
    )
    body = _body()
    assert [e["seq"] for e in body] == list(range(1, len(body) + 1))
    assert [e["type"] for e in body] == [
        "session/opened",
        "turn/started",
        "tool/called",
        "tool/completed",
        "turn/completed",
    ]


def test_a_turn_is_a_thread_anchored_on_its_started_entry():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_tool_called(SESSION, 4, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 4, name="fs_read", status="completed", call_id="tc-1")
    emit.on_turn_completed(SESSION, 4, stop_reason="end_turn")
    by_type = {e["type"]: e for e in _body()}
    anchor = by_type["turn/started"]["seq"]
    assert "thread" not in by_type["turn/started"], "the anchor threads nothing"
    for kind in ("tool/called", "tool/completed", "turn/completed"):
        assert by_type[kind]["thread"] == anchor


def test_entries_outside_a_turn_carry_no_thread():
    _open_session()
    emit.on_session_closed(SESSION, "reset")
    for entry in _body():
        assert "thread" not in entry


def test_a_second_turn_anchors_its_own_thread():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_tool_called(SESSION, 2, name="fs_read", call_id="tc-2")
    starts = [e for e in _body() if e["type"] == "turn/started"]
    tool = [e for e in _body() if e["type"] == "tool/called"][0]
    assert tool["thread"] == starts[1]["seq"] != starts[0]["seq"]


def test_turn_completed_carries_the_four_token_counts_and_cost():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(
        SESSION,
        1,
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=33,
        cache_write_tokens=44,
        credits=1.5,
        duration_ms=99,
        stop_reason="end_turn",
    )
    data = _body()[-1]["data"]
    assert data["tokens"] == {
        "input": 11,
        "output": 22,
        "cache_read": 33,
        "cache_write": 44,
    }
    assert data["credits"] == 1.5
    assert data["duration_ms"] == 99
    assert data["stop_reason"] == "end_turn"


def test_an_aborted_turn_leaves_a_started_with_no_completion():
    _open_session()
    emit.on_turn_started(SESSION, 2, "cron")
    types = [e["type"] for e in _body()]
    assert types.count("turn/started") == 1
    assert "turn/completed" not in types


def test_a_recovery_reentry_is_its_own_turn_pair_marked_by_depth():
    _open_session()
    emit.on_turn_started(SESSION, 5, "user", depth=0)
    emit.on_turn_completed(SESSION, 5, stop_reason="cancelled", depth=0)
    emit.on_turn_started(SESSION, 5, "user", depth=1)
    emit.on_turn_completed(SESSION, 5, stop_reason="end_turn", depth=1)
    turns = [e for e in _body() if e["type"].startswith("turn/")]
    assert [e["data"]["depth"] for e in turns] == [0, 0, 1, 1]


def test_every_actor_the_dispatcher_distinguishes_is_recorded_verbatim():
    _open_session()
    for actor in ("user", "cron", "autonudge", "subagent", "crew"):
        emit.on_turn_started(SESSION, 1, actor)
    recorded = [e["data"]["actor"] for e in _body() if e["type"] == "turn/started"]
    assert recorded == ["user", "cron", "autonudge", "subagent", "crew"]


def test_an_unknown_actor_is_recorded_as_other_not_guessed():
    _open_session()
    emit.on_turn_started(SESSION, 1, "something-new")
    assert _body()[-1]["data"]["actor"] == "other"


# --- tools, approvals, model, compaction ----------------------------------


def test_a_tool_call_is_identified_by_id_in_data_and_records_no_arguments():
    _open_session()
    emit.on_turn_started(SESSION, 3, "user")
    emit.on_tool_called(
        SESSION, 3, name="execute_bash", server="", kind="execute", call_id="tc-9"
    )
    entry = _body()[-1]
    # ref is a citation of another ledger's lines, so a tool call id is data.
    assert "ref" not in entry
    assert entry["data"] == {
        "turn": 3,
        "call_id": "tc-9",
        "name": "execute_bash",
        "server": "",
        "kind": "execute",
    }


def test_an_mcp_tool_records_its_server():
    _open_session()
    emit.on_tool_called(
        SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-3"
    )
    assert _body()[-1]["data"]["server"] == "builder-mcp"


def test_tool_completion_measures_elapsed_ms_from_its_call():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-2")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-2")
    assert _body()[-1]["data"]["elapsed_ms"] >= 0


def test_completion_inherits_the_identity_the_call_frame_carried():
    """The terminal ACP frame repeats neither the tool name nor its server."""
    _open_session()
    emit.on_tool_called(
        SESSION, 1, name="InternalSearch", server="builder-mcp", call_id="tc-4"
    )
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-4")
    data = _body()[-1]["data"]
    assert data["name"] == "InternalSearch"
    assert data["server"] == "builder-mcp"


def test_an_explicit_completion_name_is_not_overwritten_by_the_remembered_one():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="old", server="old-srv", call_id="tc-5")
    emit.on_tool_completed(
        SESSION, 1, name="new", server="new-srv", status="completed", call_id="tc-5"
    )
    data = _body()[-1]["data"]
    assert data["name"] == "new"
    assert data["server"] == "new-srv"


def test_tool_completion_without_a_matching_call_omits_elapsed_ms():
    _open_session()
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="unseen")
    assert "elapsed_ms" not in _body()[-1]["data"]


def test_approval_request_and_decision_share_the_approval_id():
    _open_session()
    emit.on_turn_started(SESSION, 2, "user")
    emit.on_approval_requested(SESSION, 2, approval_id="ap-1", tool="execute_bash")
    emit.on_approval_decided(SESSION, 2, approval_id="ap-1", decision="rejected_once")
    request, decision = _body()[-2:]
    assert request["data"]["approval_id"] == decision["data"]["approval_id"] == "ap-1"
    assert request["data"]["tool"] == "execute_bash"
    assert decision["data"]["decision"] == "rejected_once"


def test_compaction_records_percentages_not_token_counts():
    _open_session()
    emit.on_compaction_applied(SESSION, pct_before=0.92, pct_after=0.31)
    data = _body()[-1]["data"]
    assert data["pct_before"] == 0.92
    assert data["pct_after"] == 0.31
    assert data["freed_pct"] == pytest.approx(0.61)
    assert "before_tokens" not in data


def test_model_selection_records_its_source():
    _open_session()
    emit.on_model_selected(SESSION, "claude-haiku-4.5", "fallback")
    assert _body()[-1]["data"] == {
        "model": "claude-haiku-4.5",
        "source": "fallback",
    }


def test_close_records_the_gateway_reason_verbatim():
    _open_session()
    emit.on_session_closed(SESSION, "shutdown")
    assert _body()[-1]["data"]["reason"] == "shutdown"


# --- fail-soft ------------------------------------------------------------


def test_a_refused_oversize_write_is_swallowed_and_warned_once(caplog):
    _open_session()
    before = len(_entries())
    with caplog.at_level(logging.WARNING, logger=emit.logger.name):
        emit.on_tool_called(SESSION, 1, name="x" * (70 * 1024), call_id="tc-big")
        emit.on_tool_called(SESSION, 1, name="y" * (70 * 1024), call_id="tc-big2")
    assert len(_entries()) == before, "a refused write must not land"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "the failure is warned once, not per call"


def test_a_refused_write_does_not_break_the_next_good_write():
    _open_session()
    emit.on_tool_called(SESSION, 1, name="z" * (70 * 1024), call_id="tc-big")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    assert _body()[-1]["type"] == "turn/completed"


def test_a_storage_layer_that_raises_never_breaks_a_caller(monkeypatch):
    _open_session()

    class Exploding:
        def append(self, *args, **kwargs):
            raise RuntimeError("disk is on fire")

    monkeypatch.setitem(emit._open, SESSION, Exploding())
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_tool_completed(SESSION, 1, name="fs_read", status="completed", call_id="tc-1")
    emit.on_approval_requested(SESSION, 1, approval_id="ap-1")
    emit.on_approval_decided(SESSION, 1, approval_id="ap-1", decision="approved")
    emit.on_model_selected(SESSION, "m", "config")
    emit.on_compaction_applied(SESSION, pct_before=0.9, pct_after=0.2)
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")


def test_a_failed_turn_start_leaves_later_entries_unthreaded(monkeypatch):
    """An anchor is only claimed from an entry that actually landed."""
    _open_session()
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    assert "thread" not in _body()[-1]


def test_events_for_a_session_with_no_ledger_create_nothing():
    emit.on_turn_started("never-opened", 1, "user")
    emit.on_tool_called("never-opened", 1, name="fs_read", call_id="tc-1")
    assert not _ledger_path("never-opened").exists()


def test_a_missing_session_id_is_a_no_op():
    emit.on_session_opened("", agent="kirocrew")
    emit.on_turn_started("", 1, "user")
    emit.on_session_closed("", "reset")
    assert not _store_root().exists()


# --- the flag ------------------------------------------------------------


def test_flag_off_writes_no_file_at_all(monkeypatch):
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "0")
    assert emit.enabled() is False
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_session_closed(SESSION, "reset")
    assert not _store_root().exists()


def test_flag_unset_writes_no_file_at_all(monkeypatch):
    monkeypatch.delenv(emit.SESSION_LEDGER_ENV, raising=False)
    assert emit.enabled() is False
    _open_session()
    assert not _store_root().exists()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_the_flag_accepts_the_repo_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, value)
    assert emit.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "2"])
def test_the_flag_rejects_everything_else(monkeypatch, value):
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, value)
    assert emit.enabled() is False


def test_the_flag_is_read_per_call_not_at_import(monkeypatch):
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "0")
    _open_session()
    assert not _store_root().exists()
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    _open_session()
    assert _ledger_path().is_file()
