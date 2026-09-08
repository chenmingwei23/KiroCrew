# Monitor architecture

## Purpose

One paradigm for every monitoring loop: watch an external subject, spend an
agent turn only when the subject has usefully changed, and stay bounded when it
never does.

This spec is the contract for the consolidation proposed in
[rfc-consolidated-monitor.md](../../request-for-change/rfc-consolidated-monitor.md).
It is written as the target, and the code does not yet match it everywhere, so
every section carries its status. Read the status table before trusting a
section as a description of what runs today.

Verified against `2f9ed9724`.

Two implementation specs sit under this one and describe what runs today:
[agent-interrupt-controller.md](agent-interrupt-controller.md) for the kernel
driving script-cron pollers, and [babysit-pr-watch.md](babysit-pr-watch.md) for
the pull-request watch built on it. Where either disagrees with a layer below,
this spec states the target and that one states the present.

| Layer | Status | Where it lives today |
|---|---|---|
| Subject and registry | `proposed` | `probes/__init__.py` maps one kind in an `if`; its own docstring says a registry was deferred |
| Probe | `partial` | typed and error-classified in `monitoring/github_pull_request.py`; per-subject on both sides, so unbatchable |
| Observation | `partial` | named entries exist in `probes/gh_pr.py`; `monitoring/` reduces a subject to one fingerprint |
| Decision | `partial` | `monitoring/decision.py` is a 119-line pure function with budgets and terminal handling, but edge-triggered and with no coalescing |
| Persistence | `partial` | versioned in `monitoring/`; unversioned in `irq.py`, which also holds decision logic |
| Driver | `implemented` | in-session timer in `autonudge.py`; out-of-session script cron in `babysit/scripts/pr_watch.py` |
| Delivery | `implemented` | session directive keyed by the call's input digest, shared by both arming paths |

## The seven layers

A monitoring loop is seven concerns, and each one has exactly one owner. The
value of the split is that six of them never learn what is being watched.

### 1. Subject

A subject is the external thing being watched. It is typed per kind and returns
a stable identity; two ticks naming the same thing must produce the same
identity, because identity is what state is filed under.

Kinds are registered as **data**, not as a branch. A new kind must not require
editing a dispatch function, because a dispatch branch is where every future
kind accumulates its special case. The current single-branch `build(kind)` is
the thing this replaces.

A subject is one addressable thing. A pipeline of several pull requests is not a
subject; it is several subjects sharing a watch.

### 2. Probe

The probe turns subjects into observations. Its signature is **plural from day
one**:

```
probe(subjects, budget) -> Mapping[SubjectId, ProbeResult]
```

`ProbeResult` carries the observations, the subject's current revision, whether
the read was complete, and a classified error or `None`.

This is the single most consequential contract in this spec. A per-subject probe
interface cannot be batched later without changing every implementation and
every caller, and batching is not a micro-optimization here: fifty subjects read
one at a time is roughly 150 process invocations against one query. Today
batching is reachable only from the single out-of-session poller, for no reason
other than the interface shape.

Rules:

- A probe that *can* batch **must**. A probe that genuinely cannot implements the
  plural signature and loops internally, so the caller never encodes the
  difference.
- One query per (host, credential) per tick. Subjects sharing a credential share
  the query.
- A partial failure degrades only the subjects it covers. One unreadable subject
  must not fail the batch.
- Every error is classified before it leaves this layer. An unclassified failure
  is `unknown` and counts as not passing.

### 3. Observation

An observation is a named entry:

```
Observation(key, severity, resets_on, brief="")
```

- **`key`** is a semantic string, stable across ticks, never a hash. `conflict`,
  `red:<check>`, `ready`, `comment:<id>`. A hash cannot be deduplicated per
  condition, cannot be coalesced with a sibling, and cannot be re-asserted,
  because nothing can tell whether two hashes describe the same condition.
- **`severity`** is `WAKE` (foldable into a coalesced wake), `TERMINAL` (an end
  state: deliver and retire the watch), or `IMMEDIATE` (bypasses the coalescing
  delay but not the budget, for a condition where waiting observes nothing
  further — a conflicted pull request dispatches no checks, so a pending count
  never drains).
- **`resets_on`** is `REVISION` when a new revision clears the condition, or
  `NEVER` when it belongs to the subject rather than the revision. A comment
  survives a force-push; a failing check does not.
- **`brief`** is operator-facing text, delivered only if the entry wakes someone.

A subject's fingerprint, where one is still needed, is **derived from** the
entries. There is one source of truth, so the two cannot disagree.

### 4. Decision

A pure function. No IO, no subprocess, no reading the clock — the clock arrives
as a value:

```
decide(entries, prior_state, budgets, now) -> Verdict
```

`Verdict` is `Quiet`, `Wake(entries, brief)`, `Terminal(entries)`, or
`Stop(reason)`.

This layer knows nothing about pull requests, hosts, or agents. That is
verifiable rather than aspirational: the existing 119-line implementation
contains zero references to GitHub and zero IO calls, and that property is what
makes it the skeleton the rest is merged into.

Evaluation order is part of the contract, because the order is what makes it
fail safe:

1. **Terminal** short-circuits everything. A merged subject is not triaged as a
   failure.
2. **Budget** exhaustion yields `Stop`. Checked before classification so an
   expensive classification cannot be what exhausts the budget.
3. **Per-key dedupe** against the re-alert window.
4. **Coalescing window**.
5. **Floor**.

The engine is **level-triggered**, not edge-triggered. Each key carries its own
alert timestamp, and a key that is still true re-asserts once its window has
elapsed. Edge triggering loses any condition that stayed true across a wake that
did not happen — a busy session, an exhausted budget — because on the next tick
it is no longer a change. Level triggering is also what the industry converged
on: a Kubernetes controller reconciles observed against desired rather than
consuming events, and Prometheus re-sends a firing alert and lets the receiver
deduplicate.

The re-alert window is what makes level triggering affordable, and the budget is
what makes it safe. A notification pipeline aimed at humans needs no token
budget because a paged human self-limits; an agent does not, which is why the
budget half of this design is not optional.

**The stall streak is engine state.** A watch whose verdict has been byte-identical
across N settled ticks with no progress is stuck, and the engine stops it. That
counter belongs in the persisted state the engine reads, not in a file that only
an instruction knows to maintain — an advisory counter maintained by prose is
lost to exactly the long-running compaction it exists to survive.

### 5. Persistence

One versioned document per watch, written atomically at mode 0600, filed under a
digest of (kind, subject identity, watch id).

Required contents:

| Field | Why |
|---|---|
| `version` | every bump ships a migration; an unrecognized version is quarantined, never guessed at |
| `revision` | what `resets_on: REVISION` is measured against |
| `alerted` | per-key alert timestamps — the level-triggered state |
| `coalescing` | the open window: when it opened, which keys joined |
| `errors` | per-kind counts, so a retryable class stays bounded |
| `budgets_spent` | turns, tokens and provider errors already charged |
| `stall` | the verdict digest and its consecutive-match count |

**The state document holds delivery bookkeeping only. It never holds subject
state.** What the subject looks like belongs in the evidence file, which is
disposable and regenerated per wake. This boundary is load-bearing in a way that
is easy to get wrong: a reader who assumes the state file holds subject state
will try to read the subject out of it and find only alert timestamps.

The document is rewritten whole and atomically, so it is a snapshot rather than
a log. Anything that needs a history needs its own append-only file.

### 6. Driver

A driver decides when a tick happens. Two are supported, and the difference
between them is a **capability**, not a configuration preference:

| Driver | Owns a chat slot | Can inject a turn | Runs with session trust |
|---|---|---|---|
| In-session timer | yes | yes | yes |
| Out-of-session poller | no | no, notification only | no, deny-by-default |

The rule that follows is absolute: **an out-of-session driver is a detector,
never a reactor.** A cron turn has no owning slot, so its tool calls land on a
deny-by-default approval path and time out — while a denied tool inside a
completed turn still records the job as healthy. A design in which a cron fixes
something reports success and does nothing.

Both drivers are needed. Out-of-session detection reaches subjects with no live
session, and in-session injection is the only path that can act.

The in-session timer counts its interval from the end of its own last cycle
toward a fixed deadline, so a user message defers a due fire without restarting
the countdown. The real cadence is therefore the interval plus each cycle's own
duration, which callers must size for.

### 7. Delivery and turn injection

A verdict becomes at most one agent turn, through a fixed sequence:

1. Spill the evidence to a file.
2. Inject **one** turn carrying a summary and the path to that evidence — never
   the raw payload. A wake that inlines its evidence pays for it in the session's
   context on every subsequent turn, because history is replayed.
3. Charge the wake **after** the turn completes and its usage is known.

Degradation is a ladder, and each rung is a different outcome rather than a
retry of the one above: a live slot takes the turn; a busy slot queues it; a slot
that is gone gets a notification instead. Headless delivery cannot start a
session, so a watch whose session is gone must not claim it woke anyone.

Deduplication is per (subject, key, revision): one wake per condition per
revision, and a re-alert only through the window. Transport is the session
directive selected by the digest of the call's own input, which both arming paths
already share.

## Rules the engine enforces, not the prose

An operational rule that lives only in an instruction can be violated silently.
These are code:

- An unclassified provider state is `unknown` and counts as **not passing**.
- Superseded attempts collapse to the newest per check identity. A host keeps
  cancelled earlier attempts in its rollup, and counting them reports a live
  failure that no longer exists. The two current implementations **disagree on
  this today**: the skill's status tool collapses to the newest attempt per
  identity, while the structured provider gives every check row its own group key
  and maps `CANCELLED` to failed, so it can wake on a phantom failure.
- A published aggregate verdict is authoritative over the individual rows. A
  reader that only enumerates rows can report green while the aggregate is
  pending, which is not a hypothetical: a subject has been observed with every
  individual check complete and green while the aggregate context still read
  pending.
- A stale reviewer stamp is an entry (`stale:<name>`), not a paragraph.
- An un-dispositioned finding is an entry, so readiness cannot be declared over
  one.
- The stall streak is engine state, so a stuck watch stops itself.

## Adding a new monitored kind

The extensibility test is mechanical: adding a kind must not touch layers 4
through 7.

1. Define the subject type with a stable `identity()`.
2. Register the kind as data in the registry.
3. Implement the plural probe. Emit **named entries**. Do not emit a fingerprint;
   the shared layer derives one.
4. Add nothing to the decision engine. If a new kind seems to need a change
   there, the entry vocabulary is wrong — express the condition as a key and a
   severity instead.
5. Ship fixtures and a golden entry table: for each fixture, the exact entries
   expected.
6. Prove it: the decision engine's own tests pass **unchanged**. That is what
   demonstrates the kind is pluggable rather than special-cased.

A kind that cannot be added without editing layer 4 is a design defect in this
spec, and should be reported as one rather than worked around with a branch.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| One fingerprint per subject | cannot say what changed, cannot coalesce siblings, cannot re-assert a condition |
| Per-subject probe signature | cannot be batched later without changing every caller |
| Subject knowledge in the decision layer | every new kind then needs a branch there, and the layer stops being testable in isolation |
| Subject state in the state document | the document is a snapshot of delivery bookkeeping; a reader looking for subject state finds timestamps |
| A cron that reacts | no owning slot means deny-by-default tool calls that time out while reporting healthy |
| Conflating a throttle with a fault | a secondary rate limit can be refused while the primary counters read full, so a loop driven off an exit code escalates a transient throttle or treats it as terminal |
| Treating a cycle cap as a finish line | a loop that stops at its cap is indistinguishable from one that converged early, and bills for the difference |

## Known deviation

Every wake re-injects into the **same** session, so its context grows for the
life of the watch. The prevailing pattern elsewhere is a fresh context per wake,
and a durable-execution engine names the timer loop accumulating one history as
an anti-pattern outright. Both current implementations share this deviation, and
it is not resolved here: the change is larger than this consolidation and belongs
in its own proposal. It is recorded so a reader does not mistake the omission for
an argument that same-session wakes are correct.
