"""An artifact edit must reach the bucket. "Artifact" was treated as "write-once".

``run_backup_cycle`` skipped any object under the artifact prefix once it had been uploaded
before, on a comment that said "artifacts are never rewritten". That is false, and the
gateway's own store says so: ``ArtifactStore._write_artifact`` rewrites ``current.html`` and
``meta.json`` at the SAME path on every update, and only ``versions/<n>.html`` is a new file
per version. So the skip dropped every edit after the first, permanently -- the bucket held
version 1 while the task served version 7, and a task replacement restored the stale one.

The skip is kept for the versions directory, which is what it was reasoning about and where
the volume is. The live pair is hashed like any other file.
"""

from __future__ import annotations

import os

from container.backup import layout, run_backup_cycle, run_sidecar
from container.backup.state import BackupState, state_path
from container.backup.store import InMemoryObjectStore

from .test_backup_sidecar import _fk, _write, make_settings


def test_an_edited_artifact_reaches_the_bucket(tmp_path) -> None:
    """The finding, stated as the property it broke."""
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()
    live = s.artifacts_dir / "slug1" / "current.html"
    key = _fk(s, "data/artifacts/slug1/current.html")

    _write(live, b"<p>version 1</p>")
    run_backup_cycle(s, store, state)
    assert store.get(key) == b"<p>version 1</p>"

    _write(live, b"<p>version 2, edited</p>")
    run_backup_cycle(s, store, state)
    assert store.get(key) == b"<p>version 2, edited</p>", "the edit never reached the bucket"


def test_an_unchanged_artifact_is_not_re_uploaded_within_a_run(tmp_path) -> None:
    """Non-vacuity: removing detection entirely would pass the test above.

    Content that does not change must not be re-uploaded every cycle. Under the unified
    predicate the snapshot and the live body are spared by the SAME mechanism -- their stored
    content hash matches -- rather than by a separate write-once path. The first cycle
    establishes each hash; the second skips both.
    """
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    state = BackupState()
    live = s.artifacts_dir / "slug1" / "current.html"
    snap = s.artifacts_dir / "slug1" / "versions" / "1.html"
    _write(live, b"<p>v1</p>")
    _write(snap, b"<p>v1</p>")

    run_backup_cycle(s, store, state)
    second = run_backup_cycle(s, store, state)

    assert store.put_count[_fk(s, "data/artifacts/slug1/current.html")] == 1
    assert store.put_count[_fk(s, "data/artifacts/slug1/versions/1.html")] == 1
    # Both spared, and both by the hash: neither re-uploaded on the second cycle.
    assert second.skipped_unchanged >= 2, "unchanged content was re-uploaded"


def test_a_seeded_snapshot_is_re_uploaded_once_then_stable(tmp_path) -> None:
    """A restart seeds keys from the bucket with NO hash, and a seeded key is UNKNOWN.

    Key presence is not evidence this version was uploaded, so the first cycle after a restart
    re-uploads the snapshot to establish a task-produced content hash. This is the write-once
    seeded-key finding's fix: the old code skipped on presence and kept stale bytes forever.
    The cost is exactly one re-upload per snapshot per restart -- the NEXT cycle has the hash
    and skips, so it is not re-uploaded every cycle.
    """
    s = make_settings(tmp_path)
    store = InMemoryObjectStore()
    snap = s.artifacts_dir / "slug1" / "versions" / "1.html"
    live = s.artifacts_dir / "slug1" / "current.html"
    _write(snap, b"payload")
    _write(live, b"<p>live</p>")
    run_sidecar(s, store=store, max_cycles=1)
    assert store.put_count[_fk(s, "data/artifacts/slug1/versions/1.html")] == 1

    os.unlink(state_path(s))  # a restart: local state gone, bucket intact
    run_sidecar(s, store=store, max_cycles=2)

    # Re-uploaded ONCE after the restart (seeded key had no hash), not skipped-on-presence and
    # not re-uploaded on every one of the two post-restart cycles.
    assert store.put_count[_fk(s, "data/artifacts/slug1/versions/1.html")] == 2


def test_the_write_once_predicate_reads_a_path_component(tmp_path) -> None:
    """A slug containing the word must not claim the exemption."""
    s = make_settings(tmp_path)
    prefix = layout.artifact_prefix(s)
    assert layout.is_write_once_artifact(s, prefix + "slug1/versions/3.html")
    assert not layout.is_write_once_artifact(s, prefix + "slug1/current.html")
    assert not layout.is_write_once_artifact(s, prefix + "slug1/meta.json")
    assert not layout.is_write_once_artifact(s, prefix + "my-versions-slug/current.html")
    assert not layout.is_write_once_artifact(s, "data/sessions/abc.jsonl")
