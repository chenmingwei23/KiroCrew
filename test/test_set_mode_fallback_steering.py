"""Authored-spec sessions still receive the three default steering roots.

A KAS projection carries global steering, workspace steering and ``AGENTS.md``
in its projected view, then writes the workspace
``chat.disableInheritingDefaultResources`` overlay so kiro-cli stops loading
those three roots natively. One ``cli.json`` serves every session in the
workspace, so a session that runs the AUTHORED spec (no projected view active)
carries that overlay from another live session and gets the two extra roots
(global steering, ``AGENTS.md``) from nobody -- its spec declares only
``.kiro/steering``. The delivery bookkeeping must not report those roots as
natively delivered for that session, or folder steering skips them and the
session loses them.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.skill_projection import (
    _INHERIT_SETTING,
    _INHERIT_SOURCE,
    _MANAGED_SETTING,
    authored_agent_misses_default_roots,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO


def _write_settings(project: Path, body: dict) -> None:
    settings = project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(body), encoding="utf-8")


def _managed_overlay(*, original_inherited: bool) -> dict:
    """The overlay a projection leaves: the native key forced true, Crew's own
    record of the preference it replaced."""
    return {
        _INHERIT_SETTING: True,
        _MANAGED_SETTING: original_inherited,
        _INHERIT_SOURCE: "local",
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".kiro" / "settings").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return tmp_path / "project"


class TestAuthoredAgentMissesDefaultRoots:
    def test_managed_overlay_over_an_inheriting_workspace_loses_the_roots(self, project):
        """The projection overlaid a workspace that inherited: the authored
        session gets neither global steering nor ``AGENTS.md``."""
        _write_settings(project, _managed_overlay(original_inherited=True))
        assert authored_agent_misses_default_roots(str(project)) is True

    def test_managed_overlay_over_an_opted_out_workspace_keeps_the_users_choice(self, project):
        """A workspace the operator opted out of never wanted those roots, so a
        missing root is their choice, not a gap to backfill."""
        _write_settings(project, _managed_overlay(original_inherited=False))
        assert authored_agent_misses_default_roots(str(project)) is False

    def test_a_raw_user_opt_out_is_not_crews_overlay(self, project):
        """Without Crew's record the native key is the user's own opt-out."""
        _write_settings(project, {_INHERIT_SETTING: True})
        assert authored_agent_misses_default_roots(str(project)) is False

    def test_an_inheriting_workspace_with_no_overlay_reaches_the_roots(self, project):
        _write_settings(project, {})
        assert authored_agent_misses_default_roots(str(project)) is False

    def test_unreadable_settings_assume_the_roots_are_reachable(self, project, monkeypatch):
        from kiro_crew.acp import skill_projection

        def boom(*_args, **_kwargs):
            raise OSError("settings unreadable")

        monkeypatch.setattr(skill_projection, "_settings", boom)
        assert authored_agent_misses_default_roots(str(project)) is False


class _Handle:
    def __init__(self, delivered: bool) -> None:
        self.native_default_roots_delivered = delivered


def _provider(*, backend: str, delivered: bool) -> AcpSessionProvider:
    runtime = MagicMock()
    runtime.acp_backend = backend
    return AcpSessionProvider(_Handle(delivered), runtime)


class TestProviderNativeSteering:
    def test_kas_projected_session_owns_steering(self):
        assert _provider(backend=ACP_BACKEND_KAS, delivered=True).native_steering is True

    def test_kas_authored_fallback_session_does_not_own_steering(self):
        """The authored rung does not deliver the default roots natively, so the
        provider must not claim it does -- folder steering backfills them."""
        assert _provider(backend=ACP_BACKEND_KAS, delivered=False).native_steering is False

    def test_a_non_kas_backend_never_owns_steering(self):
        assert _provider(backend=ACP_BACKEND_KIRO, delivered=True).native_steering is False
