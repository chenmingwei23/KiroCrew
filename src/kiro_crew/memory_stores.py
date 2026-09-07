"""Named memory stores: the two roots, the shape rule, and the resolvers.

A crew (``cfg.agents[<crew>].memory_store``) names a memory store, and a store
is a SEPARATE on-disk silo: its own markdown tree, its own FTS index and its
own vector-store SQLite file. There is no workspace column and no cutover — isolation is the
file boundary.

**There are TWO roots, and conflating them is the sharpest hazard here.** The
resolvers below answer for the same store name, and they answer with different
paths:

* :func:`memory_store_dir_for` — the MARKDOWN root, the directory holding
  ``memory/preferences.md``, ``memory/projects.md`` and ``memory/history/*.md``.
  For ``"default"`` this is :func:`kiro_crew.memory.workspace_dir`
  (``config_dir()/"workspace"``), NOT the data home: returning the data home
  would move every existing install's markdown memory out from under both the
  consolidator and ``kirocrew memory search``.
* :func:`resolve_store_path` — the VECTOR FILE. For ``"default"`` this is
  ``config_dir()/"memory.db"``, byte-identical to the path
  ``VectorMemoryStore()`` already defaults to.

:func:`memory_index_path_for` answers a third question and does NOT follow the
markdown root: the DEFAULT store's FTS index stays in the data-home root, beside
``memory.db``, because that is the only location the snapshot ``memory``
component, ``portability``'s export zip and ``scripts/sync-to-remote.sh`` name.
A NAMED store's index does live inside its own directory.

Nothing here moves data. A named store starts EMPTY; nothing is copied or
inferred from the default store.

Two failure postures, deliberately different:

* An **undeclared** name DEGRADES (:func:`degrade_store_name`) — two hops, each
  logged, ending at the always-declared ``"default"`` floor. What a raise would
  cost here: it lands inside ``HistoryConsolidator._consolidate``'s ``try``, is
  caught while ``billed`` is still ``False`` so no backoff attempt is recorded,
  and all four consolidation entry points therefore re-arm on every 60s idle
  tick forever while the CLI path logs it at DEBUG. Same shape as
  ``acp_backends.resolve_selected_backend`` and the workspace degrade in
  ``config.loader.resolve_agent_bindings``.
* A **malformed** name RAISES :class:`UnknownMemoryStore` when a caller hands
  one to a resolver directly, because degrading is the one case that would
  silently merge two crews' memory into one directory: ``../work`` and ``Work``
  must not resolve onto a store that exists.

Those two postures only stay consistent because a malformed name is
**UNDECLARED for resolution**: :func:`usable_store_names` drops it, even
though ``KiroCrewConfig.load`` deliberately keeps the operator's entry verbatim.
Both membership tests in the tree — this module's and
``resolve_agent_bindings``'s — run through that filter, so a crew bound to a
malformed store degrades at the binding and the raise above is never reached
from a config. Without the filter the two disagree: the binding hands on a name
the config declares and the resolver then refuses it at the first memory write.

LEAF module: stdlib-only imports at module scope, so ``security.py`` (imported
very early, and which needs :data:`MEMORY_STORES_DIR_NAME` to build its
sensitive-path fence) can depend on it without a cycle. Everything else is
imported inside the function that needs it.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory under the data home holding one subdirectory per NAMED store. The
#: default store is deliberately NOT under here — it keeps the pre-existing
#: ``workspace/`` tree and root ``memory.db``.
#:
#: Read+write fenced: this whole subtree is a keystone leaf in
#: ``security._CREW_SECRET_LEAVES``, so agent file tools can neither read nor
#: write another crew's memory. Legitimate readers open these paths DIRECTLY,
#: the established keystone-reader pattern.
MEMORY_STORES_DIR_NAME = "memory_stores"

#: The store name that is always resolvable. It is the FLOOR, in the sense
#: ``ACP_BACKEND_KIRO`` is the harness floor: it names the markdown tree and
#: vector file every existing install already has, so it counts as declared
#: whether or not the operator's ``memory_stores`` section mentions it. That is
#: what keeps a fresh install (no ``config.json`` at all) resolvable.
DEFAULT_MEMORY_STORE = "default"

#: Vector-store filename inside a store's own directory. Owned here because two
#: resolvers must spell it identically — this module's
#: :func:`resolve_store_path` and ``vector_memory``'s own default.
MEMORY_DB_FILE = "memory.db"

#: Longest usable store name. A store name becomes a single path segment, and a
#: 255-byte filesystem limit has to hold the name plus whatever a sidecar
#: appends to it, so the cap is well inside it rather than at it.
MEMORY_STORE_NAME_MAX = 80

# Same shape ``members._SLUG_RE`` enforces for member slugs — lowercase
# letters, digits and hyphens, no leading or trailing hyphen. Kept as a local
# constant rather than imported because it is private there, and because the
# two lists are allowed to diverge; the members store remains the source of
# truth for the spelling.
_STORE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Basenames Windows resolves to a DEVICE rather than a file, with or without an
# extension. Refused on every platform, not just Windows: a config written on
# Linux is carried to Windows, and a store whose directory cannot be created
# there is a silo that silently holds nothing.
_WINDOWS_RESERVED_BASENAMES: frozenset[str] = frozenset(
    {"con", "nul", "aux", "prn"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class UnknownMemoryStore(ValueError):
    """A memory store name cannot be turned into a path.

    Raised only by the SHAPE rule (:func:`validate_memory_store_name`) and by
    the containment re-check that follows path composition. An *undeclared*
    name is not an error — it degrades; see :func:`resolve_declared_store`.

    A ``ValueError`` subclass so a caller with a broad ``except ValueError``
    around a config read still catches it.
    """


def memory_store_name_defect(name: object) -> str | None:
    """Why *name* is unusable as a store name, or ``None`` when it is fine.

    The predicate half of :func:`validate_memory_store_name`, split out so the
    degrade cascade can TEST a candidate without raising on it.

    Rules are ordered most-specific-first so the reported reason names the
    actual defect; :data:`_STORE_NAME_RE` is the final catch-all.
    """
    if not isinstance(name, str):
        return "not a string"
    if not name:
        return "empty"
    if len(name) > MEMORY_STORE_NAME_MAX:
        return f"longer than {MEMORY_STORE_NAME_MAX} characters"
    if name != name.lower():
        return "not lowercase"
    # A SINGLE path segment, checked against BOTH separators: ``a\\b`` is one
    # segment to ``posixpath`` and two to Windows, and the config is portable.
    if os.path.basename(name) != name or Path(name).name != name or "\\" in name:
        return "not a single path segment"
    if name in _WINDOWS_RESERVED_BASENAMES:
        return "a Windows reserved device basename"
    if name[-1] in ". ":
        return "ends with a dot or a space"
    if not _STORE_NAME_RE.match(name):
        return f"does not match {_STORE_NAME_RE.pattern}"
    return None


def memory_store_binding_defect(raw: object) -> str | None:
    """Why *raw* is unusable as a crew's ``memory_store`` BINDING, or ``None``.

    The write-side companion to :func:`memory_store_name_defect`, and it differs
    from it in exactly one value: ``""`` passes. An empty binding is the absence
    of a choice rather than a broken name — ``resolve_agent_bindings`` maps it
    onto the :data:`DEFAULT_MEMORY_STORE` floor, and the crew editor sends the
    field on EVERY save while its picker spells "nothing selected" as ``""`` — so
    refusing it would leave a crew whose stored binding is already empty unsavable
    until someone hand-edits ``config.json``.

    A NON-STRING is a defect even though ``""`` is not. It reaches ``config.json``
    verbatim, every reader downstream is annotated ``str``, and
    ``kirocrew agent list`` formats the field through a width spec, which raises
    on ``None`` or a list — so one bad write takes out the command that would show
    it to you.

    SHAPE only. An undeclared but well-formed name is accepted and DEGRADES at
    resolution; :func:`warn_if_binding_degrades` is what keeps that from being
    silent. Refusing it here would make the field unsettable, because no write
    surface can declare a store — ``memory_stores`` is hand-edited config — so the
    operator could never reach the state where their own name is legal.

    Lives here rather than at each write surface so the dashboard verbs and
    ``kirocrew agent create``/``update`` cannot disagree about which values a crew
    may be bound to, the same reason :func:`usable_store_names` is the one filter
    both membership tests run through.
    """
    if isinstance(raw, str) and not raw:
        return None
    return memory_store_name_defect(raw)


def named_store_or_empty(name: object) -> str:
    """*name* as a NAMED store, or ``""`` meaning the global store.

    The one definition of "this value means the global store". Six call sites
    across five modules spelled it themselves and two of them had already
    diverged: one stripped surrounding whitespace and one did not, so a
    hand-edited ``"  coding  "`` made the consolidator WRITE into the silo while
    the context builder READ the global store — a split-brain with no error on
    either side.

    Strips deliberately. A padded value cannot pass
    :func:`validate_memory_store_name` (a trailing space is a refusal, because a
    path segment ending in one is unusable on Windows), so the choice is between
    stripping here and answering two different things in two modules. Every
    write surface already rejects padding; this is what makes a config the
    validators never saw resolve the same way everywhere.

    Non-strings answer ``""``: metadata is read from disk and a caller must not
    have to type-check before asking.
    """
    if not isinstance(name, str):
        return ""
    stripped = name.strip()
    return "" if not stripped or stripped == DEFAULT_MEMORY_STORE else stripped


def validate_memory_store_name(name: str) -> str:
    """Return *name* unchanged when it is a usable store name, else raise.

    Applied BEFORE any path composition. Reaching it from a config is not
    possible: the load reports a malformed name but KEEPS the operator's entry,
    and :func:`usable_store_names` is what makes such a name undeclared for
    resolution, so every config-driven name is degraded before it arrives. What
    remains is a caller composing a path from a name it invented.

    This is the one memory-store failure that raises rather than degrading. The
    pattern admits no ``/``, ``\\``, ``.`` or whitespace, so a validated name
    cannot traverse out of :func:`memory_stores_root` on its own; the resolvers
    still re-check containment after composition, because validation and use
    are separated by a call boundary a future caller could bypass (the same
    pairing ``members.validate_slug`` / ``members.member_dir`` uses).
    """
    defect = memory_store_name_defect(name)
    if defect is not None:
        raise UnknownMemoryStore(f"invalid memory store name {name!r}: {defect}")
    return name


def memory_stores_root() -> Path:
    """The directory holding one subdirectory per NAMED memory store."""
    from kiro_crew.config.loader import config_dir

    return config_dir() / MEMORY_STORES_DIR_NAME


def usable_store_names(declared: Iterable[str]) -> frozenset[str]:
    """The subset of *declared* that can actually become a store on disk.

    A MALFORMED name is undeclared for resolution even though
    ``KiroCrewConfig.load`` keeps the operator's entry verbatim — reporting a
    defect must not erase a line the operator wrote, and a name no resolver will
    compose a path for is still not a store any crew can run on. Both membership
    tests in the tree run through this filter (:func:`_declared_stores` here,
    ``config.loader.resolve_agent_bindings`` for a crew's binding), which is what
    keeps them from disagreeing: a raw-table test would hand a crew a name that
    :func:`validate_memory_store_name` then refuses at the first memory write.

    Filtering ONLY — :data:`DEFAULT_MEMORY_STORE` is not added here. The floor
    belongs to the resolvers, which must answer for it on an install with no
    ``config.json`` at all; a crew's binding reads a loaded table that already
    carries a synthesized default entry whenever the section was empty, so
    adding the floor there would instead change which store an existing config's
    crew lands on.

    Pure — no config load — so ``resolve_agent_bindings`` can call it with the
    config already in hand instead of re-entering the loader.
    """
    return frozenset(n for n in declared if memory_store_name_defect(n) is None)


def degrade_store_name(store: str, declared: frozenset[str], configured_default: str) -> str:
    """DEGRADE *store* onto a name in *declared*. Never raises.

    Two hops, each logged with its reason: the requested name, then
    *configured_default*, then the literal :data:`DEFAULT_MEMORY_STORE`, which
    is always resolvable and is therefore the floor even when *declared* does
    not list it. A malformed name degrades like any other undeclared one because
    *declared* comes from :func:`usable_store_names`, which already dropped it —
    the second hop needs no separate shape check for the same reason.

    Pure, so the two callers that must agree can share it: this module's
    :func:`resolve_declared_store` and ``resolve_agent_bindings``, which holds
    its config and must not re-enter the loader.
    """
    if store in declared:
        return store
    logger.warning(
        "memory store %r is not a declared, usable store; degrading to default_memory_store %r",
        store,
        configured_default,
    )
    if configured_default in declared:
        return configured_default
    logger.warning(
        "default_memory_store %r is not itself a declared, usable store; degrading to the %r store",
        configured_default,
        DEFAULT_MEMORY_STORE,
    )
    return DEFAULT_MEMORY_STORE


def warn_if_binding_degrades(
    crew: str, store: str, config_stores: Iterable[str] | None, configured_default: str | None
) -> None:
    """Log when *crew*'s *store* binding will not resolve to the store it names.

    Called where the binding is WRITTEN, because the degrade itself happens at the
    first memory read: one line attributed to no crew, in a log nobody is watching,
    however long after the choice was made. A crew bound to a store nobody declared
    reads and writes a silo it did not ask for, which is the whole failure a
    per-crew store exists to prevent, so choosing one must leave a trace at the
    moment a human is looking at the value.

    Accepted rather than refused — see :func:`memory_store_binding_defect` — which
    is the same unknown-but-accepted posture the crew create verb takes for a
    ``kiro_agent`` template that is not in the installed listing.

    The landing store comes from :func:`degrade_store_name` rather than a second
    rule, so the message cannot name a store resolution would not pick. That
    function logs its own hops first; this line is the one that names the crew.

    TOTAL, deliberately. This runs inside the crew create and update handlers, so
    an exception here fails the WRITE — a 500 on `POST /api/agents` in exchange
    for a log line, which is a strictly worse trade than the missing warning.
    Absent arguments are therefore tolerated rather than trusted: a config object
    that does not carry the table (a caller holding a partial config) reports
    nothing instead of raising.
    """
    if config_stores is None:
        logger.debug("no memory_stores table available; skipping the binding warning for %r", crew)
        return
    declared = usable_store_names(config_stores) | {DEFAULT_MEMORY_STORE}
    configured_default = configured_default or DEFAULT_MEMORY_STORE
    requested = store or DEFAULT_MEMORY_STORE
    if requested in declared:
        return
    logger.warning(
        "crew %r is bound to memory store %r, which no usable memory_stores entry "
        "declares; it reads and writes the %r store until that entry exists",
        crew,
        requested,
        degrade_store_name(requested, declared, configured_default),
    )


#: ``(config fingerprint, declared names, configured default)`` for the last
#: resolution. Not an LRU: there is exactly one config, so one slot is the whole
#: cache, and keying on the fingerprint means a stale entry is impossible rather
#: than merely unlikely.
_DECLARED_MEMO: tuple[object, frozenset[str], str] | None = None


def _set_declared_memo(fp: object, declared: frozenset[str], configured_default: str) -> None:
    global _DECLARED_MEMO
    _DECLARED_MEMO = (fp, declared, configured_default)


def _declared_stores() -> tuple[frozenset[str], str]:
    """``(resolvable store names, cfg.default_memory_store)`` off the LOADED config.

    Reads ``KiroCrewConfig.load()``, never ``_raw_config()``. The raw dict is
    the bytes on disk: it carries no ``memory_stores`` key until a write-back
    migration adds one, and that migration is SKIPPED whenever the load
    degraded a section — so a raw-dict resolver reports ``"default"`` as
    undeclared on a fresh install, and keeps reporting it on any install with a
    malformed config section. The loaded config synthesizes the default entry.

    :data:`DEFAULT_MEMORY_STORE` is unioned in unconditionally: it is the floor,
    naming the markdown tree and vector file every install already has, so it
    stays resolvable even when the config cannot be read at all.

    Never raises — a config that will not load degrades to the floor alone.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig, _config_fingerprint

        # Memoized on the loader's OWN change-detector, so it invalidates exactly
        # when the config does. A full ``KiroCrewConfig.load()`` deep-copies the
        # cached dict and rebuilds every dataclass to answer for two fields, and
        # resolution runs several times per turn on a named store; keying on the
        # fingerprint turns that into a stat.
        fp = _config_fingerprint()
        memo = _DECLARED_MEMO
        if memo is not None and memo[0] == fp:
            return memo[1], memo[2]
        cfg = KiroCrewConfig.load()
        declared = usable_store_names(cfg.memory_stores) | {DEFAULT_MEMORY_STORE}
        _set_declared_memo(fp, declared, cfg.default_memory_store)
        return declared, cfg.default_memory_store
    except Exception:
        logger.warning(
            "could not load config to enumerate memory stores; using the %r store only",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return frozenset({DEFAULT_MEMORY_STORE}), DEFAULT_MEMORY_STORE


def resolve_declared_store(store: str) -> str:
    """Shape-validate *store*, then DEGRADE it onto a declared store name.

    The validate is for a caller that invented the name; a name arriving from a
    config has already been degraded at the binding, so it cannot raise here.
    See the module docstring for what a raise would cost on the consolidation
    path, and :func:`degrade_store_name` for the two hops.
    """
    validate_memory_store_name(store)
    declared, configured_default = _declared_stores()
    return degrade_store_name(store, declared, configured_default)


def _named_store_dir(name: str) -> Path:
    """Compose a NAMED store's directory and re-check containment.

    *name* must already be shape-validated and must not be
    :data:`DEFAULT_MEMORY_STORE`.
    """
    root = memory_stores_root().resolve()
    expected = root / name
    target = expected.resolve()
    # Defence in depth behind validate_memory_store_name, mirroring
    # members.member_dir: a symlinked component must not redirect a store.
    #
    # The test is IDENTITY, not containment, and the difference is a real
    # isolation hole rather than a hypothetical one. Checking only
    # ``target.parent == root`` refuses a link that escapes the root and ACCEPTS
    # one that redirects INSIDE it: with ``memory_stores/acme`` pointing at
    # ``memory_stores/finance``, the resolved parent is still the root, so both
    # crews were handed one silo -- vector rows, markdown and lessons -- with
    # every path check reporting success. Requiring the resolved path to be the
    # one that was composed refuses the redirect and still admits a root reached
    # through a symlinked ancestor (``/tmp`` on macOS), because ``root`` is
    # resolved before the join.
    #
    # A non-existent store resolves to itself (``strict=False``), so a store
    # being created for the first time passes.
    if target != expected:
        raise UnknownMemoryStore(
            f"memory store {name!r} resolves to {target}, not {expected}; refusing a "
            f"link that would share another store's directory"
        )
    return target


def memory_store_dir_for(store: str) -> Path:
    """The MARKDOWN root for *store*. Does not create anything.

    ``"default"`` resolves to ``memory.workspace_dir()``
    (``config_dir()/"workspace"``) so no existing install's ``preferences.md``,
    ``projects.md`` or ``history/`` moves. A declared name resolves to
    ``config_dir()/memory_stores/<name>``.

    NOT the home of the FTS index for every store — the default store's index
    sits in the data-home root instead. :func:`memory_index_path_for` owns that.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    return _named_store_dir(name)


def memory_index_path_for(store: str) -> Path:
    """The FTS5 index file for *store*. Does not create anything.

    ``"default"`` resolves to ``config_dir()/memory_index.db``, the data-home
    root — where the index of every existing install already sits, and the only
    place the off-store consumers look for it: the snapshot ``memory``
    component's ``files`` tuple, ``portability``'s export/import zip and
    ``scripts/sync-to-remote.sh`` all name it root-relative. So this is
    deliberately NOT ``memory_store_dir_for(store)``'s answer for the default
    store; moving it there would silently drop the index from every backup while
    a restore wrote a copy nothing reads.

    A NAMED store's index lives inside that store's own directory, beside the
    markdown tree it describes, which is what makes the index per-store and puts
    it behind the ``memory_stores/`` fence. Whichever step gives the off-store
    consumers a per-store view owns extending them; until then a named store's
    index is simply outside their reach.

    The index is fully DERIVED — ``MemoryStore.rebuild_index`` regenerates it
    from preferences.md, projects.md and history/*.md and reads no index state —
    so a store whose index is not backed up loses search results until the next
    rebuild, never memory.
    """
    from kiro_crew.memory import INDEX_DB_FILE

    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / INDEX_DB_FILE
    return _named_store_dir(name) / INDEX_DB_FILE


def resolve_store_path(store: str) -> Path:
    """The VECTOR FILE (semantic/episodic/lessons SQLite) for *store*.

    ``"default"`` resolves to ``config_dir()/"memory.db"`` — byte-exact with
    ``VectorMemoryStore()``'s own default, so the default store keeps the file
    it already has. A named store gets ``memory.db`` inside its own directory,
    which is also what scopes ``VectorMemoryStore.init``'s owner-only
    tightening of ``db_path.parent`` to that store.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.config.loader import config_dir

        return config_dir() / MEMORY_DB_FILE
    return _named_store_dir(name) / MEMORY_DB_FILE


def named_store_of_db(path: Path) -> str:
    """The NAMED store whose vector file is *path*, or ``""`` when it is not one.

    The inverse of :func:`resolve_store_path`, and the only POSITIVE spelling of
    "this file is a crew silo". Answers ``""`` for the default store, for an eval
    or import destination, for a bare temp path, and for anything malformed —
    which is the whole point. The negation a caller would otherwise reach for,
    ``path != config_dir()/"memory.db"``, is true of four real non-silo paths
    (the eval runner's ``ws/"vector_memory.db"``, the bench ingest path, the
    onboarding importer's ``destination/"memory.db"``, and every ``tmp_path`` in
    the suite), so it would hand each of them silo treatment.

    The containment test is IDENTITY, for the reason spelled out in
    :func:`_named_store_dir`: with ``memory_stores/acme`` symlinked at
    ``memory_stores/finance``, a resolved-parent check still sees the root and
    would answer ``"acme"`` for a file that physically belongs to ``finance`` —
    naming the alias rather than the store, which is the same aliasing hole the
    forward direction already refuses.
    """
    if path.name != MEMORY_DB_FILE:
        return ""
    parent = path.parent
    name = parent.name
    # ``named_store_or_empty`` rather than a bare shape check, so the literal
    # ``memory_stores/default/`` answers "" here too. That directory is
    # unreachable through ``resolve_store_path`` (which maps the name to the
    # data-home root before composing a path), but a caller handing this function
    # an arbitrary path must not be told the name of the GLOBAL store.
    if named_store_or_empty(name) != name or memory_store_name_defect(name) is not None:
        return ""
    try:
        root = memory_stores_root().resolve()
        if parent.resolve() != root / name:
            return ""
    except OSError:
        return ""
    return name


def declared_store_names() -> list[str]:
    """Every store name a whole-install pass covers: the DEFAULT store first, then the rest.

    ONE enumeration, because a pass that builds its own is a pass that can disagree with
    another about which stores exist -- and a store missing from one of them is a store
    whose contents that pass reports nothing about while still printing a verdict.

    Names come off the operator's DECLARED table through :func:`usable_store_names`, the
    one filter every membership test runs through. A directory listing of
    ``memory_stores/`` is deliberately NOT used: it would adopt a silo the config no
    longer declares, or one a restore dropped in, and then treat it as the operator's.

    DEFAULT FIRST, then sorted -- not sorted overall. The default store is the one every
    install has, so it leads every report; a plain sort buries it wherever the alphabet
    puts it and makes two passes over the same install list in different orders.

    Never raises. A config that cannot be read degrades to the default store alone, the
    same floor :func:`_declared_stores` falls back to.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        declared = usable_store_names(KiroCrewConfig.load().memory_stores)
    except Exception:
        logger.warning(
            "could not enumerate declared memory stores; using %r alone",
            DEFAULT_MEMORY_STORE,
            exc_info=True,
        )
        return [DEFAULT_MEMORY_STORE]
    return [DEFAULT_MEMORY_STORE, *sorted(declared - {DEFAULT_MEMORY_STORE})]


def owned_store_path(store: str) -> Path | None:
    """*store*'s vector file, or ``None`` when the resolution does not belong to it.

    The confirm half of "resolve a declared name", and it exists because
    :func:`resolve_store_path` DEGRADES rather than raising: a name the memoized view no
    longer knows resolves onto the DEFAULT store's file, which exists -- so a caller that
    skips the check silently reads, reports, or copies the operator's own memory under
    another store's name. Every whole-install pass needs the same three lines, so they
    live here once instead of being remembered three times.

    ``None`` rather than an exception: each caller wants to skip that store and carry on
    with the rest, and the ones that must be loud about it say so themselves.
    """
    try:
        path = resolve_store_path(store)
    except Exception:
        logger.warning("memory store %r has no resolvable vector file", store, exc_info=True)
        return None
    if named_store_or_empty(store) and named_store_of_db(path) != store:
        logger.warning(
            "memory store %r resolved to %s, which is not that store's own file", store, path
        )
        return None
    return path


def ensure_memory_store_dir(store: str) -> Path:
    """Create *store*'s markdown root owner-only and return it.

    The stores ROOT is created and tightened before its first child exists,
    which is what the Windows half depends on: ``restrict_dir_to_owner``'s
    grants carry ``(OI)(CI)``, so a store directory created inside an
    already-tightened root inherits owner-only access instead of landing on the
    creating token's default DACL.

    ``"default"`` is returned untouched: its root is the pre-existing
    ``workspace/`` tree, created and owned by ``MemoryStore.init()``, and
    creating or tightening it from here would change the default path.
    """
    name = resolve_declared_store(store)
    if name == DEFAULT_MEMORY_STORE:
        from kiro_crew.memory import workspace_dir

        return workspace_dir()
    from kiro_crew import platform_compat

    platform_compat.make_owner_only_dir(memory_stores_root())
    target = _named_store_dir(name)
    platform_compat.make_owner_only_dir(target)
    return target
