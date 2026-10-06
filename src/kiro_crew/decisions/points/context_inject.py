"""``context.inject`` -- which background items would Jev inject at startup?

A SHADOW measurement, and nothing else. Jev decides nothing: the prompt that
ships is byte-identical with this point on and off, on every backend, and the
answer becomes rows in the decision log plus -- when it lands in time -- one line
on the reply receipt. :func:`score_context_inject` has no apply path and no
return value a caller branches on, which is what makes it safe to place beside
the prompt build at all.

What it buys is the number the live design cannot be argued about without: given
the SAME per-source budgets the prompt build already applies, which items would
an oracle inject -- including which it would SWAP IN from the ones the budget
omitted, and which admitted ones it would DROP. ``memory.recall`` already asks a
keep/drop question, but only over the ``memory_recall`` tool's own results and
only as a subset: it can shrink a block, never surface an omitted item. Nothing
asks that question about the block injected BEFORE the model has called any tool,
and surfacing a relevant omitted rule is the main thing the live design is for.

Where it runs, and where it deliberately does not
-------------------------------------------------
Beside the prompt build, never in front of it, the way ``compaction.keep`` runs
beside the compaction. The build assembles its injected block exactly as today
and hands this point the candidates it produced -- both the items the per-source
budget ADMITTED and the next few it OMITTED -- as a captured snapshot. The
scoring then runs as a background task the build does not await, so the build
never waits on an oracle and a late answer changes no byte of the prompt.

Reach is the same as ``memory.recall``: owner dashboard chats only. Scheduled
jobs, sub-agents, app requests and closed-tab conversations are never decided
for; the caller establishes that before invoking this point, and the keystone's
sampling gate backs it up.

What leaves the machine, and the TWO consents that authorize it
---------------------------------------------------------------
Two categories, each with its own keystone scope, because the design reviewed
them as two different things:

* The MEMORY candidates -- untagged lessons, on-topic past findings,
  activity-index entries, separable semantic facts. This is text the AGENT wrote
  down turns or days ago, the same category ``memory.recall`` sends, so it needs
  the ``memory_text`` scope (``gate.POINTS_NEEDING_MEMORY_TEXT`` lists this point
  beside ``memory.recall``). An install consented before that scope existed is
  INERT here rather than retroactively signed up.
* The RECENT SESSION snippets -- ``## Recent Session Context`` lines drawn from
  the owner's OTHER conversations. The docs today promise compaction scoring
  sends no other session, so this is a genuinely new category, and it needs its
  OWN scope, ``other_sessions`` (``consent.consented_other_sessions``). This
  point consults that scope ITSELF (:func:`other_sessions_consented`) and simply
  OMITS the recent-session candidates when it is not granted, rather than
  refusing the whole point: the memory candidates are still sendable under the
  first scope, so an install that granted ``memory_text`` but not
  ``other_sessions`` still measures the memory sources and asks nothing about the
  other-session snippets.

Pinned, never offered
---------------------
Critical rules, response preferences, identity and assignment blocks, the owner's
preference document and owner-authored always-apply lessons are PINNED: they are
context the oracle may see so it can judge the rest, but they are never a
``Choice`` and an answer can never drop them. :data:`PINNED_SOURCES` names the
sources whose items are pinned; everything else is offered.

Bounds
------
About :data:`MAX_CANDIDATES` candidates in total across all sources. Each is sent
as its id, its source and at most :data:`SNIPPET_CHARS` characters of text, with
credentials and exfiltration URLs replaced BEFORE the text is shortened. Order
within a source stays the ranker's.

Records
-------
A question row per request (written by the gate), plus one OUTCOME row when the
answer is usable, carrying ``baseline_keys`` / ``jev_keys`` / ``swapped_in`` /
``dropped`` / mean ``p`` / per-source counts -- ids and counts only, never text.
The receipt line reads like ``context · Jev would inject 38 of 70 · swap in 9
omitted``. A PARTIAL answer writes the row with :data:`ERROR_PARTIAL` and
publishes nothing: a fraction over a denominator some of whose members were never
asked about is a false number, not an incomplete one.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import as_text
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "context.inject"

#: The two outcomes of the shadow question, per OFFERED candidate: would the
#: oracle inject this item within the same per-source budget, or not. The gate
#: refuses an answer outside a question's declared options, so a provider
#: inventing a third reads as an invalid result rather than as a branch nobody
#: wrote.
OPTION_INJECT = "inject"
OPTION_SKIP = "skip"
OPTIONS: tuple[str, ...] = (OPTION_INJECT, OPTION_SKIP)

#: Prefix of a question's id, completed by the candidate's own index in the
#: snapshot, so an answer names one candidate and a reader can find it in the
#: request that was sent.
QUESTION_PREFIX = "cand_"

#: The rubric, sent as every question's prompt. It names the CONSEQUENCE of each
#: option rather than the option word, which alone says nothing about what a
#: reader loses.
PROMPT = (
    "This item is a candidate for the background context injected into the "
    "assistant's prompt at the start of the task. Within the same per-source "
    f"budget, is it worth injecting for this task? Answer {OPTION_INJECT} if it "
    f"helps the assistant with what the task is about, or {OPTION_SKIP} if it "
    "only shares words with the task or is not relevant to it."
)

#: The sources a candidate may come from. The MEMORY sources travel under the
#: ``memory_text`` scope; ``recent_session`` travels under ``other_sessions`` and
#: is dropped entirely when that scope is not granted (:func:`other_sessions_consented`).
SOURCE_LESSON_RULE = "lesson_rule"
SOURCE_LESSON_EXPERIENCE = "lesson_experience"
SOURCE_ACTIVITY = "activity"
SOURCE_FACT = "fact"
SOURCE_RECENT_SESSION = "recent_session"

#: Sources whose items are PINNED context -- shown so the oracle can judge the
#: rest, never offered as a ``Choice`` and never droppable. The design's point 3:
#: critical rules, response preferences, identity/assignment, the owner's
#: preference document, owner-authored always-apply lessons. A candidate from one
#: of these carries ``pinned=True`` and :func:`questions_for` skips it.
PINNED_SOURCE = "pinned"

#: The source that is gated on the SECOND scope. Its candidates are dropped from
#: the snapshot before anything is sent when ``other_sessions`` is not consented.
OTHER_SESSION_SOURCES = frozenset({SOURCE_RECENT_SESSION})

#: Candidates offered in total, across all sources, after pinned items. The
#: design's "about 60"; a small margin over it absorbs an extra admitted item
#: without a config change. Past this the OLDEST-ranked offered candidates are
#: dropped, so the admitted set stays the ranker's highest.
MAX_CANDIDATES = 70

#: Characters of any one candidate's text that are sent. Redaction happens before
#: this clip, so a credential spanning the bound cannot leave a fragment on the
#: wire.
SNIPPET_CHARS = 200

#: Longest source label sent or recorded. A label is one of the constants above,
#: so this is a guard against a malformed snapshot rather than a truncation anyone
#: should see.
MAX_SOURCE_CHARS = 40

#: Longest candidate id sent or recorded. An id is the item's own stable key
#: (a lesson row id, an activity stamp, a session key); long values are a bound,
#: not a thing to show.
MAX_ID_CHARS = 200

#: Questions one request carries. The state is a bounded list of short snippets;
#: this many keeps a request well inside the provider's own ceiling with room for
#: the shared rubric and the two options each question repeats.
QUESTIONS_PER_REQUEST = 25

#: Requests in flight at once. A shadow measurement must not be the reason a
#: provider rate-limits the points that are deciding something, so this is low
#: deliberately.
MAX_CONCURRENT_REQUESTS = 4

#: Scheduling slack on top of the provider budget, and the floor and ceiling the
#: wait is clamped into -- held equal to ``compaction.keep``'s values so one shape
#: covers both. The ceiling bounds the WHOLE run: a snapshot needing several
#: requests gets the same ceiling one request does, and a run that outlives it
#: publishes nothing.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0

#: How long the outcome row's write may hold this task, on top of the provider
#: budget. Named and valued as ``gate._LOG_BUDGET_SECS`` is, because it is the
#: same write on the same loop.
LOG_BUDGET_SECS = 0.05

#: Row ``error`` when at least one batch answered and at least one did not. The
#: counts are still written -- they say how much was learned -- but no receipt is
#: published, because a fraction whose denominator includes candidates nobody was
#: asked about is a false number rather than an incomplete one.
ERROR_PARTIAL = "partial-answers"

#: Row ``error`` when the whole run outlived :func:`wait_budget`. The gate writes
#: its own row for each request it was still inside; this one says the RUN
#: stopped, which is the fact an operator raises ``timeout_ms`` on.
ERROR_RUN_TIMEOUT = "run-timeout"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One item the prompt build offered this point, as it was ranked.

    *source* is one of the ``SOURCE_*`` constants (or :data:`PINNED_SOURCE`).
    *admitted* is whether today's per-source budget INCLUDED it (``True``) or
    omitted it (``False``) -- the baseline is every admitted candidate, and a
    "swap in" is an omitted one the oracle would inject. *pinned* items are shown
    but never offered.
    """

    index: int
    source: str
    cand_id: str
    text: str
    admitted: bool
    pinned: bool = False

    @property
    def question_id(self) -> str:
        """This candidate's question id, which is also its key in the state."""
        return f"{QUESTION_PREFIX}{self.index}"


@dataclass(slots=True)
class Snapshot:
    """Everything one scoring is computed from, captured by the prompt build.

    A plain list of :class:`Candidate` plus the booleans the caller resolved about
    the session. It is a SNAPSHOT because the build captures it synchronously and
    the scoring runs beside the build: reading the sources again inside the task
    could see a block the build has since rebuilt.
    """

    candidates: list[Candidate] = field(default_factory=list)

    def offered(self) -> list[Candidate]:
        """The non-pinned candidates, in rank order."""
        return [c for c in self.candidates if not c.pinned]

    def pinned(self) -> list[Candidate]:
        """The pinned candidates -- context for the oracle, never a question."""
        return [c for c in self.candidates if c.pinned]


def wait_budget() -> float:
    """How long the whole run may take, clamped into a sane window. Never raises."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("context.inject: provider budget unreadable", exc_info=True)
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def other_sessions_consented(session_key: str = "") -> bool:
    """Whether the owner granted the ``other_sessions`` scope. Never raises.

    Read on its OWN rather than through the gate's point-to-scope table, because
    this point needs TWO scopes and the table maps one point to one scope. The
    gate enforces the ``memory_text`` scope -- without it the point is refused
    outright and this is never reached -- and this function decides the narrower
    question of whether the recent-session candidates may travel on top. A missing
    or unreadable scope reads as NOT consented, the same fail-closed direction
    every consent read takes.
    """
    try:
        from kiro_crew.decisions import consent as _consent

        return bool(_consent.consented_other_sessions())
    except Exception:
        logger.debug("context.inject: other_sessions scope unreadable; omitting", exc_info=True)
        return False


def redacted(text: object, limit: int | None) -> str:
    """*text* with credentials and exfiltration URLs replaced, then clipped to *limit*.

    Redact-THEN-clip, through :func:`~kiro_crew.platform.context.redact_via_context`
    -- the canonical egress shim, so a host that loaded a companion applies its own
    credential and cookie spellings and a standalone process gets the baseline.
    Exfiltration URLs are cleared by the shipped pass on top. ``limit=None``
    redacts without clipping.

    Clipped AFTER redaction, never before: a cut inside a secret leaves a fragment
    neither pattern matches, and the fragment is what would travel. The clip is a
    HEAD, because the identifying part of a lesson or a finding is its opening.

    Never raises, including on a composition failure: the field is DROPPED, the
    same direction ``compaction.keep.redacted`` fails in -- a scan that did not
    complete cannot clear text for the wire.
    """
    raw = as_text(text)
    if not raw or (limit is not None and limit <= 0):
        return ""
    try:
        from kiro_crew.platform.context import redact_via_context
        from kiro_crew.security import redact_exfiltration_urls

        cleaned = redact_via_context(raw)
        cleaned, _warnings = redact_exfiltration_urls(cleaned)
    except Exception:
        logger.debug("context.inject: redaction failed; dropping the field", exc_info=True)
        return ""
    return cleaned if limit is None else cleaned[:limit]


def filter_snapshot(snapshot: Snapshot, *, other_sessions: bool) -> Snapshot:
    """The snapshot with the second-scope candidates removed when it is not granted.

    A pure transform, so it is testable without a session. When *other_sessions*
    is false every candidate whose source is in :data:`OTHER_SESSION_SOURCES` is
    dropped before anything is sent -- the memory sources still travel under the
    first scope, which is why this point omits a source rather than refusing the
    whole run. Indices are left as the caller assigned them: an id is a key, not a
    position, and gaps in it are harmless.
    """
    if other_sessions:
        return snapshot
    kept = [c for c in snapshot.candidates if c.source not in OTHER_SESSION_SOURCES]
    return Snapshot(candidates=kept)


def cap_offered(snapshot: Snapshot) -> Snapshot:
    """At most :data:`MAX_CANDIDATES` OFFERED candidates, keeping the highest-ranked.

    Pinned candidates are never counted against the cap -- they are context, not
    questions -- and are always kept. The offered list is in rank order, so the
    cap drops its TAIL: the lowest-ranked offered items, which is the right end,
    because the ranker's top is what the budget would admit first.
    """
    pinned = snapshot.pinned()
    offered = snapshot.offered()[:MAX_CANDIDATES]
    # Rebuild in original order so pinned/offered interleaving is preserved for a
    # reader, with the dropped tail simply absent.
    keep_ids = {id(c) for c in pinned} | {id(c) for c in offered}
    kept = [c for c in snapshot.candidates if id(c) in keep_ids]
    return Snapshot(candidates=kept)


def build_state(snapshot: Snapshot) -> dict[str, Any]:
    """The state one batch of questions is asked about.

    Every candidate -- pinned and offered alike -- is sent as its id, source and
    redacted-then-clipped text, so the oracle sees the pinned items as context.
    A pinned candidate carries ``pinned: true`` so the oracle can tell context
    from a thing it is being asked about; an admitted candidate carries
    ``admitted: true`` so the oracle sees which items today's budget already
    includes.
    """
    items: list[dict[str, Any]] = []
    for cand in snapshot.candidates:
        entry: dict[str, Any] = {
            "id": redacted(cand.cand_id, MAX_ID_CHARS),
            "source": redacted(cand.source, MAX_SOURCE_CHARS),
            "text": redacted(cand.text, SNIPPET_CHARS),
        }
        if cand.pinned:
            entry["pinned"] = True
        if cand.admitted:
            entry["admitted"] = True
        items.append(entry)
    return {"candidates": items}


def questions_for(snapshot: Snapshot) -> list[Question]:
    """One ``Choice`` per OFFERED candidate, in rank order.

    A pinned candidate gets none: it is injected whatever an answer would have
    said, so asking would spend a question on a decision already made.
    """
    return [
        Choice(cand.question_id, PROMPT, options=list(OPTIONS)) for cand in snapshot.offered()
    ]


def batches(questions: Sequence[Question], size: int | None = None) -> list[list[Question]]:
    """*questions* split into requests of at most *size*, in rank order.

    *size* defaults to :data:`QUESTIONS_PER_REQUEST` resolved AT CALL TIME, not as
    a default-argument value frozen at import -- so a test that lowers the constant
    is honoured.
    """
    step = max(1, QUESTIONS_PER_REQUEST if size is None else size)
    return [list(questions[i : i + step]) for i in range(0, len(questions), step)]


def read_decisions(answers: Any, asked: Sequence[Question]) -> dict[str, tuple[str, float]] | None:
    """``{question_id: (option, p)}`` for one batch, or ``None`` when unusable.

    ALL-OR-NOTHING for the batch: a mapping missing one candidate it was asked
    about cannot be folded into a fraction over all of them, and silently dropping
    the gap is what makes the receipt's number wrong rather than absent.
    """
    if not isinstance(answers, dict):
        return None
    out: dict[str, tuple[str, float]] = {}
    for question in asked:
        answer = answers.get(question.id)
        if not isinstance(answer, Answer):
            return None
        value = answer.value
        if not isinstance(value, str) or value not in OPTIONS:
            return None
        if isinstance(answer.p, bool) or not isinstance(answer.p, (int, float)):
            return None
        out[question.id] = (value, float(answer.p))
    return out


def tally(snapshot: Snapshot, decisions: Mapping[str, tuple[str, float]]) -> dict[str, Any]:
    """The counts off one COMPLETE set of *decisions*.

    ``baseline_keys`` is every admitted candidate -- what the build injects without
    this point. ``jev_keys`` is what the oracle's answer would inject: an admitted
    candidate it kept, plus an omitted one it would swap in. ``swapped_in`` and
    ``dropped`` name the delta: omitted items the oracle would inject, and admitted
    items it would not. Keys and counts only, never the text.
    """
    baseline_keys: list[str] = []
    jev_keys: list[str] = []
    swapped_in: list[str] = []
    dropped: list[str] = []
    per_source: dict[str, dict[str, int]] = {}
    p_values: list[float] = []
    for cand in snapshot.candidates:
        bucket = per_source.setdefault(
            cand.source, {"offered": 0, "admitted": 0, "jev": 0}
        )
        if cand.admitted:
            baseline_keys.append(cand.cand_id)
            bucket["admitted"] += 1
        if cand.pinned:
            # Pinned items are always injected and are not in the offered question
            # set, so they belong to both arms without a decision.
            jev_keys.append(cand.cand_id)
            bucket["jev"] += 1
            continue
        bucket["offered"] += 1
        option, p = decisions[cand.question_id]
        p_values.append(p)
        inject = option == OPTION_INJECT
        if inject:
            jev_keys.append(cand.cand_id)
            bucket["jev"] += 1
            if not cand.admitted:
                swapped_in.append(cand.cand_id)
        elif cand.admitted:
            dropped.append(cand.cand_id)
    mean_p = sum(p_values) / len(p_values) if p_values else 0.0
    return {
        "offered": len(snapshot.offered()),
        "pinned": len(snapshot.pinned()),
        "baseline_keys": baseline_keys,
        "jev_keys": jev_keys,
        "swapped_in": swapped_in,
        "dropped": dropped,
        "mean_p": mean_p,
        "per_source": per_source,
    }


def build_outcome(
    *,
    turn_id: str,
    counts: Mapping[str, Any],
    requests: int,
) -> dict[str, Any]:
    """The fields the outcome row and the receipt line share.

    ``point`` IS here, like ``compaction.keep``'s outcome and unlike
    ``skills.select``'s: the record is stamped on a field the frontend dispatches
    on, and a record with no point reads as the oldest shape. ``latency_ms`` is NOT
    here -- it is a core row field (:func:`~kiro_crew.decisions.log.build_row`), so
    an ``extra`` naming it would be dropped.
    """
    return {
        "turn_id": turn_id,
        "point": POINT,
        "requests": requests,
        **dict(counts),
    }


# ── The hand-off to the reply receipt ──
#
# Same shape and the same reasons as ``compaction.keep``'s store: one entry per
# session, a TTL, a ceiling, and an attempt token so a straggling run cannot have
# its record popped by the next build on that key. ``decisions.outcomes`` is the
# wrong vehicle -- its one slot per session belongs to a ``skills.select`` turn's
# strip -- so this point keeps its own store, and a reader that never arrives
# (the reply finalized before the scoring did) is the documented "receipt dropped".

#: How long a published record stays claimable. Short, because the consumer is the
#: reply finalizing moments later; a record still here a minute on belongs to a
#: reply already sent.
RECORD_TTL_SECONDS = 60.0

#: Most sessions holding an unclaimed record at once.
MAX_PENDING_RECORDS = 200

#: ``session_key -> (published_at_monotonic, record)``, oldest publish first.
_pending: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()

#: ``session_key -> newest attempt token``. A run whose token is not the session's
#: newest publishes nothing.
_attempts: dict[str, int] = {}

#: Process-wide monotonic token source, so no token is handed out twice -- the same
#: collision argument ``compaction.keep`` makes.
_attempt_source = itertools.count(1)

_pending_lock = threading.Lock()


def begin_attempt(session_key: str) -> int:
    """Mint this session's next build-attempt token, retiring the prior pending.

    Called SYNCHRONOUSLY by the build before the scoring task is created, so the
    token exists before anything can publish against it. The token comes from one
    process-wide counter, so a value handed out is never handed out again.
    """
    if not session_key or not isinstance(session_key, str):
        return 0
    with _pending_lock:
        token = next(_attempt_source)
        _attempts[session_key] = token
        _pending.pop(session_key, None)
        while len(_attempts) > MAX_PENDING_RECORDS:
            _attempts.pop(next(iter(_attempts)))
    return token


def publish_record(session_key: str, record: dict[str, Any], attempt: int = 0) -> None:
    """Hand *record* to whatever draws this session's reply receipt. Never raises.

    *attempt* is the token :func:`begin_attempt` gave this run. A run whose token
    is not the session's newest publishes nothing. ``0`` means "no token", which
    still stores: a caller with no attempt of its own (a test) is not correlating.
    """
    if not session_key or not isinstance(session_key, str) or not isinstance(record, dict):
        logger.debug("context.inject: record dropped (key=%r)", session_key)
        return
    now = time.monotonic()
    with _pending_lock:
        if attempt and _attempts.get(session_key) != attempt:
            logger.debug("context.inject: a later build superseded this scoring")
            return
        _drop_expired(now)
        _pending.pop(session_key, None)
        _pending[session_key] = (now, record)
        while len(_pending) > MAX_PENDING_RECORDS:
            _pending.popitem(last=False)


def take_record(session_key: str) -> dict[str, Any] | None:
    """Pop this session's pending record, or ``None`` when there is none.

    ``None`` is the common answer -- the seam is off by default -- so it is the
    cheap path. The read is DESTRUCTIVE: the record describes one build, and
    leaving it would attach it to the next reply.
    """
    if not session_key:
        return None
    now = time.monotonic()
    with _pending_lock:
        _drop_expired(now)
        entry = _pending.pop(session_key, None)
    if entry is None:
        return None
    published_at, record = entry
    return None if now - published_at > RECORD_TTL_SECONDS else record


def _drop_expired(now: float) -> None:
    """Evict entries older than the TTL. Caller holds :data:`_pending_lock`."""
    for key in list(_pending.keys()):
        published_at, _record = _pending[key]
        if now - published_at <= RECORD_TTL_SECONDS:
            return
        del _pending[key]


def pending_count() -> int:
    """How many sessions hold an unclaimed record. For tests and diagnostics."""
    with _pending_lock:
        return len(_pending)


def reset_records() -> None:
    """Forget every pending record and attempt token. For tests."""
    with _pending_lock:
        _pending.clear()
        _attempts.clear()


# ── The run ──


async def score_context_inject(
    session_key: str, snapshot: Snapshot, attempt: int = 0
) -> dict[str, Any] | None:
    """Score one prompt build's injection in the shadow. Returns the record, or ``None``.

    The return value is for TESTS and for a caller that wants to log it. Nothing in
    the product branches on it: the prompt has already shipped by the time this is
    called, and this coroutine has no apply path. ``None`` covers the seam being
    off, an unsampled session, an unconsented scope, an empty snapshot, an unusable
    or partial answer, a timeout and a refused row -- every one of which means "no
    receipt".

    *snapshot* is the captured candidate set the build produced. It is passed in
    rather than read here because this coroutine runs BESIDE the build: reading the
    sources again could see a block the build has since rebuilt.

    *attempt* is the token :func:`begin_attempt` minted for this build. It decides
    only whether the RECORD is published; the rows are written either way.

    Never raises except :class:`asyncio.CancelledError`, which the gate
    propagates: cancellation is the gateway going away, not a measurement failure.
    """
    started = time.monotonic()
    turn_id = uuid.uuid4().hex[:16]
    try:
        # The cheap refusal first: an unconsented machine must not build a state
        # it will not send. ``memory_text`` is the scope the gate enforces for this
        # point; ``other_sessions`` is consulted inside ``_run`` to drop a source,
        # not to refuse the whole point.
        if not await asyncio.to_thread(core.is_enabled, POINT, session_key=session_key):
            return None
        return await asyncio.wait_for(
            _run(session_key, snapshot, turn_id=turn_id, started=started, attempt=attempt),
            timeout=wait_budget(),
        )
    except asyncio.TimeoutError:
        logger.debug("context.inject: the run outlived its budget")
        await _record(
            session_key,
            latency_ms=_elapsed_ms(started),
            extra={"turn_id": turn_id, "point": POINT},
            error=ERROR_RUN_TIMEOUT,
        )
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("context.inject: leaving this build unmeasured", exc_info=True)
        return None


async def _run(
    session_key: str,
    snapshot: Snapshot,
    *,
    turn_id: str,
    started: float,
    attempt: int = 0,
) -> dict[str, Any] | None:
    """Filter, cap, ask and record. Bounded by the caller's one wait."""
    other_sessions = await asyncio.to_thread(other_sessions_consented, session_key)
    prepared = cap_offered(filter_snapshot(snapshot, other_sessions=other_sessions))
    asked = questions_for(prepared)
    if not asked:
        # Nothing to offer: every candidate pinned, or the snapshot was empty.
        # A row saying "0 of 0" would be furniture rather than a measurement.
        return None
    state = build_state(prepared)
    groups = batches(asked)
    decisions, complete = await _ask_all(
        state, groups, session_key=session_key, turn_id=turn_id
    )
    if not decisions:
        # No batch answered. The gate has already written a row per refused request.
        return None
    counts = (
        tally(prepared, decisions)
        if complete
        else {
            "offered": len(prepared.offered()),
            "answered": len(decisions),
        }
    )
    outcome = build_outcome(turn_id=turn_id, counts=counts, requests=len(groups))
    row = await _record(
        session_key,
        latency_ms=_elapsed_ms(started),
        extra=outcome,
        error=None if complete else ERROR_PARTIAL,
    )
    if row is None or not complete:
        return None
    publish_record(session_key, row, attempt)
    return row


async def _ask_all(
    state: Mapping[str, Any],
    groups: Sequence[Sequence[Question]],
    *,
    session_key: str,
    turn_id: str,
) -> tuple[dict[str, tuple[str, float]], bool]:
    """Every batch, at most :data:`MAX_CONCURRENT_REQUESTS` at a time.

    Returns ``(decisions, complete)``. ``complete`` is whether EVERY batch answered
    usably, which is what the caller needs to decide whether a fraction over all
    the candidates is a true number.
    """
    limit = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _one(index: int, batch: Sequence[Question]) -> dict[str, tuple[str, float]] | None:
        async with limit:
            extra: dict[str, Any] = {
                "turn_id": turn_id,
                "request": index,
                "requests": len(groups),
                "questions": len(batch),
            }
            answers = await core.decide(
                POINT, dict(state), list(batch), session_key=session_key, extra=extra
            )
            return read_decisions(answers, batch)

    results = await asyncio.gather(
        *(_one(index, batch) for index, batch in enumerate(groups)),
        return_exceptions=True,
    )
    decisions: dict[str, tuple[str, float]] = {}
    complete = True
    for result in results:
        if isinstance(result, BaseException) or result is None:
            if isinstance(result, asyncio.CancelledError):
                raise result
            complete = False
            continue
        decisions.update(result)
    return decisions, complete


async def _record(
    session_key: str,
    *,
    latency_ms: int,
    extra: dict[str, Any],
    error: str | None,
) -> dict[str, Any] | None:
    """Write one row and return it, or ``None`` when it did not land. Never raises.

    OFF THE LOOP and bounded, the shape ``gate._write`` and
    ``compaction_keep._record`` use for the same write on the same loop.
    """
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=latency_ms,
            error=error,
            extra=extra,
        )
        written = await asyncio.wait_for(asyncio.to_thread(_log.append, row), LOG_BUDGET_SECS)
    except asyncio.TimeoutError:
        logger.debug("context.inject: the row outlived its write budget")
        return None
    except Exception:
        logger.debug("context.inject: could not record the row", exc_info=True)
        return None
    return row if written else None


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
