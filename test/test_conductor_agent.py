"""Conductor agent installer + bundled acceptance evaluator.

``kirocrew-conductor`` IS the work-ledger conductor: it mounts ``kirocrew-work``,
dispatches bind-before-seed, and settles every ``done`` claim with the
``accept_eval`` MCP tool. ``kirocrew-ledger-conductor`` is a deprecated alias
emitting this same spec under its old name for one release, and the proof that
the two cannot drift lives in ``test_ledger_conductor_agent.py``.

The installer test mirrors the research-agent installer test's shape: stub the
agents dir and ``build_agent_config``, run the installer, assert on the JSON it
wrote. The evaluator's own behaviour (verdict vocabulary, the no-model-argv
invariant, the sensitive-path gate, batch isolation) is tested in
``test_accept_eval_tool.py``; the tests here cover only how the spec and skill
name and reach it.
"""

import json
from pathlib import Path

from kiro_crew import agent
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_sdk.drivers.acp import derived_agent_permissions
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills" / "goal-conductor"
)

#: A release that accepts a spec ``permissions`` block, and one that refuses it.
#: Expressed against the floor rather than as literals so raising the floor
#: cannot leave a test asserting the old boundary.
_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REFUSES = (SPEC_PERMISSIONS_MIN_VERSION[0], SPEC_PERMISSIONS_MIN_VERSION[1] - 1, 0)
_INHERITED_PERMISSIONS = {"rules": [{"capability": "web_fetch", "effect": "deny"}]}


def _pin_spec_permissions_cli(monkeypatch, which):
    """Pin what the writer's version gate believes the installed kiro-cli is.

    The generated spec writers share one gate (``_write_derived_permissions``), which
    reads ``installed_kiro_cli_version`` function-locally from
    ``kiro_crew.kiro_cli``, so the patch lands in the owning module. Without it
    the answer is whatever the test HOST has, which on CI is nothing and reads
    as "unknown" -- the refusing case -- so a writer test asserting a
    ``permissions`` block would fail for a host reason rather than a code one.
    ``which`` is one of ``"accepts"``, ``"refuses"`` or ``"unknown"``.
    """
    version = {"accepts": _ACCEPTS, "refuses": _REFUSES, "unknown": None}[which]
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: version)


class TestConductorInstaller:
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None, cli_version="accepts"):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        _pin_spec_permissions_cli(monkeypatch, cli_version)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                    "builder-mcp": {"command": "/x/builder", "args": []},
                },
                "tools": ["fs_write", "@kirocrew-core"],
                "allowedTools": ["@kirocrew-core"],
                "permissions": _INHERITED_PERMISSIONS,
            },
        )
        monkeypatch.setattr(
            agent,
            "_kirocrew_mcp_invocation",
            lambda sub: ("/resolved/kirocrew", [sub]),
        )
        # Pin the ceiling predicate: the default is an ungoverned host (keep every
        # grant), and the governed case gets its own test below.
        monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))
        agent._install_conductor_agent()
        return json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))

    def test_identity_and_charter(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-conductor"
        assert "work item" in data["prompt"]

    def test_prompt_does_not_carry_the_retired_verbosity_token(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        # Reply style now arrives as session-context chrome for every
        # agent; a token left here would reach the model as a literal.
        assert "{{VERBOSITY_BLOCK}}" not in data["prompt"]

    def test_prompt_drives_patrol_with_monitor_start_not_wait(self, tmp_path, monkeypatch):
        """A patrol round outlives a turn, so the loop must own the turn boundary.

        An in-turn ``wait`` + re-poll loop spends the turn budget on latency and
        dies at the turn cap mid-round, which loses the loop. Three halves of the
        contract are pinned: ``monitor_start`` arms the round, ``wait`` is the
        single-round fallback for a refused arm rather than the primary
        mechanism, and the conductor knows the tool it needs to stop the loop it
        armed. Whitespace is normalised so re-wrapping the prompt cannot fail
        this for a formatting reason.
        """
        data = self._install(tmp_path, monkeypatch)
        prompt = " ".join(data["prompt"].split())
        assert "Arm a loop on your own session with `monitor_start`" in prompt
        assert "If arming is refused outright" in prompt
        assert "drive that one round with `wait`" in prompt
        assert "autonudge_stop" in prompt

    def test_prompt_names_the_tools_it_expects_to_be_used(self, tmp_path, monkeypatch):
        """The charter mounts whole servers, so the prompt must name what it wants.

        ``@kirocrew-core`` puts 74 registered tools in front of this agent. Naming
        only the session-control ones left the rest unaddressed, which is how a
        conductor talks itself into running a work item in its own turn. Both
        directions are pinned: the tools the four jobs need, and the four that
        would end-run the no-write property by executing the work here.

        Those four are also the reason the core grant is per-verb rather than
        server-wide — the prompt PROHIBITS them, so a blanket auto-approve
        contradicted the charter it shipped with.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        for named in (
            "session_create",
            "session_ledger_record",
            "monitor_update",
            "resource_status",
            "ask_question",
            "skill_search",
            "tool_search",
        ):
            assert named in prompt, named
        for forbidden in ("spawn_run", "spawn_sub_agents", "workflow_run", "task_run"):
            assert forbidden in prompt, forbidden
        assert "never goes to" in prompt

    def test_no_write_tool_and_shell_not_preapproved(self, tmp_path, monkeypatch):
        """The security properties of the spec, in one place.

        No ``fs_write``: the conductor cannot do a work item's work itself.
        ``@kirocrew-core`` and ``@kirocrew-dashboard`` are MOUNTED whole but never
        granted whole — the auto-approve list names verbs, so the destructive ones
        keep prompting. ``execute_bash`` is mounted and never granted, because
        ``allowedTools`` has no argument matching and trusting the two bundled
        scripts cannot be told apart from trusting arbitrary shell.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-dashboard" not in data["allowedTools"]
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert "execute_bash" in data["tools"]
        assert "execute_bash" not in data["allowedTools"]

    def test_only_create_and_read_verbs_are_auto_approved(self, tmp_path, monkeypatch):
        """The grant invariant: create or read, never mutate someone else's thing.

        Every auto-approved verb is reachable by content the conductor ingested
        (its charter reads issue text; it holds ``web_fetch`` for that), with no
        human in the loop on a nudge-driven cycle. So a granted verb may make a
        NEW folder or session, or read — never write a resource the user already
        arranged, because that destroys state no prompt asked about.

        Pinned as literal names rather than by importing the tuple: the point is
        that WIDENING the tuple fails a test, which a tautological assertion
        against the tuple itself could never catch. Each withheld verb below is
        withheld for a reason recorded next to it in ``agent.py``:
        ``chat_folder_move_session`` PATCHes another session's ``folder_id``
        (target comes from the arguments, not the caller), ``session_send`` runs
        text as a peer's turn, ``session_stop`` discards a peer's in-flight work,
        and ``chat_folder_move`` reparents an existing tree.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        for verb in (
            "chat_folder_move_session",
            "chat_folder_move",
            "session_send",
            # The fan-out write, withheld on `session_send`'s reason multiplied by
            # the fleet: one call runs ingested text as a user-role turn in every
            # session this agent created, and nothing bounds what is sent.
            "session_broadcast",
            "session_stop",
        ):
            assert f"@kirocrew-dashboard/{verb}" not in granted, verb
        for verb in (
            "chat_folder_tree",
            "chat_folder_create",
            "chat_folder_file_self",
            "session_create",
            "session_read_message",
            # A pure read, narrower than `session_read_message` beside it (a
            # liveness word per child, no transcript content), and asked on every
            # unattended patrol cycle -- so gating it would put an approval prompt
            # in a loop with nobody at the keyboard.
            "session_status",
        ):
            assert f"@kirocrew-dashboard/{verb}" in granted, verb

    def test_no_dashboard_verb_is_granted_outside_the_named_set(self, tmp_path, monkeypatch):
        """Nothing reaches ``allowedTools`` that the audit above did not rule on.

        Guards the gap the per-verb lists cannot: a NEW tool added to the
        dashboard server, or a stray entry added to the grant tuple, would
        otherwise be auto-approved without anyone deciding it should be. The
        expected set is spelled out so adding a grant is a deliberate edit here.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        dashboard = {g for g in granted if g.startswith("@kirocrew-dashboard")}
        assert dashboard == {
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/chat_folder_file_self",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
            "@kirocrew-dashboard/session_status",
        }
        # The bare server must never appear: it would grant every verb, including
        # the four the test above withholds.
        assert "@kirocrew-dashboard" not in granted

    def test_core_grants_are_named_verbs_never_the_whole_server(self, tmp_path, monkeypatch):
        """Both directions of the core narrowing, because only one of them can rot.

        The positive half — the 14 verbs the charter needs are granted — passes
        just as well against the server-wide ``@kirocrew-core`` this replaced, so
        on its own it is vacuous on the thing being changed. The negative half is
        the real assertion: the verbs the charter never names must be DENIED, and
        it is the half a future edit can silently undo by restoring the bare
        server or widening the tuple.

        The denied set carries a representative per family rather than one token
        case, because they reach ``allowed_tools_to_permissions`` as separate
        patterns and a family could classify differently: persistent work
        (``task_run``, the ``workflow_*`` group), agent fan-out (the ``spawn_*``
        group), session and workspace mutation (``set_project``,
        ``reset_conversation``), egress (``deploy_artifact``, ``browser``,
        ``ops_mission_control_api``) and the ``artifact_*`` writes.

        Spelled as literals, deliberately: deriving the expectation from
        ``agent._CONDUCTOR_CORE_GRANTS`` would let the tuple and its test drift
        together and assert nothing.
        """
        granted = self._install(tmp_path, monkeypatch)["allowedTools"]
        core = {g for g in granted if g.startswith("@kirocrew-core")}
        assert core == {
            "@kirocrew-core/monitor_start",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/select_crew",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
            # The acceptance evaluator, as a core MCP tool. Auto-approved so
            # patrol verification never
            # blocks on an approval; it reads world state and builds its own
            # argv, so it is granted on the same rule as the reads above.
            "@kirocrew-core/accept_eval",
        }
        # The bare server is what this test exists to keep out: it would re-grant
        # all 74 registered core tools, including every verb named below.
        assert "@kirocrew-core" not in granted
        for denied in (
            # Persistent work — the three the prompt PROHIBITS by name.
            "task_run",
            "workflow_run",
            "workflow_author",
            "workflow_rerun_subtree",
            "workflow_cancel",
            # Agent fan-out with no human in the loop.
            "spawn_run",
            "spawn_sub_agents",
            "spawn_steer",
            "spawn_continue",
            # Mutating the caller's own session or project out from under it.
            "set_project",
            "reset_conversation",
            # Egress and machine reach.
            "deploy_artifact",
            "browser",
            "ops_mission_control_api",
            "file_send",
            # Artifact and memory writes.
            "artifact_save",
            "artifact_update",
            "artifact_delete",
            "learn_add",
            "knowledge_add_document",
        ):
            assert f"@kirocrew-core/{denied}" not in granted, denied
        # And the projected KAS policy must deny them too, not just the kiro-cli
        # allowlist: a ``kirocrew-core/*`` pattern here would re-grant every one
        # of them on the KAS backend while the list above still looked narrow.
        match = self._install(tmp_path, monkeypatch)["permissions"]["rules"][0]["match"]
        assert "kirocrew-core/*" not in match
        for denied in ("task_run", "spawn_run", "set_project", "browser", "artifact_save"):
            assert f"kirocrew-core/{denied}" not in match, denied

    def test_template_grants_never_leak_into_the_conductor_list(self, tmp_path, monkeypatch):
        """No-op proof: the conductor replaces ``allowedTools``
        wholesale with its own filtered ``granted`` list, so the ceiling filter
        moving into ``build_agent_config`` changes nothing here — and template
        grants (filtered or not) can never leak through.
        """
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]}
                },
                "tools": ["fs_read", "@kirocrew-core"],
                # A template list carrying floor builtins — as if the build-time
                # filter did not exist. None of it may survive the replacement.
                "allowedTools": ["fs_read", "code", "glob", "grep", "@kirocrew-core"],
            },
        )
        monkeypatch.setattr(
            agent, "_kirocrew_mcp_invocation", lambda sub: ("/resolved/kirocrew", [sub])
        )
        monkeypatch.setattr(agent, "_may_auto_approve", lambda ref: True)
        agent._install_conductor_agent()
        data = json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))
        for template_grant in ("fs_read", "code", "glob", "grep"):
            assert template_grant not in data["allowedTools"], template_grant

    def test_mounts_no_tool_the_charter_never_names(self, tmp_path, monkeypatch):
        """An unused grant is surface the charter cannot account for.

        `web_fetch` serves the skill's worked example (reading an issue list
        during triage). `web_search`, `grep` and `glob` appeared in neither the
        prompt nor the skill and were dropped — `fs_read` covers every read the
        charter describes. `tool_search` stays and is now NAMED in the prompt:
        with MCP Tool Search active the session-control specs are deferred, so
        without it the conductor cannot reach `session_create` at all.
        """
        data = self._install(tmp_path, monkeypatch)
        for absent in ("web_search", "grep", "glob"):
            assert absent not in data["tools"], absent
        assert "web_fetch" in data["tools"]
        assert "tool_search" in data["tools"]
        assert "tool_search" in data["prompt"]

    def test_mounts_no_tool_that_can_write_a_file(self, tmp_path, monkeypatch):
        """The no-write property must hold against the tool list, not the prose.

        `fs_write` is the obvious one, but `code` is the trap: governance maps it
        to `filesystem.write` because it "writes files AND can shell out"
        (`platform/governance.py` BUILTIN_TOOL_SCOPES), and it sits in
        `WITHHELD_FROM_AUTO_APPROVE` for the same reason. Mounting it would make
        "never does a work item's work itself" false while the docstring still
        claimed it, so both are pinned here together.
        """
        data = self._install(tmp_path, monkeypatch)
        for writer in ("fs_write", "code"):
            assert writer not in data["tools"], writer

    def test_mcp_surface_is_core_plus_dashboard_plus_work(self, tmp_path, monkeypatch):
        """Exactly three servers, and inherited ones the charter has no use for go.

        ``kirocrew-work`` is one of them now: this spec IS the ledger conductor,
        so the mount is the charter rather than an addition to it. Pinned
        positively AND as an exact set, so a fourth inherited server cannot ride
        in unnoticed.
        """
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {
            "kirocrew-core",
            "kirocrew-dashboard",
            "kirocrew-work",
        }
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]
        assert data["mcpServers"]["kirocrew-work"]["args"] == ["mcp-work"]
        assert "builder-mcp" not in data["mcpServers"]
        # Never auto-approved at the SERVER level: an autoApproved MCP tool is
        # approved inside kiro-cli and emits no permission request, so
        # ``hooks.on_tool_call`` — the deny floor, the sensitive-path check, the
        # governance ceiling — is never reached for it.
        assert "autoApprove" not in data["mcpServers"]["kirocrew-work"]

    def test_the_work_mount_lands_on_all_four_surfaces(self, tmp_path, monkeypatch):
        """A mount is four separate facts, and only three of them are visible.

        ``tools`` mounts the server, ``mcpServers`` gives it a command to launch,
        ``allowedTools`` decides which verbs skip the approval prompt, and
        ``permissions.rules[].match`` is what the KAS backend reads INSTEAD of
        ``allowedTools``. The last one is the one that silently survives an
        incomplete edit: a spec can look correctly mounted on kiro-cli while the
        KAS projection grants nothing, and every patrol cycle then stalls on an
        approval nobody is there to give.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "@kirocrew-work" in data["tools"]
        assert "kirocrew-work" in data["mcpServers"]
        assert "@kirocrew-work/work_ledger_read" in data["allowedTools"]
        assert "@kirocrew-work/work_ledger_record" in data["allowedTools"]
        assert "@kirocrew-work/work_brief" in data["allowedTools"]
        match = data["permissions"]["rules"][0]["match"]
        assert "kirocrew-work/work_ledger_read" in match
        assert "kirocrew-work/work_ledger_record" in match
        assert "kirocrew-work/work_brief" in match
        # ``work_report`` WRITES into a parent's record across a dispatch
        # relationship, so it is mounted and gated on every surface that grants.
        assert "@kirocrew-work/work_report" not in data["allowedTools"]
        assert "kirocrew-work/work_report" not in match
        # And never the whole server, which would hand over the worker half by
        # the back door on either backend.
        assert "@kirocrew-work" not in data["allowedTools"]
        assert "kirocrew-work/*" not in match

    def test_prompt_carries_the_work_ledger_procedure(self, tmp_path, monkeypatch):
        """The charter has to describe the flow the mount makes reachable.

        The four rules that make this procedure what it is: bind before seed, a
        ``done`` is a claim the evaluator settles, the batch is filtered to
        ``done`` before it is piped, and a claimed ``pr`` is promoted by an
        explicit ``action=accept`` rather than read as the bar.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        for token in (
            "work_ledger_read",
            "work_ledger_record",
            "work_brief",
            "work_report",
            "@kirocrew-work",
            "action=create",
            "action=bind",
            "action=verdict",
            "action=accept",
            "action=close",
            "Bind before you seed",
            "accept_batch",
            "CLAIM",
        ):
            assert token in prompt, token
        # In dispatch order, not merely all present.
        assert prompt.index("action=create") < prompt.index("session_create")
        assert prompt.index("session_create") < prompt.index("action=bind")
        assert prompt.index("action=bind") < prompt.index("session_send")
        # The codec this flow replaced must not be named: there is a store now,
        # and two records that can disagree is what the ledger removed.
        assert "ledger_entry" not in prompt

    def test_prompt_and_skill_filter_the_batch_to_done_items(self, tmp_path, monkeypatch):
        """``accept_batch`` is status-blind by design (it is the promotion seam), so
        the procedure must filter it. Unfiltered, a ``progress`` worker whose stub
        already satisfies a ``file`` condition earns a genuine ``pass`` and can be
        closed under itself. Both the prompt and the skill have to state the
        filter, because the agent copies whichever it read last.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "Filter the `accept_batch`" in prompt
        assert "whose status is `done`" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "keep only the entries whose item is currently `status: done`" in body
        assert "Never send the unfiltered document" in body

    def test_prompt_and_skill_make_a_nested_conductor_report_upward(self, tmp_path, monkeypatch):
        """A second-level conductor is bound as its parent's worker, and the parent
        reads its OWN ledger — so a nested conductor that never calls
        ``work_report`` leaves its parent's item statusless forever, which the
        parent reads as a stall. The worker half is mounted on this spec for
        exactly this caller; the text has to tell it to use it. The root case is
        named too, so a root conductor does not read ``not_bound`` as a failure.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "If a conductor dispatched you" in prompt
        assert "`work_brief` before you plan" in prompt
        assert "`work_report` `status: progress`" in prompt
        assert "not_bound" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "When a conductor dispatched you" in body
        assert "`work_brief` before Round 0" in body
        assert "`work_report` at round boundaries" in body
        assert "root conductor gets `not_bound`" in body

    def test_prompt_and_skill_require_the_ledger_watch_and_the_interval_band(
        self, tmp_path, monkeypatch
    ):
        """A loop without ``watch="work-ledger"`` is a plain timer that pays a turn
        every interval, and a 30-minute interval leaves a worker's report unread
        for half an hour. Both the prompt and the skill must make the watch
        mandatory and keep the interval inside the band ``patrol_budget.py``
        enforces.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert 'Always pass `watch="work-ledger"`' in prompt
        assert '`watch="work-ledger"` is mandatory' in body
        for text in (prompt, body):
            assert 'monitor_update(watch="work-ledger")' in text
            assert "300..900" in text
            assert "interval_secs=1800" not in text
            assert "on a timer today" not in text

    def test_prompt_and_skill_run_rounds_back_to_back_and_stop_on_two_signals(
        self, tmp_path, monkeypatch
    ):
        """A finished round must not wait on the user: the Round-0 go-ahead covers
        every round. Patrol ends only when every item is terminal or the user
        stops it, and a question parks one item instead of stopping the loop.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Rounds run back to back" in prompt
        assert "Do not wait for the user between rounds" in body
        assert "propose the next round. Then wait." not in body
        assert "Patrol ends on two signals only" in prompt
        assert "Patrol ends on exactly two signals" in body
        assert "Needs-human checklist" in body
        for text in (prompt, body):
            assert "Round-0 go-ahead" in text
            assert "runaway backstop, not a stop signal" in text
            assert "credentials, spend, deleting or overwriting someone's work" in text
            assert "Park just this item" in text

    def test_needs_human_checklist_puts_the_risk_check_before_the_default(
        self, tmp_path, monkeypatch
    ):
        """A model stops at the first step that applies. If "pick a default"
        came first, a spend, delete or force-overwrite choice with an obvious
        default would be taken unattended, so the risk check must lead.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        for text in (prompt, body):
            assert text.index("Is it credentials, spend") < text.index("Not risky? Pick a default")
            assert "Can you pick a default?" not in text
            assert "Can I pick a default?" not in text

    def test_back_to_back_rounds_keep_a_spend_bound(self, tmp_path, monkeypatch):
        """Without a pause between rounds, a default item cap and a no-progress
        back-off are the only spend gates when the user set no budget.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        for text in (prompt, body):
            assert "spend is bounded" in text
            assert "two rounds in a row" in text
            assert "re-plan may add items only" in text
        assert "at most 20 ledger items" in prompt
        assert "at most **20 ledger items**" in body

    def test_prompt_names_its_own_skill_and_not_the_deprecated_alias(self, tmp_path, monkeypatch):
        """The procedure lives in ``goal-conductor``. ``goal-ledger-conductor`` is a
        deprecation pointer kept for one release, so naming it here would send the
        agent to a paragraph instead of to the procedure.
        """
        prompt = self._install(tmp_path, monkeypatch)["prompt"]
        assert "`goal-conductor`" in prompt
        assert "goal-ledger-conductor" not in prompt

    def test_skill_body_matches_the_prompt_on_the_load_bearing_rules(self):
        """The prompt is the summary and the skill is the procedure; a disagreement
        between them is resolved nondeterministically by whichever the model weighs
        more. These four are the rules that make this flow what it is.
        """
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Bind BEFORE you seed" in body
        assert "Never leave `agent` unset" in body
        assert "work_ledger_read` with `compact=true` first, every cycle" in body
        assert "action=accept" in body

    def test_prompt_and_skill_close_a_child_once_its_item_is_terminal(self, tmp_path, monkeypatch):
        """A conductor that stops an item's loop and leaves its session open leaves a
        finished worker parked in the sidebar with nothing to re-arm. The prompt
        names the verb in the child-session tool line, and the skill ties it to
        ``action=close`` so ending the item and ending its session are one step.
        """
        prompt = " ".join(self._install(tmp_path, monkeypatch)["prompt"].split())
        assert "`session_close` (close a child once its item is terminal)" in prompt
        body = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Closing the item and closing its session happen together" in body
        assert "`session_close` archives (reopenable); it never deletes" in body
        assert "`session_close` each one whose item is terminal" in body
        assert (
            "leave open any child still holding a pending human question or driving an"
            " unmerged PR" in body
        )

    def test_skill_invokes_the_evaluator_as_the_accept_eval_tool_not_the_shell(self):
        """The acceptance document is built from ingested text, and the skill's
        example is what the agent copies. The evaluator is the ``accept_eval``
        MCP tool: the whole ``items`` batch is a structured argument, so a
        ``file`` path carrying a single quote is just a string value — there is
        no shell to interpret it and no heredoc to get right. The skill must
        therefore name the tool, and must NOT carry a shell form (a quoted
        heredoc, or a ``printf``/``python3 accept_eval.py`` invocation), or the
        agent would copy a shape the conductor has no grant for.
        """
        body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "accept_eval(items=" in body
        # The retired shell forms are gone: no heredoc, no printf, no script run.
        assert "<<'ACCEPT_BATCH'" not in body
        assert "printf '%s'" not in body
        assert "accept_eval.py" not in body

    def test_dashboard_entry_omits_managed_metadata_on_a_default_install(
        self, tmp_path, monkeypatch
    ):
        """Neither helper contributes by default, so the emitted entry stays minimal.

        Pinned explicitly rather than read off the ambient environment: the
        repo-root conftest pins ``KIROCREW_HOME`` for every test, so the real
        ``_managed_mcp_env()`` legitimately returns a pin in-suite.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: False)
        monkeypatch.setattr(agent, "_managed_mcp_env", dict)
        dash = self._install(tmp_path, monkeypatch)["mcpServers"]["kirocrew-dashboard"]
        assert dash == {"command": "/resolved/kirocrew", "args": ["mcp-dashboard"]}

    def test_dashboard_entry_carries_managed_server_metadata(self, tmp_path, monkeypatch):
        """The hand-built dashboard entry is enriched like every managed server.

        Without ``"type": "registry"`` a registry-mode client silently drops the
        entry, so the conductor's session-control tools never launch. Without the
        ``KIROCREW_HOME`` pin the shim reads the default data home while the
        gateway runs under an override, so session control acts on a different
        session store than it reports on.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/tmp/override"})
        data = self._install(tmp_path, monkeypatch)
        dash = data["mcpServers"]["kirocrew-dashboard"]
        assert dash["type"] == "registry"
        assert dash["env"] == {"KIROCREW_HOME": "/tmp/override"}

    def test_grants_pass_through_the_governance_ceiling(self, tmp_path, monkeypatch):
        """``allowedTools`` never reaches the PreToolUse gate, so it is filtered.

        A ceiling with an opinion about a granted ref must not be silently
        bypassed by a static grant list: the server stays MOUNTED (still in
        ``tools``) but the governed VERB loses its auto-approve, so its calls
        prompt and the gate applies the real per-tool rule.

        Governs one core verb rather than the whole server, which is what the
        ceiling now has to be able to reach: after the per-verb narrowing there is
        no bare ``@kirocrew-core`` entry left for it to have an opinion about.
        """
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert "@kirocrew-core/monitor_start" not in data["allowedTools"]
        # Every sibling grant survives — the ceiling removed one verb, not the set.
        assert "@kirocrew-core/monitor_update" in data["allowedTools"]
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/select_crew",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
            "@kirocrew-core/accept_eval",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/chat_folder_file_self",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
            "@kirocrew-dashboard/session_status",
            "@kirocrew-work/work_ledger_read",
            "@kirocrew-work/work_ledger_record",
            "@kirocrew-work/work_ledger_rebuild",
            "@kirocrew-work/work_brief",
        ]

    def test_kas_permissions_are_derived_from_the_filtered_grants(self, tmp_path, monkeypatch):
        """The KAS block is derived, not restated — so the ceiling reaches it too.

        Ungoverned: the per-tool grants project to EXACT ``server/tool`` resources
        rather than ``kirocrew-core/*`` / ``kirocrew-dashboard/*`` wildcards, which
        is what makes the narrowing real on the KAS backend as well as on kiro-cli
        — a wildcard here would re-grant the ``task_run`` / ``spawn_run`` and the
        ``session_stop`` / ``session_send`` that ``allowedTools`` deliberately
        withholds. Governed against one core verb: that pattern is gone while
        every sibling survives, which is the property a rule list restated as a
        literal would have lost.
        """
        core_resources = [
            "kirocrew-core/accept_eval",
            "kirocrew-core/ask_question",
            "kirocrew-core/autonudge_stop",
            "kirocrew-core/list_sessions",
            "kirocrew-core/monitor_start",
            "kirocrew-core/monitor_update",
            "kirocrew-core/resource_status",
            "kirocrew-core/select_crew",
            "kirocrew-core/send_message",
            "kirocrew-core/send_notification",
            "kirocrew-core/session_ledger_read",
            "kirocrew-core/session_ledger_record",
            "kirocrew-core/skill_fetch",
            "kirocrew-core/skill_search",
            "kirocrew-core/wait",
        ]
        dashboard_resources = [
            "kirocrew-dashboard/chat_folder_create",
            "kirocrew-dashboard/chat_folder_file_self",
            "kirocrew-dashboard/chat_folder_tree",
            "kirocrew-dashboard/session_create",
            "kirocrew-dashboard/session_read_message",
            "kirocrew-dashboard/session_status",
        ]
        work_resources = [
            "kirocrew-work/work_brief",
            "kirocrew-work/work_ledger_read",
            "kirocrew-work/work_ledger_rebuild",
            "kirocrew-work/work_ledger_record",
        ]
        data = self._install(tmp_path, monkeypatch)
        assert data["permissions"] == {
            "rules": [
                {
                    "capability": "mcp",
                    "match": [*core_resources, *dashboard_resources, *work_resources],
                    "effect": "allow",
                }
            ]
        }
        assert "kirocrew-core/*" not in data["permissions"]["rules"][0]["match"]
        assert "kirocrew-dashboard/*" not in data["permissions"]["rules"][0]["match"]
        # The work server is narrowed the same way, and for a sharper reason: a
        # ``kirocrew-work/*`` wildcard would re-grant ``work_report`` — the one
        # verb that writes across a dispatch relationship — on the backend where
        # nobody reads ``allowedTools``.
        assert "kirocrew-work/*" not in data["permissions"]["rules"][0]["match"]
        assert "kirocrew-work/work_report" not in data["permissions"]["rules"][0]["match"]

        governed = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core/monitor_start",
        )
        assert governed["permissions"] == {
            "rules": [
                {
                    "capability": "mcp",
                    "match": [
                        *(r for r in core_resources if r != "kirocrew-core/monitor_start"),
                        *dashboard_resources,
                        *work_resources,
                    ],
                    "effect": "allow",
                }
            ]
        }

    def test_a_fully_governed_host_still_emits_the_permissions_key(self, tmp_path, monkeypatch):
        """``{"rules": []}`` when nothing qualifies — the key's PRESENCE loads the spec.

        Split from the test above once the dashboard grant meant a ceiling on one
        ref alone does not empty the rule list: without this, the empty-policy
        branch would have silently lost its coverage.
        """
        data = self._install(tmp_path, monkeypatch, may_auto_approve=lambda ref: False)
        assert data["permissions"] == {"rules": []}
        assert data["allowedTools"] == []

    def test_the_permissions_field_is_gated_on_the_installed_kiro_cli(self, tmp_path, monkeypatch):
        """Written on an accepting release, withheld on a refusing or unknown one.

        The generated conductor spec gates its ``permissions`` write on the
        installed kiro-cli, sharing the default spec's gate: a kiro-cli whose
        schema predates the field would otherwise refuse the WHOLE spec and fall
        back to broader default grants. ``allowedTools`` is untouched either way
        -- it is the KAS-only projection that is withheld, not the grant list
        kiro-cli reads.
        """
        accepting = self._install(tmp_path, monkeypatch, cli_version="accepts")
        assert accepting.get("permissions"), "an accepting CLI must get the block"
        assert accepting["permissions"] != _INHERITED_PERMISSIONS
        assert accepting["permissions"] == derived_agent_permissions(
            accepting["allowedTools"], CONDUCTOR_AGENT_FILENAME
        )
        assert accepting["allowedTools"], "the grant list is never withheld"

        for refusing in ("refuses", "unknown"):
            data = self._install(tmp_path, monkeypatch, cli_version=refusing)
            assert "permissions" not in data, f"{refusing} CLI must get no block"
            assert data["allowedTools"], "the grant list is never withheld"

    def test_withholding_a_grant_is_audit_logged(self, tmp_path, monkeypatch):
        """A withheld grant is a permission DECISION and must leave a record.

        Every other writer of an ``allowedTools`` list emits this event;
        ``strip_ungoverned_auto_approve`` names a silent pop as the one withhold
        path with no audit trail. Filtering silently here would make this
        installer exactly that path.

        The withheld ref must be named in the record, which after the per-verb
        narrowing means the VERB and not just the server — an operator reading
        "@kirocrew-core was withheld" could not tell which of 14 grants he lost.
        """
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core/select_crew"
        )
        withheld = [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"]
        assert len(withheld) == 1
        assert "@kirocrew-core/select_crew" in withheld[0]["resources"]
        assert withheld[0]["source"] == "_install_conductor_agent"

    def test_no_audit_event_when_nothing_is_withheld(self, tmp_path, monkeypatch):
        """An ungoverned host withholds nothing, so it records no decision."""
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(tmp_path, monkeypatch)
        assert [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"] == []

    def test_audit_failure_does_not_break_the_install(self, tmp_path, monkeypatch):
        """The spec still lands when the audit sink is unavailable."""

        class _Broken:
            def log_api_access(self, **kw):
                raise RuntimeError("sel down")

        monkeypatch.setattr(agent, "sel", lambda: _Broken())
        data = self._install(
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core/select_crew"
        )
        assert "@kirocrew-core/select_crew" not in data["allowedTools"]
        assert data["allowedTools"] == [
            "session",
            "report",
            "tool_search",
            "@kirocrew-core/monitor_start",
            "@kirocrew-core/monitor_update",
            "@kirocrew-core/autonudge_stop",
            "@kirocrew-core/wait",
            "@kirocrew-core/resource_status",
            "@kirocrew-core/list_sessions",
            "@kirocrew-core/session_ledger_read",
            "@kirocrew-core/session_ledger_record",
            "@kirocrew-core/skill_search",
            "@kirocrew-core/skill_fetch",
            "@kirocrew-core/send_message",
            "@kirocrew-core/send_notification",
            "@kirocrew-core/ask_question",
            "@kirocrew-core/accept_eval",
            "@kirocrew-dashboard/chat_folder_tree",
            "@kirocrew-dashboard/chat_folder_create",
            "@kirocrew-dashboard/chat_folder_file_self",
            "@kirocrew-dashboard/session_create",
            "@kirocrew-dashboard/session_read_message",
            "@kirocrew-dashboard/session_status",
            "@kirocrew-work/work_ledger_read",
            "@kirocrew-work/work_ledger_record",
            "@kirocrew-work/work_ledger_rebuild",
            "@kirocrew-work/work_brief",
        ]

    def test_skill_gates_the_plan_once_instead_of_interrogating(self):
        """The opening round must be ONE plan message, not a round of questions.

        The first live run opened with a clarification round: the previous
        wording ("restate the plan, wait for the user") left room for one ahead
        of the plan, and every question spent there is latency on work the
        conductor could have assumed and let the user correct. Pinned as a doc
        ratchet because the instruction, not the code, is what would drift.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        # What the conductor can settle itself is an assumption, not a question.
        assert "Decide, do not ask" in text
        assert "Assumptions" in text
        # And a goal that already authorizes execution must not be re-gated.
        assert "Skip the gate when the user already gave one" in text

    def test_skill_states_the_real_approval_cost(self):
        """The cost note must match the spec, or patrol plans for wrong prompts.

        The granted verbs run silently while `session_send` / `session_stop`
        prompt, so a skill that claimed either "everything prompts" or "nothing
        prompts" would have the conductor sizing its nudge interval around
        approvals it does not pay — or walking into ones it does. The evaluator
        is the auto-approved `accept_eval` tool (so verification does not
        prompt), and `patrol_budget.py` is the one `execute_bash` invocation
        that does.
        """
        text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
        assert "Reads and creates do not prompt" in text
        assert "`session_send` and `session_stop` are deliberately NOT auto-approved" in text
        assert "`accept_eval` is auto-approved" in text
        assert "patrol_budget.py" in text

    def test_skill_keeps_item_state_in_the_store_and_not_in_artifacts(self):
        """One record per item, in the one place the evaluator batch reads.

        Encoding items into ``session_ledger`` ``artifacts`` as well would give
        two records that can disagree, and the ledger is the one
        ``work_ledger_read`` returns and the Crew page will render. The codec
        that squeezed an item into a 2000-character ``artifacts`` value under an
        entry cap belongs to a conductor with no item store, so the skill must
        not send a reader to it. Pinned as a doc ratchet because the
        instruction, not the code, is what would drift.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "Do not encode items into `session_ledger` artifacts" in text
        assert "ledger_entry" not in text
        # ``session_ledger`` still holds the conductor's OWN goal/phase/next, so
        # the skill has to keep naming it for that.
        assert "session_ledger_record" in text

    def test_spec_is_registered_as_kirocrew_owned(self):
        """Every managed spec registers in ``OWNED_KIRO_AGENT_FILES``.

        Three consumers key off that tuple (the Playwright convergence sweep,
        ``doctor``'s dead-path repair, connection minting). Absent from it, a
        conductor spec whose resolved MCP command path dies is classified as a
        foreign file and reported as unfixable instead of being repaired.
        """
        assert CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_builtin_skill_does_not_collide_with_the_delegation_skill(self):
        """The packaged skill must NOT be named ``conductor``.

        ``<skills>/conductor/SKILL.md`` was the delegation skill the retired
        ``agent.conductor_skill`` flag generated, and ``kirocrew setup`` still
        removes a file there whose bytes it wrote on old installs. A packaged
        skill sharing the name would be erased on an upgraded install.
        """
        assert SKILL_DIR.is_dir()
        assert not (_BUILTIN_SKILLS_DIR / "conductor").exists()
        assert "name: goal-conductor" in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_files_the_session_at_creation_not_by_a_move(self):
        """Dispatch passes ``folder`` to ``session_create``; no move step exists.

        ``session_create`` files the slot atomically at creation, which
        is what closed the create-then-move window a folder delete could land
        in. The instruction layer must not resurrect the workaround: a separate
        ``chat_folder_create`` precondition or ``chat_folder_move_session`` step
        reopens exactly the non-atomic window the tool argument removed. Pinned
        as a doc ratchet because the instruction, not the code, is what would
        drift back.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "2. `session_create`" in text, "dispatch must reach the atomic create"
        assert "`folder`" in text, "dispatch must name the folder argument at create"
        assert "chat_folder_move_session" not in text, "the move workaround must stay deleted"
        # Scoped to the dispatch STEP, not the whole document: a future
        # legitimate mention of the tool elsewhere in the skill must not fail a
        # pin whose intent is only that the precreation step stay deleted.
        assert "1. `chat_folder_create`" not in text, "no folder-precreation dispatch step"

    def test_skill_files_the_conductor_itself_under_the_goal(self):
        """The conductor sits INSIDE the goal's folder, beside its workers.

        The live shape this pins away from: workers filed under the goal while
        the conductor's own session floats at the top level, so the person has
        nothing that groups a goal's sessions with the session driving them.
        The opening plan turn files the conductor with ``chat_folder_file_self``
        — the verb that writes only the caller's own placement and so never
        prompts — and the skill must name it there, not leave it to the model
        to discover.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        opening = text.split("### Round 0")[1].split("### Dispatch a round")[0]
        assert "`chat_folder_file_self`" in opening, "the plan turn must file the conductor"
        # The auto-approved list must say it never prompts, or a patrol cycle
        # with nobody at the keyboard would be told to expect one.
        assert "`chat_folder_file_self` (it writes only your own placement)" in text

    def test_skill_files_each_worker_under_a_per_agent_subfolder(self):
        """Dispatch files a worker at ``<goal folder>/<agent>``, not the goal root.

        One heading per goal, the conductor directly under it, and one subfolder
        per agent kind holding that agent's sessions — so the tree reads as
        goal / who / what, and a nested conductor's own subtree nests under the
        ``kirocrew-conductor`` subfolder instead of flattening into its parent's.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        dispatch = text.split("### Dispatch a round")[1].split("### Patrol")[0]
        assert "`<goal folder>/<agent>`" in dispatch, "dispatch must name the per-agent path"
        assert "kirocrew-worker`" in dispatch, "the example must show a real agent segment"
        # Still the atomic create: the subfolder rides the create's own
        # ``folder`` argument, never a second move step.
        assert "2. `session_create`" in dispatch

    def test_pr_checks_seed_may_name_the_prepare_pr_skill_by_path(self):
        """A ``pr_checks`` seed can point the worker at kirocrew-prepare-pr's SKILL.md.

        ``kirocrew-worker`` is a custom agent: ``_skills_injection_plan`` gives it
        no catalog and no trigger matching, so however a seed is worded nothing
        auto-loads ``kirocrew-prepare-pr`` in the worker session. The conductor naming the
        file is the only route. Pinned as an OPTIONAL hint, not a mandate: a user
        who does not want kirocrew-prepare-pr must not have it forced on every worker.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        dispatch = text.split("### Dispatch a round")[1].split("### Patrol")[0]
        flat = " ".join(dispatch.split())
        assert "`<crew-home>/skills/kirocrew-dev/kirocrew-prepare-pr/SKILL.md`" in flat
        assert "may name the PR procedure" in flat
        # Optional, by design.
        assert "Optional" in flat
        assert "must read" not in flat and "MUST read" not in flat
