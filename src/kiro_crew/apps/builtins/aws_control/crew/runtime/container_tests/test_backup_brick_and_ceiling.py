"""Two ways a backup cycle could lose more than the file it was handling.

F1 the absorbing brick -- ``iter_backup_files`` yields a config file only ``if
   f.is_file()``, and the two authority files are written at different moments: the slot
   table as soon as the dashboard persists one, the session map not until a session is
   mapped. A task that took a turn but mapped nothing uploads ONE of the two. The next boot
   reads that bucket as PARTIAL and refuses to start -- and refusing means the map is never
   written, so the bucket never gains the second file. The state cannot be left without
   deleting the bucket by hand.

F2 the unbounded read -- ``_read_nofollow`` ended in ``fh.read()`` and ``ObjectStore.put``
   takes ``bytes``, so a cycle's peak memory is its largest file. The agent writes into
   ``artifacts_dir`` by design and nothing bounds what it writes, so one oversized artifact
   could take the task's memory limit with it. An OOM kill loses the whole cycle, including
   every small file that would have been backed up.

Both are the same shape: a failure wider than the thing that caused it.
"""

from __future__ import annotations

import json
import os

import pytest
from container.backup import layout
from container.backup import restore as restore_mod
from container.backup import sidecar as sidecar_mod
from container.backup.state import BackupState, ObjMeta
from container.backup.store import InMemoryObjectStore

from .test_backup_sidecar import make_settings


def _rewrite_same_length_new_content(path, new_content: bytes) -> None:
    """Replace a file's bytes with different bytes of the SAME length, and force a
    strictly-newer mtime.

    The mtime bump makes the test deterministic: two writes could otherwise land in the
    same st_mtime_ns on a coarse-clock filesystem, and then the size+mtime pre-filter would
    skip the rewrite for a reason that is a test artefact, not the behaviour under test. The
    content hash is what actually proves the new bytes were uploaded; the bump only
    guarantees the pre-filter lets the cycle look.
    """
    old = path.stat()
    path.write_bytes(new_content)
    bumped = old.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(bumped, bumped))


# ---------------------------------------------------------------------------
# F1
# ---------------------------------------------------------------------------
def test_a_clean_first_boot_seeds_both_authority_files(tmp_path) -> None:
    """An empty bucket must leave BOTH files on disk, so the first backup uploads both."""
    settings = make_settings(tmp_path)
    result = restore_mod.run_restore(settings, store=InMemoryObjectStore())

    assert result.empty is True
    assert not result.partial
    assert settings.session_map_path.is_file(), "session_map was not seeded"
    assert settings.open_slots_path.is_file(), "open_slots was not seeded"
    for path in (settings.session_map_path, settings.open_slots_path):
        assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_the_seeded_files_make_the_next_boot_complete(tmp_path) -> None:
    """The property that matters is the SECOND boot, so drive both in sequence.

    Seeding is only interesting because of what it prevents, and what it prevents happens
    one boot later. A test that stopped at "the files exist" would pass under a seeding that
    wrote them somewhere the backup does not look.
    """
    first = make_settings(tmp_path / "boot1")
    store = InMemoryObjectStore()
    restore_mod.run_restore(first, store=store)
    sidecar_mod.run_backup_cycle(first, store, BackupState())

    second = make_settings(tmp_path / "boot2")
    result = restore_mod.run_restore(second, store=store)
    assert not result.partial, f"the second boot would refuse to start: missing {result.missing}"
    assert result.restored >= 2


def test_a_bucket_missing_one_authority_file_still_refuses(tmp_path) -> None:
    """The guard must survive the fix: seeding is confined to the EMPTY branch.

    A bucket holding objects but lacking an authority file is the case the refusal was built
    for -- something was uploaded and one of the two was lost. Seeding there would turn a
    real loss into a silent empty slot table, which is the outcome the refusal prevents. So
    this is the test that stops the fix from being widened.
    """
    settings = make_settings(tmp_path)
    keys = layout.config_keys(settings)
    store = InMemoryObjectStore()
    store.put(layout.full_key(settings, keys["open_slots"]), b"{}\n")
    assert store.list(layout.object_prefix(settings)), "the fixture must present a non-empty bucket"

    result = restore_mod.run_restore(settings, store=store)
    assert result.partial is True
    assert result.missing == ["session_map"]


# ---------------------------------------------------------------------------
# F2
# ---------------------------------------------------------------------------
def test_a_file_past_the_ceiling_is_streamed_not_skipped(tmp_path) -> None:
    """The oversized file IS made durable -- streamed, not read whole, not dropped."""
    settings = make_settings(tmp_path)
    (settings.artifacts_dir / "huge.bin").write_bytes(b"x" * 4096)

    store = InMemoryObjectStore()
    result = sidecar_mod.run_backup_cycle(settings, store, BackupState(), max_file_bytes=1024)

    assert result.skipped_too_large == 0
    uploaded = [k for k in store.list("") if "huge.bin" in k]
    assert uploaded, "the oversized file was not uploaded"
    # Streamed through put_stream, and the bytes are the file's bytes -- not truncated
    # at the in-memory ceiling.
    assert store.get(uploaded[0]) == b"x" * 4096


def test_a_streamed_file_carries_a_content_hash(tmp_path) -> None:
    """A streamed file is tracked by a real content hash (computed during upload), not
    size alone -- and an EQUAL-LENGTH rewrite with different bytes is re-uploaded.

    This is the case size-only detection lost: a large file rewritten to the same length
    read as unchanged and was never backed up. It must upload on the rewrite.
    """
    settings = make_settings(tmp_path)
    (settings.artifacts_dir / "huge.bin").write_bytes(b"a" * 4096)
    state = BackupState()

    store = InMemoryObjectStore()
    r1 = sidecar_mod.run_backup_cycle(settings, store, state, max_file_bytes=1024)
    assert r1.uploaded >= 1
    rel = next(k for k in state.objects if "huge.bin" in k)
    first_hash = state.objects[rel].hash
    assert first_hash, "a streamed file must carry a real content hash, not an empty one"
    assert state.objects[rel].streamed is True
    key = next(k for k in store.list("") if "huge.bin" in k)
    assert store.get(key) == b"a" * 4096

    # Rewrite to the SAME length with different content. mtime moves, so the pre-filter
    # re-streams; the content differs, so it counts as an upload and the stored bytes and
    # hash both change. Size-only detection would have skipped this and kept the stale copy.
    _rewrite_same_length_new_content(settings.artifacts_dir / "huge.bin", b"b" * 4096)
    r2 = sidecar_mod.run_backup_cycle(settings, store, state, max_file_bytes=1024)
    assert r2.uploaded >= 1, "an equal-length rewrite must be re-uploaded, not skipped"
    assert store.get(key) == b"b" * 4096, "the bucket still holds the stale bytes"
    assert state.objects[rel].hash != first_hash, "the stored hash did not follow the content"


def test_an_unchanged_streamed_file_is_not_re_streamed(tmp_path) -> None:
    """The pre-filter (same size AND same mtime) skips without re-reading the file."""
    settings = make_settings(tmp_path)
    (settings.artifacts_dir / "huge.bin").write_bytes(b"x" * 4096)
    state = BackupState()

    store = InMemoryObjectStore()
    sidecar_mod.run_backup_cycle(settings, store, state, max_file_bytes=1024)
    r2 = sidecar_mod.run_backup_cycle(settings, store, state, max_file_bytes=1024)
    assert r2.uploaded == 0
    assert r2.skipped_unchanged >= 1


def test_the_rest_of_the_cycle_still_runs(tmp_path) -> None:
    """A small file enumerated alongside the oversized one is still uploaded."""
    settings = make_settings(tmp_path)
    (settings.artifacts_dir / "huge.bin").write_bytes(b"x" * 4096)
    (settings.artifacts_dir / "small.txt").write_text("keep me\n", encoding="utf-8")

    store = InMemoryObjectStore()
    result = sidecar_mod.run_backup_cycle(settings, store, BackupState(), max_file_bytes=1024)

    assert result.uploaded >= 2
    assert any("small.txt" in k for k in store.list("")), "a small file was lost"
    assert any("huge.bin" in k for k in store.list("")), "the oversized file was lost"


def test_a_file_exactly_at_the_ceiling_is_read_whole_not_streamed(tmp_path) -> None:
    """The bound is inclusive: a file exactly at the ceiling takes the in-memory path
    (size + content hash), so it is not streamed and carries a real hash."""
    settings = make_settings(tmp_path)
    (settings.artifacts_dir / "exact.bin").write_bytes(b"x" * 1024)

    store = InMemoryObjectStore()
    state = BackupState()
    result = sidecar_mod.run_backup_cycle(settings, store, state, max_file_bytes=1024)

    assert result.skipped_too_large == 0
    assert any("exact.bin" in k for k in store.list(""))
    rel = next(k for k in state.objects if "exact.bin" in k)
    assert state.objects[rel].hash != "", "an at-ceiling file must be hashed, not streamed"


def test_the_bound_is_applied_at_the_read(tmp_path) -> None:
    """A stat-then-read pair would leave the bound advisory, so the read must carry it.

    Called directly, because the point is that no prior measurement is trusted: the reader
    is handed a file larger than its ceiling and must refuse on what it actually read.
    """
    target = tmp_path / "grown.bin"
    target.write_bytes(b"x" * 4096)
    with pytest.raises(sidecar_mod._TooLarge):
        sidecar_mod._read_nofollow(target, None, max_bytes=1024)

    assert sidecar_mod._read_nofollow(target, None, max_bytes=4096) == b"x" * 4096


def test_the_oversized_file_is_never_materialized(tmp_path, monkeypatch) -> None:
    """The refusal is not the point on its own: the file must not be READ to reach it.

    Refusing after ``fh.read()`` returned would satisfy every other test in this file while
    leaving the OOM exactly where it was -- the allocation is what kills the task, and it
    happens before any size can be compared. So this asserts the size the reader ASKED for,
    which is the only place that property is visible.

    Learned by mutation: replacing the bounded read with an unbounded one left this file
    fully green, because the comparison below it still raised.
    """
    requested: list[int | None] = []
    real_fdopen = sidecar_mod.os.fdopen

    class _RecordingFile:
        def __init__(self, inner):
            self._inner = inner

        def read(self, size=-1):
            requested.append(size)
            return self._inner.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    def _fdopen(*args, **kwargs):
        return _RecordingFile(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(sidecar_mod.os, "fdopen", _fdopen)

    target = tmp_path / "huge.bin"
    target.write_bytes(b"x" * 8192)
    with pytest.raises(sidecar_mod._TooLarge):
        sidecar_mod._read_nofollow(target, None, max_bytes=1024)

    assert requested, "the reader did not go through fdopen, so this test proves nothing"
    assert all(
        size is not None and 0 < size <= 1025 for size in requested
    ), f"the read was not bounded by the ceiling: asked for {requested}"


# ---------------------------------------------------------------------------
# The skip-input matrix: every signal that ONCE concluded "skip" on its own must
# now upload. One case per input, each asserting the file reaches the bucket. This
# is the completeness check the design ruling asked for -- three findings in a row
# were a new input reaching the same false negative, so the guard is a matrix, not
# a single case.
# ---------------------------------------------------------------------------
def test_skip_input_oversized_is_uploaded(tmp_path) -> None:
    """A file above the in-memory ceiling is streamed, not dropped (was: size-ceiling skip)."""
    s = make_settings(tmp_path)
    (s.artifacts_dir / "huge.bin").write_bytes(b"z" * 4096)
    store = InMemoryObjectStore()
    r = sidecar_mod.run_backup_cycle(s, store, BackupState(), max_file_bytes=1024)
    assert r.uploaded >= 1
    assert any("huge.bin" in k for k in store.list(""))


def test_skip_input_equal_length_streamed_rewrite_is_uploaded(tmp_path) -> None:
    """A large file rewritten to the same length uploads again (was: size-equality skip)."""
    s = make_settings(tmp_path)
    f = s.artifacts_dir / "huge.bin"
    f.write_bytes(b"a" * 4096)
    store = InMemoryObjectStore()
    state = BackupState()
    sidecar_mod.run_backup_cycle(s, store, state, max_file_bytes=1024)
    _rewrite_same_length_new_content(f, b"b" * 4096)
    r = sidecar_mod.run_backup_cycle(s, store, state, max_file_bytes=1024)
    assert r.uploaded >= 1
    assert store.get(next(k for k in store.list("") if "huge.bin" in k)) == b"b" * 4096


def test_skip_input_seeded_key_with_no_identity_is_uploaded(tmp_path) -> None:
    """A key present only via seeding (no task hash) uploads (was: key-presence skip)."""
    s = make_settings(tmp_path)
    snap = s.artifacts_dir / "slug1" / "versions" / "1.html"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"payload")
    store = InMemoryObjectStore()
    state = BackupState()
    # Seed the key the way a restart does: size known from a listing, hash unknown.
    rel = "data/artifacts/slug1/versions/1.html"
    state.objects[rel] = ObjMeta(len(b"payload"), "", streamed=False)
    r = sidecar_mod.run_backup_cycle(s, store, state)
    assert r.uploaded >= 1
    assert any("versions/1.html" in k for k in store.list("")), "a seeded key was skipped"


def test_skip_input_same_size_and_mtime_but_different_hash_is_uploaded(tmp_path) -> None:
    """A buffered file rewritten in place with size and mtime preserved uploads, because it
    is read and hashed -- the fast path never applies to a non-streamed entry (was: the
    mtime/size fast path masking an in-place rewrite)."""
    s = make_settings(tmp_path)
    live = s.sessions_dir / "s1.jsonl"
    live.parent.mkdir(parents=True, exist_ok=True)
    frozen = 1_600_000_000 * 10**9
    live.write_bytes(b"A" * 16)
    os.utime(live, ns=(frozen, frozen))
    store = InMemoryObjectStore()
    state = BackupState()
    sidecar_mod.run_backup_cycle(s, store, state)
    live.write_bytes(b"B" * 16)  # same length, different content
    os.utime(live, ns=(frozen, frozen))  # same mtime, as _restore_mtime does
    r = sidecar_mod.run_backup_cycle(s, store, state)
    assert r.uploaded >= 1, "an in-place same-size same-mtime rewrite was skipped"
    assert store.get(next(k for k in store.list("") if "s1.jsonl" in k)) == b"B" * 16
