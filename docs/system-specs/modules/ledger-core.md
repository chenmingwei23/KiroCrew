# Ledger Core

Owners: `kiro_crew.ledger` (`schema.py`, `store.py`, `errors.py`)

## 1. Purpose

`kiro_crew.ledger` gives a crew or a session one durable, ordered, citable record of what happened. It is the storage layer only: it defines a file format, enforces who may write what into it, and reads it back. It carries no routes, no MCP tools, no dashboard surface and no migration.

The problem it answers is that long-horizon work keeps its state in a context window, which harness-owned compaction summarizes lossily. Transcripts are not a substitute: rotation, compaction and consolidation rewrite the whole file, the grain is a message rather than an operation, and no field defines an order a consumer can fold on. An append-only file with a writer-assigned sequence inverts that -- the record is the authority, the context is a cache -- and lets one unit cite a segment of another's history instead of copying it.

## 2. Relationship to `kiro_crew.events`

These are two layers of one story, not two competing logs, and the split is deliberate.

`kiro_crew.events` is a **global lifecycle stream**: one envelope (`v`, `kind`, `src`, `key`, `ts_ms`, `data`), day-sharded under `events/`, joined across domains by its `key`. Its own contract records that it carries no ordering field because ordering "needs a defined scope (per writer? per key? global?)" and would arrive "with the first emitter". `kiro_crew.ledger` is a **per-unit record**: one file per crew or session, where the scope question is already answered by the file itself, so `seq` is contiguous *within that file* and means something a global stream cannot give it.

That per-file scope is what the ledger adds, and it is why the two are not merged. `seq`, `thread` (a grouping key naming an earlier seq in the SAME file) and `ref` (a pointer into another file) are all defined relative to a single unit's ledger. Putting them in the global stream would require either a per-key sequencer inside a day-sharded multi-domain file, or a `seq` whose scope varies by `kind` -- the ambiguity that stream deliberately refused.

Which stream a future emitter writes:

| Emitter records | Stream | Why |
|---|---|---|
| One unit's own history, needing order, threading or citation | `kiro_crew.ledger` | The ordering scope is the unit's file. |
| A cross-domain lifecycle fact folded by correlation key | `kiro_crew.events` | No per-unit order is needed; the join axis is `key`. |

No emitter double-writes. The two envelopes are field-compatible on purpose -- the ledger's `type` is the stream's `kind`, its `time` is `ts_ms`, both `domain/action`, both epoch milliseconds -- so a projection that wants one timeline folds both with a field rename and no semantic translation.

## 3. Storage and identity

```
<data home>/crews/<store name>/ledger.jsonl
<data home>/sessions/<store name>/ledger.jsonl
.lock                                        # sibling, per ledger
```

`<store name>` is `session_ledger._store_name()` -- a readable fold plus a digest of the exact id -- and the raw id lives in the header. The id is deliberately not the directory name: a channel session key legitimately carries a colon (`slack:1712793600.123`), which POSIX accepts and Windows refuses, so the raw id as a filename turns a sanctioned id into an `OSError` on a supported platform. Identity is the digest, so `Foo` and `foo` get distinct directories on a case-insensitive filesystem. `store.ledger_dir()` refuses a separator or NUL in the raw id and requires the resolved path to stay below the root, so a folded name cannot traverse. `Ledger.open()` proves it reached the right unit by checking the id the header stores.

A ledger is non-recursive inside its root. Every reader of the flat `sessions/<key>.jsonl` transcripts globs one level deep, so the two stores share the `sessions` root without shadowing each other -- and session ledgers inherit the sandbox's existing `sessions` deny. The `crews` root is its own entry in `security.paths._CREW_SECRET_LEAVES`: the write-side rules below bind callers who go through the library, so without that floor an agent's own file tools could forge an entry attributed to `src:"gateway"` or rewrite the history a conductor is designed to trust.

## 4. Format

Line 1 is the header; every later line is an entry.

```json
{"type":"crew","version":1,"id":"qa","name":"QA Crew","template":null,"createdAt":1789000000000}
{"type":"crew:qa/report","seq":388,"time":1789000000000,"src":"crew:qa","thread":120,
 "ref":{"unit":"session","id":"s-7f3a","from":40,"to":96},"data":{"item":"pr-4127","status":"done"}}
```

| Field | Meaning |
|---|---|
| `type` | `domain/action`, or a guest-namespaced `crew:<name>/<action>` / `app:<name>/<action>`. |
| `seq` | Contiguous from 1 after the header. Writer-assigned. |
| `time` | Epoch milliseconds. Writer-assigned. |
| `src` | `gateway`, `acp`, `dashboard`, `patrol`, `session:<id>`, `crew:<name>` or `app:<name>`. |
| `thread` | Optional. The seq of an earlier entry in this same file -- a grouping key, like a chat thread id. |
| `ref` | Optional. `{unit, id, from, to?}`, a pointer to a segment of another (or the same) ledger. `to` absent means one line. |
| `data` | A JSON object. |

A session header also carries `owner`, `agent`, and the optional `task`, `pack`, `slot`, `thread` (`{crew, seq}`), `cwd` and `remote`.

## 5. Rules

Every refusal is a `LedgerError` carrying a stable `code`; the codes are API surface and are additive-only.

**Ownership** answers whether a kind of unit has such events at all. `schema.TYPE_OWNERSHIP` maps kind to owned `type` domains -- crew: `member` `activity` `slot` `patrol` `message` `crew` `item` `memory`; session: `session` `turn` `step` `tool` `approval` `model` `compaction` `remote` -- and anything else is `event_type_not_owned`. It is prefix-based, so a new action under an owned domain needs no change.

**Namespacing** answers whether an emitter may write it. A `crew:<name>` or `app:<name>` emitter is a guest: it may write only under its own prefix, and only into a crew ledger, else `namespace_violation`. A guest type is judged by this rule *instead of* ownership, which is why the registry needs no guest entries -- a guest's own name is its permission.

The remaining bounds: `thread` must name an existing, parseable, earlier seq (`bad_thread`); `ref` must be well-formed and span at most `MAX_REF_SPAN` lines (`bad_ref`); a serialized entry must be at most `MAX_ENTRY_BYTES` (`entry_too_large`). Caps refuse rather than truncate, leaving the file byte-identical -- a clipped record the caller believes landed intact is a loss the caller cannot detect.

## 6. Append-only guarantee and damage

A line is never rewritten. Exactly one mutation exists: on `open`, trailing bytes that are not a complete line are dropped. Termination decides which those are, so the rule needs no heuristic. Every append writes `line + "\n"` and fsyncs, so unterminated bytes that fail to parse are a crash artifact and go; unterminated bytes that *do* parse lost only their newline, so the record stays and the next append re-supplies the separator. A terminated line that does not parse is damage inside history: reads skip it, the file keeps it. Two readers of the same bytes therefore always agree.

`seq` is read back from a bounded window at the file's end inside the per-ledger lock rather than trusted from an in-process cache, so two writers cannot both claim one number and the read costs the same on a ten-line ledger or a million-line one. `store._anchor_exists()` reuses that same window, so proving a `thread` anchor is parseable is free for a recent anchor and falls back to a scan only for one older than the window.

`resolve` requires an explicit `may_read` decision for a ref that leaves the unit; omitting it denies rather than allows, because the shortest call shape must not be the insecure one. A ref that stays inside the ledger the caller already holds needs no callback. `forbidden` is decided before existence, so a denied read and an absent unit are indistinguishable.

## 7. Retention, and what it costs

There is no rotation, and adding one later is a format change, not a config change: `seq` is contiguous from 1 within one file, and `page`/`iter_from` stream from the header. Truncating a prefix would break contiguity and orphan every `ref` and `thread` pointing into the removed span.

That is acceptable while writers are low-frequency (a crew's members, items and patrols; a session's lifecycle). It is not acceptable for a high-frequency session emitter -- `turn`, `step`, `tool` -- and the retention story has to be settled before one lands. The two shapes that preserve the guarantees are a segment directory whose files each keep their absolute `seq` range, and a `firstSeq` in the header so a truncated prefix is declared rather than inferred. Neither is implemented.

## 8. Scope

No consumer exists in the tree. Read and write paths ship together deliberately: the format's guarantees -- contiguous seq under a lock, torn-tail repair, refusal before any byte is written -- are only demonstrable with both halves, and `test/test_ledger_core.py` exercises them against real files rather than against a mock.
