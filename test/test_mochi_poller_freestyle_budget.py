"""The freestyle agent-task spawn path is bounded by the activity tier's hourly
spawn budget, and a DECLINED attempt counts against it the same as a success.

The ``route == "execute"`` block in ``_do_poll`` spawns freestyle tasks
serially. A host that keeps DECLINING a spawn (the admission gate deferring for
low memory, surfaced as a raised ``SpawnError``) must not drive every due
freestyle task into a per-poll retry: the budget counts every ATTEMPT in the
window regardless of outcome, so repeated declines throttle the same way
repeated successes do, and the due task is deferred to a later window.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi.activity_budget import TIERS
from kiro_crew.apps.builtins.mochi.queue_poller import (
    MAX_WATCH_SPAWNS_PER_WINDOW,
    QueuePoller,
)


def _execute_queue(n_tasks: int) -> dict:
    """A queue of ``n_tasks`` due freestyle tasks that routes to 'execute'."""
    return {
        # planned_until far in the future keeps route_poll on 'execute' (no
        # plan/replan branch stealing the poll).
        "planned_until": "2999-01-01T00:00:00.000Z",
        "tasks": [
            {
                "id": f"fs-{i}",
                "type": "freestyle",
                "execute_after": "1970-01-01T00:00:00.000Z",  # long overdue
                "done": False,
                "action": {"prompt": f"do task {i}"},
            }
            for i in range(n_tasks)
        ],
    }


class _DecliningCallbacks:
    """spawn_agent always raises — the host declining every spawn (low memory)."""

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        raise RuntimeError("host declined: deferred_low_memory")


def _poller(tmp_path, callbacks, *, budget_provider=None) -> QueuePoller:
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        callbacks,
        clock=lambda: 1_000_000,  # fixed clock: all polls share one hour window
        budget_provider=budget_provider,
    )
    poller.start()
    return poller


def _silence_writeback(monkeypatch) -> None:
    # The poll ends in a locked merge-write; neutralise its disk/lock I/O so the
    # test exercises only the spawn budget. read_queue is driven by the test.
    import contextlib

    monkeypatch.setattr(qf, "write_queue_atomic", lambda p, d: None)
    monkeypatch.setattr(qf, "queue_mutation", lambda p: contextlib.nullcontext())


@pytest.mark.asyncio
async def test_declined_freestyle_spawns_stop_at_the_hourly_budget(tmp_path, monkeypatch):
    """Many polls, every spawn declined: attempts stop at max_spawns_per_hour."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    budget = TIERS["balanced"]  # max_spawns_per_hour == 12
    poller = _poller(tmp_path, cb, budget_provider=lambda: budget)

    # A queue with far more due freestyle tasks than the budget, re-read on
    # every poll (the storm's shape: the declined tasks never get marked done).
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    for _ in range(30):  # 30 one-second polls under a fixed hour window
        await poller.poll()

    assert (
        cb.attempts == budget.max_spawns_per_hour == 12
    ), f"freestyle spawns must stop at the hourly budget, got {cb.attempts}"


@pytest.mark.asyncio
async def test_unlimited_tier_falls_back_to_the_vendored_window(tmp_path, monkeypatch):
    """budget_provider None (unlimited tier): the vendored storm-breaker cap
    applies, so even 'unlimited' never storms the host."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    poller = _poller(tmp_path, cb, budget_provider=None)

    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    for _ in range(20):
        await poller.poll()

    assert cb.attempts == MAX_WATCH_SPAWNS_PER_WINDOW == 5


@pytest.mark.asyncio
async def test_window_rollover_lets_freestyle_spawns_resume(tmp_path, monkeypatch):
    """Once the hour window rolls over, the budget refills and spawns resume —
    the deferral is a backoff, not a permanent stop."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    budget = TIERS["balanced"]
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=lambda: budget,
    )
    poller.start()
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    for _ in range(20):
        await poller.poll()
    assert cb.attempts == 12

    now[0] += 3_600_001  # roll past the one-hour window
    for _ in range(20):
        await poller.poll()
    assert cb.attempts == 24, "the budget must refill when the hour window rolls over"
