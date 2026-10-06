"""The acceptance evaluator MCP tool: one tool, kind-discriminated batches.

``schemas()`` returns the ADVERTISEMENT half; ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and behavior are
read together (same template as ``logs.py``, ``skills.py``).

THE SECURITY INVARIANT (do not weaken):

    No model-authored argv ever reaches subprocess.

This is the real control, and it is the ONLY one over what the tool runs: the
``hooks.on_tool_call`` gate matches its deny list against the tool CALL
(``accept_eval(items=...)``) and never against a subprocess the handler spawns
inside the server, so no deny floor backs ``gh``. Instead, every argv this
handler runs is built HERE from a fixed template out of narrowly-typed spec
fields — a spec names a PR number or a path, never a command, an argv array or a
shell string. ``_SELF_BUILT_COMMANDS`` is the internal assertion that this
holds: a handler that leaked spec input into an exec path fails closed.

The ``file`` kind has a second, real control: it validates its path through
``hooks.safe_reads.validate_file_path`` (the Windows UNC trusted-root gate, the
link-target screen, and ``is_sensitive_path``) before any existence check, so a
spec cannot make the tool touch ``\\\\host\\share`` or a credential path.

HOW TO WIDEN IT (the supported path):

    Add a new ``kind`` whose handler builds its own argv. Never add a kind that
    takes a command, an argv array, or a shell string from the spec. A new
    binary is auto-approved and reaches no deny floor, so the fixed-template
    invariant — not a floor — is what makes adding one a reviewable decision.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.hooks import validate_file_path
from kiro_crew.validation import ACCEPT_EVAL_MAX_ITEMS

# ── Constants ──

#: Binaries this tool itself invokes, from argv it builds. NOT a spec-facing
#: allowlist — nothing in a spec can name a command at all. This exists so a
#: handler that ever passes spec input into ``_exec`` fails closed.
_SELF_BUILT_COMMANDS = {"gh"}

TIMEOUT_SECS = 300
EVIDENCE_TAIL_CHARS = 500

#: gh pr checks exits 8 when checks are still running (gh >= 2.30).
_GH_PENDING_EXIT = 8


# ── Helpers ──


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-EVIDENCE_TAIL_CHARS:] if len(text) > EVIDENCE_TAIL_CHARS else text


def _exec(argv: list, cwd: str | None = None) -> tuple:
    """Run one TOOL-BUILT argv without a shell; return ``(problem, proc)``.

    Exactly one half is not ``None``. ``problem`` is a ready
    ``(verdict, evidence)`` pair for the cases where no process ran at all.
    """
    raw = str(argv[0])
    if raw not in _SELF_BUILT_COMMANDS:
        return (
            (
                "refused",
                f"evaluator bug: {raw!r} is not a command this tool builds; "
                "no spec field may name a command",
            ),
            None,
        )
    try:
        proc = subprocess.run(  # noqa: S603 — argv array, no shell, tool-built
            [str(a) for a in argv],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return (("error", f"timed out after {TIMEOUT_SECS}s"), None)
    except FileNotFoundError:
        return (("error", f"{raw!r} not found on PATH"), None)
    except OSError as exc:
        return (("error", f"could not run: {exc}"), None)
    return (None, proc)


def _run(argv: list, cwd: str | None = None) -> tuple[str, str]:
    """Run one TOOL-BUILT check and map its exit into a verdict."""
    raw = str(argv[0])
    problem, proc = _exec(argv, cwd)
    if proc is None:
        return problem or ("error", "could not run")
    output = _tail(proc.stdout + "\n" + proc.stderr)
    if proc.returncode == 0:
        return ("pass", output or "exit 0")
    if raw == "gh" and proc.returncode == _GH_PENDING_EXIT:
        return ("pending", output or "checks still running")
    return ("fail", f"exit {proc.returncode}: {output}")


def _pr_is_draft(pr: int, repo: str | None = None) -> bool:
    """Is this pull request still a draft? ``True`` only on an unambiguous yes."""
    argv = ["gh", "pr", "view", str(pr)]
    if repo:
        argv += ["--repo", str(repo)]
    argv += ["--json", "isDraft", "-q", ".isDraft"]
    problem, proc = _exec(argv)
    if problem is not None or proc is None or proc.returncode != 0:
        return False
    return proc.stdout.strip().lower() == "true"


# ── Per-kind evaluators ──


def _evaluate(item: dict) -> tuple[str, str]:
    accept = item.get("accept") or {}
    kind = accept.get("kind")
    if kind == "pr_checks":
        pr = accept.get("pr")
        if not isinstance(pr, int) or isinstance(pr, bool):
            return ("error", "pr_checks spec needs an integer pr")
        repo = accept.get("repo")
        argv = ["gh", "pr", "checks", str(pr)]
        if repo:
            argv += ["--repo", str(repo)]
        verdict, evidence = _run(argv)
        if verdict == "pending" and _pr_is_draft(pr, repo):
            return (
                "refused",
                f"PR #{pr} is a draft and its checks have not finished; a draft "
                "is not a review-ready PR, and where readiness is gated on the "
                "draft flag no further polling resolves it — mark it ready for "
                "review or change the acceptance kind",
            )
        return (verdict, evidence)
    if kind == "file":
        path = accept.get("path")
        if not isinstance(path, str) or not path:
            return ("error", "file spec needs a path")
        exists = accept.get("exists", True)
        if not isinstance(exists, bool):
            return ("error", "file spec needs a boolean exists")
        # Validate the path through the shared file-access gate BEFORE any
        # existence check. ``validate_file_path`` enforces the Windows UNC
        # trusted-root gate (a bare ``Path(path).exists()`` on ``\\host\share``
        # is itself an outbound SMB authentication), the link-target screen, and
        # ``is_sensitive_path``, then canonicalizes — returning ``None`` on any
        # rejection. The ``on_tool_call`` gate cannot see a path nested inside
        # ``items[].accept.path``, so the handler applies this gate itself.
        canonical = validate_file_path(path)
        if canonical is None:
            return (
                "refused",
                "path is sensitive, outside a trusted root, or not a checkable "
                "file path, so the evaluator will not probe it",
            )
        want = exists
        have = Path(canonical).exists()
        verdict = "pass" if have == want else "fail"
        return (verdict, f"{path} {'exists' if have else 'does not exist'}")
    if kind == "human_approval":
        return ("pending", "awaiting human approval — not machine-checkable")
    if kind == "cmd":
        return (
            "refused",
            "the 'cmd' kind was removed: a spec may not name a command to run. "
            "Use 'pr_checks' for CI-backed acceptance (it covers 'the tests "
            "pass', since CI runs them), or ask for a new purpose-built kind",
        )
    return ("error", f"unknown accept kind {kind!r}")


# ── Tool handler ──

#: Cap on the number of items per batch, shared with the schema in
#: ``validation.ACCEPT_EVAL_SCHEMA`` so the two cannot drift. Large enough for
#: any real patrol cycle (20 items is the default ledger cap) while bounding
#: the per-call cost.
_MAX_ITEMS = ACCEPT_EVAL_MAX_ITEMS


def accept_eval(name: str, args: dict[str, Any]) -> str:
    """Evaluate acceptance conditions for a batch of work items.

    Input:  ``{"items": [{"id": "...", "accept": {"kind": "...", ...}}, ...]}``
    Output: ``{"results": [{"id": "...", "verdict": "...", "evidence": "..."}, ...]}``
    """
    # Resolve the caller for the SEL audit only — the resolve half, not the
    # refusal arm. The evaluation reads and writes no session-scoped state (it
    # answers a world-state question: are these checks green, does this file
    # exist), so an unresolved identity does not need to block it; it only
    # leaves the audit row attributed to "mcp_core" as every other such tool's
    # does. The denied-command and sensitive-path floors still apply at the
    # gate and in ``_evaluate`` regardless of who the caller is.
    session_key, _ = mcp_core.require_strict_session_key("accept_eval")
    session_key = session_key or "mcp_core"

    items = args.get("items")
    if not isinstance(items, list):
        mcp_core.sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="accept_eval",
            tool_kind="evaluate",
            outcome="error",
            metadata={"error": "items must be a JSON array"},
        )
        return json.dumps({"error": 'items must be a JSON array: {"items": [...]}'})

    if len(items) > _MAX_ITEMS:
        mcp_core.sel().log_tool_invocation(
            session_key=session_key,
            source="mcp",
            tool_name="accept_eval",
            tool_kind="evaluate",
            outcome="error",
            metadata={"error": "too many items", "count": len(items)},
        )
        return json.dumps({"error": f"too many items: {len(items)} exceeds the {_MAX_ITEMS} cap"})

    results = []
    for position, item in enumerate(items):
        item_id = f"#{position}"
        try:
            if not isinstance(item, dict):
                raise TypeError(f"item must be a JSON object, got {type(item).__name__}")
            item_id = str(item.get("id", item_id))
            verdict, evidence = _evaluate(item)
        except Exception as exc:
            verdict, evidence = "error", f"evaluator bug on this item: {exc}"
        results.append({"id": item_id, "verdict": verdict, "evidence": evidence})

    mcp_core.sel().log_tool_invocation(
        session_key=session_key,
        source="mcp",
        tool_name="accept_eval",
        tool_kind="evaluate",
        outcome="success",
        metadata={
            "item_count": len(items),
            "verdicts": sorted({r["verdict"] for r in results}),
        },
    )
    return json.dumps({"results": results}, indent=2)


# ── Registration ──


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the acceptance evaluator tool."""
    return [
        {
            "name": "accept_eval",
            "description": (
                "Evaluate acceptance conditions for a batch of work-ledger "
                "items. Each item carries a ``kind``-discriminated spec: "
                "``pr_checks`` (are a PR's checks green?), ``file`` (does a "
                "path exist?), ``human_approval`` (always pending). Returns "
                "one ``{id, verdict, evidence}`` per item; verdicts are "
                "``pass``, ``fail``, ``pending``, ``refused`` or ``error``. "
                "A per-item problem is a verdict, never a crash — one bad "
                "spec must not hide the others' results. Evaluate every "
                "``done`` item in ONE call so the cost is one invocation per "
                "patrol cycle, not one per item."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "description": (
                            "Work items to evaluate. Each is an object with "
                            "``id`` (string) and ``accept`` (the kind-"
                            "discriminated acceptance spec)."
                        ),
                        "maxItems": _MAX_ITEMS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Item identifier.",
                                },
                                "accept": {
                                    "type": "object",
                                    "description": (
                                        "Kind-discriminated spec. "
                                        "``kind`` is required; other "
                                        "fields depend on the kind."
                                    ),
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "description": (
                                                "Acceptance kind: "
                                                "pr_checks, file, "
                                                "human_approval."
                                            ),
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    ]


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "accept_eval": accept_eval,
}
