# Memory v2 — design, decisions, and handoff

> Archived handoff for the memory v2 work. Owning specs:
> [memory-skills-hooks](../../../../system-specs/modules/memory-skills-hooks.md),
> [security](../../../../system-specs/modules/security.md),
> [learn-cron-dashboard](../../../../system-specs/modules/learn-cron-dashboard.md).
> Those specs are authoritative for current behaviour. This file records what they
> cannot: why each choice was made, which conclusions were corrected mid-flight, and
> what a following session should pick up.

## 1. What memory v2 is

Before this work, memory was **one shared pool that no crew owned and that nothing
backed up**. A crew could be given a `memory_store` name in `config.json`, but the
field was decoration: nothing read it on most paths, no surface could create a store,
and the dashboard showed the default store while reading as if it showed everything.

Memory v2 makes memory **per-crew, durable, and inspectable**, under one hard
constraint that shaped every decision:

> The operator's existing global memory must not move, and must keep behaving exactly
> as it did.

That constraint is the reason for the two-lineage design in §2 and for the
absent-parameter rule in §7. It is also why `test/test_memory_v1_golden.py` is the
acceptance proof: it is never edited, and it passing unedited is the claim.

**Scope note.** "Memory v2 is crew-members-only": silos activate only on an explicit,
non-default `memory_store` binding. An install that sets nothing keeps byte-identical
behaviour. This was a standing requirement, not an optimisation.

## 2. Two schema lineages, one engine

A memory store is a **file boundary**, not a column. There is no `workspace_id`, no
tenant column, and no cutover.

| | default store | crew silo |
|---|---|---|
| `schema_version` | `1`, `2`, `3` (v1 migrations) | `1001` (`CREW_SCHEMA_VERSION`) |
| tables | `semantic_memory`, `episodic_memories`, … | `memory_items` |
| `semantic_memory` | real table | **read-only view** over `memory_items` |
| carve facets | absent | 5 columns, outside every view |
| vector file | `<home>/memory.db` | `<home>/memory_stores/<name>/memory.db` |
| markdown root | `<home>/workspace/` | `<home>/memory_stores/<name>/` |
| FTS index | `<home>/memory_index.db` | inside the store's own directory |

Owned by `src/kiro_crew/memory_schema.py` (`LINEAGE_V1`, `LINEAGE_CREW`,
`CREW_SCHEMA_VERSION`, `MIGRATIONS_CREW`, `detect_lineage`).

### 2.1 Why `detect_lineage` reads the schema table first

`detect_lineage(db)` reads **SQLite's own `sqlite_master`** first, and consults the
file's *path* only for a file that has no product tables at all.

This is the mechanism that makes "the operator's memory is untouched" a property of
**the code path**, not of a test:

* Every memory file that exists today has v1 product tables, so it answers `v1`.
* Therefore **no code path leads from a populated file into the crew migrations.**

A path-first rule would have been the obvious implementation and is the wrong one: a
store directory that a restore or a rename dropped a v1 file into would be migrated in
place. Structure beats location because structure is what the reader actually depends
on.

`CREW_SCHEMA_VERSION = 1001` is deliberately **disjoint** from `{1,2,3}`: `init()`
applies migrations by set membership, so a crew file can never accidentally satisfy a
v1 migration's guard.

### 2.2 Why the views are read-only, and must stay that way

`semantic_memory` and `episodic_memories` survive on a silo as **views in v1's exact
column order**, so no ranker, exporter, or reader needed changing.

**Never give those views an `INSTEAD OF` trigger.** `snapshot_redact`'s
`_refuse_update_triggers_that_destroy_rows` requires `target == tbl_name`, which such a
trigger can never satisfy — so adding one makes the redaction pass **refuse the whole
product database by name**. This is the single most expensive-to-rediscover
constraint in the file.

### 2.3 Where the design doc was not followed

The proposal this work started from (an external design doc, not in-repo) was followed
on `memory_items` and rejected elsewhere, with reasons:

| Doc said | Shipped | Why |
|---|---|---|
| `UNIQUE (kind, key)` | `UNIQUE (key)` | v1 is `key TEXT PRIMARY KEY`. Per-kind uniqueness lets one key exist as both a directive and a fact, and the compatibility view then returns two rows for one key. |
| `created_at REAL` | `created_at TEXT` (ISO-8601) | Seven sites rank lexicographically on this column, one of them the episodic **cap eviction**. A `REAL` would silently reorder eviction. |
| `embedding_dim` column | dropped | Nothing read it and it drifted on six backfill paths. |
| `REFERENCES` clauses | dropped | `PRAGMA foreign_keys` is `0` and per-connection, so they document a constraint that is not enforced. |
| `WITHOUT ROWID` | plain table | `snapshot_redact`'s rowid-alias check drops the whole DB otherwise. |
| `workspaces`, `workspace_repos` | refused | Constant within a single-store file; they encode an axis this design deliberately does not have. |
| `personas` | refused | `config.json` is the source of truth, and a `read_set` column would put an **authorization value inside agent-reachable data**. |
| `migration_state` | refused | There is no cutover. The doc's own `schema_version >= 3` gate is already satisfied everywhere. |

**The design doc is stale about this fork in several verified ways. Do not build
against it as written.**

## 3. Facets: the carve axes

Five columns on `memory_items`, all `NOT NULL DEFAULT ''`:

`scope`, `surface`, `crew`, `session_key`, `derived_from`

Derived from the `MemoryFacets` dataclass — `FACET_NAMES` is
`tuple(field.name for field in fields(MemoryFacets))`, so the query allowlist cannot
drift from the dataclass. `GROUPABLE_COLUMNS` is the facets **plus `kind`**; `kind` is
groupable but is *not* a facet, because it is the row type rather than a stamped
attribution.

**Facets are absent from both compatibility views.** That is what keeps a facet
unreadable by anything that ranks — a ranker cannot accidentally score on `crew`.

Writes stamp facets through `_stamp_facets`, which is **crew-only, never raises, and
rolls back on failure**. With `isolation_level=""` a failed DML leaves the transaction
open, so the rollback is load-bearing.

`_session_facets(meta, key)` derives them: `crew` from `meta["agent"]` (**the crew
alias — never `kiro_agent`, which is a kiro-cli template id from a disjoint
namespace**), `surface` from `messaging.link.telemetry_channel_of` (bounded set),
`session_key` from the key.

An **empty facet value is meaningful**: `?crew=` selects the rows no writer
attributed, which is a different question from omitting the filter. Both the API and
the CLI transmit the mapping key-by-key rather than filtering on truthiness.

Every identifier reaching SQL is one of `memory_schema`'s own literals; a caller's
mapping is consulted for **membership only**. `_HOSTILE_NAMES` in
`test/test_memory_v2_facet_read.py` proves the refusal *and* that the table survives —
"it raised" and "it did not execute" are different claims.

On the v1 lineage, facet queries **refuse** with `FacetsUnsupported` (409
`facets_unsupported`) rather than returning empty. An empty page there reads as "this
crew has no memories" about a store holding thousands of unfaceted rows.

## 4. Episodic retirement

`src/kiro_crew/vector_memory.py` — `_retire_stale_episodic`, `_retire_one_episodic`,
`get_retired_episodic`, `restore_episodic`.

The original behaviour: a semantic write tombstoned episodic rows by similarity, with
**no cap and no visibility**. On a store rebuilt hours earlier, 21 of 101 rows were
already gone, 14 of them to this rule.

Three changes:

1. **Bounded** — `_MAX_EPISODIC_RETIRED_PER_WRITE = 3`
   (`vector_memory_constants.py`).
2. **Containment required** — a candidate must *textually restate* the superseded
   value, not merely rank near it.
3. **Recoverable** — `kirocrew memory retired --restore <id>`, and
   `GET /api/memory/retired` + `POST /api/memory/retired/restore`.

### 4.1 The cheap existence probe, and its two traps

Before embedding anything, a `text LIKE '%value%'` probe runs. It is a **provable
superset** of everything either retirement arm can act on, because a candidate must
contain the superseded value and both text-fallback patterns are substrings of it.
This took the pass from 20 embeds + 20 searches to 0 in the common case.

Two non-obvious constraints, both in the code as comments:

* **Probe the PRE-casefold value.** SQLite's `LIKE` is ASCII-case-insensitive while
  `str.casefold()` is Unicode, so a folded needle can match *more* than `LIKE` does —
  probing the folded value would skip rows the arms would then act on.
* **A value that strips to nothing retires nothing.** An empty needle originally
  skipped *both* the probe and the containment test, leaving the arms retiring on
  cosine alone. Found by a test written for the probe itself.

### 4.2 Correction to an earlier claim

An earlier report said "49.8% of episodic rows were irreversibly tombstoned". **That
was wrong.** The soft-deleted rows are physically present with full text; only
`memory_events` takes a hard `DELETE`. Retirement is invisible, not destructive, which
is why restore is cheap.

## 5. Admission threshold — measured, still untuned

`src/kiro_crew/eval/bench/admission.py`, `admission_corpus.py`,
`test/test_episodic_admission_bench.py`.

The recorded `F1 = 0.980` for the episodic admission threshold is **protocol-dependent
rather than false**:

* Under 1:1 class balance it reproduces at **0.976**.
* But under that protocol **every threshold in `[0.51, 0.58]` scores ≥ 0.98**, so that
  protocol *cannot* have selected the `0.55` the code uses.
* Under realistic class balance the pooled figure is **0.360**.

The benchmark now reports the protocol alongside the number instead of asserting a
bare figure.

**The larger defect is still open**: the long-text relaxation constant claims a `0.13`
dilution where the corpus measures **0.079**. Nobody has retuned it. See §11.

## 6. Backups

`src/kiro_crew/memory_backup.py`. `BACKUP_DIR_NAME = "backups"`, `DEFAULT_KEEP = 7`,
`MIN_BACKUP_INTERVAL_HOURS = 20`. Scheduled from `heartbeat.py`
(`_MEMORY_BACKUP_TICKS = 1440`, `_MEMORY_BACKUP_OFFSET = 30`) on
`maintenance_executor`. Config: `memory.backup_enabled` (default `True`),
`memory.backup_keep` (default `7`).

Memory is the only data here that **cannot be rebuilt from another source**, and its
durability story was a manual `kirocrew snapshot` nobody runs. That was verified the
hard way: when a live 36 MB store was truncated to 29 bytes, `snapshots/` did not
exist.

Five properties, each with a reason and a test:

1. **Consistent under a live writer** — taken through SQLite's **online backup API**,
   not a file copy. Under WAL, copying `memory.db` alone yields a file that *parses*
   while missing the committed tail sitting in its `-wal` sibling. Nothing complains.
   This is the failure mode the whole module exists to avoid.
2. **Three outcomes, not two** — `Path` on success, `None` when there was nothing to
   copy, `MemoryBackupFailed` when a copy was attempted and did not land. Folding the
   third into `None` is what originally made the caller's `failed` counter
   unreachable, so a store failing every single day logged nothing.
3. **Interval guard reads the stamped filename, not the mtime.** The heartbeat tick
   counter is per process and resets on every gateway start, so without the guard a
   gateway restarted five times a day writes five copies and the seven-day window
   collapses to ~1.4 days. A restored or touched file carries a new mtime while its
   name still tells the truth.
4. **Atomic** — written to `.partial` and renamed, so an interrupted run leaves
   nothing that looks like a backup.
5. **Non-destructive restore** — verifies `PRAGMA integrity_check` on the source
   **before** displacing anything, then moves the existing file aside as
   `memory.db.superseded.<stamp>` and removes the stale `-wal`/`-shm` (a leftover WAL
   describes a database that is no longer there, and SQLite would replay it onto the
   restored file).

### 6.1 Two things that surprise people

* **A restore takes effect on the next gateway start** for a store this gateway
  already has open. The displaced file is *renamed*, and an open SQLite connection
  follows the **inode**, not the name. There is no reopen to call: `init()` reassigns
  the connection with no idempotence guard, and it runs `PRAGMA journal_mode=WAL`,
  which raises on exactly the corrupt file the route exists to replace. The UI says
  this on the success path.
* **`backup`, `backups` and `restore` dispatch BEFORE the store is opened**, because
  opening it runs `PRAGMA journal_mode=WAL` — which raises on the corrupt file those
  verbs exist to repair. The most-needed path would otherwise be the one that cannot
  run.
* **Backup names collide across stores by construction**: `back_up_all_stores` takes
  one `stamp` for the whole sweep and every store's file stem is `memory`. Any UI
  state keyed on a bare backup name must be reset when the store changes (this caused
  a real bug; see §8).

## 7. Store resolution and the security argument

### 7.1 Two roots, one name

`memory_stores.py` is a **leaf module** (stdlib-only at import time) so `security` can
depend on it without a cycle. Conflating its two resolvers is the sharpest hazard:

* `memory_store_dir_for(store)` → the **markdown** root. `"default"` →
  `memory.workspace_dir()`, *not* the data home, or every install's `preferences.md`
  moves out from under both the consolidator and `kirocrew memory search`.
* `resolve_store_path(store)` → the **vector file**. `"default"` →
  `config_dir()/"memory.db"`, byte-identical to `VectorMemoryStore()`'s own default.
* `memory_index_path_for(store)` → a third answer. The default store's FTS index stays
  in the data-home **root**, because that is the only location the snapshot `memory`
  component, `portability`'s export zip and `scripts/sync-to-remote.sh` name.

### 7.2 Two failure postures, deliberately different

* An **undeclared** name **degrades** (`degrade_store_name`), two logged hops ending at
  the always-declared `"default"` floor. A raise would land inside
  `HistoryConsolidator._consolidate`'s `try`, be caught while `billed` is still
  `False` so no backoff is recorded, and re-arm every 60s idle tick **forever**.
* A **malformed** name **raises** `UnknownMemoryStore`, because degrading is the one
  case that silently merges two crews' memory: `../work` and `Work` must not resolve
  onto a store that exists.

Those stay consistent only because `usable_store_names()` makes a malformed name
*undeclared for resolution*, and **both** membership tests in the tree run through it.

### 7.3 Positive identity, and identity-not-containment

`named_store_of_db(path)` is the only **positive** spelling of "this file is a crew
silo". The negation a caller reaches for — `path != config_dir()/"memory.db"` — is true
of four real non-silo paths (the eval runner's file, the bench ingest path, the
onboarding importer's destination, and every `tmp_path` in the suite), and would hand
each of them silo treatment.

Its containment test is **identity**, not containment, and the difference is a real
isolation hole: with `memory_stores/acme` symlinked at `memory_stores/finance`, a
resolved-*parent* check still sees the root and would answer `"acme"` for a file that
physically belongs to `finance`. `_named_store_dir` refuses the same aliasing in the
forward direction.

`owned_store_path(store)` is the resolve-then-**confirm** pair, and it exists because
`resolve_store_path` degrades: a caller that skips the confirm silently reads,
reports, or copies the operator's own memory *under another store's name*.

### 7.4 The dashboard owner gate — read this before touching `?store=`

Every memory route takes an optional `?store=`. The rule lives in one place,
`handlers/_shared.py :: resolve_requested_memory_store`.

**The parameter's PRESENCE is the gate.**

* **Absent** → the answer that adds no reach (§7.5).
* **Present** → `require_owner_dashboard_request`.

The gate excludes an agent **positively**, and this is a cross-module property worth
stating exactly:

> `token_auth_middleware`'s `X-Internal-Secret` branch (kiro-cli, MCP, subagents) calls
> `await handler(request)` **without ever setting `request["user"]`**. The
> cookie/query-token branch sets it. `is_owner_dashboard_request` requires it
> non-empty.

So an agent fails the gate because it has **no identity to present** — not because it
was recognised and rejected. A negation (`not request.get("internal_auth")`) would fail
toward the permissive answer when a third caller class appears, which is the shape
`AGENTS.md` bans for harness identity.

`test/test_memory_store_dashboard.py` pins this: adding `request["user"]` beside
`request["internal_auth"] = True` turns it red across all 55 internal paths. **Keep
that test.** The property spans three modules and can be broken by an unrelated edit.

Two more rules in the same function:

* Gating on **presence**, not on "the name differs from my binding": `?store=default`
  names the operator's own global memory, so a mismatch-only rule would wave through
  the most sensitive value the parameter can carry whenever the caller happened to be
  unbound.
* An **undeclared name is a 404**, never a degrade. `resolve_store_path` degrades onto
  the default store, which here would render the operator's own memory under a label
  for a store that does not exist — and the request would look like it worked. A
  malformed name gets the *same* answer as an unknown one, so the refusal does not
  report whether a given name is declared to a caller that has not passed the gate.

### 7.5 The absent-parameter rule — an authorization boundary, not a default

`MEMORY_STORE_ABSENT_GLOBAL` (the default) vs `MEMORY_STORE_ABSENT_BINDING`.

This exists because of a **real privilege escalation introduced during this work and
caught by review**. Threading the seam through initially made eleven content routes
resolve their store from the caller's *session binding* when no parameter was sent.
That is unsafe:

* `_read_session_key` reads `X-Session-Key` **on the caller's word** — it is unverified
  on TCP.
* `store_of_session` then trusts whatever store *that* session recorded, with no check
  that the session belongs to the caller.
* `_memory_write_gate` → `_recognize_session` checks only that a key is *recognised*,
  never that it is *yours*.

So a holder of a non-owner dashboard token — e.g. an allowlisted Slack user who ran
`!dashboard` — could read **and write** any crew's silo by naming a session key bound
to it, and the owner gate would never run, because it fires only on a present
parameter.

Verified against the pre-change tree: those eleven routes used `_get_memory(state)` /
`_get_vector_store_async(state)` — the **global store unconditionally**. Only
`api_memory_carve` consulted the binding.

**Therefore:** every route that served the global store keeps `ABSENT_GLOBAL`, and
`api_memory_carve` alone asks for `ABSENT_BINDING`. When adding a gated parameter
anywhere, ask what the *ungated default* resolves to and make it the answer that adds
no reach.

## 8. The dashboard UI

`website/src/pages/overview/` — `MemoryStoreCard.tsx` (picker + overview + New store),
`MemoryCarveCard.tsx`, `MemoryRetiredCard.tsx`, `MemoryBackupsCard.tsx`, and
`MemoryTab.tsx`'s internal `MemoryDocCard`.

The tab previously carried a note admitting the gap: *"No route behind this page takes
a memory-store name … the page shows a subset of the memory that exists while reading
as if it were all of it."* That note is gone because the gap is closed.

### 8.1 Displayed store vs wire store — do not collapse these

`store` state is the **wire** value. `''` means *no parameter is sent*. The picker
**displays** `store || active`, where `active` comes from `GET /api/memory/stores`.

Collapsing them cost two real bugs:

1. **Silent data loss that reported success.** A seeding effect flipped `store` from
   `''` to `'default'` once the listing landed. The document cards are keyed on
   `store`, so that flip **remounted** them and discarded a typed draft — and the
   following Save wrote the **stale server copy** back under a green "Saved" badge.
2. **A regression on the default path.** Sending `?store=default` is semantically
   identical to sending nothing but takes the owner gate, so on an install with no
   configured owner the whole Memory page would refuse.

`pick(name)` therefore sends `''` when `name === active`, and the name otherwise.

### 8.2 Other UI invariants with reasons

* `MemoryDocCard`'s save clears its draft **only if the draft is still what was
  saved** (`setDraft(prev => prev === saved ? null : prev)`). A PUT is not instant and
  the textarea stays editable; an unconditional reset discards every keystroke typed
  during the request.
* Each store-scoped card carries `key={store}` so a switch **remounts** it and drops
  local state. Without it an *armed* restore survives a store switch, and because
  backup names collide across stores (§6.1) the same-named row of the new store
  renders already-confirmed — one click restores a store nobody armed.
* `facets_unsupported` renders an explanation, **never an empty list**.
* Cards that do **not** follow the picker (settings, embedding model, lessons, the
  vector browser) say so on screen.
* `GET /api/memory/carve` answers `{"store": ""}` for the global store — `""` is the
  resolver's canonical spelling. Do not invent a second spelling in one handler.

### 8.3 Deliberately absent

**There is no delete-store route or button.** Undeclaring a store orphans whatever it
remembered; that needs explicit operator direction, not a dashboard button. `POST
/api/memory/stores` (create) is additive and is present.

## 9. Test isolation — three barriers, and why each exists

**A subagent truncated the operator's live 36 MB `memory.db` to 29 bytes during this
work.** Mechanism: a test called `monkeypatch.undo()`, which reverts the *shared*
instance's whole stack including the rootdir `KIROCREW_HOME` pin;
`resolve_store_path("work")` then degraded to `"default"` and a deliberate
corrupt-file write landed on live data.

Three barriers in `conftest.py`, each closing a hole the previous one left:

1. A **private `MonkeyPatch`** instance for the home pin, whose undo stack no test's
   `undo()` can reach.
2. An **import-time `mkdtemp` floor** (`_HOST_HOME_FLOOR` + `atexit`, mode `0700`,
   random name) so "`KIROCREW_HOME` unset" is unreachable. The first attempt was a
   pid-predictable `mkdir(exist_ok=True)` at mode `755` — the exact
   symlink-pre-creation hazard the same file's `_create_tmp_root` docstring forbids;
   81 orphaned directories had accumulated.
3. `_refuse_a_real_data_home()`, called from the **single** `pytest_configure`,
   refusing a home **equal to, a parent of, or a child of** a real one. The first
   version used exact equality and let `$HOME/.kiro` (kiro-cli's own home) through
   with 16 tests collected. A second `pytest_configure` was also written first — a
   module defines one, and a duplicate **silently replaces** the earlier definition,
   so the guard was dead code. A test now pins that.

**Rules for anyone writing tests here:** never call `monkeypatch.undo()`; pin
`KIROCREW_HOME` to `tmp_path` in every test that touches a store; assert containment
immediately before any deliberate corrupt-file write.

## 10. What shipped

Backend: `memory_schema.py`, `memory_backup.py`, `memory_stores.py`,
`dashboard/handlers/memory_admin.py`, plus store scoping through
`dashboard/handlers/memory.py`, `context.py`, `history_consolidation.py`,
`vector_memory.py`, `security/`, `heartbeat.py`, the Slack/Discord/Telegram dispatch
paths, `learn.py`, `mcp_tools/spawn.py`.

**CLI** — five verbs under `kirocrew memory`: `backup`, `backups`, `restore`,
`retired` (`--restore <id>`, `--limit`), `carve` (`--scope --surface --crew
--session-key --derived-from --kind --count-by --store`).

**HTTP** — `?store=` on the twelve content routes, plus
`GET|POST /api/memory/stores`, `GET /api/memory/retired`,
`POST /api/memory/retired/restore`, `GET /api/memory/backups`,
`POST /api/memory/backup`, `POST /api/memory/restore`.

**Config** — `memory.backup_enabled` (`True`), `memory.backup_keep` (`7`).

**Nothing is behind Developer Mode or Feature Previews.**

## 11. Not done — pick these up

Ordered by how much they matter.

1. **`conftest.py` still has ~11 autouse fixtures on the shared `monkeypatch`**, two of
   which (`_isolate_shared_kiro_paths`, `_isolate_subagents_dir`) write real host paths
   with **no env floor behind them**. This is the part of the incident class in §9 that
   is still open. Highest priority.
2. **Retune the long-text admission relaxation** (§5): the constant claims `0.13`
   dilution, the corpus measures `0.079`. The benchmark exists; the constant was never
   changed.
3. **The retrieval contract is untouched.** The design doc's load-and-dump vs
   retrieve-on-demand context-injection question was never addressed; the retrieval
   path is unchanged.
4. **No upgrade path for pre-existing silos.** A silo created before this work stays on
   v1 forever. Only newly created silos get `memory_items`. Decide whether that is
   permanent.
5. **Seven remaining store-identity call sites** carry no crew identity, so reading the
   global store is correct there — but they sit on `check_memory_store_seam.py`'s
   allowlist and the gate prints them as a backlog.
6. **`memory_stores/` is invisible to snapshot, portability, and redaction.** A silo's
   contents are outside the export zip and the snapshot `memory` component. Its FTS
   index is fully derived so that part is only a rebuild, but the vector file is not.
7. **A silo is no longer fenced against a SHELL read** — see §11.1. Decide whether the
   sandbox alone is enough for a silo, or whether silo paths deserve a control the
   keystone does not have.

### 11.1 What the rebase changed about the silo threat model

Upstream removed path matching from `is_sensitive_bash_command` **deliberately and with
reasons** (four issue numbers in its docstring): a text matcher over `cat <fenced
path>` adds nothing on top of controls that cannot be talked around, and it denied
ordinary read-only commands whenever a fenced spelling appeared as *data* — a grep
pattern, a commit message, a note. Its docstring now states that **a keystone read
through the shell is permitted by design**, and points at the two controls that do the
work: the OS sandbox, and `is_sensitive_path` on every resolved path the file tools
open.

The consequence for memory v2: a silo path in **command text** is not refused. The
`memory_stores/` entry in `_CREW_SECRET_LEAVES` still fences the agent's **file tools**
(`is_sensitive_path` / `is_sensitive_write_path` both answer `True`), and the sandbox
still confines the process — but the shell text gate no longer helps.

`test/test_memory_stores.py::test_the_shell_text_matcher_deliberately_does_not_fence_a_store`
records this rather than asserting the old behaviour, so the boundary is written down
where someone looking for it will find it. **If that test starts failing because a path
matcher came back, that is a decision to make deliberately — with the false-denial
class in mind — not a regression to fix by making the assertion pass.**

Silos get exactly the same treatment as `~/.aws` and the governance keystone here, so
this is consistent rather than a silo-specific hole. Whether it is *sufficient* for a
silo is the open question in item 7.

## 12. Operating instructions for the next session

### Where things are

| | |
|---|---|
| Worktree | `/workplace/bolichen/kirocrew-wt-memory-v2-ui` |
| Branch | `feat/memory-v2-ui` |
| Pre-rebase backup tag | `mv2-ui-prerebase-backup` |
| Python | `/workplace/bolichen/kc-venv-mv2ui/bin/python` (3.12) |
| Node | `export PATH=/workplace/bolichen/node22/bin:$PATH` |

**The system interpreters do not work.** `python3` is 3.9 (repo needs ≥ 3.10) and
`node` is 18, on which **vitest cannot start at all** (its worker `execArgv` carries
`--no-experimental-webstorage`, which Node 18 rejects) — it fails to launch rather
than failing a test.

**Do not put anything you need under `/tmp`.** It is reaped mid-session on this host;
it already took out the only interpreter with dependencies.

### The gates

```bash
cd /workplace/bolichen/kirocrew-wt-memory-v2-ui
PY=/workplace/bolichen/kc-venv-mv2ui/bin/python

$PY scripts/check_black_formatting.py && $PY scripts/check_subprocess_encoding.py
$PY -m isort --check-only src/kiro_crew test
$PY -m flake8 src/kiro_crew test
$PY -m mypy --platform linux src/kiro_crew
bash scripts/docs-lint.sh
HARNESS_BASE_REF=origin/main $PY scripts/check_harness_parity.py
BRAND_BASE_REF=origin/main   $PY scripts/check_brand_name.py
FEATURE_MAP_BASE_REF=origin/main $PY scripts/check_feature_map.py
$PY scripts/check_memory_store_seam.py     # exit 0; its output is the known backlog
```

Targeted tests — **not** the full suite:

```bash
$PY -m pytest test/test_memory_store_dashboard.py test/test_memory_v2_schema.py \
  test/test_memory_v2_isolation.py test/test_memory_v2_facet_read.py \
  test/test_memory_backup.py test/test_memory_lineage_drift.py \
  test/test_memory_store_seam.py test/test_episodic_retirement.py \
  test/test_scan_memory_stores.py test/test_memory_v1_golden.py \
  test/test_handlers_memory_coverage.py -q -p no:cacheprovider
```

Frontend:

```bash
cd website && export PATH=/workplace/bolichen/node22/bin:$PATH
npx tsc -b && npm run build
I18N_BASE_REF=origin/main npm run i18n:check    # 19 checks, all must pass
npx vitest run src/test/
```

### `test_memory_v1_golden.py` is the acceptance proof

It must pass **unedited**. If it fails, that is the most important finding available —
it means the default store moved. **Do not edit it to make it pass.**

### Known non-blocking gate failures

* `scripts/scrub-lint.sh` fails on a **pre-existing** violation in
  `test/test_atomic_write_named_duplicates.py` (`/home/tést`, a deliberate non-ASCII
  byte fixture). The file is byte-identical to `origin/main`. It needs an allowlist
  entry from whoever added it, not a fix here.
* `check_changelog_history.py` fails only with `CHANGELOG_BASE_REF=origin/main` while
  the branch is behind: `[0.6.0]` shipped upstream after the merge-base. `CHANGELOG.md`
  is byte-identical to the merge-base and no branch commit touches it. **This feature
  branch must not touch `CHANGELOG.md`** — the release PR writes that section.

### Notes on the rebase

The branch was squashed to one commit and rebased over **609** upstream commits. Two
upstream sweeps caused most of the conflicts and both are preserved:

* `src/kiro_crew/security.py` became the package `src/kiro_crew/security/`. The
  multi-store `scan_memory` block was re-spliced into `security/__init__.py`.
* The user-facing term **"crew" was renamed to "agent"** (32 → 0 in the agents page
  catalog). Any new user-facing string must say "agent".

The i18n catalogs were resolved by a **semantic three-way JSON merge** (base /
upstream / ours) rather than textually. Three keys were edited by *both* sides —
upstream's edit was the terminology rename, ours was a factual correction (upstream's
copy still said isolated per-crew memory "is still being built", which is now false).
Both intents were merged, not chosen between.

**If this branch waits, the next rebase costs more.** It went from 390 to 609 commits
behind in under a day.

## 13. File map

One commit: 143 files, +27,039 / −853, of which 30 are new.

### New backend modules

| File | Lines | What it owns |
|---|---|---|
| `src/kiro_crew/memory_schema.py` | ~805 | The crew lineage. `LINEAGE_V1`/`LINEAGE_CREW`, `CREW_SCHEMA_VERSION`, `CREW_SCHEMA_SQL`, `MIGRATIONS_CREW`, `detect_lineage`, the per-lineage relation/guard/insert builders, `MemoryFacets`, `FACET_NAMES`, `GROUPABLE_COLUMNS`, `FacetsUnsupported`, `UnknownFacet`, the shared `MEMORY_EVENTS_SQL`/`MEMORY_META_SQL`. |
| `src/kiro_crew/memory_stores.py` | ~635 | Store names → paths. The three resolvers (`memory_store_dir_for`, `resolve_store_path`, `memory_index_path_for`), the shape rule (`memory_store_name_defect`, `validate_memory_store_name`), the degrade cascade (`degrade_store_name`, `resolve_declared_store`), the positive predicate (`named_store_of_db`), the confirm pair (`owned_store_path`), `declared_store_names`, `ensure_memory_store_dir`, `warn_if_binding_degrades`. **Leaf module: stdlib-only at import time**, so `security` can depend on it. |
| `src/kiro_crew/memory_backup.py` | ~318 | Rotating hot backups. `backup_store`, `list_backups`, `prune_backups`, `back_up_all_stores`, `newest_backup`, `restore_from_backup`, `backup_dir_for`, `MemoryBackupFailed`. |
| `src/kiro_crew/dashboard/handlers/memory_admin.py` | ~816 | The seven new owner-gated routes. |
| `scripts/check_memory_store_seam.py` | — | The CI gate for memory-context call sites that name no store. Has a `--test` self-test that plants one probe per rule. |
| `src/kiro_crew/eval/bench/admission.py`, `admission_corpus.py` | — | The admission-threshold benchmark and its corpus. |

### Key modified backend files

`dashboard/handlers/_shared.py` (the request seam: `resolve_requested_memory_store`,
`markdown_memory_for_store`, `vector_memory_for_store`, the two `MEMORY_STORE_ABSENT_*`
constants) · `dashboard/handlers/memory.py` (twelve store-scoped routes +
`_vector_tier_for_request`) · `vector_memory.py` (lineage binding, facet stamping,
bounded recoverable retirement, facet reads) · `context.py` (`store_of_session`,
`session_store_for_turn`, `ContextBuilder.ensure_store`) · `security/paths.py` (the
`memory_stores/` keystone fence) · `security/__init__.py` (multi-store `scan_memory`) ·
`heartbeat.py` (the daily backup tick) · `config/sections.py` (the two config keys) ·
`history_consolidation.py` (`_session_facets`) · `cli.py` / `cli_commands.py` (five
verbs).

### Frontend

`website/src/pages/overview/MemoryStoreCard.tsx` is the module that owns the shared
store vocabulary the sibling cards import: `MEMORY_STORES_KEY`,
`MEMORY_QUERY_PREFIXES`, `memoryQueryRetry`, `memoryErrorCode`, `MemoryScopeNotice`,
`useMemoryStores`, `NO_MEMORY_STORES`. Start there.

Then `MemoryCarveCard.tsx`, `MemoryRetiredCard.tsx`, `MemoryBackupsCard.tsx`, and
`MemoryTab.tsx`'s internal `MemoryDocCard`. Client methods and types are in
`website/src/api/client.ts` and `website/src/types/index.ts`.

## 14. The HTTP surface

`?store=<name>` is optional on the twelve content routes. **Absent** → the global store
(`MEMORY_STORE_ABSENT_GLOBAL`), except `carve` which resolves the caller's binding.
**Present** → owner-gated.

| Method | Path | Notes |
|---|---|---|
| `GET`/`PUT` | `/api/memory/preferences` | markdown tier |
| `GET`/`PUT` | `/api/memory/projects` | markdown tier |
| `GET`/`PUT` | `/api/memory/history` | markdown tier, today's file |
| `GET`/`PUT` | `/api/memory/semantic` | |
| `DELETE` | `/api/memory/semantic/{key:.+}` | |
| `GET` | `/api/memory/episodic`, `/api/memory/episodic/search` | |
| `DELETE` | `/api/memory/episodic/{id}` | |
| `GET` | `/api/memory/stats` | counts are per store; the embedding provider, migration flag and legacy-markdown probe stay INSTALL-wide |
| `GET` | `/api/memory/events` | |
| `GET` | `/api/memory/carve` | the only `ABSENT_BINDING` route; answers `{"store": ""}` for the global store |
| `GET` | `/api/memory/stores` | owner-gated, **no** `?store=`; returns `{stores, active}` |
| `POST` | `/api/memory/stores` | declares a store; `201` |
| `GET` | `/api/memory/retired` | |
| `POST` | `/api/memory/retired/restore` | registered BEFORE `/api/memory/retired` |
| `GET` | `/api/memory/backups` | never returns a filesystem path |
| `POST` | `/api/memory/backup` | returns `{backed_up, skipped, pruned, failed}` |
| `POST` | `/api/memory/restore` | returns `{ok, superseded}` |

Route order matters — `routes/memory.py` says so at the top: aiohttp resolves in
REGISTRATION order, and several routes rely on a literal path being registered before a
pattern that would swallow it.

**Every non-2xx body carries a machine-readable `code`** (backend strings have no i18n
catalog path). The ones this work introduced or relies on:

`owner_only` 403 · `unknown_memory_store` 404 · `store_unavailable` 503 ·
`facets_unsupported` 409 · `unknown_facet` 400 · `invalid_pagination` 400 ·
`invalid_episode_id` 400 · `unknown_retired_episode` 404 ·
`invalid_memory_store_name` 400 · `memory_store_exists` 409 · `config_unreadable` 500 ·
`memory_stores_unreadable` 500 · `invalid_backup_name` 400 · `backup_not_found` 404 ·
`backup_corrupt` 409 · `restore_failed` 500 · `restricted_session` 403.

`test/test_error_code_contract.py` pins that every new non-2xx body carries one.

## 15. Test inventory — which file pins which invariant

~11,300 lines of new tests. When you change something, this is the file that will tell
you:

| Test file | Pins |
|---|---|
| `test_memory_v1_golden.py` | **The acceptance proof.** The default store's tables, rows, ranking and on-disk footprint are unchanged. Never edit to make it pass. |
| `test_memory_v2_schema.py` | The two lineages, `detect_lineage`'s structural rule, the views' column order, that facets are unreachable from every ranker, and that a pre-existing silo keeps v1. |
| `test_memory_lineage_drift.py` | That no statement drifts between lineages. |
| `test_memory_v2_facet_read.py` | The carve read side: filters EXCLUDE, hostile axis names are refused AND the table survives, the two refusals, and the route's owner gate. |
| `test_memory_v2_isolation.py` | One store's writes never reach another, across the markdown, vector and lessons tiers. |
| `test_memory_stores.py` | The name shape rule, the two failure postures, the symlink aliasing refusals, the keystone fence, and (now) that the shell text matcher deliberately does not fence a store. |
| `test_memory_store_seam.py` | The call-site backlog ratchet — it goes red if a new memory-context call site names no store. |
| `test_memory_store_dashboard.py` | The owner gate, the cross-module `request["user"]` property, the absent-parameter refusal of `X-Session-Key`, isolation through the API, the 503, the backup-name containment, and that one damaged store does not hide the healthy ones. |
| `test_memory_backup.py` | Consistency under a live writer, the three outcomes, the interval guard, atomicity, non-destructive restore. |
| `test_episodic_retirement.py` | The cap, the containment requirement, the probe, recoverability. |
| `test_scan_memory_stores.py` | That the credential scan walks every declared store and reports an unauditable one instead of rounding to clean. |
| `test_home_pin_survives_monkeypatch_undo.py` | That `monkeypatch.undo()` cannot lift the home pin, and that the guard is reached from the ONE `pytest_configure`. |
| `test_episodic_admission_bench.py` | The benchmark's protocol, not a bare F1. |
| `test_security_facade.py` | That every submodule-owned re-exported name is in the frozen manifest. |
| `website/src/test/MemoryStorePicker.test.tsx` | The picker's listing, that a switch re-reads every card under the new name, `facets_unsupported` rendering, the two-click restore, the `owner_only` message. |
| `website/src/test/OverviewMemoryTabCov80.test.tsx` | The tab's own write paths, including that a storeless save sends `undefined` rather than `store=default`. |

## 16. Recipes

### Add a store-scoped route

```python
store, denial = await resolve_requested_memory_store(request, state, "thing.read")
if denial is not None:
    return denial
tier = await vector_memory_for_store(state, store)      # or markdown_memory_for_store
if tier is None:                                        # silo only
    return _store_unavailable_response(store)
```

Do **not** pass `absent=MEMORY_STORE_ABSENT_BINDING` unless the route already followed
the caller's binding before store scoping existed. See §7.5 for what it costs.

### Add a carve facet

It is a data change, not an evaluator change:

1. Add the field to `memory_schema.MemoryFacets` (`FACET_NAMES` derives from it).
2. Add the column to `CREW_SCHEMA_SQL` **and** a migration in `MIGRATIONS_CREW` with a
   new version number.
3. Stamp it in `history_consolidation._session_facets` if a session can supply it.
4. Add the CLI flag in `cli.py` (argparse cannot build flags from a runtime tuple
   without paying an import per invocation, which is why that list is the one hand-kept
   copy — `test_memory_v2_facet_read.py::TestTheAllowlistIsDerivedNotRestated` is what
   keeps it from drifting into a silently unreachable axis).
5. Do **not** add it to either compatibility view.

### Add a store-aware frontend card

Import the shared vocabulary from `MemoryStoreCard.tsx` (`useMemoryStores`,
`MemoryScopeNotice`, `memoryErrorCode`, `memoryQueryRetry`, `MEMORY_QUERY_PREFIXES`).
Put the store in the React Query key, mount it from `MemoryTab` with `key={store}`, and
add the new English literals as plain strings — then run the i18n pass over all 13
catalogs with **real** translations (`[changed-passthrough]` is zero-tolerance).

### Verify a change against a running gateway

`docs/guides/worktree-verification-recipes.md`. Never point a harness at `~/.kiro/crew`.

## 17. Correction log — claims that were revised

Recorded because each was stated confidently before being checked, and a following
session should not rediscover them from the earlier reports.

| Claim | Correction |
|---|---|
| "49.8% of episodic rows were irreversibly tombstoned" | Wrong. The rows are physically present with full text; only `memory_events` takes a hard `DELETE`. |
| "The admission F1 of 0.980 does not reproduce (0.721)" | Wrong as stated. It reproduces at 0.976 under 1:1 balance; the real finding is that the protocol cannot have selected 0.55, and that realistic balance gives 0.360. |
| "`CONTRACT_VERSION` is the memory schema version" | Wrong. `CONTRACT_VERSION` is the PlatformContext composition contract; the memory schema version is unrelated. |
| "An absent `?store=` is byte-identical to what every route did before" | Wrong, and it was a live escalation. Eleven routes served the GLOBAL store before; resolving the binding was new reach. See §7.5. |
| "The memory-store fence survived the security package split" | Wrong. It was lost and had to be re-applied to `security/paths.py`; `test_memory_stores.py` caught it. |
| "The handoff covers the memory v2 UI" | Too narrow. The work is all of memory v2; this document covers the schema, backups, retirement, facets, isolation, CLI, security scan and UI. |

## 18. Where this stands

Pushed to `origin` (`kirodotdev/KiroCrew`) as branch **`feat/memory-v2-ui`**, one commit,
rebased onto `origin/main`. No pull request has been opened.

Verified at push time: `test_memory_v1_golden.py` passes unedited; 921 backend tests
across the memory suite; 25,537 frontend tests across 1,602 files; mypy clean on 1,325
files; flake8, isort, black, subprocess-encoding, docs-lint, harness-parity, brand,
feature-map and memory-store-seam all green; 19/19 i18n checks against `origin/main`.

Two known gate failures that are **not** this branch's to fix: `scrub-lint.sh` on a
pre-existing non-ASCII fixture, and `check_changelog_history.py` when the branch is
behind. Both are explained in §12.
