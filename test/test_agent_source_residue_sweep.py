"""Residue sweep for the agent aggregate source.

An agent-document add commits its item chunks, the previous group's deletion and
its ``agent_item_state`` ownership row as three separate autocommit transactions.
A plain cancellation is already covered -- the row is written from inside the
ingest's uncancellable finalize hop -- but a HARD KILL between the item commit
and the row commit still leaves committed items that no state row names. Nothing
on the agent path reaps by absence, so that residue is permanent: the
replacement path keys off the missing row, so the next add of the same document
stores a second copy instead of replacing the first.

``KnowledgeStore.reclaim_agent_source_residue`` removes it. The sweep keys on
"no state row names this item", which is a safe residue test for the agent
aggregate ALONE: a bundle restores ``agent_item_state`` (so an imported agent
document arrives owned), but deliberately carries no ``folder_file_state`` /
``artifact_item_state`` row, so folder- and artifact-backed items arrive unowned
by design. These tests pin both halves: residue is removed, and legitimately
unowned content elsewhere is never touched.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.agent_source import (
    add_agent_document,
    document_slug,
    ensure_agent_source,
    get_state,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

URI = "https://example.invalid/doc"
OTHER_URI = "https://example.invalid/other"


def _one_chunk(text, **kw):
    return [{"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s._close_all_for_tests()


@pytest.fixture()
def pipeline(kstore):
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        side_effect=lambda contents: [
            {"category": "document", "summary": "s", "entities": []} for _ in contents
        ]
    )
    chunker = MagicMock()
    for m in ("chunk", "chunk_markdown", "chunk_code", "chunk_slides"):
        getattr(chunker, m).side_effect = _one_chunk
    return IngestionPipeline(store=kstore, extractor=extractor, chunker=chunker,
                             reader=FileReader(), embedder=None, dedup_enabled=False)


def _live_ids(store, source_id):
    return {r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}


def _bodies(store, source_id, needle):
    return [r["content"] for r in store.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()
        if needle in r["content"]]


def _simulate_hard_kill(store, source_id, slug):
    """Drop the ownership row while leaving its items committed.

    This is exactly the issue's reproducer: a process killed between the item
    commit and the state-row commit. The items stay, the row that named them is
    gone.
    """
    store.db.execute(
        "DELETE FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug))
    store.db.commit()


class TestResidueIsRemoved:
    @pytest.mark.asyncio
    async def test_items_an_interrupted_ingest_orphaned_are_swept(
        self, kstore, pipeline
    ):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Doc", content="orphan body", source_uri=URI)
        assert _bodies(kstore, sid, "orphan body")

        _simulate_hard_kill(kstore, sid, document_slug(URI))
        # Residue: the items are present but no row names them.
        assert _bodies(kstore, sid, "orphan body")

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        assert not _bodies(kstore, sid, "orphan body")

    @pytest.mark.asyncio
    async def test_a_re_add_after_the_sweep_stores_one_copy_not_two(
        self, kstore, pipeline
    ):
        # The point of the fix: once residue is cleared, re-adding the same
        # document is a clean first add rather than a silent duplicate.
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Doc", content="dup body", source_uri=URI)
        _simulate_hard_kill(kstore, sid, document_slug(URI))

        kstore.reclaim_agent_source_residue()
        await add_agent_document(
            pipeline, title="Doc", content="dup body", source_uri=URI)

        assert len(_bodies(kstore, sid, "dup body")) == 1
        assert set(get_state(kstore, sid, document_slug(URI))[1]) == _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_only_the_orphaned_document_is_removed(self, kstore, pipeline):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Keep", content="keep body", source_uri=OTHER_URI)
        await add_agent_document(
            pipeline, title="Gone", content="gone body", source_uri=URI)

        _simulate_hard_kill(kstore, sid, document_slug(URI))
        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        assert _bodies(kstore, sid, "keep body")
        assert not _bodies(kstore, sid, "gone body")
        # The surviving document still owns exactly its own items.
        assert set(get_state(kstore, sid, document_slug(OTHER_URI))[1]) == _live_ids(kstore, sid)


class TestOwnedAndUnownableContentIsSafe:
    @pytest.mark.asyncio
    async def test_a_fully_owned_store_is_left_untouched(self, kstore, pipeline):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="A", content="a body", source_uri=URI)
        await add_agent_document(
            pipeline, title="B", content="b body", source_uri=OTHER_URI)
        before = _live_ids(kstore, sid)

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert _live_ids(kstore, sid) == before

    def test_no_agent_source_is_a_no_op(self, kstore):
        # A store that never used the agent path has no agent:// row at all.
        assert kstore.reclaim_agent_source_residue() == 0

    def test_a_deduped_marker_row_does_not_make_its_winner_residue(
        self, kstore
    ):
        # A deduped row owns an EMPTY group: its items were adopted by the
        # winner's row and are named THERE. The sweep must read the winner's
        # group, not conclude the loser owns nothing and sweep the shared items.
        sid, _ = ensure_agent_source(kstore)
        winner = kstore.add_item("W", "shared", "document", source_id=sid,
                                 content_hash="h")
        now = "2024-01-01T00:00:00"
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "winner-slug", "h", json.dumps([winner]), now, "W", "active",
             "u1"))
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "loser-slug", "h", "[]", now, "L", "deduped", "u2"))
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert winner in _live_ids(kstore, sid)

    def test_an_unreadable_group_fails_safe_and_sweeps_nothing(self, kstore):
        # A corrupt item_ids value names nothing we can trust. The fail-safe
        # choice is to leave its source's items alone, not to treat them as
        # unowned residue and delete them.
        sid, _ = ensure_agent_source(kstore)
        item = kstore.add_item("X", "body", "document", source_id=sid)
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "corrupt", "h", "{not json", "2024-01-01T00:00:00", "X",
             "active", "u"))
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # The one real item has no readable owner, but a corrupt row is not
        # proof of residue, so nothing is swept.
        assert removed == 0
        assert item in _live_ids(kstore, sid)


class TestOtherSourcesAreNeverTouched:
    def test_artifact_and_folder_sources_are_out_of_scope(self, kstore):
        # The whole safety argument is that the sweep is agent:// only. An
        # unowned item under an artifact or folder source is a legitimately
        # imported bundle item (those two tables are not restored by import),
        # and must survive.
        ensure_agent_source(kstore)
        art_sid = kstore.add_source(
            name="Artifacts", source_type="artifact", uri="artifact://",
            properties={})
        folder_sid = kstore.add_source(
            name="Folder", source_type="local_folder", uri="file:///f",
            properties={})
        art_item = kstore.add_item("A", "imported art", "document",
                                   source_id=art_sid)
        folder_item = kstore.add_item("F", "imported file", "document",
                                      source_id=folder_sid)
        # Neither has any state row -- exactly how a bundle leaves them.

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert art_item in _live_ids(kstore, art_sid)
        assert folder_item in _live_ids(kstore, folder_sid)

    def test_residue_also_held_by_another_source_is_detached_not_destroyed(
        self, kstore
    ):
        # A residue item the agent source shares with another source (a dedup
        # co-location) must move to the surviving holder, not be destroyed:
        # delete_items_batch_in_txn is called with owner_source_id so the item
        # survives under its other holder.
        sid, _ = ensure_agent_source(kstore)
        other = kstore.add_source(
            name="Folder", source_type="local_folder", uri="file:///f",
            properties={})
        item = kstore.add_item("Shared", "shared body", "document",
                               source_id=sid, content_hash="sh")
        # The other source co-holds the same item via a location row.
        kstore.add_source_location(item, other)
        # No agent_item_state row names it -> residue from the agent side.

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        # The item is NOT gone -- ownership moved to the folder source.
        row = kstore.db.execute(
            "SELECT source_id FROM items WHERE id = ?", (item,)).fetchone()
        assert row is not None, "co-held residue was destroyed instead of detached"
        assert row["source_id"] == other
        # The agent source has no location row for it.
        agent_loc = kstore.db.execute(
            "SELECT 1 FROM source_locations WHERE item_id = ? AND source_id = ?",
            (item, sid)).fetchone()
        assert agent_loc is None

    def test_an_imported_agent_document_arrives_owned_and_survives(self, kstore):
        # The twin of the artifact case: a bundle DOES restore agent_item_state,
        # so an imported agent document is owned and the sweep leaves it. This is
        # why the sweep is safe on agent:// specifically.
        sid, _ = ensure_agent_source(kstore)
        item = kstore.add_item("I", "imported body", "document", source_id=sid,
                               content_hash="ih")
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "imported-slug", "ih", json.dumps([item]),
             "2024-01-01T00:00:00", "I", "active", "iu"))
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert item in _live_ids(kstore, sid)
