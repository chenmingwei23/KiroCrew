"""``context.inject`` -- the shadow point that scores a startup injection.

The properties this file pins, each a way the measurement could be WRONG rather
than merely absent, mapped to the issue's "tests that define done":

* pinned candidates are never offered as a question, and an answer can never drop
  them;
* the candidate cap keeps the highest-ranked offered items, and the snippet cap
  holds, with redaction BEFORE truncation so no credential fragment travels;
* the two consent scopes gate independently -- the gate refuses the whole point
  without ``memory_text``, and the point omits only the other-session candidates
  when ``other_sessions`` is withheld, so an install consented to ``memory_text``
  alone still measures the memory sources and asks nothing about the snippets;
* a partial answer is recorded and never published;
* ``swapped_in`` / ``dropped`` / ``baseline_keys`` / ``jev_keys`` map onto the
  inject/skip answers;
* the record store and attempt correlation behave like ``compaction.keep``'s.

The point runs beside the prompt build and has no apply path, so there is no
"byte-identical prompt" assertion here: the point never touches the prompt, which
is the structural guarantee -- it is handed a captured snapshot and returns a
record or ``None``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.decisions import consent as consent_mod
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions.points import context_inject as point
from kiro_crew.decisions.types import Answer

SECRET = "AKIAIOSFODNN7EXAMPLE"


def cand(
    index: int,
    *,
    source: str = point.SOURCE_LESSON_RULE,
    cand_id: str | None = None,
    text: str = "a lesson about X",
    admitted: bool = True,
    pinned: bool = False,
) -> point.Candidate:
    return point.Candidate(
        index=index,
        source=source,
        cand_id=cand_id if cand_id is not None else f"id{index}",
        text=text,
        admitted=admitted,
        pinned=pinned,
    )


def snapshot(*candidates: point.Candidate) -> point.Snapshot:
    return point.Snapshot(candidates=list(candidates))


@pytest.fixture(autouse=True)
def _no_leftover_records():
    point.reset_records()
    yield
    point.reset_records()


class TestPinnedNeverOffered:
    def test_a_pinned_candidate_is_not_a_question(self):
        snap = snapshot(
            cand(0, source=point.PINNED_SOURCE, pinned=True),
            cand(1),
            cand(2),
        )
        asked = {q.id for q in point.questions_for(snap)}
        assert asked == {"cand_1", "cand_2"}

    def test_a_pinned_candidate_is_still_in_the_state_as_context(self):
        snap = snapshot(cand(0, source=point.PINNED_SOURCE, text="PINNEDTEXT", pinned=True), cand(1))
        state = point.build_state(snap)
        by_id = {c["id"]: c for c in state["candidates"]}
        assert by_id["id0"]["pinned"] is True
        assert by_id["id0"]["text"] == "PINNEDTEXT"
        assert "pinned" not in by_id["id1"]

    def test_a_pinned_candidate_is_in_both_arms_without_a_decision(self):
        snap = snapshot(
            cand(0, source=point.PINNED_SOURCE, pinned=True, admitted=True),
            cand(1, admitted=True),
        )
        counts = point.tally(snap, {"cand_1": (point.OPTION_INJECT, 0.9)})
        assert "id0" in counts["jev_keys"]
        assert "id0" in counts["baseline_keys"]


class TestCaps:
    def test_the_offered_cap_keeps_the_highest_ranked(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CANDIDATES", 3)
        snap = snapshot(*(cand(i) for i in range(10)))
        capped = point.cap_offered(snap)
        assert [c.cand_id for c in capped.offered()] == ["id0", "id1", "id2"]

    def test_pinned_items_do_not_count_against_the_cap(self, monkeypatch):
        monkeypatch.setattr(point, "MAX_CANDIDATES", 2)
        snap = snapshot(
            cand(0, source=point.PINNED_SOURCE, pinned=True),
            cand(1),
            cand(2),
            cand(3),
        )
        capped = point.cap_offered(snap)
        assert len(capped.pinned()) == 1
        assert len(capped.offered()) == 2

    def test_the_snippet_cap_holds(self, monkeypatch):
        monkeypatch.setattr(point, "SNIPPET_CHARS", 10)
        state = point.build_state(snapshot(cand(0, text="x" * 100)))
        assert state["candidates"][0]["text"] == "x" * 10

    def test_redaction_happens_before_truncation(self, monkeypatch):
        # A credential that spans the clip bound must be redacted whole, never cut
        # into a fragment the scrubber no longer matches. With the bound set to just
        # past the secret's start, a clip-first order would leak its head.
        monkeypatch.setattr(point, "SNIPPET_CHARS", len(SECRET) - 3)
        state = point.build_state(snapshot(cand(0, text=SECRET + " trailing")))
        assert SECRET not in state["candidates"][0]["text"]
        assert SECRET[:5] not in state["candidates"][0]["text"]


class TestScopeIndependence:
    def test_the_point_is_registered_under_memory_text(self):
        assert "context.inject" in gate_mod.DECISION_POINT_NAMES
        assert gate_mod.POINT_SCOPE_KEYS["context.inject"] == consent_mod.STATE_KEY_MEMORY_TEXT

    def test_without_memory_text_the_whole_point_is_refused(self, monkeypatch):
        # The gate enforces memory_text for this point exactly as it does for
        # memory.recall; an absent scope refuses the point before any candidate is
        # built.
        monkeypatch.setattr(consent_mod, "consented_memory_text", lambda *_a, **_kw: False)
        monkeypatch.setattr(consent_mod, "permits", lambda *_a, **_kw: True)
        monkeypatch.setattr(gate_mod, "configured_endpoint", lambda *_a, **_kw: "x")
        monkeypatch.setattr(gate_mod, "_capability_denied", lambda *_a, **_kw: False)
        assert gate_mod.is_enabled("context.inject") is False

    def test_other_sessions_default_is_not_consented(self):
        assert point.other_sessions_consented() is False

    def test_without_other_sessions_the_recent_snippets_are_dropped(self):
        snap = snapshot(
            cand(0, source=point.SOURCE_LESSON_RULE),
            cand(1, source=point.SOURCE_RECENT_SESSION),
            cand(2, source=point.SOURCE_FACT),
        )
        filtered = point.filter_snapshot(snap, other_sessions=False)
        sources = {c.source for c in filtered.candidates}
        assert point.SOURCE_RECENT_SESSION not in sources
        assert point.SOURCE_LESSON_RULE in sources
        assert point.SOURCE_FACT in sources

    def test_with_other_sessions_the_snippets_stay(self):
        snap = snapshot(cand(0, source=point.SOURCE_RECENT_SESSION))
        filtered = point.filter_snapshot(snap, other_sessions=True)
        assert [c.source for c in filtered.candidates] == [point.SOURCE_RECENT_SESSION]


class TestTally:
    def test_a_swapped_in_item_was_omitted_and_jev_would_inject_it(self):
        snap = snapshot(
            cand(0, cand_id="admitted0", admitted=True),
            cand(1, cand_id="omitted1", admitted=False),
        )
        counts = point.tally(
            snap,
            {
                "cand_0": (point.OPTION_INJECT, 0.9),
                "cand_1": (point.OPTION_INJECT, 0.8),
            },
        )
        assert counts["baseline_keys"] == ["admitted0"]
        assert counts["jev_keys"] == ["admitted0", "omitted1"]
        assert counts["swapped_in"] == ["omitted1"]
        assert counts["dropped"] == []

    def test_a_dropped_item_was_admitted_and_jev_would_skip_it(self):
        snap = snapshot(cand(0, cand_id="admitted0", admitted=True))
        counts = point.tally(snap, {"cand_0": (point.OPTION_SKIP, 0.9)})
        assert counts["baseline_keys"] == ["admitted0"]
        assert counts["jev_keys"] == []
        assert counts["dropped"] == ["admitted0"]
        assert counts["swapped_in"] == []

    def test_mean_p_is_over_the_offered_candidates(self):
        snap = snapshot(cand(0), cand(1))
        counts = point.tally(
            snap,
            {"cand_0": (point.OPTION_INJECT, 0.6), "cand_1": (point.OPTION_SKIP, 0.8)},
        )
        assert counts["mean_p"] == pytest.approx(0.7)

    def test_per_source_counts_track_offered_admitted_and_jev(self):
        snap = snapshot(
            cand(0, source=point.SOURCE_LESSON_RULE, admitted=True),
            cand(1, source=point.SOURCE_LESSON_RULE, admitted=False),
        )
        counts = point.tally(
            snap,
            {"cand_0": (point.OPTION_INJECT, 0.9), "cand_1": (point.OPTION_INJECT, 0.9)},
        )
        bucket = counts["per_source"][point.SOURCE_LESSON_RULE]
        assert bucket == {"offered": 2, "admitted": 1, "jev": 2}


class TestRun:
    """The whole coroutine, with the gate's ``decide`` replaced by a stub."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        monkeypatch.setattr(point.core, "is_enabled", lambda *_a, **_kw: True)
        monkeypatch.setattr(point, "wait_budget", lambda: 30.0)
        monkeypatch.setattr(point, "other_sessions_consented", lambda *_a, **_kw: True)
        self._appended: list[dict] = []

        def _append(row):
            self._appended.append(row)
            return True

        monkeypatch.setattr(point._log, "append", _append)

        async def _decide(_p, _state, questions, **_kw):
            return {q.id: Answer(id=q.id, value=point.OPTION_INJECT, p=0.8) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)

    def test_a_complete_run_writes_one_row_and_publishes_it(self):
        snap = snapshot(cand(0), cand(1))
        record = asyncio.run(point.score_context_inject("sess", snap))
        assert record is not None
        assert record["point"] == "context.inject"
        assert len(self._appended) == 1
        assert point.take_record("sess") == record

    def test_every_batch_carries_the_same_state(self, monkeypatch):
        monkeypatch.setattr(point, "QUESTIONS_PER_REQUEST", 1)
        seen: list[str] = []

        async def _decide(_p, state, questions, **_kw):
            seen.append(json.dumps(state, sort_keys=True))
            return {q.id: Answer(id=q.id, value=point.OPTION_INJECT, p=0.8) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)
        asyncio.run(point.score_context_inject("sess", snapshot(cand(0), cand(1), cand(2))))
        assert len(seen) == 3 and len(set(seen)) == 1

    def test_a_partial_answer_is_recorded_and_never_published(self, monkeypatch):
        monkeypatch.setattr(point, "QUESTIONS_PER_REQUEST", 1)
        calls = {"n": 0}

        async def _decide(_p, _state, questions, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return {q.id: Answer(id=q.id, value=point.OPTION_INJECT, p=0.7) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)
        assert asyncio.run(point.score_context_inject("sess", snapshot(cand(0), cand(1)))) is None
        assert len(self._appended) == 1
        assert self._appended[0]["error"] == point.ERROR_PARTIAL
        assert point.take_record("sess") is None

    def test_the_seam_being_off_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(point.core, "is_enabled", lambda *_a, **_kw: False)
        assert asyncio.run(point.score_context_inject("sess", snapshot(cand(0)))) is None
        assert self._appended == []

    def test_an_empty_snapshot_is_not_a_measurement(self):
        assert asyncio.run(point.score_context_inject("sess", snapshot())) is None
        assert self._appended == []

    def test_every_pinned_snapshot_asks_nothing(self):
        snap = snapshot(cand(0, source=point.PINNED_SOURCE, pinned=True))
        assert asyncio.run(point.score_context_inject("sess", snap)) is None

    def test_a_refused_row_is_never_published(self, monkeypatch):
        monkeypatch.setattr(point._log, "append", lambda _row: False)
        assert asyncio.run(point.score_context_inject("sess", snapshot(cand(0)))) is None
        assert point.take_record("sess") is None

    def test_without_other_sessions_only_the_snippets_are_withheld(self, monkeypatch):
        monkeypatch.setattr(point, "other_sessions_consented", lambda *_a, **_kw: False)
        sent: list[dict] = []

        async def _decide(_p, state, questions, **_kw):
            sent.append(state)
            return {q.id: Answer(id=q.id, value=point.OPTION_INJECT, p=0.8) for q in questions}

        monkeypatch.setattr(point.core, "decide", _decide)
        snap = snapshot(
            cand(0, source=point.SOURCE_LESSON_RULE, cand_id="lesson"),
            cand(1, source=point.SOURCE_RECENT_SESSION, cand_id="snippet"),
        )
        record = asyncio.run(point.score_context_inject("sess", snap))
        assert record is not None
        ids = {c["id"] for state in sent for c in state["candidates"]}
        assert "lesson" in ids
        assert "snippet" not in ids


class TestRecordStore:
    def test_a_record_is_claimed_once(self):
        point.publish_record("s", {"point": "context.inject"})
        assert point.take_record("s") == {"point": "context.inject"}
        assert point.take_record("s") is None

    def test_a_superseded_run_publishes_nothing(self):
        first = point.begin_attempt("s")
        point.begin_attempt("s")  # a later build retires the first
        point.publish_record("s", {"n": 1}, attempt=first)
        assert point.take_record("s") is None

    def test_a_keyless_record_is_dropped(self):
        point.publish_record("", {"n": 1})
        assert point.pending_count() == 0

    def test_every_token_is_new_and_increasing(self):
        tokens = [point.begin_attempt(f"s{i}") for i in range(5)]
        assert tokens == sorted(set(tokens))
