"""Tests for Knowledge global budget and rate controls.

Covers:
- KnowledgeConfig new field defaults
- Global sweep chunk budget enforcement in watcher
- EmbedRateLimiter token bucket
- extraction_model resolution in _install_knowledge_agent
- extraction_pool_size in LLMPool
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


# --- Explicit-import cross-file chunk budget ---
#
# The watcher path has per-sweep and global chunk budgets; the explicit import
# routes (single-file add, agent add, direct text ingest, remote sync) are bounded
# by a cross-file ceiling instead. These cover the ImportChunkBudget limiter, its
# config key, and its enforcement at the pipeline entry.


class TestImportChunkBudgetConfig:
    def test_import_chunk_budget_default_off_opt_in(self):
        # Default MUST be 0 (opt-in): a policy control that ships on would throttle
        # every existing user's imports without their choosing it. Opt in first,
        # default-on later with data. If this reddens to 500, the switch flipped.
        c = KnowledgeConfig()
        assert c.import_chunk_budget == 0

    def test_import_chunk_budget_stored_verbatim(self):
        c = KnowledgeConfig(import_chunk_budget=200)
        assert c.import_chunk_budget == 200

    def test_import_chunk_budget_zero_is_unbounded(self):
        c = KnowledgeConfig(import_chunk_budget=0)
        assert c.import_chunk_budget == 0

    def test_loader_clamps_absurd_value_to_ceiling(self, tmp_path):
        import json
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import IMPORT_CHUNK_BUDGET_MAX

        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"import_chunk_budget": IMPORT_CHUNK_BUDGET_MAX * 100}}),
            encoding="utf-8",
        )
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_dir", return_value=tmp_path
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.knowledge.import_chunk_budget == IMPORT_CHUNK_BUDGET_MAX


class TestImportChunkBudgetLimiter:
    def test_zero_budget_never_refuses(self):
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        b = ImportChunkBudget(budget=0)
        # Disabled: reserve returns None (no token) and never raises.
        assert b.reserve() is None
        b.settle(None, 10_000)  # no-op
        b.release(None)  # no-op

    def test_reserve_refuses_once_window_reaches_budget(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()          # empty window: ok, books 50 (per-file max)
            b.settle(t1, 30)          # reconcile down to 30
            t2 = b.reserve()          # 30 < 50: ok, books 50 -> spent 80
            b.settle(t2, 30)          # reconcile to 30 -> spent 60 >= 50
            # The accepted files completed; the NEXT reserve trips.
            try:
                b.reserve()
                raised = False
            except ImportChunkBudgetError as e:
                raised = True
                assert e.budget == 50
                assert e.spent >= 50
            assert raised, "reserve() must refuse once the window is at/over budget"

    def test_concurrent_reservations_do_not_overrun(self):
        # The TOCTOU fix: a reservation is booked INTO the window at reserve()
        # time, before settle(), so simultaneous imports see each other. With a
        # budget of 50 and the per-file placeholder of 50, the first reserve books
        # 50 and the second is refused -- N concurrent imports cannot each pass.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()  # books 50 immediately, BEFORE any settle
            assert t1 is not None
            # Second concurrent import (its settle has not run yet) is refused.
            try:
                b.reserve()
                overran = True
            except ImportChunkBudgetError:
                overran = False
            assert not overran, "a second concurrent reserve must not pass before the first settles"

    def test_release_frees_a_failed_reservation(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()   # books 50 -> at budget
            b.release(t1)      # failed import frees it
            # Window is empty again: a fresh reserve succeeds.
            assert b.reserve() is not None

    def test_release_reclaims_a_noop_reservation_no_leak(self):
        # The dedup / content-unchanged success paths return before any chunk
        # work, so they never settle. A finally-release must reclaim the
        # placeholder or ten no-op re-ingests would strand 10x50=500 chunks and
        # falsely refuse genuine imports at the default budget.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=500)
            for _ in range(20):          # 20 > 500/50, would exhaust if leaked
                t = b.reserve()          # books 50
                b.release(t)             # no-op path: reclaim it
            # Nothing accumulated: a genuine import still reserves fine.
            assert b.reserve() is not None

    def test_release_is_noop_after_settle_no_double_count(self):
        # settle() consumes the token; a following release() (from the wrapper's
        # finally) must NOT drop the settled real count.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t = b.reserve()
            b.settle(t, 50)      # real cost 50 -> at budget
            b.release(t)         # finally-release: must be a no-op here
            # The settled 50 still stands, so the next reserve is refused.
            try:
                b.reserve()
                refused = False
            except ImportChunkBudgetError:
                refused = True
            assert refused, "release after settle must not drop the settled count"

    def test_window_rolls_over_and_reopens(self):
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t1 = b.reserve()
            b.settle(t1, 60)   # 60 >= 50 within window
            try:
                b.reserve()
                refused = True
            except ing.ImportChunkBudgetError:
                refused = True
            else:
                refused = False
            # (either the reserve above raised, or we set refused False)
            assert refused
            now[0] += 61.0     # advance past the 60s window
            assert b.reserve() is not None  # pruned, reopened

    def test_an_open_reservation_outlives_the_window(self):
        # A file slower than the window keeps its slot until it settles. If
        # pruning expired a live reservation, a concurrent import would be
        # admitted past the very concurrency ceiling the placeholder enforces.
        import kiro_crew.knowledge.ingestion as ing
        from kiro_crew.knowledge.ingestion import ImportChunkBudget, ImportChunkBudgetError

        now = [1000.0]
        with patch.object(ing._time, "monotonic", lambda: now[0]):
            b = ImportChunkBudget(budget=50)
            t = b.reserve()                  # books 50 -> at budget
            assert t is not None
            now[0] += ing._IMPORT_CHUNK_BUDGET_WINDOW_SECS + 1.0   # still ingesting
            try:
                b.reserve()
                refused = False
            except ImportChunkBudgetError:
                refused = True
            assert refused, "an in-flight reservation must still occupy the ceiling"
            # Settling closes it, so its entry expires by age like any record.
            b.settle(t, 1)
            assert b.reserve() is not None

    def test_error_message_is_ascii_and_actionable(self):
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        msg = str(ImportChunkBudgetError(budget=50, window_secs=60.0, spent=60))
        assert msg.isascii()
        assert "import_chunk_budget" in msg


class TestImportBudgetPipelineGate:
    """The gate lives at the pipeline entry; watcher + artifact-sync paths are exempt."""

    def _pipeline(self):
        from unittest.mock import MagicMock

        from kiro_crew.knowledge.ingestion import IngestionPipeline

        return IngestionPipeline(
            store=MagicMock(), extractor=MagicMock(), chunker=MagicMock(),
            reader=MagicMock(),
        )

    def test_enter_budget_refuses_explicit_call_when_exhausted(self):
        import asyncio

        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            t = p._import_budget.reserve()   # book 50 -> at budget
            assert t is not None
            try:
                asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
                raised = False
            except ImportChunkBudgetError:
                raised = True
        assert raised, "an explicit call must be refused once the window is exhausted"

    def test_watcher_path_is_exempt_even_when_exhausted(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=50):
            p._import_budget.set_budget(50)
            p._import_budget.reserve()  # exhaust
            # count_toward_import_budget=False (watcher/artifact-sync) must not gate.
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=False))
        assert token is None

    def test_enter_budget_returns_token_when_enabled(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=500):
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
        assert token is not None  # a reservation to settle/release later

    def test_enter_budget_returns_none_when_config_zero(self):
        import asyncio

        p = self._pipeline()
        with patch("kiro_crew.knowledge.ingestion._import_chunk_budget", return_value=0):
            token = asyncio.run(p._enter_import_budget(count_toward_import_budget=True))
        assert token is None  # disabled -> nothing to settle


class TestRemoteSyncSurfacesDeferral:
    """Remote sync surfaces a budget deferral without counting it as a failure."""

    def test_sync_source_surfaces_deferred_and_records_no_failure(self, tmp_path):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError
        from kiro_crew.knowledge.store import KnowledgeStore
        from kiro_crew.knowledge.sync import SyncScheduler

        store = KnowledgeStore(str(tmp_path / "sync.db"))
        try:
            sid = store.add_source(name="remote", source_type="webhook", uri="x://remote")
            connector = MagicMock()
            connector.detect_changes = AsyncMock(return_value=True)
            connector.fetch = AsyncMock(return_value=("body text", {}))
            pipeline = MagicMock()
            pipeline.ingest_text = AsyncMock(
                side_effect=ImportChunkBudgetError(budget=500, window_secs=60.0, spent=500))

            sched = SyncScheduler(store, pipeline, {"webhook": connector})
            res = asyncio.run(sched.sync_source(sid))

            assert "deferred" in res and "import_chunk_budget" in res["deferred"]
            assert res["synced"] is False
            # A deferral is not a failure: consecutive_failures must stay 0.
            row = store.db.execute(
                "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
            import json as _json
            props = _json.loads(row["properties"] or "{}")
            assert props.get("consecutive_failures", 0) == 0
        finally:
            store.close()


class TestBackgroundDeferralStaysRetryable:
    """A budget deferral on a background path must not land in 'error'.

    ``SyncScheduler.sync_all`` skips a source whose ``sync_status`` is 'error', so
    writing that state over a window that clears in a minute would quiesce the
    source for good. Both background writers whose content is still on disk mark
    'pending' instead, which the sweep still visits.
    """

    @staticmethod
    def _recording_store():
        class _DB:
            def __init__(self):
                self.statements: list[str] = []

            def execute(self, sql, params=()):
                self.statements.append(sql)

            def commit(self):
                pass

        class _Store:
            def __init__(self):
                self.db = _DB()

        return _Store()

    @staticmethod
    def _states(store):
        return [s for s in store.db.statements if "sync_status" in s]

    def test_local_file_ingest_defers_to_pending(self, tmp_path):
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import knowledge as kh
        from kiro_crew.knowledge.ingestion import ImportChunkBudgetError

        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        store = self._recording_store()
        pipeline = SimpleNamespace(ingest_file=AsyncMock(
            side_effect=ImportChunkBudgetError(budget=50, window_secs=60.0, spent=60)))

        asyncio.run(kh._ingest_local_file_task(pipeline, store, str(doc), "src-1"))

        states = self._states(store)
        assert len(states) == 1, states
        assert "'pending'" in states[0]
        assert "'error'" not in states[0]

    def test_local_file_ingest_still_errors_on_a_real_failure(self, tmp_path):
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers import knowledge as kh

        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        store = self._recording_store()
        pipeline = SimpleNamespace(ingest_file=AsyncMock(side_effect=RuntimeError("disk gone")))

        asyncio.run(kh._ingest_local_file_task(pipeline, store, str(doc), "src-1"))

        states = self._states(store)
        assert len(states) == 1, states
        assert "'error'" in states[0]
