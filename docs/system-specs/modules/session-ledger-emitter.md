# Session Ledger Emitter

`session_ledger_emit.py` turns the gateway's ACP turn lifecycle into the append-only
per-session ledger `kiro_crew.ledger` keeps for each ACP session id. It is a **writer
only**: it owns which facts matter and where they are known, not storage or its layout.

The emitter is gated behind `KIROCREW_SESSION_LEDGER=1` (`constants.env_flag_enabled`,
read per call, truthy set `{1, true, yes, on}`) and defaults **OFF**, so this module is
inert until a later change turns it on. With the flag off no ledger directory is created
and no call reaches the storage library.

## Identity

The ledger key is the **ACP session id**, not the slot key. A slot outlives its ACP
session: an agent switch, a model switch, or a project change calls
`SessionLifecycle.reset`, which pops the live session and lets the next turn cold-start a
fresh one. When that reset cleared the conversation the successor has a new id and a new
ledger; when it did not, the next cold start resumes the same id via `session/load`, so
`Ledger.exists` decides between create and open and a resumed session never truncates it.

`owner` and `agent` are header fields, written once at create time. There is no turn id in
the repo, so a turn is identified by its message boundary, `len(slot.messages)` at turn
start, and is also a **thread**: `turn/started` anchors one and every later entry in that
turn carries the anchor's `seq`, so `thread_page` answers what one turn did with no fold.

## Emitted facts

| Fact | Site | Data |
|---|---|---|
| `session/opened` | after `get_or_create`, on create or re-attach only | agent, slot key, model, cwd, `resumed` |
| `turn/started` | turn-start block in `_run_chat`, beside `_turn_t0` | turn ordinal, actor, prompt depth |
| `turn/completed` | the `EVENT_COMPLETE` arm, beside `_emit_turn_metric` | the four `TurnUsage` token counts, credits, `duration_ms`, `stop_reason`, model, provider |
| `tool/called` | `EVENT_TOOL_CALL` | `tool_call_id`, trusted `tool_name`, `mcp_server_name`, kind |
| `tool/completed` | `EVENT_TOOL_RESULT` when `tool_final` | same id, outcome, elapsed ms measured by the emitter |
| `approval/requested`, `approval/decided` | `ApprovalCoordinator.request` / `resolve` | approval id, tool name, decision |
| `model/selected` | the fallback swap in `_run_chat`, after the pick lock is released | model id, source |
| `compaction/applied` | `_settle_compact_cooldown`, once the after-reading is confirmed | `pct_before`, `pct_after`, freed |
| `session/closed` | `SessionLifecycle.reset` | the gateway's own `end_reason` |

Actor is derived in `_run_chat` from the nudge self-wake flag and the cron and subagent
message prefixes, the same signals the dispatch classifier uses; reading the queue
entry's `kind` would be stronger and needs the drain to pass it down. `approval/*` is
implemented and tested but not wired: the approval coordinator holds only a slot key.

## What is deliberately not recorded

No message bodies, no tool arguments, and no tool results. A tool call is identified by
its `tool_call_id` inside `data`, not by `ref`: a `Ref` cites lines of another ledger, and
an ACP frame is not a ledger unit. That id is the join key the transcript already uses.

A tool's `name` and `server` carry ONLY the trusted `_meta.kiro` identity, so both are
empty when the backend supplies none, and the call id still joins the entry to the
transcript. Neither `title` nor `wire_title` is used as a fallback: for a shell tool those
are model-authored, and a log whose record of what ran can be written by the model is
worth less than one that admits it does not know.

`compaction/applied` carries **percentages, not token counts**: that boundary measures
`provider.context_usage_pct()`, never raw tokens. The session's STARTING model is not a
`model/selected` entry: it resolves before the session id exists, so it rides on
`session/opened`, while the model a turn served rides on `turn/completed`.

A turn that dies before `EVENT_COMPLETE` writes no `turn/completed`. That is the intended
encoding: in an append-only log a `turn/started` with no matching completion **is** the
record of an aborted turn, and synthesizing one would assert an outcome nobody observed. A
recovery re-entry writes its own pair rather than folding into the original turn, and
`depth` tells them apart. Tombstones, projections, a reader, and any UI are out of scope.

## Failure policy

Every entry point is fail-soft and returns `None`. A storage error is caught, reported
once per process at warning level, and demoted to debug afterwards so a broken ledger
cannot flood the log. This matters at three sites: the approval decision resolves on the
websocket handler task, so an exception there would break the click handler and hang the
waiting turn; `_default_session_model` runs inside `asyncio.to_thread` behind a
swallow-all `except`; and `_fallback_swap_for_turn` holds `slot._model_pick_lock`. The
emitter is called from the callers of the last two, never inside them, and never holds a
lock or awaits I/O on the gateway event loop.

`kiro_crew.events` is not a second emitter to reconcile with: its only constructor today
is the read-only `events/backfill.py` validator, and this module does not route through it.
