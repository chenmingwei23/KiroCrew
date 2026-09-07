"""Validation of ``agents.<crew>.memory_store`` at the surfaces that WRITE it.

A crew's memory store is the only binding whose value decides which files the
crew's memory lands in, and the resolvers refuse to compose a path from a
malformed name. Persisting one therefore buys the operator nothing and costs
them the thing they asked for: the crew runs on the GLOBAL store while the
dashboard reports the name they typed, so the isolation they configured exists
only in the config file. These tests pin that both write surfaces refuse the
shape before it reaches ``config.json``.

The two postures are deliberately different, and the split is the interesting
part rather than the rejection:

* A MALFORMED name is refused — 400 with ``code: "invalid_memory_store"`` on the
  dashboard verbs, exit 1 on the CLI ones.
* An UNDECLARED but well-formed name is ACCEPTED and warned about. No write
  surface can declare a store (``memory_stores`` is hand-edited config with no
  create endpoint), so a 400 there would make the field unsettable ahead of the
  operator's own edit — and worse, unsavable for a crew already holding a name
  whose store was later removed, because the crew editor sends the field on every
  save. Accepting keeps the field usable; the warning is what keeps the degrade
  from being silent.

``""`` is the third case and it is neither: it is the inherit sentinel, the
spelling ``resolve_agent_bindings`` maps onto the default-store floor and the one
a picker emits for "nothing selected", so it must pass a write that refuses
``None``.
"""

from __future__ import annotations

import json
import logging
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cli import main
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    memory_store_binding_defect,
    memory_store_name_defect,
)

# One malformed value per rule in the shape table, so a rule dropped from
# ``memory_store_name_defect`` stops being enforced at the write boundary too and
# this file is what reports it.
MALFORMED = [
    "Work",  # not lowercase
    "../escape",  # traversal, and not a single segment
    "work/notes",  # not a single segment
    "work\\notes",  # a single segment to posixpath, two on Windows
    "con",  # a Windows reserved device basename
    "trailing.",  # ends with a dot
    "-leading",  # the slug dialect admits no leading hyphen
    "under_score",  # nor an underscore
    "x" * 200,  # over the length cap
]

# A non-string is refused even though ``""`` is not: it lands in config.json
# verbatim and every reader downstream is annotated ``str``.
NON_STRINGS = [None, 7, ["work"], {"work": 1}]


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the agent handlers past their independent owner-auth boundary."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent() -> str:
    """One stored crew on the default store, written through the real config API."""
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store=DEFAULT_MEMORY_STORE
    )
    cfg.save()
    return "existing"


def _declare_store(name: str) -> None:
    """Add *name* to the operator's ``memory_stores`` table."""
    cfg = KiroCrewConfig.load()
    cfg.memory_stores[name] = MemoryStoreConfig()
    cfg.save()


def _store_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warnings from ``memory_stores`` alone.

    ``caplog`` captures at the ROOT, so the create handler's own "template is not
    in the installed agent listing" warning — which fires in a test environment
    with no installed kiro agents and names the same crew — lands in the same list.
    A substring match on the crew name would find that one instead.
    """
    return [r.getMessage() for r in caplog.records if r.name == "kiro_crew.memory_stores"]


class TestTheBindingPredicate:
    """``memory_store_binding_defect`` is the shape rule plus one exception."""

    def test_it_delegates_every_rule_to_the_name_predicate(self) -> None:
        """No second copy of the shape rule.

        A restated rule is how a write boundary comes to accept a name the
        resolvers refuse to compose a path for, so the binding predicate must
        answer identically to the name predicate everywhere the one exception
        does not apply — including the reason text, which is what the surfaces
        show the author.
        """
        for value in [*MALFORMED, *NON_STRINGS, "work", DEFAULT_MEMORY_STORE, "a-b-9"]:
            assert memory_store_binding_defect(value) == memory_store_name_defect(value), value

    def test_the_empty_string_is_the_one_divergence(self) -> None:
        """``""`` is the absence of a choice, not a broken name.

        ``resolve_agent_bindings`` maps it onto the default-store floor and the
        crew editor emits it for "nothing selected" while sending the field on
        every save, so a write that refused it would make a crew whose stored
        binding is already empty unsavable.
        """
        assert memory_store_name_defect("") == "empty"
        assert memory_store_binding_defect("") is None

    def test_an_undeclared_name_is_not_a_defect(self) -> None:
        """Shape only. Declaredness is resolution's question, not the write's."""
        assert memory_store_binding_defect("never-declared-anywhere") is None


class TestTheDashboardRefusesAMalformedBinding:
    @pytest.mark.parametrize("bad", MALFORMED)
    @pytest.mark.asyncio
    async def test_create_refuses_and_stores_nothing(self, bad: str) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": bad},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_memory_store"
            # The message must name the ACTUAL defect; "invalid" alone leaves the
            # author guessing which of nine rules they tripped.
            assert memory_store_name_defect(bad) in body["error"]

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.parametrize("bad", NON_STRINGS)
    @pytest.mark.asyncio
    async def test_create_refuses_a_non_string(self, bad: object) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": bad},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.parametrize("bad", MALFORMED)
    @pytest.mark.asyncio
    async def test_update_refuses_and_writes_nothing_at_all(
        self, seeded_agent: str, bad: str
    ) -> None:
        """The whole request is a no-op on disk, not just the offending field.

        A rejected store cannot smuggle a workspace rebind through with it. What
        this pins is the PERSISTED outcome, deliberately: the refusal returns
        before the config save, so moving the check after the in-memory field
        assignments would leave this assertion true. Pinning the assignment order
        instead would need to observe the un-saved config object, and the property
        that matters to a user is that a 400 changes nothing they can later read.
        """
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "memory_store": bad},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.memory_store == DEFAULT_MEMORY_STORE
        assert stored.workspace == "default"

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_string(self, seeded_agent: str) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"memory_store": None})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_memory_store"

        assert KiroCrewConfig.load().agents[seeded_agent].memory_store == DEFAULT_MEMORY_STORE


class TestTheDashboardAcceptsAUsableBinding:
    @pytest.mark.asyncio
    async def test_create_without_the_field_binds_the_default_store(self) -> None:
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200

        assert KiroCrewConfig.load().agents["reviewer"].memory_store == DEFAULT_MEMORY_STORE

    @pytest.mark.asyncio
    async def test_create_stores_a_declared_name_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        _declare_store("work")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.post(
                    "/api/agents",
                    json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": "work"},
                )
                assert resp.status == 200

        assert KiroCrewConfig.load().agents["reviewer"].memory_store == "work"
        assert _store_warnings(caplog) == [], "a declared store must cost no log line"

    @pytest.mark.asyncio
    async def test_update_accepts_the_inherit_sentinel(self, seeded_agent: str) -> None:
        """``""`` rebinds the crew to the global store rather than 400-ing.

        Retracting a silo has to be reachable from the same surface that granted
        it, and the loader already owns the mapping from ``""`` to the floor.
        """
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"memory_store": ""})
            assert resp.status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].memory_store == ""


class TestAnUndeclaredNameIsAcceptedAndLogged:
    """The deliberate half of the decision.

    Refusing an undeclared name would leave the field unsettable, so it is
    accepted — but a crew bound to a store nobody declared reads and writes a silo
    it did not ask for, which is the failure per-crew memory exists to prevent. So
    the write logs it, naming the crew AND the store the crew will actually run
    on, at the one moment a human is looking at the value. Without that the only
    trace is a crew-less line emitted at the first memory read.
    """

    @pytest.mark.asyncio
    async def test_create_accepts_it_and_names_the_landing_store(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.post(
                    "/api/agents",
                    json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": "ghost"},
                )
                assert resp.status == 200

        assert KiroCrewConfig.load().agents["reviewer"].memory_store == "ghost"
        named = [m for m in _store_warnings(caplog) if "reviewer" in m]
        assert named, f"no warning named the crew: {_store_warnings(caplog)}"
        assert "'ghost'" in named[0]
        assert f"{DEFAULT_MEMORY_STORE!r}" in named[0]

    @pytest.mark.asyncio
    async def test_update_accepts_it_and_names_the_landing_store(
        self, seeded_agent: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.put(
                    f"/api/agents/{seeded_agent}", json={"memory_store": "ghost"}
                )
                assert resp.status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].memory_store == "ghost"
        assert any(seeded_agent in m for m in _store_warnings(caplog))

    @pytest.mark.asyncio
    async def test_the_landing_store_is_the_configured_default_when_that_is_declared(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning must not claim a store resolution would not pick.

        It reports ``degrade_store_name``'s own answer, so with a declared
        ``default_memory_store`` the crew lands there, not on the floor.
        """
        cfg = KiroCrewConfig.load()
        cfg.memory_stores["work"] = MemoryStoreConfig()
        cfg.default_memory_store = "work"
        cfg.save()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"):
            async with TestClient(TestServer(_crud_app())) as client:
                resp = await client.post(
                    "/api/agents",
                    json={"name": "reviewer", "kiro_agent": "kirocrew", "memory_store": "ghost"},
                )
                assert resp.status == 200

        named = [m for m in _store_warnings(caplog) if "reviewer" in m]
        assert named and "'work'" in named[0], named


def _cli_config(tmp_path: Path) -> Path:
    """A minimal config.json with a default crew, declaring only the default store."""
    payload = {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": DEFAULT_MEMORY_STORE,
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {DEFAULT_MEMORY_STORE: {}},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class TestTheCliRefusesAMalformedBinding:
    """``kirocrew agent create``/``update`` apply the same predicate.

    Two surfaces writing one ``config.json`` must not accept different sets of
    values, or the stricter one is decoration: the operator reaches the same bad
    state through the other verb.
    """

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_create_exits_nonzero_and_writes_nothing(
        self, tmp_path: Path, bad: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _cli_config(tmp_path)
        # ``--memory-store=<value>``, never the two-token form: a value that opens
        # with a hyphen is an option to argparse, so the two-token form would test
        # the parser rather than the guard.
        argv = ["kirocrew", "agent", "create", "--name", "reviewer", f"--memory-store={bad}"]
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert memory_store_name_defect(bad) in err
        assert "reviewer" not in json.loads(cfg_path.read_text(encoding="utf-8"))["agents"]

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_update_exits_nonzero_and_leaves_the_binding_alone(
        self, tmp_path: Path, bad: str
    ) -> None:
        cfg_path = _cli_config(tmp_path)
        # ``update`` takes the crew name positionally.
        argv = ["kirocrew", "agent", "update", "default", f"--memory-store={bad}"]
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
            pytest.raises(SystemExit) as exc,
        ):
            main()

        assert exc.value.code != 0
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["default"]["memory_store"] == DEFAULT_MEMORY_STORE

    def test_update_accepts_an_undeclared_name_and_logs_it(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_path = _cli_config(tmp_path)
        argv = ["kirocrew", "agent", "update", "default", "--memory-store=ghost"]
        with (
            caplog.at_level(logging.WARNING, logger="kiro_crew.memory_stores"),
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", argv),
        ):
            main()

        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert saved["agents"]["default"]["memory_store"] == "ghost"
        assert any("'ghost'" in m for m in _store_warnings(caplog))


class TestWhyAMalformedBindingHadToBeRefused:
    def test_a_non_string_binding_breaks_the_command_that_would_show_it(
        self, tmp_path: Path
    ) -> None:
        """``kirocrew agent list`` formats the field through a width spec.

        Characterizes the state the write guard now prevents rather than any
        behaviour of the guard: a hand-edited ``config.json`` can still reach it,
        and this is what it costs — the listing raises, so the operator cannot see
        the binding that is wrong. Nothing sanitizes it on the read side, and
        nothing should: repairing a name is what would merge two crews' memory
        into one directory.
        """
        payload = json.loads(_cli_config(tmp_path).read_text(encoding="utf-8"))
        payload["agents"]["default"]["memory_store"] = None
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")

        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kirocrew", "agent", "list"]),
            pytest.raises(TypeError),
        ):
            main()
