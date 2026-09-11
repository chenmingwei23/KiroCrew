"""The append-only ledger store: one file per unit, the gateway the only writer.

Layout, resolved against the live data home on every call (never captured at
import, so pod isolation and test isolation both keep working)::

    <data home>/crews/<store name>/ledger.jsonl
    <data home>/sessions/<store name>/ledger.jsonl

``<store name>`` is the readable-plus-digest fold of the unit id that
``session_ledger`` and ``work_ledger`` already use, and the raw id lives in the
header (see :func:`ledger_dir` for why the id is not the directory name). Both
files carry a ``.lock`` sibling in the same directory.

A ledger is NON-RECURSIVE inside its root, which is what lets the session root
be shared with the existing flat ``sessions/<key>.jsonl`` transcripts: every
reader of those globs ``*.jsonl`` one level deep, so a ledger in a subdirectory
is invisible to them and neither store can shadow the other. That reuse also
puts session ledgers behind the sandbox's existing ``sessions`` deny; the
``crews`` root carries its own entry on the sensitive-path floor, because a
record a conductor is meant to trust as authority must not be forgeable by an
agent's own file tools.

Three properties are the whole design.

**Append only.** A line, once written, is never rewritten. There is exactly ONE
mutation: on ``open``, trailing bytes that are not a complete line are dropped.
Everything else -- a damaged interior line, an unknown envelope key, a type from
a newer writer -- is handled on the READ side by skipping or ignoring, never by
repairing the file. So two readers of the same bytes always agree, and a reader
is never the thing that changes history.

**Torn is decided by termination, not by taste.** Every append writes
``line + "\\n"`` and fsyncs, so a file that does not end in a newline was
interrupted mid-write. Those trailing bytes are the crash artifact and are
truncated -- unless they happen to parse whole, in which case only the newline
was lost: the record is kept and the next append re-supplies the separator. A
line that IS newline-terminated but does not parse is damage *inside* history,
so it is skipped on read and left on disk. Nothing else can be torn, which is
why this rule needs no heuristics.

**Seq comes from the file, under the lock.** ``seq`` is read back from the tail
inside the critical section on every append rather than trusted from the
in-process cache, so two writers cannot both believe they own the same number.
The read is a bounded window at the end of the file (``_TAIL_WINDOW``), not a
scan, so it costs the same on a ledger with ten lines and one with a million.
``.last_seq`` serves the cached value for callers that only want to look.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.config.paths import data_home
from kiro_crew.ledger.errors import (
    CODE_ALREADY_EXISTS,
    CODE_BAD_HEADER,
    CODE_BAD_THREAD,
    CODE_INVALID_ID,
    CODE_NO_LEDGER,
    LedgerError,
)
from kiro_crew.ledger.schema import (
    KIND_CREW,
    KIND_SESSION,
    Entry,
    Header,
    Ref,
    build_header,
    check_ownership,
    parse_header,
    require_data,
    require_entry_line,
    require_kind,
    require_unit_id,
    serialize,
)
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_ledger import _store_name, resolved_within

logger = logging.getLogger(__name__)

LEDGER_FILE = "ledger.jsonl"
_LOCK_FILE = ".lock"

#: Directory under the data home that holds each kind's units.
_ROOT_DIR: dict[str, str] = {KIND_CREW: "crews", KIND_SESSION: "sessions"}

#: How much of the file's end a tail read covers. One maximum-size entry plus
#: slack, so the newest complete line is inside the window even when it is the
#: largest line the format allows.
_TAIL_WINDOW = 64 * 1024 + 8 * 1024

#: Largest page a single read may materialize.
MAX_PAGE_LIMIT = 500
DEFAULT_PAGE_LIMIT = 50

#: ``resolve`` outcomes.
STATUS_OK = "ok"
STATUS_FORBIDDEN = "forbidden"
STATUS_GONE = "gone"


def now_ms() -> int:
    """Epoch milliseconds, the clock every ``time`` and ``createdAt`` uses."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def ledger_root(kind: str) -> Path:
    """Root directory holding every ledger of *kind*."""
    return data_home() / _ROOT_DIR[require_kind(kind)]


def ledger_dir(kind: str, unit_id: str) -> Path:
    """The validated directory for one unit's ledger. Does not create it.

    The directory is named with the readable-plus-digest fold
    (``session_ledger._store_name``) rather than the raw id, and the raw id is
    persisted in the header instead. Two reasons, and the second is the load
    bearing one:

    * A legitimate id is not always a legitimate FILENAME. A channel session key
      carries a colon (``slack:1712793600.123``), which POSIX accepts and Windows
      refuses, so the raw id as a directory name turns a sanctioned id into an
      ``OSError`` on a supported platform.
    * Identity is the DIGEST over the exact id, so ``Foo`` and ``foo`` get
      distinct directories on a case-insensitive filesystem and two long ids
      sharing a prefix cannot land in one file. The readable half is a
      convenience for a human reading the directory listing and is capped for
      filesystem name limits; it is not the identity, and the fold is not
      reversible -- ``open`` proves it reached the right unit by checking the id
      the header stores.

    Containment is what makes the path safe regardless: the shape gate refuses a
    separator or a NUL in the raw id, and ``resolved_within`` re-checks
    symlink-safely that the resolved path stays under the root.
    """
    require_unit_id(unit_id)
    resolved = resolved_within(ledger_root(kind), _store_name(unit_id))
    if resolved is None:
        raise LedgerError(
            f"path traversal blocked for ledger id: {unit_id!r}",
            code=CODE_INVALID_ID,
            field="id",
        )
    return resolved


def ledger_path(kind: str, unit_id: str) -> Path:
    """The ledger file for one unit."""
    return ledger_dir(kind, unit_id) / LEDGER_FILE


def _lock_path(kind: str, unit_id: str) -> Path:
    return ledger_dir(kind, unit_id) / _LOCK_FILE


@contextmanager
def _open_lock(path: Path) -> Iterator[None]:
    """Hold the advisory lock on *path*, creating the lock file if absent.

    ``"r+"`` -- writable but NOT truncating -- for the reason ``work_ledger``
    documents: ``msvcrt.locking`` needs a writable handle, so ``"r"`` is out,
    while ``"w"`` truncates, and on Windows a truncating open of a file another
    process already holds locked raises a sharing violation instead of waiting.
    That would make the second contender crash before it reached the lock,
    defeating the serialization the lock exists for. ``file_lock`` itself fails
    closed, which is why nothing here has a lock-less fallback.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, "r+") as handle:
        with file_lock(handle.fileno(), exclusive=True):
            yield


# --------------------------------------------------------------------------- #
# Tail scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Tail:
    """What a bounded read of the file's end says about its state.

    ``torn_offset`` and ``needs_newline`` are mutually exclusive: the trailing
    unterminated bytes either parse (the newline was lost, keep the record) or
    they do not (a crash artifact, drop it).

    ``window`` is the bytes that were read, already stripped of any torn tail,
    and ``at_start`` says whether they reach the beginning of the file. Both are
    carried so a caller that needs a SECOND answer about the tail -- does this
    seq exist -- can have it without a second read, and without this function
    parsing the whole window for a caller that does not ask.
    """

    last_seq: int
    torn_offset: int | None
    needs_newline: bool
    empty: bool
    window: bytes = b""
    at_start: bool = True


def _parses_to_object(blob: bytes) -> dict[str, Any] | None:
    """*blob* as a JSON object, or ``None``. Never raises."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _seq_of(blob: bytes) -> int | None:
    """The ``seq`` of the entry *blob* encodes, or ``None`` when it has none."""
    parsed = _parses_to_object(blob)
    if parsed is None:
        return None
    entry = Entry.from_dict(parsed)
    return None if entry is None else entry.seq


def _scan_tail(path: Path) -> _Tail:
    """Read the end of *path* and report seq, torn bytes, and emptiness."""
    size = path.stat().st_size
    if size == 0:
        return _Tail(last_seq=0, torn_offset=None, needs_newline=False, empty=True)
    window = min(size, _TAIL_WINDOW)
    with open(path, "rb") as handle:
        handle.seek(size - window)
        blob = handle.read(window)
    at_start = window == size

    torn_offset: int | None = None
    needs_newline = False
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        trailing = blob[cut + 1 :]
        if _parses_to_object(trailing) is not None:
            needs_newline = True
        else:
            torn_offset = size - len(trailing)
            blob = blob[: cut + 1]

    segments = blob.split(b"\n")
    if not at_start:
        # The window may begin mid-line; that first fragment is not a record.
        segments = segments[1:]
    last_seq = 0
    for segment in reversed(segments):
        stripped = segment.strip()
        if not stripped:
            continue
        seq = _seq_of(stripped)
        if seq is not None:
            last_seq = seq
            break
    if last_seq == 0 and not at_start:
        # Every line in the window was the header-less kind or damaged; only a
        # full scan can answer, and it is the rare path by construction.
        last_seq = _full_scan_last_seq(path)
    return _Tail(
        last_seq=last_seq,
        torn_offset=torn_offset,
        needs_newline=needs_newline,
        empty=False,
        window=blob,
        at_start=at_start,
    )


def _anchor_exists(path: Path, seq: int, tail: _Tail) -> bool:
    """Whether *seq* names a PARSEABLE entry in *path*.

    A thread pointing at a line no reader can parse is a pointer to nothing, so
    the range check ``1 <= seq <= last_seq`` is not enough on its own: a damaged
    interior line at exactly that seq is skipped by every reader, and the group
    would hang off an anchor that never appears.

    Bounded, in three steps, so proving this costs an ordinary append nothing:

    1. The window is already in memory, so an anchor inside it is free. A thread
       anchor is normally recent, which is exactly where that lands.
    2. If the window reached the start of the file, its answer is complete -- the
       whole file was examined.
    3. Only an anchor OLDER than the window falls back to a scan, and that scan
       stops AT the anchor instead of reading to the end.

    An append that passes no ``thread`` never calls this, so the bounded-tail
    cost of the ordinary write path is unchanged.
    """
    segments = tail.window.split(b"\n")
    if not tail.at_start:
        segments = segments[1:]
    for segment in segments:
        stripped = segment.strip()
        if stripped and _seq_of(stripped) == seq:
            return True
    if tail.at_start:
        return False
    for entry in _iter_entries(path):
        if entry.seq == seq:
            return True
        if entry.seq > seq:
            return False
    return False


def _full_scan_last_seq(path: Path) -> int:
    """The highest seq any parseable line carries. The fallback path only."""
    highest = 0
    for entry in _iter_entries(path):
        if entry.seq > highest:
            highest = entry.seq
    return highest


def _iter_entries(path: Path) -> Iterator[Entry]:
    """Every parseable entry in *path*, oldest first, header excluded.

    A malformed interior line is SKIPPED, not raised on: the log is append-only
    and one damaged line must not hide the history in front of it. The file is
    streamed line by line, so a large ledger costs one line of memory, not its
    size.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="\n") as handle:
            for index, line in enumerate(handle):
                if index == 0:
                    continue  # the header
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None:
                    yield entry
    except FileNotFoundError:
        return


def _read_header_line(path: Path) -> str | None:
    """Line 1 of *path*, or ``None`` when the file has none."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="\n") as handle:
            for line in handle:
                return line.strip()
    except FileNotFoundError:
        return None
    return None


# --------------------------------------------------------------------------- #
# Read results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Page:
    """One page of entries, NEWEST first.

    ``next_before`` is the cursor for the following page, or ``None`` when this
    page reached the oldest entry. It is set only when an older entry actually
    exists, so paging never hands back a phantom empty page at the end.
    """

    entries: tuple[Entry, ...]
    next_before: int | None


@dataclass(frozen=True)
class Resolution:
    """The outcome of following a :class:`~kiro_crew.ledger.schema.Ref`."""

    status: str
    entries: tuple[Entry, ...]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


class Ledger:
    """One unit's append-only ledger.

    Construct through :meth:`create` or :meth:`open`, never directly: both do
    the file-level work (existence, header, torn-tail repair, seq recovery) that
    the instance then assumes has happened.
    """

    __slots__ = ("_kind", "_id", "_path", "_header", "_last_seq", "_needs_newline")

    def __init__(
        self,
        *,
        kind: str,
        unit_id: str,
        path: Path,
        header: Header,
        last_seq: int,
        needs_newline: bool,
    ) -> None:
        self._kind = kind
        self._id = unit_id
        self._path = path
        self._header = header
        self._last_seq = last_seq
        self._needs_newline = needs_newline

    # -- identity ----------------------------------------------------------- #

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def header(self) -> Header:
        return self._header

    @property
    def last_seq(self) -> int:
        """The newest seq this instance knows of. Authoritative only for its own
        appends -- the file is re-read under the lock on every write."""
        return self._last_seq

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Ledger(kind={self._kind!r}, id={self._id!r}, last_seq={self._last_seq})"

    # -- lifecycle ---------------------------------------------------------- #

    @classmethod
    def exists(cls, kind: str, unit_id: str) -> bool:
        """Whether *unit_id*'s ledger file is there. Raises on an invalid id."""
        return ledger_path(kind, unit_id).is_file()

    @classmethod
    def create(cls, kind: str, unit_id: str, **header_fields: Any) -> Ledger:
        """Write a new ledger's header. Refuses if the file already exists.

        Existence is checked twice, the second time under the lock: the first
        check is the cheap answer for the ordinary caller, the second is what
        makes "create refuses an existing ledger" true when two processes race,
        rather than one of them appending a second header to a live file.
        """
        kind = require_kind(kind)
        path = ledger_path(kind, unit_id)
        if path.is_file():
            raise LedgerError(
                f"{kind} ledger {unit_id!r} already exists", code=CODE_ALREADY_EXISTS, field="id"
            )
        header = build_header(kind, unit_id, now_ms(), header_fields)
        line = require_entry_line(serialize(header.to_dict()))
        with _open_lock(_lock_path(kind, unit_id)):
            if path.is_file():
                raise LedgerError(
                    f"{kind} ledger {unit_id!r} already exists",
                    code=CODE_ALREADY_EXISTS,
                    field="id",
                )
            _append_line(path, line, needs_newline=False)
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=0,
            needs_newline=False,
        )

    @classmethod
    def open(cls, kind: str, unit_id: str) -> Ledger:
        """Open an existing ledger, repairing a torn tail if there is one."""
        kind = require_kind(kind)
        path = ledger_path(kind, unit_id)
        if not path.is_file():
            raise LedgerError(f"no {kind} ledger for {unit_id!r}", code=CODE_NO_LEDGER, field="id")
        with _open_lock(_lock_path(kind, unit_id)):
            tail = _scan_tail(path)
            if tail.torn_offset is not None:
                dropped = path.stat().st_size - tail.torn_offset
                _truncate(path, tail.torn_offset)
                logger.warning(
                    "dropped %d torn trailing byte(s) from %s ledger %r",
                    dropped,
                    kind,
                    unit_id,
                )
            raw = _read_header_line(path)
        if not raw:
            raise LedgerError(
                f"{kind} ledger {unit_id!r} has no header line",
                code=CODE_BAD_HEADER,
                field="type",
            )
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise LedgerError(
                f"{kind} ledger {unit_id!r} header is not JSON: {exc}",
                code=CODE_BAD_HEADER,
                field="type",
            ) from exc
        header = parse_header(parsed, kind=kind, unit_id=unit_id)
        return cls(
            kind=kind,
            unit_id=unit_id,
            path=path,
            header=header,
            last_seq=tail.last_seq,
            needs_newline=tail.needs_newline,
        )

    # -- write -------------------------------------------------------------- #

    def append(
        self,
        type: str,
        data: dict[str, Any],
        *,
        src: str,
        thread: int | None = None,
        ref: Ref | dict[str, Any] | None = None,
    ) -> Entry:
        """Append one entry and return it, with ``seq`` and ``time`` filled in.

        Every refusal happens before any byte is written, so a rejected append
        leaves the file identical. ``thread`` is checked against the seq read
        back under the lock, which is also what assigns this entry's own seq.
        """
        require_data(data)
        check_ownership(self._kind, type, src)
        pointer = None if ref is None else (ref if isinstance(ref, Ref) else Ref.from_dict(ref))
        if thread is not None and (
            not isinstance(thread, int) or isinstance(thread, bool) or thread < 1
        ):
            raise LedgerError(
                f"thread must be a positive seq in this ledger: {thread!r}",
                code=CODE_BAD_THREAD,
                field="thread",
            )
        with _open_lock(_lock_path(self._kind, self._id)):
            tail = _scan_tail(self._path)
            if tail.empty:
                raise LedgerError(
                    f"{self._kind} ledger {self._id!r} has no header line",
                    code=CODE_BAD_HEADER,
                    field="type",
                )
            if tail.torn_offset is not None:
                _truncate(self._path, tail.torn_offset)
            if thread is not None and (
                thread > tail.last_seq or not _anchor_exists(self._path, thread, tail)
            ):
                raise LedgerError(
                    f"thread {thread} names no parseable entry in this ledger "
                    f"(newest seq is {tail.last_seq})",
                    code=CODE_BAD_THREAD,
                    field="thread",
                )
            entry = Entry(
                type=type,
                seq=tail.last_seq + 1,
                time=now_ms(),
                src=src,
                data=data,
                thread=thread,
                ref=pointer,
            )
            line = require_entry_line(serialize(entry.to_dict()))
            _append_line(self._path, line, needs_newline=tail.needs_newline)
        self._last_seq = entry.seq
        self._needs_newline = False
        return entry

    # -- read --------------------------------------------------------------- #

    def iter_from(self, seq: int = 1) -> Iterator[Entry]:
        """Every entry from *seq* onward, OLDEST first -- the shape a fold wants."""
        for entry in _iter_entries(self._path):
            if entry.seq >= seq:
                yield entry

    def get(self, seq: int) -> Entry | None:
        """The entry at *seq*, or ``None``.

        Stops as soon as the stream passes *seq*: entries are written in seq
        order, so a miss costs the prefix, not the file.
        """
        for entry in _iter_entries(self._path):
            if entry.seq == seq:
                return entry
            if entry.seq > seq:
                return None
        return None

    def page(self, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> Page:
        """Up to *limit* entries older than *before*, NEWEST first.

        ``before`` is exclusive, so feeding ``next_before`` straight back walks
        the history without repeating or skipping a line.
        """
        return self._page(before, limit, keep=lambda _entry: True)

    def thread_page(
        self, anchor: int, before: int | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> Page:
        """One thread's entries, NEWEST first, the anchor last.

        A thread is a GROUPING key, not a tree: members carry ``thread ==
        anchor`` and the anchor carries its own seq. The anchor is included, so
        the final page of a thread ends with the entry the group hangs off --
        which is what makes a thread readable bottom-up without a second call.
        """
        return self._page(
            before, limit, keep=lambda entry: entry.thread == anchor or entry.seq == anchor
        )

    def _page(self, before: int | None, limit: int, *, keep: Callable[[Entry], bool]) -> Page:
        bound = max(1, min(int(limit), MAX_PAGE_LIMIT))
        # One extra slot answers "is there an older entry" exactly, so the
        # cursor is None precisely when the caller has seen everything.
        window: deque[Entry] = deque(maxlen=bound + 1)
        for entry in _iter_entries(self._path):
            if before is not None and entry.seq >= before:
                continue
            if keep(entry):
                window.append(entry)
        newest_first = list(reversed(window))
        has_more = len(newest_first) > bound
        entries = tuple(newest_first[:bound])
        return Page(entries=entries, next_before=entries[-1].seq if has_more and entries else None)

    def resolve(
        self,
        ref: Ref | dict[str, Any],
        *,
        may_read: Callable[[str, str], bool] | None = None,
    ) -> Resolution:
        """Follow *ref* and return the segment it cites.

        A ref that leaves this unit needs an access decision, and the ABSENCE of
        one is a refusal, not a grant. Omitting ``may_read`` is the easiest call
        shape there is, so letting it mean "allow" would make the default path
        the insecure one and expose the first caller that forgets the argument.
        A ref that stays inside THIS ledger needs no callback: the caller already
        holds this unit open, so there is no boundary left to check.

        ``forbidden`` is decided BEFORE existence. Answering "does that unit
        exist" first would leak the existence of a unit the caller is not allowed
        to read, which is the one thing an access check on a pointer is for --
        so a denied read and an absent unit are deliberately indistinguishable.

        ``gone`` is a normal outcome, not an error: the cited ledger may
        legitimately have been deleted, and the citing entry stays honest about
        having pointed at it.
        """
        pointer = ref if isinstance(ref, Ref) else Ref.from_dict(ref)
        own = pointer.unit == self._kind and pointer.id == self._id
        if not own and (may_read is None or not may_read(pointer.unit, pointer.id)):
            return Resolution(status=STATUS_FORBIDDEN, entries=())
        if own:
            target: Ledger | None = self
        else:
            try:
                target = Ledger.open(pointer.unit, pointer.id)
            except LedgerError:
                target = None
        if target is None:
            return Resolution(status=STATUS_GONE, entries=())
        last = pointer.last_seq
        found = tuple(entry for entry in target.iter_from(pointer.from_seq) if entry.seq <= last)
        return Resolution(status=STATUS_OK, entries=found)


# --------------------------------------------------------------------------- #
# File primitives
# --------------------------------------------------------------------------- #


def _append_line(path: Path, line: str, *, needs_newline: bool) -> None:
    """Append *line* plus its terminator, then fsync.

    ``newline="\\n"`` is load-bearing, not cosmetic: without it Windows
    translates the terminator to ``\\r\\n``, and the byte offsets the torn-tail
    truncation computes stop matching what is on disk.

    *needs_newline* re-supplies a separator the previous write lost -- the one
    case where a record survived but its terminator did not. It PREPENDS rather
    than rewriting that line, so the append-only rule holds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if needs_newline else ""
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{prefix}{line}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _truncate(path: Path, offset: int) -> None:
    """Drop everything at or after *offset* -- the one allowed mutation."""
    with open(path, "r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
