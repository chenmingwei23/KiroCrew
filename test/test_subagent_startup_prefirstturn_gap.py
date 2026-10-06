"""Characterization tests for the pre-first-turn reaper coverage gap.

A subagent shown as ``starting`` is registered but has not advanced past
turn 0. Two watchdogs are meant to bound that state:

* the fast startup watchdog (:meth:`SubagentManager._is_startup_stalled`,
  window :data:`_STARTUP_TIMEOUT_SECS`), and
* the stuck-wave sweep (:meth:`SubagentManager._sweep_stuck_waves`).

This file covers two pre-execution states against the reaper's bounds.
The first is a wave member still sitting in the spawn
queue (``_queue``, never in ``_agents``): it carries an independent
pre-execution deadline measured from its wall-clock enqueue stamp, enforced by
:meth:`SubagentManager._sweep_stranded_queue_entries` from the reaper loop. The
tests below assert that coverage; a direct test of the sweep is
added at the end.

The second state -- a registered run with ``_exec_started is None`` (e.g. parked
on a spawn approval) -- is deliberately NOT bounded by a fast reaper here: it is
owned by the approval window (2 h on the dashboard/Slack paths), which denies an
unanswered prompt and ends the run as ``spawn rejected``. Those tests still pin
that the reaper's per-agent loop adds no shorter bound, which is correct and
intended.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from kiro_crew.subagent import (
    _STARTUP_TIMEOUT_SECS,
    _WAVE_STUCK_SECS,
    SubagentInfo,
    SubagentManager,
)
from kiro_crew.subagent_manager.admission.types import QUEUED_AT_KEY
from kiro_crew.subagent_manager.monitoring import _QUEUE_STRAND_MAX_SECS


def _make_manager(max_concurrent: int = 1) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        max_concurrent=max_concurrent,
    )
    mgr._on_done = None
    return mgr


def _reaper_would_terminate(mgr: SubagentManager, info: SubagentInfo, now: float) -> bool:
    """Reproduce the reaper's per-agent terminal decision without running the
    async loop: a live run is force-reaped only when the startup watchdog
    fires or when elapsed exceeds the wall-clock ``_default_timeout``. Mirrors
    ``_MonitoringMixin._reaper_loop`` in ``subagent_manager/monitoring.py``.
    """
    if info.done:
        return False
    if mgr._is_startup_stalled(info, now):
        return True
    return (now - info.started) > mgr._default_timeout


# --- A queued wave member carries a pre-execution deadline ----


def test_queued_member_absent_from_agents_but_covered_by_strand_sweep():
    """A wave member behind the stagger/concurrency gate lives only in
    ``_queue``; it is never registered in ``_agents``, so the reaper's per-agent
    loop and the startup watchdog never see it.

    The member carries a wall-clock enqueue stamp and
    the reaper's stranded-queue sweep reaps it once it outlives
    ``_QUEUE_STRAND_MAX_SECS``, so a queued member stays visible to
    the reaper.
    """
    mgr = _make_manager(max_concurrent=1)
    batch_id = "wave"
    now = time.time()
    mgr._queue.append(
        {
            "task": "t",
            "parent_session_key": "p",
            "agent": "amzn-builder",
            "batch_id": batch_id,
            "batch_total": 2,
            "_preassigned_id": "queued_member",
            QUEUED_AT_KEY: now - _QUEUE_STRAND_MAX_SECS - 100,
        }
    )
    assert "queued_member" not in mgr._agents

    asyncio.run(mgr._sweep_stranded_queue_entries(time.time()))

    # The stranded member is reaped out of the queue by its own deadline.
    assert all(p.get("_preassigned_id") != "queued_member" for p in mgr._queue)


def test_stuck_wave_sweep_reconciles_once_the_strand_sweep_clears_the_queue():
    """The stuck-wave sweep reconciles a wave only when nothing of it is still
    queued, and it skips a wave with a lost submission while any
    member remains in ``_queue``.

    The stranded-queue sweep removes the queued member first,
    after which ``_sweep_stuck_waves`` reconciles the lost-submission wave and
    ``batch_members_pending`` reads it as not pending.
    """
    mgr = _make_manager(max_concurrent=1)
    batch_id = "wave"
    now = time.time()
    terminal = SubagentInfo(
        id="done_member", task="t", agent="", batch_id=batch_id, batch_total=2, done=True
    )
    mgr._agents["done_member"] = terminal
    mgr._queue.append(
        {
            "task": "t",
            "parent_session_key": "p",
            "agent": "amzn-builder",
            "batch_id": batch_id,
            "batch_total": 2,
            "_preassigned_id": "queued_member",
            QUEUED_AT_KEY: now - _QUEUE_STRAND_MAX_SECS - 100,
        }
    )
    # Lost-submission shape: 1 of 2 submitted, past the grace window.
    mgr._batch_submitted[batch_id] = [1, 2]
    mgr._batch_progress_ts[batch_id] = now - _WAVE_STUCK_SECS - 100

    # While the member is queued the stuck-wave sweep still skips it.
    before = list(mgr._batch_submitted[batch_id])
    mgr._sweep_stuck_waves(now)
    assert list(mgr._batch_submitted[batch_id]) == before

    # The strand sweep clears the queued member...
    asyncio.run(mgr._sweep_stranded_queue_entries(time.time()))
    assert all(p.get("batch_id") != batch_id for p in mgr._queue)

    # ...and the stuck-wave sweep is now free of the queued-member block.
    mgr._sweep_stuck_waves(time.time())
    assert mgr.batch_members_pending(batch_id) is False


def test_strand_sweep_leaves_a_fresh_queued_member_in_place():
    """A queued member that has NOT outlived the deadline is untouched -- a
    legitimate long-queued fan-out tail keeps waiting for its slot.
    """
    mgr = _make_manager(max_concurrent=1)
    now = time.time()
    mgr._queue.append(
        {
            "task": "t",
            "parent_session_key": "p",
            "agent": "amzn-builder",
            "batch_id": "wave",
            "batch_total": 2,
            "_preassigned_id": "fresh_member",
            QUEUED_AT_KEY: now,  # just enqueued
        }
    )
    asyncio.run(mgr._sweep_stranded_queue_entries(now + 10))
    assert any(p.get("_preassigned_id") == "fresh_member" for p in mgr._queue)


def test_strand_sweep_stamps_a_legacy_entry_rather_than_reaping_it():
    """An entry with no enqueue stamp (legacy or re-appended) is stamped on
    first sight and left in place, so its clock starts now and it is only reaped
    a full window later -- never retroactively on the first sweep.
    """
    mgr = _make_manager(max_concurrent=1)
    entry = {
        "task": "t",
        "parent_session_key": "p",
        "agent": "amzn-builder",
        "batch_id": "wave",
        "batch_total": 2,
        "_preassigned_id": "legacy_member",
    }
    mgr._queue.append(entry)
    now = time.time()
    asyncio.run(mgr._sweep_stranded_queue_entries(now))
    # Still queued, now carrying a stamp set to ~now.
    assert any(p.get("_preassigned_id") == "legacy_member" for p in mgr._queue)
    assert abs(float(entry[QUEUED_AT_KEY]) - now) < 5.0


def test_strand_sweep_excludes_entries_with_their_own_owner():
    """A resume, an approval-released start, and a memory-deferred row each have
    their own bound, so the strand sweep never reaps them even past the deadline.
    """
    from kiro_crew.subagent_manager.admission.types import MEMORY_WAIT_UNTIL_KEY

    mgr = _make_manager(max_concurrent=1)
    old = time.time() - _QUEUE_STRAND_MAX_SECS - 100
    mgr._queue.extend(
        [
            {"_preassigned_id": "resume", "_resume_id": "r1", QUEUED_AT_KEY: old},
            {"_preassigned_id": "released", "_startup_release": True, QUEUED_AT_KEY: old},
            {"_preassigned_id": "memwait", MEMORY_WAIT_UNTIL_KEY: 0.0, QUEUED_AT_KEY: old},
        ]
    )
    asyncio.run(mgr._sweep_stranded_queue_entries(time.time()))
    ids = {p.get("_preassigned_id") for p in mgr._queue}
    assert ids == {"resume", "released", "memwait"}


# --- Gap 2 (NOT a reaper's job): a run awaiting approval is owned by the ---
#     approval window, not the reaper. The reaper adds no shorter bound.


def test_gap_startup_watchdog_ignores_a_run_that_never_entered_execution():
    """``_is_startup_stalled`` keys on ``_exec_started``. A run registered in
    ``_agents`` but parked before ``_run_inner`` (``_exec_started is None`` --
    e.g. awaiting a spawn approval no surface answered) is not seen by the fast
    watchdog, no matter how long it has been registered.

    Intended behaviour, not a gap to close here: a run parked awaiting a spawn
    approval is bounded by the approval window (2 h on the dashboard/Slack
    paths), which denies an unanswered prompt and ends the run as ``spawn
    rejected``. The fast startup watchdog deliberately does not fire for it --
    the ``_exec_started``-keyed clock measures time spent executing, and this
    run has not entered execution -- so a reaper-level deadline here would risk
    killing a legitimate run whose approval the user still intends to answer.
    The reaper bounds the queued (``_queue``) tail, which has no owner of its own;
    this approval-parked case already has one.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="parked", task="t", agent="")
    info._exec_started = None
    info._awaiting_approval = True
    info.turns = 0
    info._pid = None
    now = info.started + _STARTUP_TIMEOUT_SECS * 100  # far past the startup window

    assert mgr._is_startup_stalled(info, now) is False


def test_gap_pre_execution_run_has_no_bound_shorter_than_the_wall_clock():
    """For a registered pre-execution run (``_exec_started is None``), the
    reaper's per-agent decision does not terminate it at any instant short of
    the wall-clock ``_default_timeout`` -- the startup watchdog cannot see it,
    so the wall clock is the only bound.

    Intended behaviour: the reaper adds no bound shorter than the wall clock
    for an approval-parked run, because that case is owned by the 2 h approval
    window, not the reaper (see the test above). The reaper adds a shorter bound
    only for the ownerless ``_queue`` tail. The test pins the absence of a
    shorter reaper-level bound, not the wall-clock value.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="parked", task="t", agent="")
    info._exec_started = None
    info._awaiting_approval = True
    info.turns = 0
    info._pid = None

    # Just before the wall clock: still not terminated.
    just_before = info.started + mgr._default_timeout - 1
    assert _reaper_would_terminate(mgr, info, just_before) is False

    # Only once the wall clock is exceeded does the reaper act.
    past_wall_clock = info.started + mgr._default_timeout + 1
    assert _reaper_would_terminate(mgr, info, past_wall_clock) is True


def test_startup_watchdog_reaps_a_run_wedged_after_entering_execution():
    """Contrast (the covered half): once ``_run_inner`` has set
    ``_exec_started`` and the run is still on turn 0 with no runtime pid past
    the startup window, the fast watchdog fires. This is stable expected
    behaviour, not a gap; the tests above pin the uncovered half.
    """
    mgr = _make_manager(max_concurrent=1)
    info = SubagentInfo(id="wedged", task="t", agent="")
    info._exec_started = time.time() - (mgr._startup_deadline + 10)
    info.turns = 0
    info._pid = None

    assert mgr._is_startup_stalled(info, time.time()) is True
