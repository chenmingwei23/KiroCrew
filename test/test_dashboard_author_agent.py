"""Tests for the kirocrew-dashboard-author managed crewmate.

``kirocrew-dashboard-author`` authors one dashboard template and lands it as a pull
request. Its charter ships as the ``dashboard-template`` skill's ``agent-spec.md``, and
until it is wired through the installer, the owned-files list and an eager install call,
nothing writes the spec to ``~/.kiro/agents/`` -- so ``session_create`` cannot resolve
the name and the crewmate can never be selected.

These tests pin the registration (the filename is owned), the materialization (a rebuild
writes a loadable spec), the charter the spec carries (the author writes files and drives
git, so it mounts ``fs_write`` and ``execute_bash`` but never auto-approves them, and
auto-approves only the reading core verbs its skill names), and PROVENANCE: the spec at
the owned filename is attributed by an INSTALLER-RECORDED OWNERSHIP DIGEST in the
``agent_state`` sidecar (the SHA-256 of the exact bytes the installer last wrote there),
so the installer rewrites its own file but never overwrites a user file -- or a copy of
another owned agent -- hand-placed at the once-user-creatable stem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import worker_agent
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


class _Rig:
    """A private agents directory with the machine-specific inputs pinned."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )

    def read(self, filename: str) -> dict[str, Any]:
        return json.loads((self.agents / filename).read_text(encoding="utf-8"))


def _is_installers(path: Path) -> bool:
    """Does the file at *path* confirm as this installer's own write -- its bytes reproduce
    the installer-recorded ownership digest?"""
    spec = worker_agent.agent_mod._read_spec_capped(path)
    return worker_agent._is_confirmed_managed_dashboard_author(spec)


# --------------------------------------------------------------------------- #
# Registration and charter (items 1-2: install + owned filename).
# --------------------------------------------------------------------------- #


def test_the_dashboard_author_filename_is_owned() -> None:
    """A managed spec Kiro Crew writes must be in the owned-files allowlist, or the
    Playwright convergence sweep and the self-heal pass skip it."""
    assert DASHBOARD_AUTHOR_AGENT_FILENAME == "kirocrew-dashboard-author.json"
    assert DASHBOARD_AUTHOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES


def test_a_rebuild_materializes_the_dashboard_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh rebuild writes the spec so ``session_create`` can resolve the name."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert spec["name"] == "kirocrew-dashboard-author"


def test_the_dashboard_author_mounts_but_never_auto_approves_write_or_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fs_write`` and ``execute_bash`` are the job, so they are mounted; a human reads
    the author's diff, so neither is auto-approved -- the line ``kirocrew-conductor``
    draws, and for the same reason (``allowedTools`` has no argument matching)."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "fs_write" in spec["tools"]
    assert "execute_bash" in spec["tools"]
    assert "fs_write" not in spec["allowedTools"]
    assert "execute_bash" not in spec["allowedTools"]


def test_the_dashboard_author_auto_approves_only_reading_core_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every auto-approved entry only reads or recalls -- the safety story on a path
    that never reaches the PreToolUse gate. ``fs_read`` is mounted but filtered off the
    auto-approve list by the governance ceiling, exactly as it is for every spec derived
    from the template, so it reaches the gate like the write and shell tools do."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert set(spec["allowedTools"]) == {
        "tool_search",
        "@kirocrew-core/skill_search",
        "@kirocrew-core/skill_discover",
        "@kirocrew-core/memory_recall",
        "@kirocrew-core/resource_status",
    }
    # No session verb, no work-ledger mount, no publication surface, no file-write grant.
    for ref in spec["allowedTools"]:
        assert not ref.startswith("@kirocrew-dashboard")
        assert not ref.startswith("@kirocrew-work")
        assert ref not in ("fs_write", "execute_bash", "code")


def test_the_prompt_and_description_come_from_the_shipped_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The charter has ONE authoritative copy -- the shipped ``agent-spec.md`` -- so the
    installed spec's prompt and description are what ``parse_markdown_spec`` reads from
    it, never a Python constant held equal to it by nothing."""
    from kiro_crew.agent_materialization.worker_agent import _DASHBOARD_AUTHOR_SPEC_PATH
    from kiro_crew.agent_spec_format import parse_markdown_spec

    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    source = parse_markdown_spec(_DASHBOARD_AUTHOR_SPEC_PATH.read_text(encoding="utf-8"))
    assert spec["prompt"] == source["prompt"]
    assert spec["description"] == source["description"]


# --------------------------------------------------------------------------- #
# Digest provenance (GPT 6.1 F1, maintainer ruling): ownership is an
# installer-recorded digest of the last managed write, NOT a content mark.
# --------------------------------------------------------------------------- #


def test_the_install_records_the_ownership_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer records, in the ``agent_state`` sidecar, the digest of exactly the
    bytes it wrote. That recorded digest is the ownership record a later rebuild (and the
    fork / capability gates) confirm the on-disk file against -- so the installed file
    confirms as this installer's own, and the recorded digest equals the file's digest."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert spec["name"] == "kirocrew-dashboard-author"
    recorded = agent_state.get_managed_digest("kirocrew-dashboard-author")
    assert recorded is not None
    assert recorded == agent_state.spec_digest(spec)
    assert _is_installers(target) is True


def test_a_prior_managed_write_is_refreshed_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose bytes reproduce the installer-recorded digest is proven ours, so a
    second rebuild recognises it and refreshes it in place (idempotent) rather than refusing
    or duplicating."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert _is_installers(target) is True

    # A second rebuild recognises our prior write (its digest) and refreshes it in place.
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    assert rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)["name"] == "kirocrew-dashboard-author"


def test_a_user_file_is_left_untouched_and_not_installed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file at the stem for which NO ownership digest is recorded is a user artefact: the
    managed spec is NOT written and the user's file is left exactly as it was -- no overwrite,
    no backup, no exception. (No digest is recorded until the installer itself writes.)"""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {"name": "my-own-template", "prompt": "my own template"}
    target.write_text(json.dumps(user), encoding="utf-8")
    assert _is_installers(target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched
    assert not (rig.agents / (DASHBOARD_AUTHOR_AGENT_FILENAME + ".saved")).exists()


def test_a_user_file_reusing_the_name_but_with_no_recorded_digest_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user file that reuses the agent NAME and even mounts ``kirocrew-core`` is still not
    ours when no ownership digest is recorded for the name (nothing the installer wrote), so
    it is left untouched rather than overwritten. The name is not the record; the digest is."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {
        "name": "kirocrew-dashboard-author",
        "prompt": "a user spec reusing the name and the core mount",
        "mcpServers": {"kirocrew-core": {}},
        "tools": ["@kirocrew-core/skill_search"],
    }
    target.write_text(json.dumps(user), encoding="utf-8")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    assert _is_installers(target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched


def test_a_non_regular_pre_existing_file_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink (or any non-regular file) at the target is not a plain spec we can
    attribute; it is left untouched and not written over or followed."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    target.symlink_to(elsewhere)

    worker_agent._install_dashboard_author_agent()  # must not raise

    assert target.is_symlink()  # untouched, still points where it did
    assert target.resolve() == elsewhere.resolve()


def test_an_install_failure_runs_independent_repairs_then_fails_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F2: an author-spec install failure must NOT be swallowed. The independent
    repairs (fork refresh, hook repair) still run -- so an unrelated installer's failure
    cannot skip them -- but the rebuild then PROPAGATES the failure instead of reporting
    success. This is what keeps a ceiling-tightening rebuild whose governed spec rewrite
    failed from being recorded as projected: ``rebuild_agent_config_reporting`` does not
    report a write, so ``reproject_for_ceiling_change`` leaves its memo behind and the next
    poll retries rather than leaving forbidden auto-approvals live."""
    _Rig(tmp_path, monkeypatch)  # pins the private agents dir and inputs

    def _boom() -> None:
        raise OSError("agents dir unwritable")

    monkeypatch.setattr(worker_agent, "_install_dashboard_author_agent", _boom)

    repairs: list[str] = []
    real_refresh = agent.fork_refresh.refresh_after_rebuild
    real_repair = agent.repair_agent_configs
    monkeypatch.setattr(
        agent.fork_refresh,
        "refresh_after_rebuild",
        lambda *a, **k: (repairs.append("fork_refresh"), real_refresh(*a, **k))[1],
    )
    monkeypatch.setattr(
        agent,
        "repair_agent_configs",
        lambda *a, **k: (repairs.append("hook_repair"), real_repair(*a, **k))[1],
    )

    # The failure PROPAGATES (not swallowed) -- and the reporting wrapper reflects it.
    with pytest.raises(OSError, match="agents dir unwritable"):
        agent.rebuild_agent_config()

    # The independent repairs ran BEFORE the failure propagated.
    assert repairs == ["fork_refresh", "hook_repair"]


def test_the_reporting_wrapper_does_not_report_a_write_on_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success contract: a propagated author-spec failure escapes before ``_wrote_out``
    is marked True, so ``rebuild_agent_config_reporting`` raises rather than returning
    ``(path, True)`` -- the signal ``reproject_for_ceiling_change`` relies on to NOT advance
    its generation memo on a rebuild that did not land the governed spec."""
    _Rig(tmp_path, monkeypatch)

    def _boom() -> None:
        raise OSError("agents dir unwritable")

    monkeypatch.setattr(worker_agent, "_install_dashboard_author_agent", _boom)

    with pytest.raises(OSError, match="agents dir unwritable"):
        agent.rebuild_agent_config_reporting()


# --------------------------------------------------------------------------- #
# The legacy-hook repair sweep honours the same digest attribution.
# --------------------------------------------------------------------------- #


def test_repair_pass_sweeps_the_dashboard_author_stem_on_the_plain_owned_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FP item 4 subtraction: the hook-repair sweep carries NO dashboard-author special
    case. The stem is swept exactly like every other owned name -- a legacy hook key is
    stripped from a file at the stem whatever its other content. The once-user-creatable
    protection lives in the install gate alone (it never overwrites a user file with the
    managed spec); the hook sweep only normalizes recognized hook keys, so there is no
    overwrite for a per-file content gate to prevent here."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A file at the stem with no recorded ownership digest, carrying the legacy hook key.
    squat = {"name": "my-template", "hooks": {"auto_approve_tools": ["x"]}}
    target.write_text(json.dumps(squat), encoding="utf-8")
    assert _is_installers(target) is False  # not the managed spec (no recorded digest)

    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()

    # Swept on the plain owned terms: the legacy key is stripped, like any owned file.
    swept = json.loads(target.read_text(encoding="utf-8"))
    assert "auto_approve_tools" not in swept.get("hooks", {})


def test_repair_pass_rewrites_an_owned_dashboard_author_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: when the file reproduces the installer-recorded digest, the repair
    sweep DOES strip the legacy hook key, exactly as it does for every other owned spec."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A real install's own output + a recorded digest; inject the legacy hook key the sweep
    # should strip, then re-record the digest so the file still confirms as ours.
    agent.rebuild_agent_config()
    managed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    managed["hooks"] = {"auto_approve_tools": ["x"]}
    target.write_text(json.dumps(managed, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(managed))
    assert _is_installers(target) is True  # ours by recorded digest

    # The prior rebuild's own hook-sweep recorded this file's mtime; clear that cache so the
    # re-injected key is not skipped as already-swept.
    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()

    assert "auto_approve_tools" not in json.loads(target.read_text(encoding="utf-8"))["hooks"]


# --------------------------------------------------------------------------- #
# Authorized edits (reset-model / PATCH) keep the file attributable.
# --------------------------------------------------------------------------- #


def test_reset_model_on_the_managed_spec_keeps_it_attributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authorized reset-model edit rewrites the spec's bytes. For the installer to keep
    recognising the rewritten file as its own, the writer that performs the edit must record
    the new digest -- so after the edit the file still confirms, and the next rebuild keeps
    refreshing it under the live ceiling."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert _is_installers(target) is True

    agent.reset_agent_model("kirocrew-dashboard-author")

    # The edited file still confirms as ours (its digest was re-recorded by the edit path)
    # -> still refreshed on the next rebuild.
    assert _is_installers(target) is True
    agent.rebuild_agent_config()
    assert _is_installers(target) is True


# --------------------------------------------------------------------------- #
# GPT 6.1 install-gate branches (maintainer ruling): absent -> install; present +
# digest reproduces -> refresh; present not reproducing OR read-fails -> untouched.
# The installer-recorded digest is the recorded provenance.
# --------------------------------------------------------------------------- #


def test_a_present_file_reproducing_the_digest_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 2: a present file whose bytes reproduce the installer-recorded ownership digest
    IS this installer's own prior write, so a rebuild refreshes it in place -- the digest is
    the recorded provenance the refresh is allowed on. A field the refresh overwrites
    (``model``) stands in for an out-of-date managed spec the refresh should bring back; the
    stale-field edit re-records the digest so the file still confirms before the rebuild."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A real prior install's own output, with its digest recorded.
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    marked = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    marked["model"] = "a-stale-model-from-a-prior-build"
    target.write_text(json.dumps(marked, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(marked))
    assert _is_installers(target) is True  # reproduces the recorded digest

    agent.rebuild_agent_config()

    # Refreshed in place (the stale model is replaced), still the managed spec.
    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert refreshed["name"] == "kirocrew-dashboard-author"
    assert refreshed.get("model") != "a-stale-model-from-a-prior-build"
    assert _is_installers(target) is True


def test_a_forgery_not_reproducing_the_digest_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 -- the core of the digest model. A hand-built spec that reuses the agent
    name and mounts ``kirocrew-core`` -- a forgery the OLD content-mark model would have read
    as the managed spec and overwritten -- does NOT reproduce the installer-recorded digest
    (none is recorded, and its bytes differ from any managed write), so it is left UNTOUCHED.
    Content marks are forgeable; the recorded digest is not."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    forged = {
        "name": "kirocrew-dashboard-author",
        "mcpServers": {"kirocrew-core": {"command": "kirocrew"}},
        "tools": ["@kirocrew-core/skill_search"],
        "prompt": "a hand-built spec reusing the name and the core mount",
        "description": "forges the old content marks but not the digest",
    }
    target.write_text(json.dumps(forged), encoding="utf-8")
    assert _is_installers(target) is False  # no recorded digest -> not ours

    agent.rebuild_agent_config()

    # Left untouched -- the forgery's charter is intact.
    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == (
        "a hand-built spec reusing the name and the core mount"
    )


def test_a_conductor_duplicate_renamed_to_the_stem_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 -- the duplicate the content-mark discriminator could not catch. A copy of
    ANOTHER owned agent (a conductor narrows its servers too, so it has the name, a
    ``kirocrew-core`` reference AND no ``kirocrew-cron`` mount) renamed to this stem would
    forge every content mark. It does NOT reproduce the installer-recorded digest, so it is
    read as a user file and left UNTOUCHED -- its custom charter is never silently
    overwritten. This is the whack-a-mole the digest closes once for every owned agent."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    conductor_dup = {
        "name": "kirocrew-dashboard-author",
        "mcpServers": {"kirocrew-core": {"command": "kirocrew"}},
        "tools": ["@kirocrew-core/skill_search"],
        "prompt": "the user's conductor charter, renamed onto the stem",
        "description": "a renamed conductor copy, not the dashboard author",
    }
    target.write_text(json.dumps(conductor_dup), encoding="utf-8")
    assert _is_installers(target) is False  # no recorded digest -> not ours

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == (
        "the user's conductor charter, renamed onto the stem"
    )


def test_a_managed_file_with_the_digest_record_lost_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate fail-closed tradeoff: if the ownership digest record is LOST (e.g. the
    user deleted ``agent_model_state.json``) but a valid managed ``.json`` is still present,
    the rebuild sees NO recorded digest -> treats the file as unconfirmed -> leaves it
    untouched rather than overwriting a file it cannot attribute. Never destroys data;
    the user removes the stale file to let the install heal."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    before = target.read_bytes()

    # Lose the ownership record (sidecar wiped), keep the managed file.
    agent_state.set_managed_digest("kirocrew-dashboard-author", None)
    assert _is_installers(target) is False  # no record -> unconfirmed, fail closed

    agent.rebuild_agent_config()

    assert target.read_bytes() == before  # untouched; not overwritten


def test_a_present_file_whose_read_fails_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 3 (read-fail): an existing file at the stem whose read FAILS (oversized,
    non-UTF-8, otherwise unparseable) cannot be confirmed as this installer's own write, so
    it is left untouched -- never overwritten on a failed read. A None from the capped
    reader is NOT treated as 'ours to write'."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    raw = b"\xff\xfe not valid utf-8 or json at all"
    target.write_bytes(raw)
    # The capped reader returns None for this file (unparseable); the gate must leave it.
    monkeypatch.setattr(wa.agent_mod, "_read_spec_capped", lambda p: None)

    wa._install_dashboard_author_agent()  # must not raise, must not overwrite

    assert target.read_bytes() == raw  # untouched


def test_a_user_markdown_spec_at_the_stem_blocks_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: both the ``.json`` and ``.md`` candidates are checked before the name is
    claimed. kiro-cli reads ``<stem>.json`` and the v3 engine also reads ``<stem>.md``, and
    a ``.json`` written beside a user's ``.md`` would SHADOW that markdown spec. So a user
    ``kirocrew-dashboard-author.md`` (which the installer never writes -- it only writes the
    ``.json`` form) blocks the install: no ``.json`` is written and the user's ``.md`` is
    left untouched."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    md = rig.agents / "kirocrew-dashboard-author.md"
    json_target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    md_body = (
        "---\nname: kirocrew-dashboard-author\n---\nThe user's own markdown dashboard author.\n"
    )
    md.write_text(md_body, encoding="utf-8")

    wa._install_dashboard_author_agent()

    # The user's .md is untouched, and no .json was written beside it to shadow it.
    assert md.read_text(encoding="utf-8") == md_body
    assert not json_target.exists()
