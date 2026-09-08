"""A queued send keeps its attachment lists through the drain.

A dispatched send persists the client's ``meta.files`` (and ``meta.dirs``) on
its user row, and the renderer resolves each ``[attached_file N] path`` marker
LOSSLESSLY against that list. A send that arrived while the slot was busy went
through the queue instead, and the queue entry carried only the containment
snapshot and the send id -- so the row the drain wrote had no list, the
renderer fell back to a whitespace-bounded capture of the marker text, and a
path with a space (``/tmp/My Report.pdf``) came back as ``/tmp/My``: an
attachment card that opens nothing.

These pins cover the producer (the busy-slot and sub-agent-hold branches stamp
the lists onto the entry), the leg the fix relies on (the drain's meta union
carries them onto the persisted row), the client's leg (the ``queue_pop`` frame
carries them, since no ``chat_message`` echo follows for a user row), the
validation (a malformed list is dropped rather than carried, because the lists
are indexed by marker number), and the merge rule (an attachment-bearing entry
drains alone, because a merged row has one meta for several texts and every
other entry's markers would resolve against the wrong list).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_delivery import attachment_meta

_PATH = "/tmp/My Report.pdf"
_WIRE = f"summarize this\n[attached_file 1] {_PATH}"
_DIR = "/home/u/my designs/"


async def _post_busy(state, slot_key: str, message: str, meta: dict | None):
    body: dict = {"message": message, "slot": slot_key}
    if meta is not None:
        body["meta"] = meta
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json=body)
        assert resp.status == 200
        payload = await resp.json()
        assert payload.get("queued") is True
        return payload


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


def _busy_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("busy-chat")
    slot._in_stage_execution = True  # force the busy queue path
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    return state, slot


class TestAttachmentMeta:
    def test_reduces_to_the_two_ordered_lists(self):
        assert attachment_meta({"files": [_PATH], "dirs": [_DIR], "sendId": "s-1"}) == {
            "files": [_PATH],
            "dirs": [_DIR],
        }

    @pytest.mark.parametrize(
        "meta",
        [
            None,
            {},
            {"files": []},
            {"files": "not-a-list"},
            {"files": [_PATH, 42]},
            {"files": [_PATH, ""]},
            {"dirs": {"a": 1}},
        ],
    )
    def test_drops_anything_but_a_nonempty_list_of_paths(self, meta):
        """The lists are indexed by marker number on the render side, so one bad
        entry would shift every later marker onto the wrong path -- drop the
        whole list rather than carry a corrupt one."""
        assert attachment_meta(meta) == {}

    def test_keeps_the_good_list_when_the_other_is_bad(self):
        assert attachment_meta({"files": [_PATH], "dirs": "nope"}) == {"files": [_PATH]}


class TestBusySlotQueueEntry:
    @pytest.mark.asyncio
    async def test_entry_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)

        await _post_busy(
            state, "busy-chat", _WIRE, {"files": [_PATH], "dirs": [_DIR], "sendId": "s-1"}
        )

        entry = next(i for i in slot._queue if i["content"] == _WIRE)
        assert entry["meta"].get("files") == [_PATH]
        assert entry["meta"].get("dirs") == [_DIR]
        # The id contract is untouched by the addition.
        assert entry["meta"].get("sendId") == "s-1"

    @pytest.mark.asyncio
    async def test_a_send_without_attachments_keeps_the_prior_entry_shape(
        self, tmp_path, monkeypatch
    ):
        """Pinned as ABSENT keys: an empty list on the row would still be a shape
        change for every meta-keyed consumer."""
        state, slot = _busy_state(tmp_path, monkeypatch)

        await _post_busy(state, "busy-chat", "plain text", {"sendId": "s-2"})

        entry = next(i for i in slot._queue if i["content"] == "plain text")
        assert "files" not in entry["meta"]
        assert "dirs" not in entry["meta"]


class TestDrainedRow:
    @pytest.mark.asyncio
    async def test_drained_row_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        """End to end: the list on the entry is worth nothing on its own -- the
        persisted row is what history replay reads back."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH]})

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows, "the drain must have written a user row for the queued send"
        meta = rows[-1].get("meta") or {}
        assert meta.get("files") == [_PATH]
        # Queue plumbing must not ride into the persisted row.
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        assert QUEUED_CONTAINMENT_META_KEY not in meta

    @pytest.mark.asyncio
    async def test_queue_pop_frame_carries_the_attachment_lists(self, tmp_path, monkeypatch):
        """The client rebuilds the drained entry as a user row from the frame
        alone (no chat_message echo follows for a user row), so the lists must
        be ON the frame or the rebuilt row truncates the spaced path until the
        next reload."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", _WIRE, {"files": [_PATH], "dirs": [_DIR]})

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        pops = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_pop"
        ]
        pop = next(p for p in pops if p.get("content") == _WIRE)
        assert pop.get("meta") == {"files": [_PATH], "dirs": [_DIR]}

    @pytest.mark.asyncio
    async def test_queue_pop_frame_without_attachments_has_no_meta_key(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post_busy(state, "busy-chat", "plain text", None)

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        pops = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_pop"
        ]
        pop = next(p for p in pops if p.get("content") == "plain text")
        assert "meta" not in pop


class TestAttachmentEntriesDrainAlone:
    """Merging joins several texts under ONE meta. Each text indexes the lists
    by marker number, so a merged row would resolve every entry's
    ``[attached_file 1]`` against whichever list won -- a card that opens a
    different file. An attachment-bearing entry therefore ends a merge run and,
    at the head of the queue, pops alone."""

    def _slot(self, entries):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._queue = list(entries)
        return slot

    def test_carries_attachments_reads_the_two_lists(self):
        from kiro_crew.dashboard.chat_utils import carries_attachments

        assert carries_attachments({"meta": {"files": [_PATH]}}) is True
        assert carries_attachments({"meta": {"dirs": [_DIR]}}) is True
        assert carries_attachments({"meta": {"sendId": "s-1"}}) is False
        assert carries_attachments({"meta": {"files": []}}) is False
        assert carries_attachments({"meta": "nope"}) is False
        assert carries_attachments({}) is False

    def test_head_entry_with_attachments_pops_alone(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        slot = self._slot(
            [
                {"id": "a", "content": _WIRE, "meta": {"files": [_PATH]}},
                {"id": "b", "content": "and this", "meta": {}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == _WIRE
        assert [c["id"] for c in consumed] == ["a"]
        assert [i["id"] for i in slot._queue] == ["b"]

    def test_merge_run_stops_before_an_attachment_entry(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        other_wire = "read this\n[attached_file 1] /tmp/other.txt"
        slot = self._slot(
            [
                {"id": "a", "content": "first", "meta": {}},
                {"id": "b", "content": "second", "meta": {}},
                {"id": "c", "content": other_wire, "meta": {"files": ["/tmp/other.txt"]}},
                {"id": "d", "content": _WIRE, "meta": {"files": [_PATH]}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == "[2 queued messages merged]\n\nfirst\n\nsecond"
        assert [c["id"] for c in consumed] == ["a", "b"]
        # The two attachment entries stay queued, in order, each to drain alone
        # with its own list.
        assert [i["id"] for i in slot._queue] == ["c", "d"]
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == other_wire
        assert [c["id"] for c in consumed] == ["c"]
        assert consumed[0]["meta"]["files"] == ["/tmp/other.txt"]

    def test_plain_entries_still_merge(self):
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        slot = self._slot(
            [
                {"id": "a", "content": "first", "meta": {"sendId": "s-1"}},
                {"id": "b", "content": "second", "meta": {"sendId": "s-2"}},
            ]
        )
        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert next_msg == "[2 queued messages merged]\n\nfirst\n\nsecond"
        assert len(consumed) == 2
