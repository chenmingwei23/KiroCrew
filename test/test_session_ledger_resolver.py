"""The slot-key -> ledger-unit resolver, and the emitters it unlocks.

Kept separate from ``test_session_ledger_emit.py`` because these tests are about
a different question. That file asks whether an entry the runner hands over lands
correctly; this one asks whether a site that holds only a KEY can find the unit to
hand it to at all, and whether the eight families keyed that way say only what
their site observed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import session_ledger_emit as emit
from kiro_crew import session_ledger_resolve as resolve
from kiro_crew.ledger import ledger_path

SESSION = "acp-sid-1"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "1")
    monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    emit._child_origin.clear()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()
    emit._child_origin.clear()


def _entries(session_id: str = SESSION) -> list[dict]:
    path: Path = ledger_path("session", session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _body(session_id: str = SESSION) -> list[dict]:
    return _entries(session_id)[1:]


def _of(kind: str) -> list[dict]:
    return [e["data"] for e in _body() if e["type"] == kind]


def _open_session() -> None:
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )


# --- doubles ---------------------------------------------------------------


class _Provider:
    """A session provider, which is what ``session_id_of`` reads an id off."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _Slot:
    def __init__(self, key: str, *, client=None, linked: str = "") -> None:
        self.key = key
        self._acp_client = client
        self.linked_session_key = linked


class _Sessions:
    """A registry that RECORDS every method reached, so a test can prove which."""

    def __init__(self, providers: dict[str, _Provider]) -> None:
        self._providers = providers
        self.calls: list[tuple[str, str]] = []

    def get_provider(self, key: str):
        self.calls.append(("get_provider", key))
        return self._providers.get(key)


class _State:
    def __init__(self, slots: dict[str, _Slot], sessions: _Sessions) -> None:
        self._slots = slots
        self.sessions = sessions

    def get_slot(self, name: str):
        return self._slots.get(name)


# --- identity: which unit is this slot's work landing in NOW ---------------


def test_a_running_turn_resolves_through_the_slot_s_own_acp_client():
    """The same object the runner reads its own session id from.

    This is what keeps an entry produced by a background-facing site inside a turn
    from landing in a different unit than that turn's own entries.
    """
    sessions = _Sessions({})
    slot = _Slot("chat-7", client=_Provider("live-sid"))
    state = _State({"chat-7": slot}, sessions)
    assert resolve.unit_for_slot(state, "chat-7") == "live-sid"
    # The registry was never asked: the live client already answered.
    assert sessions.calls == []


def test_an_idle_slot_resolves_through_the_registry_instead():
    """A title pass runs BETWEEN turns, where ``_acp_client`` is None.

    Without this level the whole background family would be unresolvable exactly
    when it fires, which is what made it schema-only.
    """
    sessions = _Sessions({"dashboard:chat-7": _Provider("registry-sid")})
    state = _State({"chat-7": _Slot("chat-7", client=None)}, sessions)
    assert resolve.unit_for_slot(state, "chat-7") == "registry-sid"


def test_a_channel_born_slot_resolves_through_the_channel_s_own_session():
    """Its turns run on the channel's session, so folding the slot key misses.

    ``effective_session_key`` is the repo's own answer to "which session does this
    slot's work run on", and using anything else files a linked slot's background
    spend under a session that does not exist.
    """
    sessions = _Sessions({"slack:1712.44": _Provider("slack-sid")})
    slot = _Slot("chat-9", client=None, linked="slack:1712.44")
    state = _State({"chat-9": slot}, sessions)
    assert resolve.unit_for_slot(state, "chat-9") == "slack-sid"


def test_a_slot_that_never_ran_a_turn_resolves_to_unknown():
    """Unknown is an answer, and the emitter's no-op turns it into "do not write".

    A ledger that omits a fact is behind. One that files a fact under the wrong
    session is wrong, and no reader can tell.
    """
    sessions = _Sessions({})
    state = _State({"chat-7": _Slot("chat-7", client=None)}, sessions)
    assert resolve.unit_for_slot(state, "chat-7") == resolve.UNKNOWN


def test_resolution_only_ever_reads_the_live_registry():
    """No persisted map, and therefore no repair-on-read.

    ``SessionMap.get`` prunes an entry it finds stale, so consulting it would make
    describing a session mutate it. The double records every method reached, so a
    future edit that reaches for another one fails here.
    """
    sessions = _Sessions({"dashboard:chat-7": _Provider("sid")})
    state = _State({"chat-7": _Slot("chat-7", client=None)}, sessions)
    resolve.unit_for_slot(state, "chat-7")
    assert [name for name, _ in sessions.calls] == ["get_provider"]


def test_a_bare_slot_name_is_retried_in_dashboard_form_but_a_namespaced_key_is_not():
    """The retry has a premise: a key with no colon cannot already be namespaced.

    Rewriting one that IS namespaced is how ``slack:<ts>`` becomes the nonexistent
    ``dashboard:slack:<ts>`` -- the exact mistake ``_history_key_for`` documents.
    """
    sessions = _Sessions({"dashboard:chat-7": _Provider("sid")})
    assert resolve.unit_for_session_key(sessions, "chat-7") == "sid"
    assert sessions.calls == [("get_provider", "chat-7"), ("get_provider", "dashboard:chat-7")]

    namespaced = _Sessions({})
    assert resolve.unit_for_session_key(namespaced, "slack:1712.44") == resolve.UNKNOWN
    assert namespaced.calls == [("get_provider", "slack:1712.44")]


def test_a_registry_that_raises_answers_unknown_rather_than_propagating():
    """Describing a session must never be why the work around it fails."""

    class _Angry:
        def get_provider(self, key: str):
            raise RuntimeError("registry is mid-teardown")

    assert resolve.unit_for_session_key(_Angry(), "dashboard:chat-7") == resolve.UNKNOWN


# --- approvals -------------------------------------------------------------


def test_an_approval_is_requested_before_it_is_decided():
    """A closer never precedes its opener, even though two tasks write them."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="rm -rf build")
    emit.on_approval_decided(SESSION, 1, approval_id="r1", decision="approved")
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("approval/requested") < kinds.index("approval/decided")


def test_a_host_decline_names_the_host_and_its_cause_and_a_human_one_names_neither():
    """Only the host's own auto-declines are attributable at this site.

    A decision that came back through the approval future was made by a person at
    the dashboard or in Slack, and the runner cannot see which -- so it says
    nothing rather than asserting ``user``.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_decided(
        SESSION, 1, approval_id="r1", decision="rejected", by="host", cause="approval_timeout"
    )
    emit.on_approval_decided(SESSION, 1, approval_id="r2", decision="approved")
    assert emit.flush()
    host, human = _of("approval/decided")
    assert host["by"] == "host" and host["cause"] == "approval_timeout"
    assert host["decision"] == "rejected", "the cause must not displace the decision"
    assert "by" not in human and "cause" not in human


def test_an_approval_whose_tool_the_frame_did_not_name_omits_the_field():
    """A permission frame can arrive with no resolvable tool name.

    Writing `""` would record "the tool is the empty string", a value no reader can
    tell from a real one -- in a log whose whole worth is that it says only what was
    observed.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="", reason="")
    emit.on_approval_requested(SESSION, 1, approval_id="r2", tool="shell", reason="ls")
    assert emit.flush()
    unnamed, named = _of("approval/requested")
    assert "tool" not in unnamed and "reason" not in unnamed
    assert unnamed["approval_id"] == "r1", "the request is still recorded"
    assert named["tool"] == "shell"


def test_a_long_approval_reason_is_clipped_rather_than_losing_the_entry():
    """An entry refused for one oversize field is a fact silently missing."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="x" * 5000)
    assert emit.flush()
    (data,) = _of("approval/requested")
    assert len(data["reason"]) == emit._MAX_SHORT_TEXT
    assert data["reason"].endswith("\u2026"), "a clipped value must say it was cut"


def test_an_approval_reason_is_redacted_at_the_emitter_not_trusted_from_the_site():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_approval_requested(
        SESSION,
        1,
        approval_id="r1",
        tool="shell",
        reason="curl -H 'Authorization: Bearer sk-live-ABCDEF1234567890abcdef'",
    )
    assert emit.flush()
    (data,) = _of("approval/requested")
    assert "sk-live-ABCDEF1234567890abcdef" not in data["reason"]


# --- plan ------------------------------------------------------------------


def test_a_plan_records_only_the_two_states_the_stream_carries():
    """The backend's todo model is a boolean, so a third state would be invented."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[
            {"id": "a", "text": "read the code", "completed": True},
            {"id": "b", "text": "write the test", "completed": False},
        ],
    )
    assert emit.flush()
    (data,) = _of("plan/updated")
    assert [row["state"] for row in data["items"]] == ["done", "open"]
    assert {row["state"] for row in data["items"]} <= {"done", "open"}


def test_no_task_list_writes_nothing_but_an_empty_one_records_a_cleared_plan():
    """Absent data and a plan of zero tasks are different facts."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=None)
    assert emit.flush()
    assert _of("plan/updated") == []
    emit.on_plan_updated(SESSION, 1, items=[])
    assert emit.flush()
    assert _of("plan/updated") == [{"turn": 1, "items": []}]


def test_the_two_unconditionally_called_emitters_do_no_work_with_the_flag_off(monkeypatch):
    """The runner calls these two on every plan change and every approval prompt.

    Both do real work before `_write` gets its own chance to no-op -- a redaction
    per task and a serialize probe per admitted row -- so relying on that guard
    alone spends event-loop time on a feature that is off by default. The subagent
    and background emitters need no guard of their own: their callers already check.

    Asserted by making the work itself fail if it runs, which is what keeps this
    from passing for the wrong reason once the bodies change.
    """
    monkeypatch.setenv(emit.SESSION_LEDGER_ENV, "0")
    assert not emit.enabled()

    def _boom(*_a, **_kw):
        raise AssertionError("did work with the flag off")

    monkeypatch.setattr(emit, "_safe_text", _boom)
    monkeypatch.setattr(emit, "_entry_line_fits", _boom)
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    emit.on_approval_requested(SESSION, 1, approval_id="r1", tool="shell", reason="ls")


def test_a_plan_of_emoji_is_bounded_by_bytes_and_still_lands():
    """Character bounds do not bound bytes, and a refused entry is a lost fact.

    ``_clip`` bounds each text in characters while the store serializes with
    ``ensure_ascii`` -- six bytes for a BMP character, twelve for a surrogate pair.
    A dozen separately-legal rows of emoji therefore serialize past the 64 KiB entry
    ceiling, where the append is REFUSED and the whole update disappears. Eleven
    rows of 500 emoji is enough to cross it, so this is the shape a real todo list
    reaches, not a synthetic extreme.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": "\U0001f600" * 500, "completed": False} for n in range(40)],
    )
    assert emit.flush()
    written = _of("plan/updated")
    assert written, "the entry must land rather than be refused whole"
    (data,) = written
    assert data["items"], "and it must carry some of the plan"
    assert data["total"] == 40, "while saying how many tasks there really were"
    assert len(data["items"]) < 40, "having dropped the rows that would not fit"


def test_one_oversize_plan_row_does_not_suppress_the_whole_entry():
    """The first row is admitted unmeasured so an entry always carries something.

    A single task whose clipped text alone approaches the ceiling would otherwise
    measure as not fitting, and an empty ``items`` would report a cleared plan --
    the one reading that is actively wrong.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION, 1, items=[{"id": "a", "text": "\U0001f600" * 500, "completed": True}]
    )
    assert emit.flush()
    (data,) = _of("plan/updated")
    assert len(data["items"]) == 1
    assert data["items"][0]["state"] == "done"


def test_a_runaway_plan_is_clipped_and_still_reports_its_real_size():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": f"t{n}", "completed": False} for n in range(500)],
    )
    assert emit.flush()
    (data,) = _of("plan/updated")
    assert len(data["items"]) == emit._MAX_PLAN_ITEMS
    assert data["total"] == 500


def test_a_plan_entry_is_ignorable_because_nothing_later_depends_on_reading_it():
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_plan_updated(SESSION, 1, items=[{"id": "a", "text": "t", "completed": False}])
    assert emit.flush()
    (entry,) = [e for e in _body() if e["type"] == "plan/updated"]
    assert entry.get("ignorable") is True


# --- background ------------------------------------------------------------


class _Usage:
    def __init__(self, **fields) -> None:
        self.input_tokens = fields.get("input_tokens", 0)
        self.output_tokens = fields.get("output_tokens", 0)
        self.cache_read_tokens = fields.get("cache_read_tokens", 0)
        self.cache_creation_tokens = fields.get("cache_creation_tokens", 0)
        self.credits = fields.get("credits", 0.0)


def test_a_background_call_records_the_dimensions_that_were_billed_and_omits_the_rest():
    """A provider fills the dimensions it bills in; a zero is not a measurement."""
    _open_session()
    emit.on_background_completed(
        SESSION,
        kind="title",
        model="claude-haiku",
        provider="acp",
        credits=0.0,
        input_tokens=900,
        output_tokens=12,
        duration_ms=430,
    )
    assert emit.flush()
    (data,) = _of("background/completed")
    assert data["tokens"] == {"input": 900, "output": 12}
    assert "credits" not in data, "an unbilled dimension must be absent, not zero"
    assert data["ms"] == 430


def test_a_background_call_names_no_turn():
    """It runs after a turn ends, so naming the last one charges the wrong work."""
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_completed(SESSION, 1, stop_reason="end_turn")
    emit.on_background_completed(SESSION, kind="summary", model="m", credits=0.4)
    assert emit.flush()
    (data,) = _of("background/completed")
    assert "turn" not in data
    assert data["credits"] == pytest.approx(0.4)


def test_a_background_call_with_no_owner_or_no_kind_resolves_no_owner():
    """Most background work is charged to nobody, and must stay that way.

    Titling is charged to the session it titles; a tip or a cron label is shared
    infrastructure, and picking a session for it would put someone else's spend in
    a user's log.
    """
    from kiro_crew.llm_helpers import _background_ledger_owner

    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    assert _background_ledger_owner(sessions, "", "title") == ""
    assert _background_ledger_owner(sessions, "dashboard:chat-7", "") == ""
    assert sessions.calls == [], "an unnamed call must not even resolve a session"


def test_a_background_owner_is_resolved_before_the_call_not_after_it():
    """The resolver answers "now", so resolving in the teardown is the wrong now.

    A slot reset, switch or compaction during the model call cold-starts a NEW ACP
    session id. Resolving at teardown would hand this call's spend to the successor
    -- a session that never incurred it -- silently, in an append-only file. So the
    owner is pinned before the call and the teardown writes to that pin.
    """
    from kiro_crew.llm_helpers import _background_ledger_owner, _record_background_ledger

    _open_session()
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_ledger_owner(sessions, "dashboard:chat-7", "title")
    assert owner == SESSION
    # The slot is reset mid-call: the registry now serves a different unit.
    sessions._providers["dashboard:chat-7"] = _Provider("successor-sid")
    _record_background_ledger(
        owner, "title", _Usage(credits=1.0), model="m", provider="acp", elapsed_ms=1
    )
    assert emit.flush()
    assert _entries(SESSION), "the spend stayed with the session that incurred it"
    assert _of("background/completed")[0]["kind"] == "title"
    assert _entries("successor-sid") == [], "and did not follow the successor"


def test_a_named_background_call_lands_in_the_owner_s_ledger():
    from kiro_crew.llm_helpers import _background_ledger_owner, _record_background_ledger

    _open_session()
    sessions = _Sessions({"dashboard:chat-7": _Provider(SESSION)})
    owner = _background_ledger_owner(sessions, "dashboard:chat-7", "memory_consolidation")
    _record_background_ledger(
        owner,
        "memory_consolidation",
        _Usage(credits=2.5, input_tokens=40),
        model="kirocrew-lite",
        provider="acp",
        elapsed_ms=77,
    )
    assert emit.flush()
    (data,) = _of("background/completed")
    assert data["kind"] == "memory_consolidation"
    assert data["credits"] == pytest.approx(2.5)
    assert data["tokens"] == {"input": 40}


# --- children --------------------------------------------------------------


def test_a_child_is_spawned_then_closed_and_the_spawn_carries_its_scope():
    _open_session()
    emit.on_turn_started(SESSION, 4, "user")
    emit.on_subagent_spawned(
        SESSION,
        4,
        agent_id="ab12",
        agent="kirocrew",
        model="claude-opus-5",
        scope={"memory": False, "lessons": True, "project": True},
    )
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=9100)
    assert emit.flush()
    kinds = [e["type"] for e in _body()]
    assert kinds.index("subagent/spawned") < kinds.index("subagent/completed")
    (spawned,) = _of("subagent/spawned")
    assert spawned["turn"] == 4
    assert spawned["scope"] == {"memory": False, "lessons": True, "project": True}


def test_a_spawn_carries_no_ref_because_the_child_has_no_ledger_to_cite():
    """The schema describes one; no subagent path opens a ledger to point at.

    A ``ref`` written now would cite a file that does not exist, which a reader
    cannot distinguish from one that was deleted. Checked in BOTH places a citation
    could appear -- the envelope's own field and the entry's data -- because only
    the envelope form is a real reference and a `ref` key smuggled into data would
    read like one to anything scanning the line.
    """
    _open_session()
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_subagent_spawned(SESSION, 1, agent_id="ab12", agent="kirocrew")
    assert emit.flush()
    (entry,) = [e for e in _body() if e["type"] == "subagent/spawned"]
    assert "ref" not in entry
    assert "ref" not in entry["data"]


def test_a_spawn_with_no_asking_turn_omits_the_field_rather_than_writing_zero():
    """A slash command, a cron and a hook all dispatch children with nothing running.

    Turns are numbered from one, so a literal `0` would name a turn that never
    existed and match no `turn/started`. The child is still recorded: it is a real
    child of that session, and dropping it to keep a field populated would be the
    worse trade.
    """
    _open_session()
    emit.on_subagent_spawned(SESSION, 0, agent_id="ab12", agent="kirocrew")
    assert emit.flush()
    (data,) = _of("subagent/spawned")
    assert "turn" not in data
    assert data["agent_id"] == "ab12", "the child is still recorded"


def test_a_completed_child_reports_no_tokens_and_no_credits():
    """Nothing in the subagent runtime measures either; zeros would be a claim."""
    _open_session()
    emit.on_subagent_completed(SESSION, agent_id="ab12", duration_ms=5)
    assert emit.flush()
    (data,) = _of("subagent/completed")
    assert "tokens" not in data and "credits" not in data


def test_a_stopped_child_is_not_recorded_as_a_completion():
    """The runtime's own outcome separates the three; the log must not merge them.

    A user stop is not a success and not an error. It closes through the
    non-success closer carrying which one it was.
    """
    _open_session()
    emit.on_subagent_failed(SESSION, agent_id="ab12", reason="", outcome="stopped", duration_ms=12)
    emit.on_subagent_failed(SESSION, agent_id="cd34", reason="provider refused", outcome="failed")
    assert emit.flush()
    assert _of("subagent/completed") == []
    stopped, failed = _of("subagent/failed")
    assert stopped["outcome"] == "stopped" and "reason" not in stopped
    assert failed["outcome"] == "failed" and failed["reason"] == "provider refused"


def test_a_child_s_origin_is_pinned_once_so_a_queued_member_keeps_the_asking_turn():
    """A member held behind the stagger gate re-enters the spawn path.

    Re-pinning on the second pass would move the child onto whatever turn the
    parent had reached by then -- a turn ordinal re-derived after the fact, which
    is the one thing this log may not do.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.remember_child_origin("ab12", SESSION, 9)
    assert emit.open_child_origin("ab12") == (SESSION, 4)


def test_a_pinned_origin_is_invisible_until_the_run_actually_starts():
    """Accepted is not started, and only a started run may have an opener.

    A spawn clears the approval gate after it is registered, and a decline returns
    without ever running. Gating every later read on `opened` is what keeps a
    declined spawn from producing a steer or a terminal entry whose cause never
    got written.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.child_origin("ab12") == ("", 0)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.child_origin("ab12") == (SESSION, 4)


def test_opening_an_origin_twice_cannot_produce_two_openers():
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.open_child_origin("ab12") == (SESSION, 4)
    assert emit.open_child_origin("never-pinned") == ("", 0)


def test_a_declined_spawn_releases_its_pin_and_closes_nothing():
    """The exact shape of a spawn refused at the approval gate.

    It was pinned when accepted and never opened, so its terminal report must
    close nothing -- and must still drop the pin rather than leaving it for the
    FIFO to evict much later.
    """
    emit.remember_child_origin("ab12", SESSION, 4)
    assert emit.forget_child_origin("ab12") == ("", 0), "an unopened pin closes nothing"
    assert emit.child_origin("ab12") == ("", 0), "and the pin is gone, not retained"


def test_forgetting_an_opened_origin_returns_it_once_and_then_answers_unknown():
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.open_child_origin("ab12")
    assert emit.forget_child_origin("ab12") == (SESSION, 4)
    assert emit.forget_child_origin("ab12") == ("", 0)


def test_resetting_the_emitter_forgets_pinned_origins():
    """Every other map in the module is cleared on reset; this one is no different."""
    emit.remember_child_origin("ab12", SESSION, 4)
    emit.open_child_origin("ab12")
    emit.reset_caches()
    assert emit.child_origin("ab12") == ("", 0)


def test_an_unrecorded_dispatch_produces_no_closer():
    """An unknown origin yields an empty session id, which the emitter drops.

    That is how a child whose spawn was never recorded -- the flag came on
    mid-flight -- cannot appear in the log as an outcome with no cause.
    """
    _open_session()
    sid, _turn = emit.forget_child_origin("never-seen")
    assert sid == ""
    emit.on_subagent_completed(sid, agent_id="never-seen", duration_ms=5)
    assert emit.flush()
    assert _of("subagent/completed") == []


def test_the_origin_map_is_bounded_by_uptime_not_by_the_number_of_live_turns():
    """Its entries deliberately OUTLIVE the turn that made them, so FIFO it is."""
    for n in range(emit._MAX_CHILD_ORIGINS + 50):
        emit.remember_child_origin(f"a{n}", SESSION, 1)
    assert len(emit._child_origin) == emit._MAX_CHILD_ORIGINS
    assert emit.open_child_origin("a0") == ("", 0), "the oldest is evicted first"
    assert emit.open_child_origin(f"a{emit._MAX_CHILD_ORIGINS + 49}") == (SESSION, 1)
