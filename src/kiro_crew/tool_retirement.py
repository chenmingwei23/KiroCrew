"""Retired tool names, and the ONE direction it is safe to carry them forward.

A published tool name is a KEY that other people's persisted records were written
against. Every one of those records is a decision, and a decision silently changes
meaning when its key changes. Renaming a tool therefore does not merely age some
prose -- it re-points, or orphans, every stored rule that named the old spelling.

This module exists for exactly one of those record classes: a persisted
RESTRICTION. It must never be used for the other.

THE ASYMMETRY, WHICH IS THE WHOLE SAFETY ARGUMENT
=================================================

A RESTRICTION migrates forward. Reading an old-name exclusion as also excluding
the tool's current name is fail-CLOSED: the worst case is that something stays
denied that an operator would have allowed, and they can SEE that and undo it.

A GRANT DOES NOT MIGRATE. Widening a permission is fail-OPEN, and on this codebase
it is worse than ordinary fail-open. ``acp/kas_agents.py`` records that an
auto-approved call never reaches Crew's permission callback, so widening a grant
removes the deny-list and the audit trail along with it: the bypass then leaves no
record that it happened. There is deliberately no grant-side helper here, and
adding one would be the bug.

So this module is named for the direction rather than for the mapping. A table
called "aliases" is structurally fine and reads as safe to consult anywhere, which
is how one gets used in the direction that is not -- the map does not object, and
the call site looks like every other lookup. A reader who meets
:func:`widen_restriction_for_retired_names` has to say the word "restriction" out
loud to use it, and there is nothing here to reach for on the grant side.

WHERE THIS IS CONSULTED
=======================

Every point at which a restriction is RESOLVED or PROJECTED, so a reader can check
the list against the code rather than trust it:

* :func:`kiro_crew.mcp_shared._resolve_excluded_tools` -- per-call resolution of
  ``managedToolPolicy.exclude``, cached per session. Widened.
* ``acp/kas_agents.to_client_custom_agent`` -- the ``excludedTools`` block relayed
  to an agent host at startup, before the session exists. Widened.
* ``acp/session_mcp.session_mcp_disabled_tools`` -- the ``(server, tool)`` pairs a
  dashboard tool-off persists, unioned from the agent spec and the global settings
  file. NOT widened here; that is a different shape with two sources and several
  consumers, so it is its own change.

Deliberately NOT consulted, so that omission reads as a decision:

* ``agent._WORKER_MIRRORED_SHAPES`` COPIES a persisted ``excludedTools`` key rather
  than resolving it. Widening a copy would rewrite the user's stored value.
* ``GET /api/session-tool-policy`` serves the RAW rule, which is what lets an
  operator see the stale spelling and re-key it. Expanding it would hide the thing
  they need to see.
* An app-composed ``managedToolPolicy`` is regenerated from shipped code on upgrade,
  so it names the current tool; its resolution is already covered above.

There is no grant-side entry in either list, and that is the point.

WHAT THIS DOES NOT COVER
========================

A restriction resolved somewhere Kiro Crew does not compose is out of reach. Per
``acp/kas_agents.py``, "a hand-written block is not ignored, just not Crew's to
relay: it lives in the profile on disk, which the backend reads itself when Crew is
not injecting an agent over the wire." A deny written there naming a retired tool
keeps not matching, and this module cannot see it, cannot expand it, and cannot
warn about it. That is a real gap, not a covered case: an operator holding such a
rule must re-key it by hand.

The startup fail-open is also out of reach, for a different reason. When the policy
cannot be resolved at all -- no session key, an agent-not-resolved race, an
unreachable gateway -- ``_resolve_excluded_tools`` returns an empty set BEFORE the
exclude list is parsed, so there is nothing for this module to widen. That window
withholds every exclusion equally and is not specific to a retired name; a rename
neither creates it nor worsens it.

RETIREMENT
==========

An entry goes when no persisted policy names the old tool. That is checkable rather
than a date: for each agent config on the host, read its ``managedToolPolicy``
``exclude`` list (the dashboard serves the same thing per agent at
``GET /api/session-tool-policy``, which deliberately reports the RAW persisted
rule so an operator can see the stale spelling), and confirm no entry is an old
name in :data:`RETIRED_TOOL_NAMES`. Any hand-written on-disk profile has to be read
directly, for the reason above. When both hold for an entry, delete it.
"""

from __future__ import annotations

from collections.abc import Iterable

#: OLD tool name -> the name that replaced it.
#:
#: Consulted ONLY when resolving a restriction. Never in dispatch, never in grant
#: or allowlist composition, and never advertised: nothing here is callable, and an
#: entry does not make the old name work again. A call naming a retired tool still
#: fails with ``Unknown tool``, which is the loud failure the rename intends.
RETIRED_TOOL_NAMES: dict[str, str] = {"monitor_start": "monitor_patrol"}


def widen_restriction_for_retired_names(names: Iterable[str]) -> set[str]:
    """Return *names* plus the current name of any retired tool among them.

    For a persisted DENY only -- an exclusion list, an ``excludedTools`` block, any
    set whose members are things a caller may NOT do. The returned set is always a
    superset of the input, which is what makes it safe: this function can only ever
    cause more to be refused.

    Do NOT reach for this when composing a grant, an allowlist, an ``autoApprove``
    entry or a permissions rule. Widening a permission through a retired name is a
    governance bypass, and one that also skips the audit trail (see the module
    docstring).

    Both spellings are kept, not just the current one. An operator's rule naming
    the old tool must keep being enforced under that name too, in case anything
    still reads the raw persisted value.
    """
    widened = {name for name in names if isinstance(name, str) and name}
    for retired, current in RETIRED_TOOL_NAMES.items():
        if retired in widened:
            widened.add(current)
    return widened
