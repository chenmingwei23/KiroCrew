"""The ``accept_eval`` MCP tool: verdict vocabulary, the no-model-argv invariant,
the sensitive-path floor the ``file`` kind enforces, and batch isolation.

The tool is the sole acceptance evaluator — the work ledger's contract
(`work_ledger.py`) is pinned to it, and `test_work_ledger.py` runs its
`accept_batch` through this very handler.

The handler is called directly — the registry wiring is covered by
``test_mcp_tool_registry`` — with ``mcp_core.sel`` stubbed so the real async SEL
writer is not started, and the strict-identity resolver left as-is: the tool
tolerates an unresolved caller (it reads world state and writes no session
state), so an unresolved identity in-suite simply attributes the audit to
``mcp_core``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools import accept_eval as tool


class _NoopSel:
    """A SEL stand-in that records nothing — the real writer is async and the
    tool's only SEL use is one ``log_tool_invocation`` per call."""

    def log_tool_invocation(self, **kwargs: object) -> None:  # noqa: D401
        return None


@pytest.fixture(autouse=True)
def _stub_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mcp_core, "sel", lambda: _NoopSel())


def _eval(items: list) -> list[dict]:
    """Run the tool over *items* and return the parsed ``results`` list."""
    out = json.loads(tool.accept_eval("accept_eval", {"items": items}))
    return out["results"]


# ── Verdict vocabulary across kinds ──


def test_file_kind_pass_and_fail(tmp_path: Path) -> None:
    present = tmp_path / "there"
    present.write_text("x", encoding="utf-8")
    absent = tmp_path / "gone"
    results = _eval(
        [
            {"id": "p", "accept": {"kind": "file", "path": str(present), "exists": True}},
            {"id": "a", "accept": {"kind": "file", "path": str(absent), "exists": False}},
            {"id": "f", "accept": {"kind": "file", "path": str(absent), "exists": True}},
        ]
    )
    verdicts = {r["id"]: r["verdict"] for r in results}
    assert verdicts == {"p": "pass", "a": "pass", "f": "fail"}


def test_file_absent_exists_key_defaults_to_presence(tmp_path: Path) -> None:
    present = tmp_path / "here"
    present.write_text("x", encoding="utf-8")
    (verdict,) = (
        r["verdict"] for r in _eval([{"id": "1", "accept": {"kind": "file", "path": str(present)}}])
    )
    assert verdict == "pass"


def test_file_rejects_a_non_boolean_exists(tmp_path: Path) -> None:
    present = tmp_path / "here"
    present.write_text("x", encoding="utf-8")
    (r,) = _eval([{"id": "1", "accept": {"kind": "file", "path": str(present), "exists": "false"}}])
    assert r["verdict"] == "error"
    assert "boolean exists" in r["evidence"]


def test_human_approval_is_pending() -> None:
    (r,) = _eval([{"id": "1", "accept": {"kind": "human_approval"}}])
    assert r["verdict"] == "pending"


def test_unknown_kind_is_an_error() -> None:
    (r,) = _eval([{"id": "1", "accept": {"kind": "nope"}}])
    assert r["verdict"] == "error"
    assert "unknown accept kind" in r["evidence"]


def test_the_cmd_kind_is_refused_and_says_what_to_use() -> None:
    (r,) = _eval([{"id": "1", "accept": {"kind": "cmd", "cmd": ["git", "status"]}}])
    assert r["verdict"] == "refused"
    assert "may not name a command" in r["evidence"]
    assert "pr_checks" in r["evidence"]


def test_pr_checks_rejects_a_non_integer_pr() -> None:
    for bad in ("123", True, 1.0, None):
        (r,) = _eval([{"id": "1", "accept": {"kind": "pr_checks", "pr": bad}}])
        assert r["verdict"] == "error", bad
        assert "integer pr" in r["evidence"]


# ── The sensitive-path floor the file kind enforces ──


def test_a_sensitive_file_path_is_refused() -> None:
    """A ``file`` spec naming a credential path is refused. The tool validates
    the path through ``hooks.safe_reads.validate_file_path`` (sensitive-path +
    the Windows UNC gate) because ``on_tool_call`` cannot see a path nested
    inside ``items[].accept.path``."""
    (r,) = _eval(
        [{"id": "1", "accept": {"kind": "file", "path": str(Path.home() / ".aws" / "credentials")}}]
    )
    assert r["verdict"] == "refused"


def test_a_rejected_path_does_not_touch_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is decided by ``validate_file_path`` returning ``None``,
    BEFORE any existence check — so a path the gate rejects is never probed,
    which is the point for a UNC path whose probe would be an outbound SMB
    authentication."""
    import kiro_crew.mcp_tools.accept_eval as m

    called = {"exists": 0}
    real_exists = Path.exists

    def counting_exists(self: Path) -> bool:
        called["exists"] += 1
        return real_exists(self)

    monkeypatch.setattr(m, "validate_file_path", lambda raw: None)
    monkeypatch.setattr(Path, "exists", counting_exists)
    (r,) = _eval([{"id": "1", "accept": {"kind": "file", "path": "/anything"}}])
    assert r["verdict"] == "refused"
    assert called["exists"] == 0


def test_a_validated_path_is_the_one_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existence is checked on the CANONICAL path ``validate_file_path`` returns,
    not on the raw spec string — so canonicalization (and its UNC/link screen)
    is on the path to any filesystem touch."""
    import kiro_crew.mcp_tools.accept_eval as m

    probed: list[str] = []
    real_exists = Path.exists

    def recording_exists(self: Path) -> bool:
        probed.append(str(self))
        return real_exists(self)

    monkeypatch.setattr(m, "validate_file_path", lambda raw: "/canonical/here")
    monkeypatch.setattr(Path, "exists", recording_exists)
    _eval([{"id": "1", "accept": {"kind": "file", "path": "/raw/spec"}}])
    assert probed == [str(Path("/canonical/here"))]


# ── The no-model-argv invariant ──


def test_no_spec_field_can_name_a_command() -> None:
    """The source carries no path from a spec field into ``_exec`` — every argv
    is built from a fixed template. Pinned as a source check so a future handler
    that passed spec input through would trip it."""
    src = Path(tool.__file__).read_text(encoding="utf-8")
    # The one allowlisted command set, and the only binary any handler builds.
    assert tool._SELF_BUILT_COMMANDS == {"gh"}
    # The invariant is stated where a new-kind author will read it.
    assert "No model-authored argv ever reaches subprocess" in src


def test_run_refuses_a_command_it_did_not_build() -> None:
    """``_exec`` fails closed on anything outside ``_SELF_BUILT_COMMANDS`` — the
    internal assertion that a handler never started passing spec input through."""
    problem, proc = tool._exec(["git", "status"])
    assert proc is None
    assert problem[0] == "refused"
    assert "not a command this tool builds" in problem[1]


def test_pr_checks_builds_its_own_gh_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pr_checks`` runs ``gh pr checks <n> --repo <r>`` and nothing the spec
    chose — the number and repo are the only spec-derived values, and they are
    positional/flag arguments, never the command."""
    seen: list[list] = []

    def fake_run(argv: list, cwd: object = None) -> tuple[str, str]:
        seen.append(argv)
        return ("pass", "ok")

    monkeypatch.setattr(tool, "_run", fake_run)
    _eval([{"id": "1", "accept": {"kind": "pr_checks", "pr": 42, "repo": "o/n"}}])
    assert seen == [["gh", "pr", "checks", "42", "--repo", "o/n"]]


# ── Batch isolation ──


def test_a_non_object_item_does_not_abort_the_run(tmp_path: Path) -> None:
    """One malformed entry becomes its own ``error`` verdict; its siblings'
    verdicts still come back."""
    present = tmp_path / "here"
    present.write_text("x", encoding="utf-8")
    results = _eval(
        [
            "not-a-dict",
            {"id": "ok", "accept": {"kind": "file", "path": str(present)}},
        ]
    )
    assert len(results) == 2
    assert results[0]["verdict"] == "error"
    assert results[0]["id"] == "#0"  # positional fallback keeps it identifiable
    assert results[1]["id"] == "ok"
    assert results[1]["verdict"] == "pass"


def test_a_missing_id_falls_back_to_a_positional_marker() -> None:
    (r,) = _eval([{"accept": {"kind": "human_approval"}}])
    assert r["id"] == "#0"


# ── Top-level shape ──


def test_items_must_be_a_list() -> None:
    out = json.loads(tool.accept_eval("accept_eval", {"items": "nope"}))
    assert "error" in out
    assert "must be a JSON array" in out["error"]


def test_over_cap_batch_is_refused() -> None:
    too_many = [
        {"id": str(i), "accept": {"kind": "human_approval"}} for i in range(tool._MAX_ITEMS + 1)
    ]
    out = json.loads(tool.accept_eval("accept_eval", {"items": too_many}))
    assert "error" in out
    assert "too many items" in out["error"]
