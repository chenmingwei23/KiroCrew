"""Answer "which ledger unit does this slot's work belong to?".

:mod:`kiro_crew.session_ledger_emit` keys every entry by an ACP session id,
because the turn path holds one: the runner has the client in hand and reads the
id straight off it. Several sites that produce facts about a session do NOT hold
that client -- a background model call charged to the session, a subagent spawned
by it, the agent's own task list -- and carry only the key naming the slot or the
session. This module is the one place that gap is closed.

**What the resolver may claim.** A slot owns exactly one ACP session id AT A
TIME, not for its whole life. A plain resume reuses the persisted id
(``AcpSessionHandle`` replays it into ``session/load``), but a reset, an
agent/model/effort switch, a compaction that recycles the session, and a provider
swap all tear the ACP session down, and the successor cold-starts a NEW id --
``session_lifecycle`` says so at the teardown itself. So the honest question this
module answers is "which unit is this slot's work landing in *now*", and the
answer is only valid at the moment it is asked. That is exactly the guarantee an
emit site needs, because every entry records what was observed at that site when
it was observed; it is NOT enough to reconstruct which unit some earlier fact
went to, and no caller should use it that way.

**Read-only, and synchronous.** Both lookups are attribute reads on objects the
caller already holds, so this never awaits and never touches disk. The persisted
:class:`~kiro_crew.session_map.SessionMap` is deliberately NOT consulted: its
``get`` repairs or removes an entry it finds stale, which is a write, and this
module is called from paths that must not mutate session state as a side effect
of describing it. It also answers only for ids that reached disk, so it would
miss exactly the live session the caller is asking about.

**Unknown is an answer.** Every function returns :data:`UNKNOWN` (the empty
string) when the key has no live ACP session -- a slot that has never run a turn,
one whose session was torn down and not yet re-created, a key naming nothing. The
emitter treats an empty session id as a no-op, so an unresolved key drops the
entry rather than filing it under a guess. A ledger that omits a fact is behind;
one that attributes a fact to the wrong session is wrong, and nothing downstream
can tell.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.session_ledger_emit import session_id_of

__all__ = ["UNKNOWN", "unit_for_slot", "unit_for_session_key"]

#: What every function here returns when the key has no live ACP session. The
#: emitter's own no-op guard is ``not session_id``, so this value flows straight
#: through as "do not write" without the caller needing a second branch.
UNKNOWN = ""


def _live_client(slot: Any) -> Any:
    """The ACP client the slot is running a turn on, or None.

    ``_acp_client`` is published on the slot as a turn begins and cleared in the
    turn's ``finally``, so this is populated only DURING a turn. That is the
    window in which it is also the most authoritative source available: it is the
    very object the runner reads its own ``_ledger_sid`` from, so a site resolving
    through here inside a turn lands in the same unit as that turn's own entries.
    """
    return getattr(slot, "_acp_client", None)


def unit_for_slot(state: Any, slot_key: str) -> str:
    """The ledger unit *slot_key*'s work is landing in now, or :data:`UNKNOWN`.

    Two levels, in this order:

    1. The slot's live ACP client, when a turn is running. Same object, same id
       as the runner's own.
    2. The session registry, keyed by the slot's effective session key. This is
       what covers a site firing OUTSIDE any turn -- a background title pass, a
       terminal subagent report -- where level 1 is None but the session itself is
       very much alive.

    The effective session key matters rather than the slot key: a channel-born
    slot runs its turns on the channel's own session, so folding the slot key
    would address a session that does not exist while the real one sits under
    ``slack:<ts>``.
    """
    if not slot_key or state is None:
        return UNKNOWN
    try:
        slot = state.get_slot(slot_key)
    except Exception:
        return UNKNOWN
    if slot is None:
        # No slot under this name. It may still be a bare session key (a channel
        # conversation with no tab open), which the session registry can answer.
        return unit_for_session_key(getattr(state, "sessions", None), slot_key)
    live = session_id_of(_live_client(slot))
    if live:
        return live
    try:
        from kiro_crew.dashboard.chat_utils import effective_session_key
    except Exception:  # pragma: no cover - import cycle guard, not a runtime path
        return UNKNOWN
    try:
        session_key = effective_session_key(slot)
    except Exception:
        return UNKNOWN
    return unit_for_session_key(getattr(state, "sessions", None), session_key)


def unit_for_session_key(sessions: Any, session_key: str) -> str:
    """The ledger unit *session_key*'s provider is serving, or :data:`UNKNOWN`.

    For callers that hold a SessionManager and a session key but no dashboard
    state -- the subagent manager is the case that matters, since a subagent's
    parent is named to it as a key and it never sees the parent's slot.

    ``get_provider`` is an exact registry lookup, so a key naming nothing answers
    None and this answers :data:`UNKNOWN`. One retry is allowed, and only under a
    premise that makes it a lookup rather than a guess: a key containing no colon
    cannot already be a namespaced session key (``dashboard:``, ``slack:``,
    ``subagent:`` all carry one), so a bare slot name is retried in its dashboard
    form. A key that already carries a namespace is never rewritten -- doing so is
    how ``slack:<ts>`` becomes the nonexistent ``dashboard:slack:<ts>``.
    """
    if not session_key or sessions is None:
        return UNKNOWN
    get_provider = getattr(sessions, "get_provider", None)
    if not callable(get_provider):
        return UNKNOWN
    try:
        found = session_id_of(get_provider(session_key))
    except Exception:
        return UNKNOWN
    if found or ":" in session_key:
        return found
    try:
        return session_id_of(get_provider(f"dashboard:{session_key}"))
    except Exception:
        return UNKNOWN
