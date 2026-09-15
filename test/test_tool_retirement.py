"""A retired tool name migrates forward through a RESTRICTION, and never a grant.

A published tool name is a key that other people's persisted records were written
against, so renaming a tool re-points or orphans every stored rule that named the
old spelling. This file pins the asymmetry that makes carrying one forward safe:

* a RESTRICTION migrates -- fail-CLOSED, the worst case is something stays denied
  that an operator would have allowed, and they can see it and undo it;
* a GRANT does NOT -- fail-OPEN, and per ``acp/kas_agents.py`` an auto-approved
  call never reaches Crew's permission callback, so widening a grant removes the
  deny-list and the audit trail with it.

Both directions are pinned. Either test alone passes on the broken code: the
restriction test alone would pass on a build that also widened grants, and the
grant test alone passes on a build that widens nothing. The pair is the contract.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent
from kiro_crew.acp import kas_agents
from kiro_crew.tool_retirement import (
    RETIRED_TOOL_NAMES,
    widen_restriction_for_retired_names,
)

OLD = "monitor_start"
NEW = "monitor_patrol"


class TestTheMapIsRestrictionOnly:
    def test_the_retired_name_maps_to_the_current_one(self) -> None:
        assert RETIRED_TOOL_NAMES[OLD] == NEW

    def test_widening_only_ever_adds(self) -> None:
        # The safety property in one line: the result is always a superset, so this
        # function can only ever cause MORE to be refused.
        for names in ([], [OLD], [NEW], [OLD, "echo"], ["echo"]):
            assert set(names) <= widen_restriction_for_retired_names(names)

    def test_the_old_name_pulls_in_the_current_one(self) -> None:
        assert widen_restriction_for_retired_names([OLD]) == {OLD, NEW}

    def test_the_current_name_does_not_pull_in_the_old_one(self) -> None:
        # One-way. The reverse would be a second spelling nothing persisted.
        assert widen_restriction_for_retired_names([NEW]) == {NEW}

    def test_an_unrelated_restriction_is_untouched(self) -> None:
        assert widen_restriction_for_retired_names(["echo", "wait"]) == {"echo", "wait"}

    @pytest.mark.parametrize("junk", [None, 123, "", [], {}])
    def test_non_string_entries_are_dropped(self, junk: object) -> None:
        assert widen_restriction_for_retired_names([OLD, junk]) == {OLD, NEW}  # type: ignore[list-item]

    def test_there_is_no_grant_side_helper(self) -> None:
        # The absence is the design. A grant-side equivalent would be the bug, so
        # this asserts nothing in the module offers one.
        import kiro_crew.tool_retirement as mod

        exported = {n for n in dir(mod) if not n.startswith("_")}
        offenders = {
            n for n in exported if any(w in n.lower() for w in ("grant", "allow", "permit"))
        }
        assert offenders == set(), offenders


class TestAPersistedRestrictionSurvivesTheRename:
    """The fail-open the rename itself would otherwise produce.

    An operator persisted ``exclude: ["monitor_start"]`` to stop a restricted agent
    arming unattended loops. After the rename the exact-name check sees
    ``monitor_patrol``, so without widening the rule silently stops matching and the
    capability comes back ungoverned.
    """

    def _persisted_policy(self, monkeypatch, exclude: list[str]) -> None:
        """Stub the POLICY TRANSPORT, not the resolver, so the widening runs."""
        import io
        from types import SimpleNamespace

        from kiro_crew import mcp_shared

        mcp_shared._excluded_tools_by_session.clear()
        mcp_shared._last_failure_time = 0.0
        mcp_shared._last_startup_race_time = 0.0
        mcp_shared._failure_count = 0
        body = json.dumps({"exclude": exclude}).encode("utf-8")
        monkeypatch.setattr(mcp_shared, "loopback_urlopen", lambda req, timeout=0: io.BytesIO(body))
        monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port: "internal")
        monkeypatch.setattr(
            mcp_shared.KiroCrewConfig,
            "load",
            classmethod(
                lambda _cls: SimpleNamespace(dashboard=SimpleNamespace(url="http://localhost:5476"))
            ),
        )

    def test_an_old_name_exclusion_resolves_to_cover_the_current_name(self, monkeypatch) -> None:
        # Through the REAL resolver, not the helper: this is the wiring that would
        # silently stop matching, so a test on the pure function alone would pass
        # on the broken build.
        from kiro_crew import mcp_shared

        self._persisted_policy(monkeypatch, [OLD])
        resolved = mcp_shared._resolve_excluded_tools("dashboard:alice")
        assert resolved == {OLD, NEW}

    def test_a_policy_naming_nothing_retired_resolves_unchanged(self, monkeypatch) -> None:
        from kiro_crew import mcp_shared

        self._persisted_policy(monkeypatch, ["echo"])
        assert mcp_shared._resolve_excluded_tools("dashboard:bob") == {"echo"}

    def test_the_relayed_excluded_tools_block_is_widened(self) -> None:
        out = kas_agents.to_client_custom_agent("a", {"name": "a", "excludedTools": [OLD]}, "p")
        assert NEW in out["excludedTools"]
        assert OLD in out["excludedTools"]

    def test_a_relayed_block_naming_nothing_retired_is_unchanged(self) -> None:
        out = kas_agents.to_client_custom_agent("a", {"name": "a", "excludedTools": ["echo"]}, "p")
        assert out["excludedTools"] == ["echo"]


class TestAGrantDoesNotMigrate:
    """The test that stops the next person completing the symmetry.

    Widening a grant through a retired name is a governance bypass that also skips
    the audit trail, so a grant naming the old tool must NOT reach the new one.
    """

    @pytest.mark.parametrize(
        "grants",
        [agent._CONDUCTOR_CORE_GRANTS, agent._PIPELINE_CONDUCTOR_CORE_GRANTS],
        ids=["conductor", "pipeline-conductor"],
    )
    def test_a_grant_naming_the_retired_tool_does_not_grant_the_current_one(
        self, grants: tuple[str, ...], monkeypatch
    ) -> None:
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        # A stale grant list, as an older on-disk spec would carry it.
        stale = tuple(
            f"@kirocrew-core/{OLD}" if g == f"@kirocrew-core/{NEW}" else g for g in grants
        )
        out = agent._filter_auto_approve(stale, source="test")
        assert f"@kirocrew-core/{OLD}" in out  # passed through, as any ref is
        assert f"@kirocrew-core/{NEW}" not in out  # NOT widened -- the whole point

    def test_the_shipped_grant_tuples_name_only_the_current_tool(self) -> None:
        for grants in (agent._CONDUCTOR_CORE_GRANTS, agent._PIPELINE_CONDUCTOR_CORE_GRANTS):
            assert f"@kirocrew-core/{NEW}" in grants
            assert f"@kirocrew-core/{OLD}" not in grants
