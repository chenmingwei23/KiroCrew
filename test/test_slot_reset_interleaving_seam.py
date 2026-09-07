"""The test-only interleaving seam, and the reload-vs-switch race it reaches.

``chat_handlers._test_interleave`` is awaited at four named points across the
session-teardown paths. These tests pin its contract -- unset by default, called
with the point names in the order the code reaches them -- and then use it for the
thing it exists for: driving a reload's teardown into the middle of a switch
handler's commit-then-reset span, deterministically, with no sleep.

Reload joins the same two locks the switch handlers take, so the two teardowns
are serialized rather than interleaved, and the tests below assert that
ordering -- a revert of the lock reddens them on their own order assertions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_model, api_chat_slot_reload
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# Registry aliases the model guard accepts, matching the ids the sibling
# switch-atomicity tests use.
_MODEL_OLD = "claude-opus-4.8"
_MODEL_NEW = "gpt-5.6-sol"
_SLOT = "s1"
_SESSION_KEY = f"dashboard:{_SLOT}"

# Loop turns a bounded yield will spend before giving up. Turns, not seconds:
# the cap exists only to bound a coroutine that can never make progress, and is
# far above what any interleaving here needs.
_MAX_TURNS = 500


async def _yield_until(predicate: Callable[[], bool]) -> bool:
    """Hand the loop back until *predicate* holds, then report whether it did.

    Scheduler turns, not wall clock. ``asyncio.sleep(0)`` reschedules this
    coroutine behind whatever is already runnable, so how many turns a given
    interleaving needs is a property of the code under test, identical on a busy
    host and an idle one -- unlike a sleep, whose duration decides the outcome.

    Returning a bool rather than asserting is what keeps a caller readable in
    both worlds: a racer that becomes blocked by a future fix reports False here
    and the caller fails on its own assertion, instead of hanging until the
    suite-wide timeout kills it with no explanation.
    """
    for _ in range(_MAX_TURNS):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def _make_app(state: DashboardState) -> web.Application:
    # Mirror production: the token_auth middleware sets request["app"] on every
    # authenticated path ("" = dashboard user), and the app-isolation guards on
    # both routes fail closed without it.
    @web.middleware
    async def dashboard_auth_marker(request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[dashboard_auth_marker])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/model", api_chat_slot_model)
    app.router.add_post("/api/chat/slots/{slot}/reload", api_chat_slot_reload)
    return app


def _idle_provider() -> MagicMock:
    provider = MagicMock()
    provider.has_active_turn = MagicMock(return_value=False)
    return provider


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=_idle_provider())
    return state


@pytest.fixture
def slot() -> _ChatSlot:
    s = _ChatSlot(_SLOT)
    s.model = _MODEL_OLD
    return s


@pytest.fixture
def state(slot: _ChatSlot) -> DashboardState:
    return _mock_state(slot)


@pytest.fixture(autouse=True)
def _no_eager_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reload re-arms the resume spawn; there is no runtime here to spawn onto."""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock(return_value=None))


class TestInterleaveSeamContract:
    """What the seam promises when nothing sets it, and what it reports when set."""

    def test_hook_defaults_to_none(self):
        # Production cost is this attribute being None: each point reads the
        # global, compares, and creates no coroutine.
        assert chat_handlers._test_interleave is None

    @pytest.mark.asyncio
    async def test_unset_hook_leaves_the_teardown_untouched(self, state, slot):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with(_SESSION_KEY, skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_reload_reaches_its_points_in_order(self, state, slot, monkeypatch):
        seen: list[str] = []

        async def _record(point: str) -> None:
            seen.append(point)

        monkeypatch.setattr(chat_handlers, "_test_interleave", _record)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200
        # Reload owns the outer point and reaches the shared chokepoint's two
        # through _reset_slot_session. No switch point: reload commits nothing.
        assert seen == ["reload:pre_reset", "reset:pre_pop", "reset:post_pop"]

    @pytest.mark.asyncio
    async def test_model_switch_reaches_its_points_in_order(self, state, slot, monkeypatch):
        seen: list[str] = []

        async def _record(point: str) -> None:
            seen.append(point)

        monkeypatch.setattr(chat_handlers, "_test_interleave", _record)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW})

        assert resp.status == 200
        assert slot.model == _MODEL_NEW
        # switch:post_commit precedes the chokepoint's pair, which is the whole
        # point of where it sits: the new value is already committed and both
        # locks are held while the old session is still alive.
        assert seen == ["switch:post_commit", "reset:pre_pop", "reset:post_pop"]

    @pytest.mark.asyncio
    async def test_a_raising_hook_is_not_swallowed(self, state, slot, monkeypatch):
        """A broken hook must fail its test, not silently skip the interleaving.

        Pins the ABSENCE of a suppress around the points: were one added, the
        teardown would proceed and every seam test would still pass while
        interleaving nothing.
        """

        async def _boom(point: str) -> None:
            raise RuntimeError(f"hook failed at {point}")

        monkeypatch.setattr(chat_handlers, "_test_interleave", _boom)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 500
        state.sessions.reset.assert_not_awaited()


class TestReloadSerializesOntoTheSwitchSessionLock:
    """Reload serialized onto the switch session lock, driven through the seam.

    ``api_chat_slot_reload`` tears down the slot's effective session under
    the SAME two locks the four commit-before-reset switch handlers take --
    ``slot._lock`` then the session-keyed switch lock -- and in the same order.
    So a reload and a switch on the same session cannot interleave their
    teardowns: whichever takes the locks first runs its whole span before the
    other starts. These tests drive the two racers together through the seam and
    assert that ordering. Reverting the lock (dropping reload back to no lock)
    reddens each test on its own order assertion, because the two spans would
    then interleave again.
    """

    @pytest.mark.asyncio
    async def test_switch_started_inside_reload_blocks_until_reload_releases(
        self, state, slot, monkeypatch
    ):
        """Reload holds both locks at its pre-teardown point; a switch must wait.

        A switch is started while reload is suspended at ``reload:pre_reset`` --
        which now sits INSIDE both of reload's locks. The switch blocks on
        ``slot._lock``, so it cannot make progress while reload is suspended
        (``switch.done()`` is False). When reload resumes, completes its teardown
        and releases the locks, the switch runs to completion. Reload's whole
        teardown pair therefore precedes ``switch:post_commit`` in the order.

        On a lockless reload (the pre-fix state) the switch would take its own
        locks unopposed and complete INSIDE reload's window, so ``switch.done()``
        would be True at the check below and the order would interleave -- either
        way this test's assertions fail.
        """
        order: list[str] = []
        switch: asyncio.Task[object] | None = None

        async with TestClient(TestServer(_make_app(state))) as client:

            async def _interleave(point: str) -> None:
                order.append(point)
                if point != "reload:pre_reset":
                    return
                # Suspended inside reload's teardown, both locks held. Start the
                # switch: whether it can proceed while reload is mid-teardown IS
                # the question. It must NOT, so _yield_until times out False.
                nonlocal switch
                switch = asyncio.create_task(
                    client.post(f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW})
                )
                completed = await _yield_until(lambda: switch is not None and switch.done())
                # The serialization guarantee: the switch cannot finish while
                # reload holds the session lock. Reverting the lock makes this
                # True and fails here.
                assert not completed, "switch completed while reload held the locks"

            monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
            reload_resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

            assert switch is not None
            try:
                # Reload has now returned and released both locks, so the switch
                # can finally run to completion.
                completed = await _yield_until(lambda: switch.done())
                assert completed, "switch never completed after reload released the locks"
                switch_resp = switch.result()
            finally:
                switch.cancel()
                await asyncio.gather(switch, return_exceptions=True)

            assert switch_resp.status == 200
            assert reload_resp.status == 200

        # Serialized: reload's own teardown pair comes FIRST, then the switch's
        # commit and its own teardown pair -- never interleaved. On a lockless
        # reload ``switch:post_commit`` and the switch's reset pair would fall
        # between reload's two points instead.
        assert order == [
            "reload:pre_reset",
            "reset:pre_pop",
            "reset:post_pop",
            "switch:post_commit",
            "reset:pre_pop",
            "reset:post_pop",
        ]
        assert slot.model == _MODEL_NEW
        # Still two teardowns of the one key -- but ordered, not interleaved.
        assert state.sessions.reset.await_count == 2
        assert {c.args[0] for c in state.sessions.reset.await_args_list} == {_SESSION_KEY}

    @pytest.mark.asyncio
    async def test_reload_started_inside_switch_span_waits_for_the_switch(
        self, state, slot, monkeypatch
    ):
        """The other direction: switch first, holding both locks; reload must wait.

        The switch is suspended after committing its new model and before
        tearing the old session down (``switch:post_commit``), holding both of
        its locks. Reload is started from there. Because reload now joins the
        same two locks, it blocks on ``slot._lock`` and cannot run its teardown
        while the switch is suspended (``reload.done()`` is False). The switch
        resumes, finishes its own teardown and releases the locks, and only then
        does reload run -- so the switch's reset pair precedes reload's.

        On a lockless reload (the pre-fix state) reload would complete its whole
        teardown from inside the switch span, so ``reload.done()`` would be True
        at the check below and the order would interleave -- either way this
        test's assertions fail.
        """
        order: list[str] = []
        reload_task: asyncio.Task[object] | None = None
        # Guards against a vacuous pass: proves the hook body ran to its end
        # rather than the yield loop exiting on an early predicate.
        hook_completed = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:

            async def _interleave(point: str) -> None:
                order.append(point)
                if point != "switch:post_commit":
                    return
                nonlocal reload_task
                reload_task = asyncio.create_task(client.post(f"/api/chat/slots/{_SLOT}/reload"))
                completed = await _yield_until(
                    lambda: reload_task is not None and reload_task.done()
                )
                # The serialization guarantee, from the other side: reload cannot
                # finish while the switch holds the session lock. Reverting the
                # lock makes this True and fails here.
                assert not completed, "reload completed while the switch held the locks"
                hook_completed.set()

            monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
            switch_resp = await client.post(
                f"/api/chat/slots/{_SLOT}/model", json={"model": _MODEL_NEW}
            )

            assert reload_task is not None
            try:
                # The switch has returned and released both locks, so reload can
                # finally run its teardown.
                completed = await _yield_until(lambda: reload_task.done())
                assert completed, "reload never completed after the switch released the locks"
                reload_resp = reload_task.result()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

            assert reload_resp.status == 200
            assert switch_resp.status == 200

        assert hook_completed.is_set()
        # Serialized: the switch's commit and its own teardown pair come first,
        # then reload's teardown pair -- reload never lands between the switch's
        # commit and the switch's pop.
        assert order == [
            "switch:post_commit",
            "reset:pre_pop",
            "reset:post_pop",
            "reload:pre_reset",
            "reset:pre_pop",
            "reset:post_pop",
        ]
        assert state.sessions.reset.await_count == 2


class TestReloadSerializesAcrossAliasSlotsSharingOneSession:
    """The pair ``slot._lock`` alone does NOT cover: two DIFFERENT slots whose
    turns run on ONE session (both carrying the same ``linked_session_key`` --
    the alias case the switch lock is keyed on the session for).

    Their ``slot._lock`` instances are DISJOINT, so only the session-keyed
    switch lock can order a reload on one against a switch on the other. This
    test drives exactly that cross-slot race, so neutralizing the session lock
    (leaving ``slot._lock`` in place) reddens it -- which the same-slot tests
    above cannot show, because there ``slot._lock`` alone already serializes.
    """

    @pytest.mark.asyncio
    async def test_reload_on_one_alias_slot_waits_for_a_switch_on_the_other(self, monkeypatch):
        _SHARED = "slack:shared-ts"
        reload_slot = _ChatSlot("s_reload")
        switch_slot = _ChatSlot("s_switch")
        # Both slots' turns run on the ONE shared session. effective_session_key
        # returns linked_session_key when set, so both resolve to _SHARED and
        # take the SAME _slot_switch_session_lock -- but their per-slot _lock is
        # disjoint.
        reload_slot.linked_session_key = _SHARED
        switch_slot.linked_session_key = _SHARED
        switch_slot.model = _MODEL_OLD

        state = MagicMock(spec=DashboardState)
        state._slots = {reload_slot.key: reload_slot, switch_slot.key: switch_slot}
        state.sessions = MagicMock()
        state.sessions.reset = AsyncMock(return_value=True)
        state.sessions.get_provider = MagicMock(return_value=_idle_provider())

        order: list[str] = []
        reload_task: asyncio.Task[object] | None = None
        hook_completed = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:

            async def _interleave(point: str) -> None:
                order.append(point)
                if point != "switch:post_commit":
                    return
                # The switch on switch_slot is suspended after committing, both
                # its locks held (its own slot._lock + the shared session lock).
                # Start a reload on the OTHER slot: its slot._lock is free, so if
                # the session lock were the only thing stopping it, dropping that
                # lock lets it run its whole teardown here.
                nonlocal reload_task
                reload_task = asyncio.create_task(
                    client.post(f"/api/chat/slots/{reload_slot.key}/reload")
                )
                completed = await _yield_until(
                    lambda: reload_task is not None and reload_task.done()
                )
                # Serialized by the SHARED session lock, not by slot._lock (which
                # is disjoint here). Neutralizing the session lock makes this True
                # and fails the test -- the same-slot tests cannot catch that.
                assert not completed, "reload ran while the alias switch held the session lock"
                hook_completed.set()

            monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
            switch_resp = await client.post(
                f"/api/chat/slots/{switch_slot.key}/model", json={"model": _MODEL_NEW}
            )

            assert reload_task is not None
            try:
                completed = await _yield_until(lambda: reload_task.done())
                assert (
                    completed
                ), "reload never completed after the switch released the session lock"
                reload_resp = reload_task.result()
            finally:
                reload_task.cancel()
                await asyncio.gather(reload_task, return_exceptions=True)

            assert reload_resp.status == 200
            assert switch_resp.status == 200

        assert hook_completed.is_set()
        # The switch's whole span (commit + its own reset pair) precedes reload's
        # teardown pair: the shared session lock ordered them despite the disjoint
        # slot locks.
        assert order == [
            "switch:post_commit",
            "reset:pre_pop",
            "reset:post_pop",
            "reload:pre_reset",
            "reset:pre_pop",
            "reset:post_pop",
        ]
        assert switch_slot.model == _MODEL_NEW
        # Both teardowns addressed the ONE shared key.
        assert state.sessions.reset.await_count == 2
        assert {c.args[0] for c in state.sessions.reset.await_args_list} == {_SHARED}


class TestReloadRebindDuringResetAnswers409:
    """The compare-and-set at reload's commit point.

    A slot's ``linked_session_key`` is bound OUTSIDE reload's locks -- an unbound
    cron/workflow slot gets linked when its first result is injected. So a rebind
    can land while reload's ``reset`` await is in flight: reload tore down the key
    it resolved BEFORE the rebind, but the slot now runs on a different, live
    session that never saw the reload. Returning 200 would report a stale-config
    success. Reload rechecks ``effective_session_key(slot)`` after the reset and
    answers ``session_rebound`` 409 on mismatch. The seam's ``reset:post_pop``
    point -- after the pop, before reload's post-reset checks -- is where the
    rebind is injected deterministically.
    """

    @pytest.mark.asyncio
    async def test_rebind_landing_during_reset_answers_session_rebound_409(
        self, state, slot, monkeypatch
    ):
        rebound = asyncio.Event()

        async def _interleave(point: str) -> None:
            if point == "reset:post_pop" and not rebound.is_set():
                # The reset has popped the resolved (old) session; now the slot
                # is linked to a DIFFERENT, live session out from under reload.
                slot.linked_session_key = "cron:job-9019"
                rebound.set()

        monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
            data = await resp.json()

        assert rebound.is_set()
        # Reverting the post-reset effective_session_key recheck makes reload
        # fall through to 200 here and reddens this assertion.
        assert resp.status == 409
        assert data["code"] == "session_rebound"
        # The teardown DID run on the resolved (old) key -- the recheck is about
        # the RESPONSE, not about skipping the reset.
        assert state.sessions.reset.await_count == 1
        assert state.sessions.reset.await_args_list[0].args[0] == _SESSION_KEY

    @pytest.mark.asyncio
    async def test_no_rebind_reloads_normally(self, state, slot, monkeypatch):
        """Control: with no rebind, the recheck passes and reload answers 200."""

        async def _interleave(point: str) -> None:
            return

        monkeypatch.setattr(chat_handlers, "_test_interleave", _interleave)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200


class TestReloadRefusesAnAliasColdStartOnTheSharedSession:
    """The alias cold-start guard.

    A reload's turn-in-flight guard is SESSION-scoped, not slot-scoped: the
    session can be shared by an alias slot, whose cold-starting first turn is
    invisible to THIS slot's ``slot.running`` (a different object) and to
    ``get_provider`` (``provider.start()`` has not registered a session yet).
    ``session_key in state.running_session_keys()`` is what sees it -- the set
    folds every slot through effective_session_key. Without it, reload tears
    down a session a sibling is mid-cold-start on and returns 200; the sibling
    then registers its pre-reload provider with stale config.
    """

    @pytest.mark.asyncio
    async def test_running_turn_on_the_shared_session_refuses_reload(
        self, state, slot, monkeypatch
    ):
        # No turn on THIS slot (fresh fixture slot: task is None -> running
        # False) and no registered provider -- the two probes that would
        # otherwise catch a turn. Only the session-scoped set knows an alias
        # slot is cold-starting a turn on the shared key.
        state.sessions.get_provider = MagicMock(return_value=None)
        state.running_session_keys = MagicMock(return_value=frozenset({_SESSION_KEY}))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
            data = await resp.json()

        # Reverting the session-scoped clause lets reload fall through to reset +
        # 200 and reddens this on the status assertion.
        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        # The teardown must NOT have run -- refused before reset.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_running_session_reloads_normally(self, state, slot, monkeypatch):
        """Control: an empty running set lets reload proceed to 200."""
        state.sessions.get_provider = MagicMock(return_value=_idle_provider())
        state.running_session_keys = MagicMock(return_value=frozenset())

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with(_SESSION_KEY, skip_if_busy=True)


class TestReloadCancelsInFlightEagerSpawnBeforeReset:
    """Reload cancels the slot's in-flight eager-spawn before tearing down.

    ``schedule_eager_spawn`` cancels the slot's prior ``_eager_spawn_task`` as a
    side effect of scheduling a new one. Reload SUPPRESSES that call for a linked
    session, so a focus prefetch already mid-handshake from an earlier signal
    would otherwise survive the teardown and register an old-config session AFTER
    the reload reported success. Reload now cancels AND awaits the task before the
    reset. Reverting the cancel leaves the task pending after reload and reddens
    the assertion below.
    """

    @pytest.mark.asyncio
    async def test_in_flight_eager_task_is_cancelled_and_awaited_before_reset(
        self, state, slot, monkeypatch
    ):
        # A linked session, so reload takes the suppression branch and does NOT
        # schedule a replacement spawn -- the only cancellation is the explicit
        # pre-reset one under test.
        slot.linked_session_key = _SESSION_KEY

        started = asyncio.Event()
        settled = {"cancelled": False}

        async def _never_finishes() -> None:
            # Stands in for an eager prefetch mid-handshake: runnable, not done,
            # until cancelled.
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                settled["cancelled"] = True
                raise

        eager = asyncio.get_event_loop().create_task(_never_finishes())
        await started.wait()  # ensure it is genuinely in flight, not just created
        slot._eager_spawn_task = eager

        # Order probe: the reset must run only AFTER the eager task has settled.
        order: list[str] = []
        orig_reset = state.sessions.reset

        async def _record_reset(*a, **k):
            order.append(f"reset:eager_done={eager.done()}")
            return await orig_reset(*a, **k)

        state.sessions.reset = _record_reset

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200
        # Reverting the pre-reset cancel leaves this False (task still pending).
        assert eager.done(), "in-flight eager task was not cancelled by reload"
        assert eager.cancelled() or settled["cancelled"]
        # And it was settled BEFORE the reset ran, not concurrently.
        assert order == ["reset:eager_done=True"]


class TestReloadRechecksTurnStateAfterEagerCancelAwait:
    """A turn starting DURING the eager-cancel await is caught before the reset.

    The eager cancel+await is the only real suspension point between resolving the
    session and the reset. If the turn-in-flight guard ran BEFORE that await, a
    send could start a turn (and post an approval card) during it, and
    ``_reset_slot_session`` runs ``_unblock_pending_waits`` unconditionally --
    before its ``skip_if_busy`` decline -- so the card would be discarded even as
    the reset then declines and answers 409. Placing the eager cancel+await BEFORE
    the guard, with no await between the guard and the reset, keeps the guard's
    answer true at teardown. Reverting to guard-before-await lets the reset run and
    reddens the ``reset.assert_not_awaited()`` below.
    """

    @pytest.mark.asyncio
    async def test_turn_appearing_during_eager_await_answers_409_without_reset(
        self, state, slot, monkeypatch
    ):
        started = asyncio.Event()

        async def _turn_appears_mid_cancel() -> None:
            # Stands in for an in-flight eager prefetch. When reload cancels+awaits
            # it, a turn becomes visible on the session before control returns to
            # the guard -- exactly the window under test.
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The turn is now in flight on the shared session key.
                state.running_session_keys = MagicMock(return_value=frozenset({_SESSION_KEY}))
                raise

        eager = asyncio.get_event_loop().create_task(_turn_appears_mid_cancel())
        await started.wait()
        slot._eager_spawn_task = eager
        # Guard reads empty until the cancel above flips it.
        state.running_session_keys = MagicMock(return_value=frozenset())

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        # The teardown must NOT have run: the guard, read AFTER the await, saw the
        # turn. Guard-before-await would miss it and reach the reset.
        state.sessions.reset.assert_not_awaited()


class TestReloadRevalidatesSlotIdentityAfterEagerCancel:
    """A same-name slot replacement during the cancel-await answers 404.

    ``slot`` is resolved from ``state._slots`` before this handler takes any lock,
    and ``close_slot`` pops that mapping WITHOUT taking ``slot._lock``. So a
    same-name delete can complete while reload holds the old slot's lock, and a
    recreate can bind a NEW slot to the same session key. The app-ownership check
    was decided against the slot read before the suspension, so proceeding would
    apply that authorization to a different owner's session and tear down the
    replacement's idle session. Reload revalidates the registry entry by IDENTITY
    after the await and answers the same indistinguishable 404 a missing slot
    gets. Dropping the identity check lets the teardown run and reddens the
    assertions below.
    """

    @pytest.mark.asyncio
    async def test_same_name_replacement_during_cancel_await_answers_404(
        self, state, slot, monkeypatch
    ):
        started = asyncio.Event()
        replacement = _ChatSlot(_SLOT)

        async def _replace_slot_mid_cancel() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A same-name delete + recreate lands while reload is suspended:
                # the registry entry is now a DIFFERENT object under the same key.
                state._slots[_SLOT] = replacement
                raise

        eager = asyncio.get_event_loop().create_task(_replace_slot_mid_cancel())
        await started.wait()
        slot._eager_spawn_task = eager

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")
            data = await resp.json()

        # The replacement really did take the name.
        assert state._slots[_SLOT] is replacement
        # Identity, not presence: a slot IS registered under this name, so a
        # presence test would pass here and the teardown would proceed.
        assert resp.status == 404
        assert data["code"] == "slot_not_found"
        # The replacement owner's session must be untouched.
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreplaced_slot_reloads_normally(self, state, slot, monkeypatch):
        """Control: the same registry entry after the await proceeds to 200."""
        started = asyncio.Event()

        async def _plain_prefetch() -> None:
            started.set()
            await asyncio.Event().wait()

        eager = asyncio.get_event_loop().create_task(_plain_prefetch())
        await started.wait()
        slot._eager_spawn_task = eager

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{_SLOT}/reload")

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with(_SESSION_KEY, skip_if_busy=True)
