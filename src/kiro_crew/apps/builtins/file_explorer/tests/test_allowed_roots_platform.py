"""Windows portability: the browsing allow-list holds no POSIX-only root.

``/home`` and ``/opt`` are POSIX filesystem conventions with no Windows
counterpart -- on Windows a bare ``Path("/home")`` is read relative to the
current drive and lands on ``C:\\home``, a directory nobody asked for.
``server._compute_allowed_roots`` appends the two conventions only under
``platform_compat.IS_POSIX``, so a Windows allow-list is the user profile
(``Path.home()``, which reads %USERPROFILE%) plus the system temp dir, and
nothing else.

These tests drive the platform switch through ``monkeypatch`` instead of the
host, so every shard asserts the same thing.
"""

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.file_explorer import server


@pytest.fixture
def home_and_tmp(tmp_path):
    """Two distinct existing directories standing in for the profile and temp dirs."""
    home = tmp_path / "profile"
    tmp = tmp_path / "temp"
    for d in (home, tmp):
        d.mkdir()
    return home, tmp


def test_windows_allow_list_is_exactly_home_and_tmp(monkeypatch, home_and_tmp):
    """With ``IS_POSIX`` false the function synthesises no third root."""
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    roots = server._compute_allowed_roots(home, tmp)

    assert roots == [home, tmp]


def test_windows_allow_list_names_no_drive_relative_convention_dir(monkeypatch, home_and_tmp):
    """No root is a drive-relative reading of a POSIX convention directory.

    The shape being guarded is a root sitting directly under a drive letter and
    named ``home`` or ``opt`` -- the form ``Path("/home")`` takes on Windows.
    """
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", False)

    roots = server._compute_allowed_roots(home, tmp)

    drive_relative = [p for p in roots if p.name in {"home", "opt"} and p.parent == Path(p.anchor)]
    assert not drive_relative, drive_relative


def test_posix_branch_appends_and_keeps_home_first(monkeypatch, home_and_tmp):
    """The POSIX branch appends, so the frontend's ``roots[0]`` stays the home dir."""
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    roots = server._compute_allowed_roots(home, tmp)

    assert roots[0] == home
    assert roots[1] == tmp
    # Whatever the POSIX branch contributes is absolute and present: the
    # exists() filter drops a convention dir the host does not have.
    assert all(p.is_absolute() and p.exists() for p in roots)


def test_no_configured_root_is_a_whole_volume():
    """The live allow-list never exposes a drive or filesystem root.

    ``ALLOWED_ROOTS`` is computed against the real host at import time. A root
    equal to its own anchor (``C:\\`` or ``/``) would make every file on the
    volume reachable through a browser scoped to the user's own directories.
    """
    assert server.ALLOWED_ROOTS
    volume_roots = [p for p in server.ALLOWED_ROOTS if p == Path(p.anchor)]
    assert not volume_roots, volume_roots


# ---------------------------------------------------------------------------
# Configurable extra roots
# ---------------------------------------------------------------------------


def test_configured_extra_root_is_appended_after_defaults(monkeypatch, home_and_tmp, tmp_path):
    """A configured, existing root widens the list but never displaces home."""
    home, tmp = home_and_tmp
    extra = tmp_path / "mnt-data"
    extra.mkdir()
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    roots = server._compute_allowed_roots(home, tmp, [extra])

    # roots[0] (the frontend default folder) is still home: extras are
    # appended, not prepended.
    assert roots[0] == home
    assert extra in roots
    # The extra sits after the built-in defaults.
    assert roots.index(extra) > roots.index(tmp)


def test_configured_extra_root_cannot_narrow_the_builtin_floor(monkeypatch, home_and_tmp, tmp_path):
    """Configuring extras adds roots; it never removes the built-in defaults."""
    home, tmp = home_and_tmp
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    without_extra = server._compute_allowed_roots(home, tmp)
    with_extra = server._compute_allowed_roots(home, tmp, [extra])

    # Every root present without the config is still present with it.
    assert set(without_extra).issubset(set(with_extra))


def test_nonexistent_configured_root_is_dropped(monkeypatch, home_and_tmp, tmp_path):
    """A configured root absent on this host is dropped by the exists() filter."""
    home, tmp = home_and_tmp
    missing = tmp_path / "does-not-exist"  # never created
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    roots = server._compute_allowed_roots(home, tmp, [missing.resolve()])

    assert missing.resolve() not in roots


def test_configured_extra_root_is_deduped(monkeypatch, home_and_tmp):
    """A configured root equal to a built-in default does not appear twice."""
    home, tmp = home_and_tmp
    monkeypatch.setattr(server.platform_compat, "IS_POSIX", True)

    roots = server._compute_allowed_roots(home, tmp, [home])

    assert roots.count(home) == 1


def test_parse_extra_roots_keeps_only_absolute_strings(tmp_path):
    """``_parse_extra_roots`` filters non-list, non-string, and relative entries."""
    existing = tmp_path / "ok"
    existing.mkdir()

    # Non-list input -> empty.
    assert server._parse_extra_roots("not-a-list") == []
    assert server._parse_extra_roots(None) == []

    parsed = server._parse_extra_roots(
        [str(existing), "relative/path", "", 123, None, str(tmp_path / "missing")]
    )

    # Absolute strings survive and are resolved; the resolved missing one is
    # kept here (exists() filtering happens later in _compute_allowed_roots).
    assert existing.resolve() in parsed
    # Relative, empty, and non-string entries are dropped.
    assert all(p.is_absolute() for p in parsed)
    assert len(parsed) == 2  # existing + the absolute-but-missing one


def test_load_configured_extra_roots_fails_closed(monkeypatch):
    """A config load error widens nothing — the helper returns no extras."""

    class _Boom:
        @staticmethod
        def load():
            raise RuntimeError("config unreadable")

    monkeypatch.setattr(server, "KiroCrewConfig", _Boom)

    assert server._load_configured_extra_roots() == []


def test_load_configured_extra_roots_reads_the_agent_field(monkeypatch, tmp_path):
    """The helper reads ``agent.file_explorer_extra_roots`` from the loaded config."""
    existing = tmp_path / "configured"
    existing.mkdir()

    class _Agent:
        file_explorer_extra_roots = [str(existing)]

    class _Cfg:
        agent = _Agent()

        @staticmethod
        def load():
            return _Cfg()

    monkeypatch.setattr(server, "KiroCrewConfig", _Cfg)

    assert existing.resolve() in server._load_configured_extra_roots()


def test_config_file_holding_extra_roots_is_agent_write_protected():
    """The widen-only knob is safe ONLY because agents cannot write its home.

    ``agent.file_explorer_extra_roots`` lives in the Kiro Crew config file. The
    whole trust-boundary argument for this feature is that an agent cannot set
    it to widen its own browse scope — which holds only while that file is on
    the agent file-edit write-deny floor. This pins that dependency: if a future
    change moved config.json off ``is_sensitive_write_path`` (or relocated the
    key to an agent-writable surface), this test fails rather than the hole
    shipping silently.

    config.json is WRITE-protected but readable (``is_sensitive_path`` False),
    which is exactly what the backend needs: it reads the key via
    ``KiroCrewConfig.load()`` while the agent's edit tool is refused.
    """
    from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

    for prefix in ("~/.kiro/crew", "~/.kirocrew"):
        cfg = f"{prefix}/config.json"
        assert is_sensitive_write_path(cfg) is True, cfg  # agent edit denied
        assert is_sensitive_path(cfg) is False, cfg  # backend read allowed
