"""Tests for Knowledge global budget and rate controls.

Covers:
- KnowledgeConfig new field defaults
- Global sweep chunk budget enforcement in watcher
- EmbedRateLimiter token bucket
- extraction_model resolution in _install_knowledge_agent
- extraction_pool_size in LLMPool
"""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KnowledgeConfig

# --- Config defaults ---


class TestKnowledgeConfigBudgetDefaults:
    def test_sweep_chunk_budget_default_500(self):
        c = KnowledgeConfig()
        assert c.sweep_chunk_budget == 500

    def test_embed_rate_limit_default_120(self):
        c = KnowledgeConfig()
        assert c.embed_rate_limit == 120

    def test_extraction_model_default_empty(self):
        c = KnowledgeConfig()
        assert c.extraction_model == ""

    def test_extraction_pool_size_default_3(self):
        c = KnowledgeConfig()
        assert c.extraction_pool_size == 3

    def test_sweep_chunk_budget_zero_is_unbounded(self):
        c = KnowledgeConfig(sweep_chunk_budget=0)
        assert c.sweep_chunk_budget == 0

    def test_embed_rate_limit_zero_is_unlimited(self):
        c = KnowledgeConfig(embed_rate_limit=0)
        assert c.embed_rate_limit == 0


# --- EmbedRateLimiter ---

class TestEmbedRateLimiter:
    def test_zero_rate_is_noop(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=0)
        # Should not block
        asyncio.run(limiter.acquire())

    def test_high_rate_does_not_block(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=10000)
        tokens_before = limiter._tokens
        # "Does not block" means the sleeping slow path is never reached:
        # patch asyncio.sleep and assert it was never awaited (a wall-clock
        # bound here would only measure asyncio.run() setup cost, which flakes
        # under CI load). Note: ingestion.py does a plain `import asyncio`, so
        # this rebinds asyncio.sleep process-wide for the duration of the
        # block; asyncio.run() internals never call asyncio.sleep, and the
        # patch is reverted on exit.
        fake_sleep = AsyncMock()
        with patch("kiro_crew.knowledge.ingestion.asyncio.sleep", fake_sleep):
            asyncio.run(limiter.acquire())
        fake_sleep.assert_not_awaited()
        # The fast path must still consume exactly one token, proving
        # acquire() did real work rather than returning early.
        assert limiter._tokens == tokens_before - 1.0

    def test_rate_limit_setter_resets_bucket(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=1)
        limiter.rate_limit = 10000
        assert limiter.rate_limit == 10000

    def test_get_embed_rate_limiter_reads_config(self):
        import kiro_crew.knowledge.ingestion as ing_mod
        from kiro_crew.knowledge.ingestion import get_embed_rate_limiter

        # Reset the singleton
        ing_mod._embed_rate_limiter = None
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.embed_rate_limit = 200
            limiter = get_embed_rate_limiter()
            assert limiter.rate_limit == 200
        ing_mod._embed_rate_limiter = None


# --- Sweep chunk budget ---

class TestSweepChunkBudget:
    def test_sweep_budget_read_from_config(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 1000
            assert KnowledgeWatcher._sweep_chunk_budget() == 1000

    def test_sweep_budget_zero_means_unbounded(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 0
            assert KnowledgeWatcher._sweep_chunk_budget() == 0


# --- Single-file (local_file) sweep budget ---
#
# The folder loop checks the global sweep budget before every source and charges
# scan_source's chunks_ingested back after each scan. The single-file local_file
# loop shares that counter: without the gate, a library with many changed
# local_file sources re-ingests everything in one unpaced burst — and a source
# registered with no mtime/content_hash bookkeeping reads as changed to every
# sweep, so the burst repeats until each such source is read.


def _single_file_watcher(store, budget: int, chunks_per_file: int = 1, commit: bool = True):
    """A KnowledgeWatcher whose pipeline reports ``chunks_per_file`` per ingest.

    The fake ``ingest_file`` reports extraction progress through ``on_progress``
    (the attempted count the watcher charges) and, when ``commit`` is true, the
    committed chunk ids through ``on_committed`` — the same callbacks the real
    pipeline invokes. ``commit=False`` models a post-extraction partial failure:
    the LLM calls were spent but the pipeline rolled the write back and never
    invoked ``on_committed``.
    """
    from kiro_crew.knowledge.watcher import KnowledgeWatcher

    pipeline = MagicMock()
    pipeline.embedder = None
    ingested_uris: list[str] = []

    async def fake_ingest(path, **kwargs):
        ingested_uris.append(path)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            for i in range(chunks_per_file):
                on_progress("extracting", i + 1, chunks_per_file)
        if commit:
            on_committed = kwargs.get("on_committed")
            if on_committed is not None:
                on_committed(
                    [f"item-{len(ingested_uris)}-{i}" for i in range(chunks_per_file)])
            # The real finalize hop stamps last_synced on the source; the
            # watcher's least-recently-synced-first ordering rotates on it.
            sid = kwargs.get("source_id")
            if sid:
                store.update_source(
                    sid, last_synced=f"2026-01-01T00:00:{len(ingested_uris):02d}")
        return "job-id"

    pipeline.ingest_file = AsyncMock(side_effect=fake_ingest)
    watcher = KnowledgeWatcher(store=store, pipeline=pipeline)
    watcher._maybe_reembed_stale = AsyncMock()  # type: ignore[method-assign]
    watcher._maybe_dedup_sweep = AsyncMock()  # type: ignore[method-assign]
    watcher._sweep_chunk_budget = lambda: budget  # type: ignore[method-assign]
    return watcher, ingested_uris


def _add_local_files(store, tmp_path, count: int) -> list[str]:
    """Register ``count`` changed local_file sources (no mtime/hash recorded).

    No stored mtime/content_hash is exactly the state a deferred explicit
    import leaves behind, so every one of these reads as changed to the sweep.
    """
    sids = []
    for i in range(count):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# doc {i}")
        sids.append(store.add_source(f"doc{i}.md", "local_file", str(f)))
    return sids


def _props_of(store, sid: str) -> dict:
    raw = store.db.execute(
        "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()["properties"]
    return json.loads(raw or "{}")


class TestSingleFileSweepBudget:
    @pytest.fixture()
    def store(self, tmp_path):
        from kiro_crew.knowledge.store import KnowledgeStore

        s = KnowledgeStore(str(tmp_path / "knowledge.db"))
        yield s
        s.close()

    @pytest.mark.asyncio
    async def test_sweep_stops_at_budget(self, store, tmp_path):
        """A sweep over many changed local_file sources stops at the budget."""
        _add_local_files(store, tmp_path, 5)
        watcher, ingested = _single_file_watcher(store, budget=2, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2

    @pytest.mark.asyncio
    async def test_attempted_chunk_count_is_charged(self, store, tmp_path):
        """The charge is the per-file attempted chunk count, not one per file.

        Budget 10 with 6 chunks per file: after the first file 6 < 10 so the
        second still runs; after the second 12 >= 10 so the third is deferred.
        (On the commit path attempted == committed, so this also pins the
        committed count.)
        """
        _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(store, budget=10, chunks_per_file=6)

        await watcher._scan()

        assert len(ingested) == 2

    @pytest.mark.asyncio
    async def test_deferred_sources_keep_no_bookkeeping_and_resume(self, store, tmp_path):
        """Deferral records no mtime/content_hash, so the next sweep resumes.

        Asserted as a partition (exactly two ingested, the rest untouched)
        rather than by identity: the driving query has no ORDER BY, so which
        two rows a sweep reaches first is a query-plan detail, not a contract.
        """
        sids = _add_local_files(store, tmp_path, 4)
        watcher, ingested = _single_file_watcher(store, budget=2, chunks_per_file=1)

        await watcher._scan()

        assert len(ingested) == 2
        with_bookkeeping = [sid for sid in sids if "mtime" in _props_of(store, sid)]
        assert len(with_bookkeeping) == 2
        for sid in sids:
            props = _props_of(store, sid)
            if sid in with_bookkeeping:
                assert "content_hash" in props
            else:
                assert "mtime" not in props, "a deferred source must stay resumable"
                assert "content_hash" not in props

        # The next sweep (fresh budget) picks up where this one stopped.
        await watcher._scan()
        assert len(ingested) == 4

    @pytest.mark.asyncio
    async def test_folder_loop_consumption_defers_single_files(self, store, tmp_path):
        """Both loops share one counter: a folder that spends the whole budget
        leaves nothing for the single-file loop's ingests that sweep."""
        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder))
        _add_local_files(store, tmp_path, 2)

        watcher, ingested = _single_file_watcher(store, budget=5, chunks_per_file=1)
        watcher._folder_watcher.scan_source = AsyncMock(  # type: ignore[method-assign]
            return_value={"chunks_ingested": 5})

        await watcher._scan()

        assert ingested == []

    @pytest.mark.asyncio
    async def test_zero_budget_leaves_the_sweep_unbounded(self, store, tmp_path):
        """budget=0 disables the bound, matching the folder loop's contract."""
        _add_local_files(store, tmp_path, 4)
        watcher, ingested = _single_file_watcher(store, budget=0, chunks_per_file=100)

        await watcher._scan()

        assert len(ingested) == 4

    @pytest.mark.asyncio
    async def test_exhausted_budget_still_marks_vanished_files_missing(self, store, tmp_path):
        """Zero-cost status upkeep survives budget exhaustion.

        The gate defers reads and ingests with a per-row ``continue`` below the
        existence check, never a loop-level ``break``, so a vanished file's
        'missing' marker still lands on a sweep whose folder sources spent the
        whole budget.
        """

        folder = tmp_path / "vault"
        folder.mkdir()
        store.add_source("vault", "local_folder", str(folder))
        gone = tmp_path / "gone.md"
        gone.write_text("# gone")
        sid = store.add_source("gone.md", "local_file", str(gone))
        store.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
        store.db.commit()
        gone.unlink()

        watcher, ingested = _single_file_watcher(store, budget=5, chunks_per_file=1)
        watcher._folder_watcher.scan_source = AsyncMock(  # type: ignore[method-assign]
            return_value={"chunks_ingested": 5})

        await watcher._scan()

        assert ingested == []
        status = store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()["sync_status"]
        assert status == "missing"

    @pytest.mark.asyncio
    async def test_contended_sweeps_rotate_across_sources(self, store, tmp_path):
        """Under sustained contention every source makes progress.

        The sweep orders local_file rows least-recently-synced first, and a
        served source's fresh ``last_synced`` moves it behind rows still
        waiting — so even when every source changes every sweep and the budget
        admits one file per sweep, three sweeps serve three DIFFERENT sources
        rather than re-serving whichever row a stable query order puts first.
        """
        paths = [tmp_path / f"doc{i}.md" for i in range(3)]
        _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(store, budget=1, chunks_per_file=1)

        for sweep in range(3):
            # Every file changes before every sweep: new content, newer mtime.
            for p in paths:
                p.write_text(f"# rev {sweep} of {p.name}")
                os.utime(p, (2_000_000_000 + sweep, 2_000_000_000 + sweep))
            await watcher._scan()

        assert len(ingested) == 3
        assert len(set(ingested)) == 3, (
            "a contended sweep must rotate, not re-serve the same source")

    @pytest.mark.asyncio
    async def test_partial_failure_charges_and_stays_resumable(self, store, tmp_path):
        """A rolled-back ingest still charges its spent extraction calls, and
        keeps no bookkeeping so the next sweep can retry it.

        The pipeline invokes ``on_committed`` only on the fully-committed
        branch; extraction already spent one LLM call per chunk by then. If the
        watcher charged only committed chunks, repeated post-extraction
        failures would spend without bound; if it persisted mtime/content_hash
        anyway, the changed file would never be re-read.
        """
        sids = _add_local_files(store, tmp_path, 3)
        watcher, ingested = _single_file_watcher(
            store, budget=4, chunks_per_file=2, commit=False)

        await watcher._scan()

        # Charged 2 attempted chunks per failed ingest: 2, then 4 >= 4 — the
        # third source is deferred, so the failure loop is budget-bounded.
        assert len(ingested) == 2
        # And nothing was recorded, so every source stays retryable.
        for sid in sids:
            assert "mtime" not in _props_of(store, sid)
            assert "content_hash" not in _props_of(store, sid)


# --- Pool size from config ---

class TestPoolSizeConfig:
    def test_default_pool_size(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        assert _get_pool_size({}) == DEFAULT_POOL_SIZE

    def test_configured_pool_size(self):
        from kiro_crew.knowledge.llm_pool import _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 5}}
        assert _get_pool_size(config) == 5

    def test_pool_size_clamped_to_max_10(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 99}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE

    def test_pool_size_clamped_to_min_1(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 0}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE


# --- Extraction model resolution ---

class TestExtractionModelResolution:
    def test_empty_extraction_model_uses_agent_model(self):
        """When extraction_model is empty, _install_knowledge_agent uses agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = ""
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-sonnet-4.5"

    def test_explicit_extraction_model_overrides(self):
        """When extraction_model is set, it overrides agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = "claude-haiku-4.5"
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-haiku-4.5"
