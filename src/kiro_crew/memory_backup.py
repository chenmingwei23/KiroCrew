"""Rotating hot backups of every memory store.

The gap this closes is not subtle. Memory is the one thing here that cannot be
rebuilt from anywhere else — config can be retyped and sessions replayed, but a
superseded preference nobody remembers stating is gone — and until now its whole
durability story was a manual ``kirocrew snapshot``. An operator who never ran it
had no copy, which is exactly how a 36 MB store became 29 bytes with nothing to
restore from.

Deliberately NOT the snapshot machinery. That is a tar of many components with
redaction, upload paths and a purpose model, aimed at moving an install somewhere
else; this is one cheap file copy aimed at surviving the next corruption. Sharing
code would drag redaction and component resolution onto a path that must be able to
run unattended every day and finish in milliseconds.

Uses SQLite's ONLINE BACKUP API (``Connection.backup``), not a file copy. That is
the difference between a backup and a coin flip: the gateway holds the store open
under WAL, so ``shutil.copy`` of ``memory.db`` alone captures a file whose committed
tail lives in a ``-wal`` sibling it did not take, and the result parses cleanly while
missing recent writes. The backup API walks a consistent snapshot with the writer
still running, and produces a single self-contained file with no WAL to pair.

Backups live beside the store they came from, inside ``memory_stores/<name>/`` for a
silo, so a store's backups inherit the fence that store already sits behind and no
new sensitive-path entry is needed. The default store's go under the data home in
their own directory.
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMORY_DB_FILE,
    declared_store_names,
    owned_store_path,
    resolve_store_path,
)

logger = logging.getLogger(__name__)

#: Directory name holding a store's backups, created beside that store's own file.
BACKUP_DIR_NAME = "backups"

#: ``<stem>.<UTC timestamp>.db``. Sorts chronologically as a string, which is what lets
#: retention pick victims without stat-ing every candidate — and keeps the answer stable
#: when a restore copies a file and resets its mtime.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: How many backups of one store to keep. Bounded because this runs unattended: a
#: 36 MB store at one copy a day is ~1 GB a month with no ceiling.
DEFAULT_KEEP = 7

#: Shortest gap between two backups of one store. The heartbeat's tick counter is
#: PER PROCESS and resets on every gateway start, so without this the pass runs once
#: per restart: a gateway restarted five times a day writes five copies and the
#: DEFAULT_KEEP window collapses from seven days to a day and a half. The retention
#: window IS the feature, so a restart must not be able to shrink it.
#:
#: An interval rather than a same-calendar-day test: a boot at 23:50 reaches the
#: offset tick after midnight, and a day compare would take a second copy that night.
MIN_BACKUP_INTERVAL_HOURS = 20


class MemoryBackupFailed(RuntimeError):
    """A store's copy was attempted and did not succeed.

    Distinct from "there was nothing to copy", which :func:`backup_store` reports as
    ``None``. The two need OPPOSITE handling — one is routine, the other means
    durability has stopped for that store — and collapsing them is how a store that
    fails its copy every single day gets counted as "skipped" and logged as nothing.
    """


def backup_dir_for(db_path: Path) -> Path:
    """Where *db_path*'s backups live. Does not create anything."""
    return db_path.parent / BACKUP_DIR_NAME


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(_STAMP_FORMAT)


def _read_only_uri(path: Path) -> str:
    """A read-only SQLite URI for *path*, percent-escaped.

    ``as_uri()`` rather than interpolating the path into ``file:...``: a filename
    containing ``?`` or ``#`` is otherwise parsed as the start of the URI's query or
    fragment, truncating the path so the connection opens a DIFFERENT database. Store
    names are shape-validated, but ``KIROCREW_HOME`` is operator-chosen, so the
    truncation is reachable through the data home. One helper because two spellings of
    one URI is how they diverge.
    """
    return f"{path.absolute().as_uri()}?mode=ro"


def backup_store(db_path: Path, *, now: datetime | None = None) -> Path | None:
    """Copy *db_path* consistently into its backup directory. ``None`` if there is nothing.

    Three outcomes, not two: a ``Path`` on success, ``None`` when there was nothing to
    copy (a store never opened, an empty file — routine on a timer), and
    :class:`MemoryBackupFailed` when a copy was attempted and did not land. The caller
    counts the third; folding it into ``None`` is what made its failure counter
    unreachable and left a permanently failing store logging nothing at all.

    The copy is written to a ``.partial`` name and RENAMED, so an interrupted run
    cannot leave a truncated file that looks like a backup. Rename is atomic within a
    directory, which is why the temporary sits beside the target rather than in a temp
    root on another filesystem.
    """
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None

    out_dir = backup_dir_for(db_path)
    # Owner-only, and created before the first child so the Windows grants carry
    # (OI)(CI) and files landing inside inherit them rather than the default DACL.
    platform_compat.make_owner_only_dir(out_dir)

    target = out_dir / f"{db_path.stem}.{_stamp(now)}.db"
    partial = target.with_suffix(".partial")
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        # Read-only URI so a backup can never be the thing that writes to the store.
        src = sqlite3.connect(_read_only_uri(db_path), uri=True)
        dst = sqlite3.connect(str(partial))
        src.backup(dst)
        dst.close()
        dst = None
        # Verify the STAGED copy before it becomes a backup. Without this an unsound
        # copy is renamed into place, `prune_backups` counts it as the newest, and a
        # real one is deleted to make room -- so the operator loses their last
        # restorable copy at the moment they reach for it. The restore path already
        # checks; checking only there is checking at the wrong end.
        with closing(sqlite3.connect(_read_only_uri(partial), uri=True)) as probe:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MemoryBackupFailed(f"the staged copy of {db_path} failed its integrity check")
        platform_compat.restrict_to_owner(partial)
        replace_with_retry(partial, target)
        return target
    except MemoryBackupFailed:
        raise
    except Exception as exc:
        raise MemoryBackupFailed(f"memory backup of {db_path} failed: {exc}") from exc
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("closing a backup connection failed", exc_info=True)
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            logger.debug("removing a partial backup failed", exc_info=True)


def list_backups(db_path: Path) -> list[Path]:
    """*db_path*'s backups, NEWEST FIRST.

    Sorted by the stamped NAME rather than mtime, because a copied or restored file
    carries whatever mtime the copy gave it while its name still says when its contents
    were taken.
    """
    out_dir = backup_dir_for(db_path)
    if not out_dir.is_dir():
        return []
    return sorted(out_dir.glob(f"{db_path.stem}.*.db"), reverse=True)


def prune_backups(db_path: Path, keep: int = DEFAULT_KEEP) -> int:
    """Delete all but the *keep* newest backups of *db_path*. Returns how many went.

    ``keep`` below 1 is treated as 1. Retention exists to bound disk, and a policy that
    can empty the directory turns the feature into a scheduled deletion — the opposite
    of the point.
    """
    keep = max(1, keep)
    victims = list_backups(db_path)[keep:]
    removed = 0
    for old in victims:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.warning("could not remove old memory backup %s", old, exc_info=True)
    return removed


def _stores_to_back_up() -> list[Path]:
    """Every declared store's vector file, default first.

    Both halves are shared: :func:`memory_stores.declared_store_names` is the one
    enumeration (so this pass and the injection audit cannot disagree about which
    stores exist), and :func:`memory_stores.owned_store_path` is the one
    resolve-then-confirm (so a name the memoized view no longer knows cannot resolve
    onto the DEFAULT store's file and get copied into a silo's backup directory).
    """
    paths: list[Path] = []
    for name in declared_store_names():
        path = owned_store_path(name)
        if path is None:
            logger.warning("memory store %r is not backed up", name)
            continue
        paths.append(path)
    return paths


def back_up_all_stores(keep: int = DEFAULT_KEEP, *, now: datetime | None = None) -> dict[str, int]:
    """Back up and prune every declared store. Returns ``{"backed_up", "pruned", "failed"}``.

    FAIL SOFT PER STORE, and that is the whole reason the loop is here rather than at
    the caller: one unreadable silo must not cost the default store its backup. Counted
    rather than raised so the periodic caller can log a number without a try of its own,
    and so a store that starts failing is visible as a non-zero count.
    """
    result = {"backed_up": 0, "skipped": 0, "pruned": 0, "failed": 0}
    stamp = now or datetime.now(timezone.utc)
    for path in _stores_to_back_up():
        try:
            existing = list_backups(path)
            if existing and _age_hours(existing[0], stamp) < MIN_BACKUP_INTERVAL_HOURS:
                result["skipped"] += 1
                continue
            if backup_store(path, now=stamp) is None:
                continue
            result["backed_up"] += 1
            result["pruned"] += prune_backups(path, keep)
        except MemoryBackupFailed:
            result["failed"] += 1
            logger.warning("memory backup failed for %s", path, exc_info=True)
        except Exception:
            result["failed"] += 1
            logger.warning("memory backup pass failed for %s", path, exc_info=True)
    return result


def _age_hours(backup: Path, now: datetime) -> float:
    """How old *backup* is, read from its STAMPED NAME rather than its mtime.

    A copied or restored file carries whatever mtime the copy gave it while its name
    still says when its contents were taken — and the interval guard has to answer
    about the contents. An unparseable name answers ``inf`` so the guard never SKIPS on
    a name it did not understand: erring toward taking a backup is the safe direction.
    """
    try:
        taken = datetime.strptime(backup.stem.rsplit(".", 1)[-1], _STAMP_FORMAT)
    except ValueError:
        return float("inf")
    return (now - taken.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0


def newest_backup(store: str = DEFAULT_MEMORY_STORE) -> Path | None:
    """The most recent backup of *store*, or ``None``. The restore entry point's input."""
    try:
        db_path = resolve_store_path(store)
    except Exception:
        return None
    backups = list_backups(db_path)
    return backups[0] if backups else None


def restore_from_backup(backup: Path, store: str = DEFAULT_MEMORY_STORE) -> Path:
    """Put *backup* back in place as *store*'s vector file, keeping what it replaces.

    The displaced file is MOVED ASIDE rather than overwritten, under a
    ``.superseded.<stamp>`` name beside it. A restore is performed by someone who has
    already lost data once, and the file they are replacing may be the only copy of
    whatever it still held — so this never destroys, even when what it displaces looks
    worthless.

    Raises rather than returning a flag: a restore is an explicit operator action and a
    silent no-op is the one outcome that must not be possible. Callers surface the
    message.
    """
    if not backup.is_file():
        raise FileNotFoundError(f"backup {backup} does not exist")
    # `closing`, not the connection's own context manager: ``Connection.__exit__``
    # commits or rolls back and never CLOSES. A leaked handle on the recovery path
    # blocks the very rename below on Windows.
    with closing(sqlite3.connect(_read_only_uri(backup), uri=True)) as probe:
        # Refuse a corrupt source BEFORE displacing anything. Restoring a damaged file
        # over a damaged file leaves the operator strictly worse off than before.
        if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError(f"backup {backup} fails its integrity check; refusing to restore")

    target = resolve_store_path(store)
    if target.name != MEMORY_DB_FILE:  # pragma: no cover - resolver contract
        raise ValueError(f"{target} is not a memory store file")

    if target.is_file():
        aside = target.with_name(f"{target.name}.superseded.{_stamp()}")
        replace_with_retry(target, aside)
        logger.info("moved the existing store aside to %s", aside.name)
    # The WAL and SHM of the file just displaced describe a database that is no longer
    # here; left in place SQLite would try to apply them to the restored one.
    for sidecar in (Path(f"{target}-wal"), Path(f"{target}-shm")):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            logger.debug("removing %s failed", sidecar, exc_info=True)

    platform_compat.make_owner_only_dir(target.parent)
    with (
        closing(sqlite3.connect(_read_only_uri(backup), uri=True)) as src,
        closing(sqlite3.connect(str(target))) as dst,
    ):
        src.backup(dst)
    platform_compat.restrict_to_owner(target)
    logger.info("restored memory store %r from %s", store, backup.name)
    return target
