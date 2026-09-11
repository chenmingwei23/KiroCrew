"""Write the ACP turn lifecycle to an append-only per-session ledger.

See ``docs/system-specs/modules/session-ledger-emitter.md`` for the contract this
module implements. In short: each entry point is a one-line call at a site in the
dashboard chat path where a lifecycle fact is already known, and every one of
them is **fail-soft** -- a ledger error is reported once and then swallowed, so a
broken ledger can never break a turn.

The whole module is inert unless ``KIROCREW_SESSION_LEDGER`` is truthy. With the
flag off nothing is created and every call returns immediately.

A turn is a **thread**: ``on_turn_started`` anchors one, and every entry that
belongs to that turn carries the anchor's ``seq`` as its ``thread``, so the
storage layer's own grouping answers "what happened in this turn" without a fold.

Three things are recorded differently from a naive reading of the lifecycle, all
deliberate and all explained in the spec:

* ``compaction/applied`` carries context-usage **percentages**, because the
  compaction boundary never learns a raw token count.
* A tool call and an approval are identified by an id inside ``data``, not by
  ``ref``. A ``Ref`` is a citation of another ledger's lines, and a tool call id
  names a frame on the ACP stream, which is not a ledger unit.
* A turn that dies before its terminal event writes no ``turn/completed``, and a
  recovery re-entry anchors its own thread carrying ``depth``. A started entry
  with no completion is how an aborted turn is recorded.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from kiro_crew.constants import env_flag_enabled

logger = logging.getLogger(__name__)

#: Turning this on is a separate change from landing the emitter.
SESSION_LEDGER_ENV = "KIROCREW_SESSION_LEDGER"

_KIND = "session"

#: Facts read off the ACP stream vs. facts the gateway decided by itself.
_SRC_ACP = "acp"
_SRC_GATEWAY = "gateway"

#: Used when a call site has no agent name, matching the repo's own default.
_DEFAULT_AGENT = "kirocrew"

#: Who caused a turn to run. ``_start_next_queued_turn`` already distinguishes
#: these; anything unrecognised is recorded as ``other`` rather than guessed.
ACTORS = frozenset({"user", "crew", "cron", "autonudge", "subagent", "other"})

#: Bounded so a long-lived gateway cannot grow any map without limit.
_MAX_OPEN_LEDGERS = 128
_MAX_PENDING_TOOLS = 512

_lock = threading.Lock()
_open: "OrderedDict[str, Any]" = OrderedDict()
_turn_anchor: "OrderedDict[str, int]" = OrderedDict()
_tool_started: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_ledger_cls: Any = None
_import_failed = False
_warned = False


def enabled() -> bool:
    """True when the emitter should write. Read per call, never cached."""
    return env_flag_enabled(SESSION_LEDGER_ENV)


def reset_caches() -> None:
    """Drop cached handles, anchors and timings. Tests and gateway restart."""
    global _warned
    with _lock:
        _open.clear()
        _turn_anchor.clear()
        _tool_started.clear()
        _warned = False


def session_id_of(client: Any) -> str:
    """The ACP session id behind a session handle, or ``""`` when it has none.

    A provider exposes it as ``session_id`` and the inner client as
    ``_session_id``; a turn that failed before ``session/new`` has neither, and
    an empty id makes every call in this module a no-op.
    """
    for candidate in (client, getattr(client, "client", None)):
        if candidate is None:
            continue
        for attr in ("session_id", "_session_id"):
            value = getattr(candidate, attr, "")
            if isinstance(value, str) and value:
                return value
    return ""


def _report(what: str, exc: BaseException) -> None:
    """Report a ledger failure once at warning level, then stay quiet."""
    global _warned
    with _lock:
        first = not _warned
        _warned = True
    if first:
        logger.warning(
            "session ledger writes are failing (%s: %s%s); further failures "
            "are logged at debug only",
            what,
            exc,
            f", code={getattr(exc, 'code', '')}" if getattr(exc, "code", "") else "",
        )
    else:
        logger.debug("session ledger %s failed", what, exc_info=True)


def _ledger_class() -> Any:
    """The storage class, or None when the library is unavailable."""
    global _ledger_cls, _import_failed
    if _ledger_cls is not None or _import_failed:
        return _ledger_cls
    try:
        from kiro_crew.ledger import Ledger
    except Exception as exc:  # pragma: no cover - the package ships beside this
        _import_failed = True
        _report("importing kiro_crew.ledger", exc)
        return None
    _ledger_cls = Ledger
    return _ledger_cls


def _bound(store: "OrderedDict[Any, Any]", limit: int) -> None:
    while len(store) > limit:
        store.popitem(last=False)


def _remember(session_id: str, ledger: Any) -> None:
    with _lock:
        _open[session_id] = ledger
        _open.move_to_end(session_id)
        _bound(_open, _MAX_OPEN_LEDGERS)


def _handle(session_id: str) -> Any:
    """An open ledger for *session_id*, or None. Never creates one.

    An event for a session whose ledger was never opened is dropped rather than
    starting a ledger with no header.
    """
    if not session_id or not enabled():
        return None
    with _lock:
        cached = _open.get(session_id)
        if cached is not None:
            _open.move_to_end(session_id)
            return cached
    cls = _ledger_class()
    if cls is None:
        return None
    try:
        if not cls.exists(_KIND, session_id):
            return None
        ledger = cls.open(_KIND, session_id)
    except Exception as exc:
        _report("opening a ledger", exc)
        return None
    _remember(session_id, ledger)
    return ledger


def _anchor(session_id: str) -> int | None:
    with _lock:
        return _turn_anchor.get(session_id)


def _write(
    session_id: str,
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str = _SRC_ACP,
    in_turn: bool = True,
) -> Any:
    """Append one entry, threaded under the current turn. None on any failure."""
    ledger = _handle(session_id)
    if ledger is None:
        return None
    try:
        return ledger.append(
            entry_type,
            data,
            src=src,
            thread=_anchor(session_id) if in_turn else None,
        )
    except Exception as exc:
        _report(f"appending {entry_type}", exc)
        return None


def on_session_opened(
    session_id: str,
    *,
    agent: str = "",
    slot: str = "",
    model: str = "",
    cwd: str = "",
    owner: str = "default",
    resumed: bool = False,
) -> None:
    """Create the ledger if this session has none, then echo its header.

    Called once per turn, because the per-turn session claim is where the ACP
    id becomes known -- but an entry is written only when there is something new
    to say: the ledger was just created, or this claim RE-ATTACHED to an existing
    conversation (a new gateway process taking over the same session id). A warm
    reuse of a session already carrying a ledger adds nothing, so it is silent.

    ``owner`` and ``agent`` are header fields, written once at create time and
    never rewritten. A resumed session id reuses its existing ledger and
    appends, so an agent or model switch that kept the conversation continues
    one log rather than starting a second one. ``model`` is not a header field
    in the storage schema, so it is carried on this entry instead.
    """
    if not session_id or not enabled():
        return
    cls = _ledger_class()
    if cls is None:
        return
    created = False
    try:
        if cls.exists(_KIND, session_id):
            ledger = cls.open(_KIND, session_id)
        else:
            ledger = cls.create(
                _KIND,
                session_id,
                owner=owner or "default",
                agent=agent or _DEFAULT_AGENT,
                slot=slot or None,
                cwd=cwd or None,
            )
            created = True
    except Exception as exc:
        _report("creating a ledger", exc)
        return
    _remember(session_id, ledger)
    with _lock:
        _turn_anchor.pop(session_id, None)
    if not (created or resumed):
        return
    try:
        ledger.append(
            "session/opened",
            {
                "agent": agent or _DEFAULT_AGENT,
                "slot": slot,
                "model": model,
                "cwd": cwd,
                "owner": owner or "default",
                "resumed": bool(resumed),
            },
            src=_SRC_GATEWAY,
        )
    except Exception as exc:
        _report("appending session/opened", exc)


def on_turn_started(
    session_id: str,
    turn: int,
    actor: str = "user",
    *,
    depth: int = 0,
) -> None:
    """Anchor a turn's thread. ``turn`` is the message-boundary ordinal."""
    if not session_id:
        return
    with _lock:
        _turn_anchor.pop(session_id, None)
    entry = _write(
        session_id,
        "turn/started",
        {
            "turn": int(turn),
            "actor": actor if actor in ACTORS else "other",
            "depth": int(depth),
        },
        src=_SRC_GATEWAY,
        in_turn=False,
    )
    seq = getattr(entry, "seq", None)
    if isinstance(seq, int):
        with _lock:
            _turn_anchor[session_id] = seq
            _turn_anchor.move_to_end(session_id)
            _bound(_turn_anchor, _MAX_OPEN_LEDGERS)


def on_turn_completed(
    session_id: str,
    turn: int,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    credits: float = 0.0,
    duration_ms: int = 0,
    stop_reason: str = "",
    model: str = "",
    provider: str = "",
    depth: int = 0,
) -> None:
    """Record a turn's terminal event and what it cost."""
    _write(
        session_id,
        "turn/completed",
        {
            "turn": int(turn),
            "depth": int(depth),
            "stop_reason": stop_reason,
            "duration_ms": int(duration_ms),
            "credits": float(credits),
            "model": model,
            "provider": provider,
            "tokens": {
                "input": int(input_tokens),
                "output": int(output_tokens),
                "cache_read": int(cache_read_tokens),
                "cache_write": int(cache_write_tokens),
            },
        },
    )
    if session_id:
        with _lock:
            _turn_anchor.pop(session_id, None)


def on_tool_called(
    session_id: str,
    turn: int,
    *,
    name: str,
    server: str = "",
    kind: str = "",
    call_id: str = "",
) -> None:
    """Record a tool call by its id. Arguments are never recorded."""
    if session_id and call_id and enabled():
        with _lock:
            _tool_started[(session_id, call_id)] = (time.monotonic(), name, server)
            _bound(_tool_started, _MAX_PENDING_TOOLS)
    _write(
        session_id,
        "tool/called",
        {
            "turn": int(turn),
            "call_id": call_id,
            "name": name,
            "server": server,
            "kind": kind,
        },
    )


def on_tool_completed(
    session_id: str,
    turn: int,
    *,
    name: str = "",
    server: str = "",
    status: str = "",
    call_id: str = "",
) -> None:
    """Record a tool call's terminal frame. Results are never recorded.

    The terminal frame does not repeat the tool's identity -- only the call
    frame carries the trusted name and server -- so both are remembered per
    call id and filled in here rather than being recorded empty.
    """
    elapsed_ms = -1
    if session_id and call_id:
        with _lock:
            started = _tool_started.pop((session_id, call_id), None)
        if started is not None:
            began, called_name, called_server = started
            elapsed_ms = max(0, int((time.monotonic() - began) * 1000))
            name = name or called_name
            server = server or called_server
    data: dict[str, Any] = {
        "turn": int(turn),
        "call_id": call_id,
        "name": name,
        "server": server,
        "status": status,
    }
    if elapsed_ms >= 0:
        data["elapsed_ms"] = elapsed_ms
    _write(session_id, "tool/completed", data)


def on_approval_requested(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    tool: str = "",
) -> None:
    """Record that a tool call is waiting on a human."""
    _write(
        session_id,
        "approval/requested",
        {"turn": int(turn), "approval_id": approval_id, "tool": tool},
        src=_SRC_GATEWAY,
    )


def on_approval_decided(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    decision: str,
) -> None:
    """Record how an approval resolved, including a timeout.

    This runs on the task that handled the click, not the task running the
    turn, which is why it must never raise.
    """
    _write(
        session_id,
        "approval/decided",
        {"turn": int(turn), "approval_id": approval_id, "decision": decision},
        src=_SRC_GATEWAY,
    )


def on_model_selected(session_id: str, model: str, source: str = "") -> None:
    """Record the model a session will serve and why it was chosen."""
    _write(
        session_id,
        "model/selected",
        {"model": model, "source": source},
        src=_SRC_GATEWAY,
    )


def on_compaction_applied(
    session_id: str,
    *,
    pct_before: float,
    pct_after: float,
) -> None:
    """Record a compaction as context-usage percentages.

    Compaction is session-scoped rather than per-turn, so no turn ordinal is
    recorded; the entry threads under the live turn when one is open. The
    compaction boundary measures ``provider.context_usage_pct()`` and never
    learns a raw token count, so this records what the site knows.
    """
    _write(
        session_id,
        "compaction/applied",
        {
            "pct_before": round(float(pct_before), 4),
            "pct_after": round(float(pct_after), 4),
            "freed_pct": round(float(pct_before) - float(pct_after), 4),
        },
        src=_SRC_GATEWAY,
    )


def on_session_closed(session_id: str, reason: str) -> None:
    """Record a session teardown and drop its cached state.

    ``reason`` is the gateway's own ``end_reason`` (``reset``, ``shutdown``,
    ``evicted``, ``destroyed`` and the rest), recorded verbatim rather than
    remapped onto a second vocabulary.
    """
    _write(
        session_id,
        "session/closed",
        {"reason": reason},
        src=_SRC_GATEWAY,
        in_turn=False,
    )
    if not session_id:
        return
    with _lock:
        _open.pop(session_id, None)
        _turn_anchor.pop(session_id, None)
        for key in [k for k in _tool_started if k[0] == session_id]:
            _tool_started.pop(key, None)


__all__ = [
    "ACTORS",
    "SESSION_LEDGER_ENV",
    "enabled",
    "on_approval_decided",
    "on_approval_requested",
    "on_compaction_applied",
    "on_model_selected",
    "on_session_closed",
    "on_session_opened",
    "on_tool_called",
    "on_tool_completed",
    "on_turn_completed",
    "on_turn_started",
    "reset_caches",
]
