"""The queue-proof lookup, run end to end against a stubbed ``gh``.

``.github/scripts/queue-proof.sh`` makes the queue-on push path self-verifying
(issue #15566). On a push to main with ``MERGE_QUEUE_ENABLED`` set, the heavy CI
jobs trim to the macOS boot leg on the premise that the merge queue already ran
the full matrix on this exact tree. This script checks that premise per commit:
it queries the API for a successful ``merge_group`` CI run whose ``head_sha`` is
this commit, and publishes ``queue_proved=true|false``. The trim clauses read
that output, so a direct push that bypassed the queue falls back to the full
matrix instead of trusting the variable.

The one property that matters is FAIL-SAFE: every path that cannot CONFIRM a
successful merge_group run -- the variable unset, a lookup error, a rate limit,
an empty list, a run that failed or is still running -- must publish
``queue_proved=false``, i.e. run the full matrix. There is no path that trims
the matrix on an unconfirmed queue. These cases drive the script's whole control
flow through a stubbed ``gh`` and assert the published output.

``gh`` is a stub on PATH that returns one canned ``/actions/workflows/ci.yml/
runs`` response, exactly how the real job reads the run list.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "queue-proof.sh"

_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_REPO = "kirodotdev/KiroCrew"

needs_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="runs the queue-proof lookup under a POSIX bash with jq on PATH",
)


def _run(status: str, conclusion: str | None) -> dict:
    return {"id": 1, "status": status, "conclusion": conclusion}


def _page(*runs: dict) -> str:
    return json.dumps({"workflow_runs": list(runs)})


def _invoke(
    tmp_path: Path,
    *,
    queue_on: str,
    response: str | None,
) -> str:
    """Run the script with a stub gh returning ``response`` (or failing) and
    return the published ``queue_proved`` value."""
    # A gh stub that prints the canned response, or exits non-zero when the
    # response is None (the real gh's behaviour when the API errors).
    gh = tmp_path / "gh"
    if response is None:
        gh.write_text(
            "#!/usr/bin/env bash\necho 'gh: simulated API failure' >&2\nexit 1\n",
            encoding="utf-8",
        )
    else:
        # printf the payload verbatim; the script captures stdout.
        payload = tmp_path / "payload.json"
        payload.write_text(response, encoding="utf-8")
        gh.write_text(
            f"#!/usr/bin/env bash\ncat {payload}\n",
            encoding="utf-8",
        )
    gh.chmod(0o755)

    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        GH_TOKEN="x",
        REPO=_REPO,
        SHA=_SHA,
        WORKFLOW="ci.yml",
        QUEUE_ON=queue_on,
        GITHUB_OUTPUT=str(github_output),
    )
    proc = subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        **UTF8_TEXT,
    )
    assert proc.returncode == 0, f"queue-proof must always exit 0: {proc.stderr}"
    written = github_output.read_text(encoding="utf-8")
    lines = [ln for ln in written.splitlines() if ln.startswith("queue_proved=")]
    assert len(lines) == 1, f"expected exactly one queue_proved line, got {written!r}"
    return lines[0].split("=", 1)[1]


@needs_bash
class TestQueueProofFailsSafe:
    def test_variable_unset_short_circuits_to_false_without_a_lookup(self, tmp_path: Path) -> None:
        # QUEUE_ON != 'true' is every non-queue-on event; the full matrix already
        # runs, so there is nothing to prove. No gh call is made (the stub would
        # fail if called, but the response is a successful run to prove the
        # short-circuit, not the lookup, is what returns false).
        proved = _invoke(
            tmp_path,
            queue_on="false",
            response=_page(_run("completed", "success")),
        )
        assert proved == "false"

    def test_a_successful_merge_group_run_proves_the_queue(self, tmp_path: Path) -> None:
        proved = _invoke(
            tmp_path,
            queue_on="true",
            response=_page(_run("completed", "success")),
        )
        assert proved == "true"

    def test_an_empty_run_list_falls_back_to_the_full_matrix(self, tmp_path: Path) -> None:
        # The canned empty response the issue names: no merge_group run exists for
        # this SHA, so the push did not come through the queue.
        proved = _invoke(tmp_path, queue_on="true", response=_page())
        assert proved == "false"

    def test_a_lookup_error_falls_back_to_the_full_matrix(self, tmp_path: Path) -> None:
        # A rate limit or 5xx proves only that the barrier could not look; it must
        # never be read as "queue proved". Fail safe to the full matrix.
        proved = _invoke(tmp_path, queue_on="true", response=None)
        assert proved == "false"

    @pytest.mark.parametrize(
        ("status", "conclusion"),
        [
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", "timed_out"),
            ("completed", "startup_failure"),
            ("in_progress", None),
            ("queued", None),
        ],
    )
    def test_a_non_success_run_does_not_prove_the_queue(
        self, tmp_path: Path, status: str, conclusion: str | None
    ) -> None:
        # Only a completed+success merge_group run is proof. A failed, cancelled,
        # or still-running one means the queue has not vouched for this tree.
        proved = _invoke(tmp_path, queue_on="true", response=_page(_run(status, conclusion)))
        assert proved == "false"

    def test_one_success_among_non_success_runs_proves_the_queue(self, tmp_path: Path) -> None:
        # The API can return several runs for a SHA (a re-run). One success is
        # enough: the tree was tested green by the queue at least once.
        proved = _invoke(
            tmp_path,
            queue_on="true",
            response=_page(
                _run("completed", "failure"),
                _run("completed", "success"),
            ),
        )
        assert proved == "true"
