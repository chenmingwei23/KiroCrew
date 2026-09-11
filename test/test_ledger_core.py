"""Append-only ledger core -- one test per rule the format promises.

Grouped the way the module is reasoned about: identity and lifecycle, the seq
contract, the two namespace rules, the caps, the read shapes (fold, page,
thread, ref) and finally damage tolerance. Every ``LedgerError`` code has a test
that pins it, because the code strings are API surface a caller branches on, not
log text.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from kiro_crew import ledger as lg
from kiro_crew.ledger import Ledger, LedgerError, Ref
from kiro_crew.session_ledger import _store_name

CREW = "qa"
SESSION = "s-7f3a"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _crew(unit_id: str = CREW, **fields) -> Ledger:
    fields.setdefault("name", "QA Crew")
    return Ledger.create(lg.KIND_CREW, unit_id, **fields)


def _session(unit_id: str = SESSION, **fields) -> Ledger:
    fields.setdefault("owner", CREW)
    fields.setdefault("agent", "kirocrew")
    return Ledger.create(lg.KIND_SESSION, unit_id, **fields)


def _raises(code: str):
    return pytest.raises(LedgerError)


def _code(excinfo) -> str:
    return excinfo.value.code


# --- layout and lifecycle -------------------------------------------------


def test_each_kind_lands_in_its_own_root_one_directory_deep():
    crew, session = _crew(), _session()
    assert crew.path == lg.ledger_root("crew") / _store_name(CREW) / "ledger.jsonl"
    assert session.path == lg.ledger_root("session") / _store_name(SESSION) / "ledger.jsonl"
    assert crew.path.parent.parent == lg.ledger_root("crew")


def test_a_colon_bearing_channel_session_id_is_storable():
    # The id is sanctioned by the schema and legal on POSIX, illegal as a
    # Windows directory name. The fold is what keeps it storable everywhere.
    sid = "slack:1712793600.123"
    led = _session(sid, task="watch the thread")
    assert ":" not in led.path.parent.name
    reopened = Ledger.open(lg.KIND_SESSION, sid)
    assert reopened.header.id == sid
    assert reopened.header.task == "watch the thread"


def test_ids_differing_only_in_case_never_share_a_ledger():
    # Identity is the digest over the exact id, so a case-insensitive filesystem
    # cannot fold two crews into one file.
    lower, upper = _crew("qa"), _crew("QA")
    assert lower.path != upper.path
    lower.append("item/opened", {"which": "lower"}, src="gateway")
    assert upper.last_seq == 0


def test_the_raw_id_is_recoverable_from_the_header_not_the_directory_name():
    led = _crew("qa-team")
    assert led.path.parent.name != "qa-team"
    assert Ledger.open(lg.KIND_CREW, "qa-team").header.id == "qa-team"


def test_a_session_ledger_is_invisible_to_a_flat_transcript_glob():
    # The session root is shared with the existing flat ``<key>.jsonl``
    # transcripts, whose readers glob one level deep. A ledger nested a
    # directory down must not appear to them, or the two stores shadow.
    session = _session()
    assert session.path.is_file()
    assert list(lg.ledger_root("session").glob("*.jsonl")) == []


def test_exists_is_false_before_create_and_true_after():
    assert Ledger.exists(lg.KIND_CREW, CREW) is False
    _crew()
    assert Ledger.exists(lg.KIND_CREW, CREW) is True


def test_create_refuses_an_existing_ledger():
    _crew()
    with _raises(lg.CODE_ALREADY_EXISTS) as exc:
        _crew()
    assert _code(exc) == lg.CODE_ALREADY_EXISTS


def test_open_refuses_a_missing_ledger():
    with _raises(lg.CODE_NO_LEDGER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_an_unknown_kind_is_refused_rather_than_rooted_somewhere():
    with _raises(lg.CODE_BAD_KIND) as exc:
        Ledger.create("swarm", CREW, name="x")
    assert _code(exc) == lg.CODE_BAD_KIND


@pytest.mark.parametrize("hostile", ["", "../escape", "a/b", "a\\b", "with\0nul", ".."])
def test_a_path_hostile_id_is_refused_loudly_not_folded(hostile):
    with _raises(lg.CODE_INVALID_ID) as exc:
        _crew(hostile)
    assert _code(exc) == lg.CODE_INVALID_ID


# --- headers --------------------------------------------------------------


def test_crew_header_round_trips_through_the_file():
    created = _crew(name="QA Crew", template="reviewer").header
    reopened = Ledger.open(lg.KIND_CREW, CREW).header
    assert reopened == created
    assert reopened.to_dict() == {
        "type": "crew",
        "version": 1,
        "id": CREW,
        "name": "QA Crew",
        "template": "reviewer",
        "createdAt": created.created_at,
    }


def test_session_header_round_trips_including_its_crew_thread_anchor():
    created = _session(
        task="port the gate",
        pack="review",
        slot="chat-7",
        thread={"crew": CREW, "seq": 120},
        cwd="/w/repo",
        remote={"host": "pod-3"},
    ).header
    reopened = Ledger.open(lg.KIND_SESSION, SESSION).header
    assert reopened == created
    assert reopened.to_dict() == {
        "type": "session",
        "version": 1,
        "id": SESSION,
        "owner": CREW,
        "task": "port the gate",
        "pack": "review",
        "agent": "kirocrew",
        "slot": "chat-7",
        "thread": {"crew": CREW, "seq": 120},
        "cwd": "/w/repo",
        "remote": {"host": "pod-3"},
        "createdAt": created.created_at,
    }


def test_the_header_is_line_one_and_optional_fields_are_present_as_null():
    _crew()
    first = lg.ledger_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["template"] is None


def test_a_missing_required_header_field_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        Ledger.create(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD
    assert exc.value.field == "name"


def test_an_unknown_header_field_is_refused_rather_than_dropped():
    # Silently dropping it would tell the caller the header stored what they
    # asked for while the value vanished.
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _crew(nickname="qa")
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD
    assert exc.value.field == "nickname"


def test_a_header_field_of_the_wrong_type_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _crew(name=7)
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_a_session_thread_anchor_of_the_wrong_shape_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _session(thread={"crew": CREW})
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_a_session_remote_that_is_not_an_object_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _session(remote="pod-3")
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_opening_a_ledger_whose_header_names_another_unit_is_refused():
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["id"] = "other"
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_a_ledger_whose_header_is_not_json_is_refused():
    _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_text("not json\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_an_empty_file_is_refused_as_a_missing_header():
    _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_appending_to_a_ledger_whose_file_was_emptied_is_refused():
    crew = _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        crew.append("item/opened", {}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_HEADER


# --- seq ------------------------------------------------------------------


def test_seq_starts_at_one_after_the_header_and_time_is_epoch_ms():
    crew = _crew()
    first = crew.append("item/opened", {"item": "pr-1"}, src="gateway")
    assert (first.seq, crew.last_seq) == (1, 1)
    assert first.time > 1_600_000_000_000


def test_seq_stays_contiguous_across_a_reopen():
    crew = _crew()
    for index in range(3):
        crew.append("item/opened", {"i": index}, src="gateway")
    reopened = Ledger.open(lg.KIND_CREW, CREW)
    assert reopened.last_seq == 3
    assert reopened.append("item/opened", {"i": 3}, src="gateway").seq == 4
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2, 3, 4]


def test_two_concurrent_writers_never_claim_the_same_seq():
    # The point of reading seq back from the file INSIDE the lock rather than
    # trusting the in-process cache.
    _crew()
    writers = [Ledger.open(lg.KIND_CREW, CREW) for _ in range(4)]
    barrier = threading.Barrier(len(writers))

    def run(handle: Ledger) -> None:
        barrier.wait()
        for index in range(5):
            handle.append("activity/tick", {"i": index}, src="gateway")

    threads = [threading.Thread(target=run, args=(handle,)) for handle in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()]
    assert seqs == list(range(1, 21))


def test_the_entry_envelope_is_exactly_the_documented_shape():
    crew = _crew()
    anchor = crew.append("item/opened", {"item": "pr-4127"}, src="gateway")
    entry = crew.append(
        "crew:qa/report",
        {"item": "pr-4127", "status": "done"},
        src="crew:qa",
        thread=anchor.seq,
        ref=Ref("session", SESSION, 40, 96),
    )
    assert entry.to_dict() == {
        "type": "crew:qa/report",
        "seq": 2,
        "time": entry.time,
        "src": "crew:qa",
        "thread": 1,
        "ref": {"unit": "session", "id": SESSION, "from": 40, "to": 96},
        "data": {"item": "pr-4127", "status": "done"},
    }
    stored = lg.ledger_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[2]
    assert json.loads(stored) == entry.to_dict()


def test_the_optional_keys_are_absent_not_null_when_unused():
    crew = _crew()
    entry = crew.append("item/opened", {}, src="gateway")
    stored = json.loads(
        lg.ledger_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[1]
    )
    assert "thread" not in stored and "ref" not in stored
    assert entry.thread is None and entry.ref is None


# --- rule 1: ownership ----------------------------------------------------


@pytest.mark.parametrize(
    "kind,owned",
    [
        (lg.KIND_CREW, "member/joined"),
        (lg.KIND_CREW, "patrol/swept"),
        (lg.KIND_SESSION, "turn/start"),
        (lg.KIND_SESSION, "compaction/ran"),
    ],
)
def test_a_kind_accepts_the_domains_it_owns(kind, owned):
    unit = _crew() if kind == lg.KIND_CREW else _session()
    assert unit.append(owned, {}, src="gateway").type == owned


@pytest.mark.parametrize(
    "kind,foreign",
    [(lg.KIND_CREW, "turn/start"), (lg.KIND_SESSION, "member/joined")],
)
def test_a_kind_refuses_a_domain_the_other_kind_owns(kind, foreign):
    unit = _crew() if kind == lg.KIND_CREW else _session()
    with _raises(lg.CODE_EVENT_TYPE_NOT_OWNED) as exc:
        unit.append(foreign, {}, src="gateway")
    assert _code(exc) == lg.CODE_EVENT_TYPE_NOT_OWNED


def test_the_ownership_registry_is_the_documented_partition():
    assert lg.TYPE_OWNERSHIP[lg.KIND_CREW] == {
        "member",
        "activity",
        "slot",
        "patrol",
        "message",
        "crew",
        "item",
        "memory",
    }
    assert lg.TYPE_OWNERSHIP[lg.KIND_SESSION] == {
        "session",
        "turn",
        "step",
        "tool",
        "approval",
        "model",
        "compaction",
        "remote",
    }


@pytest.mark.parametrize("bad", ["noslash", "/leading", "trailing/", "a/b/c", "-bad/x", "x/-bad"])
def test_a_type_that_is_not_domain_slash_action_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_TYPE) as exc:
        crew.append(bad, {}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_TYPE


@pytest.mark.parametrize("src", ["gateway", "acp", "dashboard", "patrol", "session:slack:1712.5"])
def test_the_fixed_and_session_emitters_are_accepted(src):
    assert _crew().append("activity/tick", {}, src=src).src == src


@pytest.mark.parametrize("bad", ["", "Gate way", "session:", "crew:", "app:bad/name", "unknown"])
def test_an_unrecognized_src_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_SRC) as exc:
        crew.append("activity/tick", {}, src=bad)
    assert _code(exc) == lg.CODE_BAD_SRC


# --- rule 2: guest namespaces ---------------------------------------------


def test_a_guest_writes_under_its_own_prefix_on_a_crew_ledger():
    crew = _crew()
    assert crew.append("crew:qa/report", {}, src="crew:qa").type == "crew:qa/report"
    assert crew.append("app:radar/scan", {}, src="app:radar").type == "app:radar/scan"


def test_a_session_ledger_refuses_a_guest_type_outright():
    session = _session()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        session.append("crew:qa/report", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_a_guest_may_not_write_another_guests_namespace():
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("crew:other/report", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_a_guest_may_not_write_an_owned_domain_either():
    # Its own name is its whole permission; the registry is not also open to it.
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("member/joined", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION
    assert exc.value.field == "src"


def test_a_non_guest_emitter_may_not_borrow_a_guest_namespace():
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("crew:qa/report", {}, src="gateway")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


# --- rule 3: thread, ref, size --------------------------------------------


def test_thread_groups_entries_under_an_earlier_anchor():
    crew = _crew()
    anchor = crew.append("item/opened", {}, src="gateway")
    child = crew.append("item/updated", {}, src="gateway", thread=anchor.seq)
    assert child.thread == anchor.seq


@pytest.mark.parametrize("bad", [0, -1, True, 99])
def test_a_thread_that_names_no_earlier_entry_is_refused(bad):
    crew = _crew()
    crew.append("item/opened", {}, src="gateway")
    with _raises(lg.CODE_BAD_THREAD) as exc:
        crew.append("item/updated", {}, src="gateway", thread=bad)
    assert _code(exc) == lg.CODE_BAD_THREAD


def test_a_thread_may_not_name_the_entry_being_written():
    crew = _crew()
    with _raises(lg.CODE_BAD_THREAD) as exc:
        crew.append("item/opened", {}, src="gateway", thread=1)
    assert _code(exc) == lg.CODE_BAD_THREAD


def test_a_thread_naming_a_seq_no_reader_can_parse_is_refused():
    # In range but unreadable: a group hanging off this anchor would never show
    # the anchor, so the pointer is to nothing.
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # seq 2, terminated, unparseable
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reopened = Ledger.open(lg.KIND_CREW, CREW)

    with _raises(lg.CODE_BAD_THREAD) as exc:
        reopened.append("activity/tick", {}, src="gateway", thread=2)
    assert _code(exc) == lg.CODE_BAD_THREAD
    # The intact anchors on either side still work.
    assert reopened.append("activity/tick", {}, src="gateway", thread=1).thread == 1
    assert reopened.append("activity/tick", {}, src="gateway", thread=3).thread == 3


def test_an_anchor_older_than_the_tail_window_is_still_accepted():
    # The in-window check cannot see it, so this is the bounded fallback.
    crew = _crew()
    anchor = crew.append("item/opened", {"n": "anchor"}, src="gateway")
    for _ in range(3):
        crew.append("item/updated", {"blob": "x" * 40_000}, src="gateway")
    assert lg.ledger_path(lg.KIND_CREW, CREW).stat().st_size > 72 * 1024

    child = crew.append("item/updated", {}, src="gateway", thread=anchor.seq)

    assert child.thread == anchor.seq
    assert crew.thread_page(anchor.seq).entries[-1].seq == anchor.seq


@pytest.mark.parametrize(
    "raw",
    [
        {"unit": "swarm", "id": "x", "from": 1},
        {"unit": "crew", "id": "", "from": 1},
        {"unit": "crew", "id": "a/b", "from": 1},
        {"unit": "crew", "id": "x", "from": 0},
        {"unit": "crew", "id": "x", "from": "1"},
        {"unit": "crew", "id": "x", "from": 5, "to": 4},
        {"unit": "crew", "id": "x", "from": 1, "to": 1 + lg.MAX_REF_SPAN},
        "not-an-object",
    ],
)
def test_a_malformed_ref_is_refused(raw):
    crew = _crew()
    with _raises(lg.CODE_BAD_REF) as exc:
        crew.append("item/opened", {}, src="gateway", ref=raw)
    assert _code(exc) == lg.CODE_BAD_REF


def test_a_ref_without_to_cites_one_line():
    assert Ref("crew", CREW, 7).last_seq == 7
    assert Ref("crew", CREW, 7, 9).last_seq == 9


def test_an_entry_over_the_size_ceiling_is_refused_whole():
    crew = _crew()
    crew.append("item/opened", {"note": "kept"}, src="gateway")
    before = lg.ledger_path(lg.KIND_CREW, CREW).read_bytes()
    with _raises(lg.CODE_ENTRY_TOO_LARGE) as exc:
        crew.append("item/opened", {"blob": "x" * (lg.MAX_ENTRY_BYTES + 1)}, src="gateway")
    assert _code(exc) == lg.CODE_ENTRY_TOO_LARGE
    # Caps refuse; a refused append leaves the file byte-identical and does not
    # burn the seq it would have used.
    assert lg.ledger_path(lg.KIND_CREW, CREW).read_bytes() == before
    assert crew.append("item/opened", {}, src="gateway").seq == 2


def test_an_entry_just_under_the_ceiling_is_accepted():
    crew = _crew()
    room = lg.MAX_ENTRY_BYTES - 200
    assert crew.append("item/opened", {"blob": "x" * room}, src="gateway").seq == 1


@pytest.mark.parametrize("bad", [None, [], "text", 7])
def test_data_that_is_not_a_json_object_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_DATA) as exc:
        crew.append("item/opened", bad, src="gateway")
    assert _code(exc) == lg.CODE_BAD_DATA


def test_data_that_cannot_be_serialized_is_refused():
    crew = _crew()
    with _raises(lg.CODE_BAD_DATA) as exc:
        crew.append("item/opened", {"when": object()}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_DATA


# --- reads: fold, get, page -----------------------------------------------


def test_iter_from_is_oldest_first_and_starts_where_asked():
    crew = _crew()
    for index in range(5):
        crew.append("activity/tick", {"i": index}, src="gateway")
    assert [entry.seq for entry in crew.iter_from()] == [1, 2, 3, 4, 5]
    assert [entry.seq for entry in crew.iter_from(4)] == [4, 5]


def test_get_returns_the_entry_or_none():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    assert crew.get(1).data == {"i": 0}
    assert crew.get(2) is None
    assert crew.get(0) is None


def test_page_walks_the_whole_history_newest_first_with_no_phantom_page():
    crew = _crew()
    for index in range(7):  # odd count, so the last page is a partial one
        crew.append("activity/tick", {"i": index}, src="gateway")
    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        page = crew.page(before=cursor, limit=3)
        seen.extend(entry.seq for entry in page.entries)
        pages += 1
        if page.next_before is None:
            break
        cursor = page.next_before
    assert seen == [7, 6, 5, 4, 3, 2, 1]
    assert pages == 3


def test_a_page_that_lands_exactly_on_the_oldest_entry_ends_the_walk():
    # 6 entries at limit 3 is the boundary a naive cursor turns into a fourth,
    # empty page.
    crew = _crew()
    for index in range(6):
        crew.append("activity/tick", {"i": index}, src="gateway")
    first = crew.page(limit=3)
    second = crew.page(before=first.next_before, limit=3)
    assert [entry.seq for entry in second.entries] == [3, 2, 1]
    assert second.next_before is None


def test_page_limit_is_clamped_to_the_read_ceiling():
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    assert len(crew.page(limit=10**6).entries) == 1
    assert len(crew.page(limit=0).entries) == 1


def test_page_of_an_empty_ledger_is_empty_with_no_cursor():
    page = _crew().page()
    assert page.entries == () and page.next_before is None


def test_thread_page_returns_the_group_newest_first_with_the_anchor_last():
    crew = _crew()
    anchor = crew.append("item/opened", {"n": "anchor"}, src="gateway")
    crew.append("activity/tick", {"n": "unrelated"}, src="gateway")
    members = [
        crew.append("item/updated", {"n": index}, src="gateway", thread=anchor.seq)
        for index in range(3)
    ]
    page = crew.thread_page(anchor.seq)
    assert [entry.seq for entry in page.entries] == [
        members[2].seq,
        members[1].seq,
        members[0].seq,
        anchor.seq,
    ]
    assert page.next_before is None


def test_thread_page_pages_and_reaches_the_anchor_on_the_last_page():
    crew = _crew()
    anchor = crew.append("item/opened", {}, src="gateway")
    for index in range(4):
        crew.append("item/updated", {"i": index}, src="gateway", thread=anchor.seq)
    first = crew.thread_page(anchor.seq, limit=2)
    assert [entry.seq for entry in first.entries] == [5, 4]
    second = crew.thread_page(anchor.seq, before=first.next_before, limit=2)
    assert [entry.seq for entry in second.entries] == [3, 2]
    third = crew.thread_page(anchor.seq, before=second.next_before, limit=2)
    assert [entry.seq for entry in third.entries] == [anchor.seq]
    assert third.next_before is None


def test_thread_page_of_an_unknown_anchor_is_empty_rather_than_a_refusal():
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    assert crew.thread_page(99).entries == ()


# --- reads: resolve -------------------------------------------------------


def _allow(unit: str, uid: str) -> bool:
    return True


def test_resolve_returns_the_cited_segment_of_another_ledger():
    session = _session()
    for index in range(5):
        session.append("turn/start", {"i": index}, src="acp")
    crew = _crew()
    resolution = crew.resolve(Ref(lg.KIND_SESSION, SESSION, 2, 4), may_read=_allow)
    assert resolution.status == lg.STATUS_OK and resolution.ok
    assert [entry.seq for entry in resolution.entries] == [2, 3, 4]


def test_a_cross_ledger_ref_without_an_access_check_is_refused_not_allowed():
    # Omitting the callback is the shortest call shape there is, so it must not
    # be the one that hands over another unit's entries.
    session = _session()
    session.append("turn/start", {"private": True}, src="acp")
    resolution = _crew().resolve(Ref(lg.KIND_SESSION, SESSION, 1))
    assert resolution.status == lg.STATUS_FORBIDDEN
    assert resolution.entries == ()


def test_resolve_without_to_returns_the_single_cited_line():
    session = _session()
    session.append("turn/start", {"i": 0}, src="acp")
    session.append("turn/end", {"i": 1}, src="acp")
    resolution = _crew().resolve({"unit": "session", "id": SESSION, "from": 2}, may_read=_allow)
    assert [entry.type for entry in resolution.entries] == ["turn/end"]


def test_resolve_follows_a_ref_into_the_citing_ledger_itself_with_no_callback():
    # No boundary is crossed: the caller already holds this ledger open.
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    resolution = crew.resolve(Ref(lg.KIND_CREW, CREW, 1))
    assert resolution.ok and [entry.seq for entry in resolution.entries] == [1]


def test_resolve_is_gone_when_the_cited_ledger_does_not_exist():
    resolution = _crew().resolve(Ref(lg.KIND_SESSION, "s-missing", 1, 3), may_read=_allow)
    assert resolution.status == lg.STATUS_GONE
    assert resolution.entries == () and not resolution.ok


def test_resolve_is_forbidden_and_says_nothing_about_existence():
    session = _session()
    session.append("turn/start", {}, src="acp")
    crew = _crew()
    denied = crew.resolve(Ref(lg.KIND_SESSION, SESSION, 1), may_read=lambda unit, uid: False)
    absent = crew.resolve(Ref(lg.KIND_SESSION, "s-missing", 1), may_read=lambda unit, uid: False)
    # Both answers are identical, so a caller that may not read a unit cannot
    # use the status to learn whether it is there.
    assert denied == absent
    assert denied.status == lg.STATUS_FORBIDDEN and denied.entries == ()


def test_resolve_passes_the_unit_and_id_to_the_access_check():
    seen: list[tuple[str, str]] = []
    _crew().resolve(
        Ref(lg.KIND_SESSION, SESSION, 1),
        may_read=lambda unit, uid: seen.append((unit, uid)) or True,
    )
    assert seen == [(lg.KIND_SESSION, SESSION)]


# --- damage tolerance -----------------------------------------------------


def test_a_torn_last_line_is_truncated_on_open():
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    path.write_bytes(intact + b'{"type":"item/opened","seq":2,"tim')

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert path.read_bytes() == intact  # the crash artifact is gone, history is not
    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {}, src="gateway").seq == 2


def test_a_complete_last_line_missing_only_its_newline_is_kept():
    # Only the separator was lost, so the record is real; the next append
    # re-supplies the newline instead of rewriting the line.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1, 2]


def test_a_malformed_interior_line_is_skipped_on_read_and_left_on_disk():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # terminated, so NOT torn
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert [entry.seq for entry in reopened.iter_from()] == [1, 3]
    assert reopened.last_seq == 3
    assert '{"type":"activity/tick","seq":' in path.read_text(encoding="utf-8")


def test_a_blank_interior_line_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_an_interior_line_whose_envelope_is_wrong_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps({"type": "activity/tick", "seq": 2, "time": 1, "src": "gateway"})
        + "\n"
        + json.dumps({"type": "activity/tick", "seq": 3, "time": 1, "src": "g", "data": 4})
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_seq_recovers_from_the_newest_valid_line_when_the_tail_is_damaged():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = "{damaged"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The newest PARSEABLE seq is 2, so the next entry is 3 -- the damaged
    # line's number is not reused, because a reader cannot know it was 3.
    assert Ledger.open(lg.KIND_CREW, CREW).append("activity/tick", {}, src="gateway").seq == 3


def test_a_torn_tail_is_repaired_by_an_append_too_not_only_by_a_reopen():
    # A long-lived writer holds its handle open across a crash somewhere else,
    # so the repair cannot live only in ``open``.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes() + b'{"type":"item/opened","seq":2,"tim')

    assert crew.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in crew.iter_from()] == [1, 2]


def test_an_interior_line_carrying_a_malformed_ref_is_skipped():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "item/opened",
                "seq": 2,
                "time": 1,
                "src": "gateway",
                "ref": {"unit": "swarm", "id": "x", "from": 1},
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_a_stored_session_thread_anchor_that_is_damaged_reads_as_absent():
    _session()
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header["thread"] = {"crew": CREW}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    assert Ledger.open(lg.KIND_SESSION, SESSION).header.thread is None


@pytest.mark.parametrize("key,bad", [("createdAt", "yesterday"), ("version", "1"), ("name", 7)])
def test_a_header_field_of_the_wrong_stored_type_refuses_the_open(key, bad):
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header[key] = bad
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_session_header_missing_its_owner_refuses_the_open():
    _session()
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    del header["owner"]
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_SESSION, SESSION)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_ledger_of_one_kind_cannot_be_opened_as_the_other():
    # The two roots are separate, so this needs the file moved -- which is
    # exactly what a mistaken restore or a hand-edit does.
    _crew()
    target = lg.ledger_dir(lg.KIND_SESSION, CREW)
    target.mkdir(parents=True, exist_ok=True)
    (target / lg.LEDGER_FILE).write_bytes(lg.ledger_path(lg.KIND_CREW, CREW).read_bytes())
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_SESSION, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


# --- large files: the bounded tail read -----------------------------------


def test_seq_comes_off_a_bounded_window_on_a_file_past_that_window():
    # The window can begin mid-line, so its first fragment is not a record. If
    # that fragment were trusted the seq would come from a partial line.
    crew = _crew()
    for _ in range(3):  # three ~40 KiB lines clear the ~72 KiB window
        crew.append("item/opened", {"blob": "x" * 40_000}, src="gateway")
    assert crew.append("item/opened", {}, src="gateway").seq == 4
    assert Ledger.open(lg.KIND_CREW, CREW).last_seq == 4


def test_seq_falls_back_to_a_full_scan_when_the_window_holds_nothing_valid():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    # One terminated garbage line wider than the tail window, so the window
    # sees only garbage and the real seq is behind it.
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{" + "z" * 80_000 + "\n")

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"i": 1}, src="gateway").seq == 2
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2]


# --- containment ----------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_id_that_symlinks_out_of_its_root_is_refused():
    # The shape gate cannot see this one: the id has no separator, the ESCAPE is
    # in the filesystem. Containment is re-checked on the resolved path.
    _crew()
    root = lg.ledger_root(lg.KIND_CREW)
    outside = root.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (root / _store_name("escape")).symlink_to(outside, target_is_directory=True)
    with _raises(lg.CODE_INVALID_ID) as exc:
        _crew("escape")
    assert _code(exc) == lg.CODE_INVALID_ID
