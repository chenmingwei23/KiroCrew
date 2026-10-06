"""Watchdog recycle must not drop held deferred card notes.

The RSS watchdog recycles a session by resetting its runtime and then firing
the dashboard's recycle callback (``_on_recycled`` in
``wire_session_recycle_callback``). That callback appends a banner that tells
the user "Conversation history is preserved". Deferred card notes are
slot-side in-memory buffers (``_ChatSlot._deferred_notes``) that the reset
leaves in place, so they are still held when the callback runs.

``_on_recycled`` flushes the held notes BEFORE appending the banner,
delivering them as real transcript rows the normal save persists, so the
banner's claim holds. These tests pin that behaviour: the held notes are
delivered ahead of the banner, an empty hold still banners, and a flush that
raises neither suppresses the banner nor drops the notes from the durable
hold.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state


def _hold(slot, *texts: str) -> None:
    """Put notes in the held queue the way the /note endpoint does."""
    for text in texts:
        slot._deferred_notes.append(
            {
                "content": text,
                "cls": "reconcile-note",
                "session": None,
                "context": None,
            }
        )


def _recycle_callback(state):
    """Register and return the dashboard's ``_on_recycled`` callback."""
    captured = {}
    real = state.sessions.set_recycle_callback

    def _capture(cb):
        captured["cb"] = cb
        return real(cb)

    state.sessions.set_recycle_callback = _capture  # type: ignore[assignment]
    try:
        state.wire_session_recycle_callback()
    finally:
        state.sessions.set_recycle_callback = real  # type: ignore[assignment]
    return captured["cb"]


@pytest.mark.asyncio
class TestRecycleFlushesDeferredNotes:
    async def test_held_notes_delivered_before_recycle_banner(self, tmp_path, monkeypatch):
        """A note held when the recycle fires lands in the transcript, and the
        'history preserved' banner follows it rather than replacing it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._titled = True
        _hold(slot, "deferred card note")

        on_recycled = _recycle_callback(state)
        await on_recycled("dashboard:chat-1", reason="memory limit (1600MB)")

        # The note was delivered as a real inject row, not dropped.
        injected = [m for m in slot.messages if m.get("role") == "inject"]
        assert [m["content"] for m in injected] == ["deferred card note"]
        assert slot._deferred_notes == []

        # The banner is present AND comes after the delivered note, so the
        # "history is preserved" claim is true for that note.
        banner_idx = next(
            i
            for i, m in enumerate(slot.messages)
            if m.get("content", "").startswith("\u267b\ufe0f ")
        )
        note_idx = next(
            i for i, m in enumerate(slot.messages) if m.get("content") == "deferred card note"
        )
        assert note_idx < banner_idx

        # The delivered rows mark the slot dirty so the normal save persists
        # them across the recycle.
        assert slot._dirty is True

    async def test_recycle_with_no_held_notes_still_banners(self, tmp_path, monkeypatch):
        """The flush is a no-op when nothing is held; the banner is unaffected."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._titled = True

        on_recycled = _recycle_callback(state)
        await on_recycled("dashboard:chat-1", reason="memory limit (1600MB)")

        assert not [m for m in slot.messages if m.get("role") == "inject"]
        assert any(m.get("content", "").startswith("\u267b\ufe0f ") for m in slot.messages)

    async def test_flush_failure_does_not_block_banner(self, tmp_path, monkeypatch):
        """If the flush raises, the held notes stay in the durable hold for the
        next restart and the recycle banner is still delivered -- the recycle
        must not be suppressed by a flush failure."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._titled = True
        _hold(slot, "deferred card note")

        def _boom(self) -> int:
            raise RuntimeError("flush blew up")

        monkeypatch.setattr(type(slot), "flush_deferred_notes", _boom)

        on_recycled = _recycle_callback(state)
        await on_recycled("dashboard:chat-1", reason="memory limit (1600MB)")

        # Banner still delivered despite the flush failure.
        assert any(m.get("content", "").startswith("\u267b\ufe0f ") for m in slot.messages)
        # The held note is untouched (still in the hold for the next restart).
        assert [n["content"] for n in slot._deferred_notes] == ["deferred card note"]
