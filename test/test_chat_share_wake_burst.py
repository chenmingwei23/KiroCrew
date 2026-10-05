"""Discarding one start of a shared chat runtime must take its session with it.

Both rules here are about the SECOND start of one crew member's chat slot. When
several slots of one member wake together, the allocator legitimately starts one
session twice at once -- it carries a race budget for exactly that -- and both
starts place on the same process because their spawn inputs match. One of them is
then discarded.

With sharing off that discard was a pid kill and the whole process went, so
whatever the discarded start had built went with it. On a shared process the kill
is refused by the lease gate -- correctly, the co-tenants are mid-turn -- so the
discard has to end its own ACP session explicitly. Nothing else ever will: the
process outlives every single session on it, and a session left resident holds
its whole MCP set for that process's life.
"""

from __future__ import annotations

import pytest

from kiro_crew import runtime_ownership as ro


class _FakeHandle:
    """The session handle, recording whether it was destroyed."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.memory_mode = "persistent"
        self.keep_transcript = False
        self.is_turn_active = False
        self.destroyed = 0

    async def destroy(self) -> None:
        self.destroyed += 1

    async def cancel(self) -> None:  # pragma: no cover - no active turn in these tests
        return None


class _FakeSharedRuntime:
    """One kiro-cli process serving several sessions."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kills: list[str] = []

    def is_alive(self) -> bool:
        return True

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)


def _tenant(runtime: object, handle: _FakeHandle, lease: str) -> object:
    """A chat session resident on *runtime*, holding the lease it placed with."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        owns_runtime=False,
        shared_runtime=True,
        runtime_lease=lease,
        session_key="dashboard:chat-2235-1790642756",
    )


async def _place(runtime: object, session_key: str) -> str:
    """Take one lease on *runtime* for *session_key*, as a placement does."""

    async def _already_spawned() -> object:
        return runtime

    acquisition = await ro.RUNTIME_OWNERSHIP.acquire(
        ("member-key",), session_key, _already_spawned, cap=10
    )
    return acquisition.lease


@pytest.mark.asyncio
async def test_a_discarded_shared_tenant_leaves_no_session_on_the_process() -> None:
    """The leak: a release that drops the lease and nothing else.

    Two starts of one slot, both placed on one process. The loser is discarded
    through the release the allocator's failure arms call, then hard-killed --
    and the gate refuses that kill because the winner still holds a lease. So the
    release is the ONLY thing that runs on the loser, and if it does not end the
    loser's session, that session stays resident on a process nobody may kill.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=1893246)
    key = "dashboard:chat-2235-1790642756"

    winner_lease = await _place(runtime, key)
    loser_lease = await _place(runtime, key)
    assert winner_lease != loser_lease, "two starts of one slot get two leases by design"

    winner = _tenant(runtime, _FakeHandle("sid-winner"), winner_lease)
    loser_handle = _FakeHandle("sid-loser")
    loser = _tenant(runtime, loser_handle, loser_lease)

    await ro.release_session_lease(loser)

    # The hard kill the allocator dispatches next is refused, exactly as the live
    # gate refused it: the winner is still leased. So nothing after the release
    # can clean up on the loser's behalf.
    assert (
        ro.authorize_runtime_kill(
            runtime, reason="leaked provider teardown", caller="session_pid._sync_kill_provider"
        )
        is False
    )
    assert loser_handle.destroyed == 1, (
        "the discarded start's ACP session is still resident on the shared process; "
        "its MCP set is held for the process's whole life"
    )
    assert not runtime.kills, "the winner is mid-turn on this process"
    assert getattr(winner, "_runtime_lease", None) == winner_lease
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_the_discarded_tenants_transcript_survives_its_eviction() -> None:
    """Both starts are ONE slot, so they resume the same transcript.

    Evicting the loser must not take the resume material the winner is running
    on. ``destroy`` unlinks a session's transcript unless told otherwise, and the
    discarded start is the one case where the files belong to a sibling that is
    still live.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=1893247)
    key = "dashboard:chat-2235-1790642756"
    await _place(runtime, key)
    loser_lease = await _place(runtime, key)
    loser_handle = _FakeHandle("sid-shared")
    loser = _tenant(runtime, loser_handle, loser_lease)

    await ro.release_session_lease(loser)

    assert loser_handle.destroyed == 1
    assert (
        loser_handle.keep_transcript is True
    ), "the evicted duplicate must not unlink the transcript its live sibling resumes"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_the_last_shared_tenant_discarded_ends_the_process() -> None:
    """The founder whose own setup failed is the only holder.

    Its eviction has to end the process too, or a kiro-cli with no sessions at
    all is left running: the hard kill that follows is authorized only once the
    lease is gone, and the reconciler's unowned sweep is what the live gateway
    had to fall back on.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=1888709)
    lease = await _place(runtime, "dashboard:chat-2235-1790642756")
    handle = _FakeHandle("sid-only")
    sole = _tenant(runtime, handle, lease)

    await ro.release_session_lease(sole)

    assert handle.destroyed == 1
    assert runtime.kills, "the last tenant out leaves no process behind"
    ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_sole_owner_release_still_only_drops_the_lease() -> None:
    """Sharing off, and the shape this fix must not disturb.

    A sole-owner provider's discard is followed by a pid kill the gate now
    authorizes, and that kill ends the session with the process. Destroying the
    handle first would unlink a transcript the kill path deliberately keeps.
    """
    ro._reset_for_tests()
    runtime = _FakeSharedRuntime(pid=1887260)
    handle = _FakeHandle("sid-sole")

    from kiro_crew.acp.session_provider import AcpSessionProvider

    owner = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        owns_runtime=True,
        session_key="dashboard:chat-1882-1790264309",
    )
    await owner.acquire_runtime_lease()
    await ro.release_session_lease(owner)

    assert handle.destroyed == 0, "the pid kill that follows ends this session"
    assert ro.authorize_runtime_kill(runtime, reason="leaked provider teardown", caller="t") is True
    ro._reset_for_tests()
