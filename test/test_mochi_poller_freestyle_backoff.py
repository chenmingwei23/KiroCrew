"""The freestyle agent-task spawn path backs off when the HOST declines.

The ``route == "execute"`` block in ``_do_poll`` spawns freestyle tasks
serially. On ``origin/main`` a declined spawn (the admission gate deferring for
low memory, surfaced as a raised ``SpawnError``) leaves the due task undone, so
it re-attempts on EVERY one-second poll with no backoff — the reported
~400 attempts/min storm.

The fix backs off ONLY on a decline: a raised ``SpawnError`` arms a cooldown
during which no freestyle spawn is attempted, and a SUCCESSFUL spawn clears it
at once. A healthy host whose spawns succeed is never throttled — the hourly
``activity_budget`` meter (recorded only after a successful spawn, see
``hooks.py``) is deliberately left untouched by this path.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi.queue_poller import (
    FREESTYLE_DECLINE_BACKOFF_MS,
    QueuePoller,
)
from kiro_crew.apps.spawn_sdk import SpawnError


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
    """spawn_agent always raises SpawnError — the host declining for low memory."""

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        raise SpawnError("deferred_low_memory")


class _HealthyThenDone:
    """spawn_agent succeeds; the queue marks the task done so it is not re-due.

    Models a well-behaved host: every spawn lands, so the backoff must never
    engage no matter how many polls run.
    """

    def __init__(self) -> None:
        self.attempts = 0

    async def spawn_agent(self, prompt: str) -> str:
        self.attempts += 1
        return f"spawn-{self.attempts}"


def _silence_writeback(monkeypatch) -> None:
    import contextlib

    monkeypatch.setattr(qf, "write_queue_atomic", lambda p, d: None)
    monkeypatch.setattr(qf, "queue_mutation", lambda p: contextlib.nullcontext())


@pytest.mark.asyncio
async def test_declined_freestyle_spawns_back_off_instead_of_storming(tmp_path, monkeypatch):
    """Repro: many polls, every spawn declined. On main every poll re-attempts
    (hundreds of attempts); the fix caps attempts to one per backoff window."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    # Far more due freestyle tasks than any budget, re-read each poll (the
    # storm's shape: a declined task is never marked done).
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    # 30 one-second polls inside one backoff window.
    for _ in range(30):
        now[0] += 1_000
        await poller.poll()

    # On main this is 30+ (one per poll, and more per poll before any gate).
    # With the decline backoff only the FIRST poll attempts; the rest are
    # deferred until the cooldown elapses.
    assert cb.attempts == 1, (
        "a declined freestyle spawn must arm a backoff, not re-attempt every "
        f"poll; got {cb.attempts} attempts in one window"
    )


@pytest.mark.asyncio
async def test_backoff_releases_after_the_window(tmp_path, monkeypatch):
    """The backoff is a deferral, not a permanent stop: once the cooldown
    elapses, exactly one more attempt is made (and re-arms the backoff)."""
    _silence_writeback(monkeypatch)
    cb = _DecliningCallbacks()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(50))

    await poller.poll()  # first attempt arms the backoff
    assert cb.attempts == 1

    for _ in range(5):  # still inside the window: no new attempt
        now[0] += 1_000
        await poller.poll()
    assert cb.attempts == 1

    now[0] += FREESTYLE_DECLINE_BACKOFF_MS + 1  # window elapses
    await poller.poll()
    assert cb.attempts == 2, "one more attempt is allowed once the backoff elapses"


@pytest.mark.asyncio
async def test_healthy_host_is_never_throttled(tmp_path, monkeypatch):
    """A host whose spawns all SUCCEED is never backed off, however many run —
    the fix throttles declines only, not healthy throughput."""
    import asyncio

    _silence_writeback(monkeypatch)
    cb = _HealthyThenDone()
    now = [1_000_000]
    poller = QueuePoller(
        str(tmp_path / "q.json"),
        cb,
        clock=lambda: now[0],
        budget_provider=None,
    )
    poller.start()

    # One due freestyle task per poll, each spawn succeeding. The poller's
    # serial wait is resolved by notify_agent_done so each poll completes.
    monkeypatch.setattr(qf, "read_queue", lambda p: _execute_queue(1))

    async def _resolve_when_waiting(task: asyncio.Future) -> None:
        # Spin until the poll has created its serial future, then resolve it.
        while not task.done() and poller._spawn_future is None:
            await asyncio.sleep(0)
        poller.notify_agent_done()

    for _ in range(20):
        now[0] += 1_000
        poll_task = asyncio.ensure_future(poller.poll())
        await _resolve_when_waiting(poll_task)
        await poll_task

    assert cb.attempts == 20, (
        "every successful freestyle spawn must be allowed; a healthy host is "
        f"never throttled, got {cb.attempts}/20"
    )
    assert poller._freestyle_backoff_until == 0, "a success must leave no backoff armed"
