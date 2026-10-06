"""Tests for directory results in FileIndex and /api/file-search.

Covers the folder @-mention feature: directory entries carry ``kind: "dir"``,
the ``kinds`` query param filters the result set, files outrank directories on
an otherwise equal score, and symlinked directories are resolved before the
sensitivity check.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.file_index import FileIndex
from kiro_crew.dashboard.handlers import api_file_search
from kiro_crew.dashboard.handlers import files as files_mod


def _make_app(index=None) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-search", api_file_search)
    state = MagicMock()
    state.file_indexes.get.return_value = index
    # A MagicMock attribute reads as a non-empty configured owner id, which no
    # caller can equal, so the owner gate would answer every row alike.
    state.owner_id = ""
    app["state"] = state
    return as_owner(app)


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _populate(tmp_path):
    """Tree with directories and files sharing the "widget" stem."""
    (tmp_path / "widgets.py").write_text("x")
    wdir = tmp_path / "widgets"
    wdir.mkdir()
    (wdir / "button.py").write_text("y")
    nested = wdir / "widgetsinner"
    nested.mkdir()
    (nested / "leaf.py").write_text("z")
    # An empty directory: proof dirs are walked, not derived from file paths.
    (tmp_path / "widgetsempty").mkdir()
    # A dot-prefixed dir IS offered as a candidate: it must not be
    # descended into, but the directory itself is a valid @-mention target.
    (tmp_path / ".widgetshidden").mkdir()
    # Skip-listed dirs stay excluded from both descent and results -- including
    # the dot-prefixed skip-listed ones (.git, .cache, .venv), which the shared
    # _SKIP_DIRS names explicitly, since candidacy does not filter on a dot.
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "widgetsdep").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".cache").mkdir()


def _scorer(q, name, rel):
    nl = name.lower()
    if q in nl:
        return 10.0
    if q in rel.lower():
        return 5.0
    return 0.0


class TestFileIndexDirs:
    @pytest.mark.asyncio
    async def test_index_includes_dir_entries(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            results = idx.search("widgets", _scorer)
            by_name = {r["name"]: r for r in results}
            assert by_name["widgets"]["kind"] == "dir"
            assert by_name["widgets.py"]["kind"] == "file"
            # Directory entries report size 0 and a real mtime.
            assert by_name["widgets"]["size"] == 0
            assert by_name["widgets"]["mtime"] > 0
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_includes_empty_dir(self, tmp_path):
        """An empty dir has no files, so it can only appear if dirs are walked."""
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            names = {r["name"] for r in idx.search("widgetsempty", _scorer)}
            assert "widgetsempty" in names
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_kinds_filter(self, tmp_path):
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            dirs = idx.search("widgets", _scorer, 15, "dirs")
            assert dirs, "expected at least one directory hit"
            assert all(r["kind"] == "dir" for r in dirs)

            files = idx.search("widgets", _scorer, 15, "files")
            assert files, "expected at least one file hit"
            assert all(r["kind"] == "file" for r in files)

            both = {r["kind"] for r in idx.search("widgets", _scorer, 15, "all")}
            assert both == {"dir", "file"}
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_offers_dot_dirs_but_not_skip_dirs(self, tmp_path):
        """Dot-prefixed dirs ARE candidates; skip-listed dirs are not."""
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            names = {r["name"] for r in idx.search("widgets", _scorer, 50, "dirs")}
            assert ".widgetshidden" in names, "dot-prefixed dir must be offered as a candidate"
            assert "widgetsdep" not in names, "a dir inside node_modules is never descended into"
            assert "node_modules" not in names, "skip-listed dir must stay excluded from results"
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_dot_skip_dirs_are_not_offered(self, tmp_path):
        """A dot-prefixed SKIP-listed dir (.git, .cache) is never a candidate.

        Candidacy does not filter on a leading
        dot, so the shared _SKIP_DIRS must name .git/.cache/.venv explicitly --
        otherwise the indexed fast path would start offering them.
        """
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            for q in ("git", "cache"):
                names = {r["name"] for r in idx.search(q, _scorer, 50, "dirs")}
                assert f".{q}" not in names, f".{q} must never be offered as a candidate"
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_does_not_descend_into_dot_dirs(self, tmp_path):
        """A dot-dir is offered, but its CONTENTS are never walked."""
        hidden = tmp_path / ".secretcfg"
        hidden.mkdir()
        (hidden / "widgetsinside").mkdir()
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            names = {r["name"] for r in idx.search("widgets", _scorer, 50, "dirs")}
            assert ".secretcfg" not in names, "does not match query, not expected here"
            assert "widgetsinside" not in names, "must not descend into a dot-dir"
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_files_outrank_dirs_on_equal_score(self, tmp_path):
        """Equal score and equal name length: the file must sort first."""
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha.x").write_text("x")

        def flat_scorer(q, name, rel):
            return 10.0 if q in name.lower() else 0.0

        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            results = idx.search("alpha", flat_scorer)
            kinds = [r["kind"] for r in results]
            assert kinds[0] == "file", f"expected file first, got {results}"
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_index_dir_symlink_resolved_before_sensitive_check(self, tmp_path):
        """A symlink into a sensitive tree must be rejected on its real path."""
        _populate(tmp_path)
        link = tmp_path / "widgetslink"
        os.symlink(str(tmp_path / "widgets"), str(link))

        idx = FileIndex(str(tmp_path))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch("kiro_crew.dashboard.file_index.is_sensitive_path", side_effect=fake_sensitive):
            entries, _ = idx._walk()
        names = {e[1] for e in entries if e[5] == "dir"}
        assert "widgetslink" not in names
        assert "widgets" not in names, "the symlink target shares the real path and must also be filtered"

    @pytest.mark.asyncio
    async def test_index_file_symlink_resolved_before_sensitive_check(self, tmp_path):
        """A symlinked FILE into a sensitive tree is rejected on its real path."""
        _populate(tmp_path)
        link = tmp_path / "widgetslink.py"
        os.symlink(str(tmp_path / "widgets.py"), str(link))

        idx = FileIndex(str(tmp_path))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch("kiro_crew.dashboard.file_index.is_sensitive_path", side_effect=fake_sensitive):
            entries, _ = idx._walk()
        names = {e[1] for e in entries if e[5] == "file"}
        assert "widgetslink.py" not in names
        assert "widgets.py" not in names, "the symlink target shares the real path"


class TestApiFileSearchDirs:
    @pytest.mark.asyncio
    async def test_walk_fallback_returns_dirs(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            assert resp.status == 200
            results = (await resp.json())["results"]
            by_name = {r["name"]: r for r in results}
            assert by_name["widgets"]["kind"] == "dir"
            assert by_name["widgets"]["size"] == 0
            assert by_name["widgets.py"]["kind"] == "file"

    @pytest.mark.asyncio
    async def test_walk_fallback_kinds_dirs_only(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
            )
            results = (await resp.json())["results"]
            assert results
            assert all(r["kind"] == "dir" for r in results)

    @pytest.mark.asyncio
    async def test_walk_fallback_kinds_files_only(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=files&project={tmp_path}"
            )
            results = (await resp.json())["results"]
            assert results
            assert all(r["kind"] == "file" for r in results)

    @pytest.mark.asyncio
    async def test_unknown_kinds_value_falls_back_to_all(self, tmp_path, mock_sel):
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=bogus&project={tmp_path}"
            )
            assert resp.status == 200
            kinds = {r["kind"] for r in (await resp.json())["results"]}
            assert kinds == {"dir", "file"}

    @pytest.mark.asyncio
    async def test_walk_fallback_offers_dot_dirs_but_not_skip_dirs(self, tmp_path, mock_sel):
        """The walk fallback offers dot-dirs but never skip-listed dirs."""
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
            )
            names = {r["name"] for r in (await resp.json())["results"]}
            assert ".widgetshidden" in names, "dot-prefixed dir must be offered as a candidate"
            assert "widgetsdep" not in names, "a dir inside node_modules is never descended into"
            assert "node_modules" not in names, "skip-listed dir must stay excluded from results"

    @pytest.mark.asyncio
    async def test_walk_fallback_dot_skip_dirs_are_not_offered(self, tmp_path, mock_sel):
        """The walk fallback never offers a dot-prefixed skip-listed dir.

        Same source of truth as the fast path (shared _SKIP_DIRS), so the two
        paths of this endpoint agree on .git/.cache/.venv.
        """
        _populate(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            for q in ("git", "cache"):
                resp = await client.get(
                    f"/api/file-search?q={q}&kinds=dirs&project={tmp_path}"
                )
                names = {r["name"] for r in (await resp.json())["results"]}
                assert f".{q}" not in names, f".{q} must never be offered by the fallback"

    @pytest.mark.asyncio
    async def test_index_fast_path_forwards_kinds(self, tmp_path, mock_sel):
        """The fast path must pass the kinds param through to FileIndex.search."""
        _populate(tmp_path)
        idx = FileIndex(str(tmp_path))
        await idx.start()
        try:
            app = _make_app(index=idx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(
                    f"/api/file-search?q=widgets&kinds=dirs&project={tmp_path}"
                )
                data = await resp.json()
                assert data["root"] == os.path.realpath(str(tmp_path))
                assert data["results"]
                assert all(r["kind"] == "dir" for r in data["results"])
        finally:
            idx.stop()

    @pytest.mark.asyncio
    async def test_walk_fallback_file_symlink_resolved_before_sensitive_check(
        self, tmp_path, mock_sel
    ):
        """A symlinked FILE into a sensitive tree is rejected on its real path.

        The directory branch already resolved symlinks first; files must match so
        the two branches cannot disagree about what counts as sensitive.
        """
        _populate(tmp_path)
        link = tmp_path / "widgetslink.py"
        os.symlink(str(tmp_path / "widgets.py"), str(link))
        real = os.path.realpath(str(link))

        def fake_sensitive(p):
            return os.path.realpath(p) == real

        with patch(
            # api_file_search imports is_sensitive_path locally from
            # kiro_crew.security, so the source module is the patch target.
            "kiro_crew.security.is_sensitive_path",
            side_effect=fake_sensitive,
        ):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(
                    f"/api/file-search?q=widgets&kinds=files&project={tmp_path}"
                )
                names = {r["name"] for r in (await resp.json())["results"]}
        assert "widgetslink.py" not in names
        assert "widgets.py" not in names, "the symlink target shares the real path"

    @pytest.mark.asyncio
    async def test_many_matching_dirs_do_not_starve_files(self, tmp_path, mock_sel):
        """Directories must not consume the whole candidate budget.

        A shared collection cap let a burst of matching directories fill it
        before any file was examined, so the best-scoring file was never even a
        candidate. The directories here score LOWER than the file, so a correct
        implementation returns the file regardless of ranking: the only way to
        lose it is to never collect it.
        """
        # max_collect is max_results * 10 = 150, so 400 matching directories
        # would exhaust it before the file was reached.
        for i in range(400):
            (tmp_path / f"zzz_widgets{i:03d}").mkdir()
        (tmp_path / "widgets.py").write_text("x")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            results = (await resp.json())["results"]
        names = [r["name"] for r in results]
        assert "widgets.py" in names, "the file was crowded out by directory candidates"

    @pytest.mark.asyncio
    async def test_files_and_dirs_have_independent_scan_budgets(
            self, tmp_path, mock_sel, monkeypatch):
        """A file-heavy root must not starve the directory scan.

        A single shared scan counter let files spend the whole budget before any
        directory was examined, so directory search returned nothing even though
        it was enabled. Non-matching files are used so only the SCAN budget (not
        the candidate cap) is under test.

        Budgets patched DOWN (module-level exactly so tests can, per
        ``files.py``'s comment on ``_WALK_MAX_SCAN_SCOPED``) so a much smaller
        tree still exceeds them and exercises the same starvation path.
        """
        monkeypatch.setattr(files_mod, "_WALK_MAX_SCAN_SCOPED", 100)
        monkeypatch.setattr(files_mod, "_WALK_MAX_SCAN_UNSCOPED", 100)
        for i in range(200):
            (tmp_path / f"zz{i:04d}.py").write_text("x")
        (tmp_path / "widgets_dir").mkdir()

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widgets&project={tmp_path}")
            results = (await resp.json())["results"]
        names = [r["name"] for r in results]
        assert "widgets_dir" in names, "directory candidates were starved by the file scan"

    @pytest.mark.asyncio
    async def test_walk_stops_at_overall_scan_ceiling(self, tmp_path, mock_sel):
        """The walk must stop descending once the dirs-visited ceiling is hit.

        The per-kind budgets do not terminate the walk: on `kinds=all` the dir
        counter advances per directory NAME, so a deep tree with few dirs per
        level exhausts the file budget at level one while the dir half of the
        done-check stays false forever, and `os.walk` descends the whole tree.

        The tree must be DEEP AND NARROW. A wide tree does not reproduce it --
        the root's many directory names exhaust the dir budget immediately, which
        is why the flat 5,000-file budget test could not catch this.
        """
        cur = tmp_path
        levels = 30
        for d in range(levels):
            # A handful of files per level is enough to spend the (shrunk) file
            # budget; the bug is about depth, not file volume.
            for f in range(3):
                (cur / f"zz{d:02d}_{f}.py").write_text("x")
            cur = cur / f"n{d:02d}"
            cur.mkdir()

        real_walk = os.walk
        calls = {"n": 0}

        def counting_walk(*a, **kw):
            # Count yielded levels, not the call: os.walk is a generator, so this
            # measures how far the traversal actually got. Patched on the
            # handler's `os` -- patching os.scandir does nothing, since os.walk
            # bound it at import time.
            for item in real_walk(*a, **kw):
                calls["n"] += 1
                yield item

        with patch.object(files_mod, "_WALK_MAX_SCAN_SCOPED", 10_000), \
                patch.object(files_mod, "_WALK_MAX_DIRS_VISITED", 10), \
                patch.object(files_mod.os, "walk", counting_walk):
            async with TestClient(TestServer(_make_app())) as client:
                # Matches nothing, so no candidate cap can end the walk.
                resp = await client.get(f"/api/file-search?q=qqqnomatch&project={tmp_path}")
                assert resp.status == 200

        # The per-kind budget is left out of reach on purpose: a budget below the
        # level count would be exhausted by the directory names themselves and
        # mask the bug. So the ceiling (10) is the only thing that can stop the
        # walk. Unbounded, it descends all 31 levels.
        assert calls["n"] <= 12, (
            f"walk descended {calls['n']} directories of {levels + 1} -- the "
            "dirs-visited ceiling did not stop the traversal"
        )


class TestFileSearchWalkDeadline:
    """The file-search walk's wall-clock budget and its slow-store escape hatch.

    The entry/dir ceilings (``_WALK_MAX_*``) bound how MANY filesystem calls the
    walk makes, not how LONG each one takes. On local disk a ceiling-reaching
    walk measured 4.20s worst, under the client's 15s deadline with ~3.6x
    headroom -- but that figure is local disk only. On an NFS/SSHFS-class mount
    each ``os.stat``/``scandir`` is a network round trip, so the same bounded
    number of calls can run many times longer and blow past the client bound,
    and its Retry re-enters the same bound, failing every attempt (#11419).

    Worker-1 has no network mount, so these tests SIMULATE a slow store: a thin
    wrapper around ``os.walk`` sleeps per yielded directory, standing in for the
    per-syscall latency a slow mount adds. The budget is patched DOWN to keep the
    tests fast; the production default is 10s, read via
    ``files_mod._file_search_walk_budget_secs()``.
    """

    @pytest.mark.asyncio
    async def test_a_slow_store_truncates_the_walk_at_the_wall_clock_budget(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """MEASUREMENT: the local-disk headroom does NOT hold on a slow store.

        A tree small enough that neither the entry nor the dir ceiling fires, but
        whose per-directory latency (the slow-store stand-in) sums past the walk
        budget. Without a time bound the walk runs to completion; with it, the
        walk stops early and the response is marked ``truncated``.
        """
        # 20 directories, each matching the query, none hitting a count ceiling.
        for d in range(20):
            (tmp_path / f"widget{d:02d}").mkdir()

        real_walk = files_mod.os.walk

        def slow_walk(*a, **kw):
            for item in real_walk(*a, **kw):
                # 60ms per directory: 20 dirs ~= 1.2s of walk, well past the 100ms
                # budget below, standing in for a slow mount's per-dir latency.
                time.sleep(0.06)
                yield item

        # Budget far below the simulated walk time, dir/entry ceilings far above
        # it, so ONLY the wall-clock bound can stop this walk.
        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "100")
        with patch.object(files_mod.os, "walk", slow_walk):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-search?q=widget&project={tmp_path}")
                assert resp.status == 200
                body = await resp.json()

        assert body["truncated"] is True, (
            "a slow store that outruns the walk budget must mark the result "
            "truncated, not run to completion"
        )
        # It still returns the matches it DID collect before the deadline -- a
        # partial, bounded answer, not an empty one or a hang.
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_a_fast_store_is_not_truncated(self, tmp_path, mock_sel, monkeypatch):
        """Control: with no artificial latency and the production-class budget, the
        same tree finishes and ``truncated`` is False."""
        for d in range(20):
            (tmp_path / f"widget{d:02d}").mkdir()

        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "10000")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widget&project={tmp_path}")
            assert resp.status == 200
            body = await resp.json()

        assert body["truncated"] is False
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_the_escape_hatch_lets_a_slow_store_finish(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """ESCAPE HATCH: an operator on a store slow enough that the default would
        truncate can raise ``KIROCREW_FILE_SEARCH_WALK_BUDGET_MS`` and get a
        complete walk. Same slow store as the truncation test, but a budget large
        enough to let it finish -> ``truncated`` is False."""
        for d in range(20):
            (tmp_path / f"widget{d:02d}").mkdir()

        real_walk = files_mod.os.walk

        def slow_walk(*a, **kw):
            for item in real_walk(*a, **kw):
                time.sleep(0.06)
                yield item

        # The walk needs ~1.2s; a 10s budget clears it comfortably.
        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "10000")
        with patch.object(files_mod.os, "walk", slow_walk):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/file-search?q=widget&project={tmp_path}")
                assert resp.status == 200
                body = await resp.json()

        assert body["truncated"] is False, (
            "raising the budget past the slow store's walk time must let it finish"
        )
        assert len(body["results"]) >= 1

    def test_the_budget_env_override_parsing(self, monkeypatch):
        """The escape-hatch knob: a positive millisecond value is honoured; a
        missing, empty, non-numeric or non-positive value falls back to the 10s
        default, so a typo never silently sets a zero or negative budget."""
        monkeypatch.delenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", raising=False)
        assert files_mod._file_search_walk_budget_secs() == 10.0

        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "250")
        assert files_mod._file_search_walk_budget_secs() == 0.25

        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "30000")
        assert files_mod._file_search_walk_budget_secs() == 30.0

        for bad in ("", "   ", "abc", "0", "-5", "nan-ish"):
            monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", bad)
            assert files_mod._file_search_walk_budget_secs() == 10.0, bad

    @pytest.mark.asyncio
    async def test_the_env_knob_reaches_the_thread_that_runs_the_walk(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """PROOF the knob is not stripped: the value set in ``os.environ`` is the
        value the code DOING the walk reads.

        #17414's FE_EXTRA_ROOTS was rejected because ``minimal_env`` strips it:
        that knob was meant to be read in a SPAWNED subprocess whose env is a
        scrubbed copy, so the operator's value never arrived. This endpoint is
        different in kind -- ``api_file_search`` is an in-process aiohttp handler
        and ``_walk_file_search`` runs on the bounded probe POOL (threads in the
        same process via ``_run_path_probe``), never a subprocess, so it reads the
        gateway's own live ``os.environ``. ``minimal_env`` is nowhere on this path.

        This test closes that gap end-to-end: it records the budget the walk
        actually used from INSIDE the walk, drives the real endpoint, and asserts
        the recorded value equals what was set in ``os.environ`` -- so a future
        change that moved the walk behind an env-scrubbing boundary (a subprocess,
        a cleared env) would fail here instead of silently ignoring the operator.
        """
        for d in range(3):
            (tmp_path / f"widget{d:02d}").mkdir()

        # The budget function is called inside api_file_search, on the loop,
        # before the walk is handed to the pool. Capture every value it returns so
        # the assertion pins the value the running search used, read from the live
        # process env -- not a value this test computed on the side.
        real_budget = files_mod._file_search_walk_budget_secs
        seen: list[float] = []

        def _recording_budget() -> float:
            v = real_budget()
            seen.append(v)
            return v

        monkeypatch.setenv("KIROCREW_FILE_SEARCH_WALK_BUDGET_MS", "7000")
        monkeypatch.setattr(files_mod, "_file_search_walk_budget_secs", _recording_budget)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-search?q=widget&project={tmp_path}")
            assert resp.status == 200

        assert seen, "api_file_search never read the walk budget"
        # 7000 ms env value -> 7.0 s is what the running search used: the operator's
        # os.environ value reached the code path, unstripped.
        assert seen[-1] == 7.0
