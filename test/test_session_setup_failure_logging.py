"""A failed session setup has to say what failed.

The cleanup around a failed start was complete and silent: the exception went to
a caller that retries without logging it, and the cleanup kill is a DELIBERATE
one, so the death line that carries the child's stderr tail is emitted at INFO --
below the level a running gateway keeps. What a burst of failing starts left in
the log was a spawn, a kill reading "failed session setup cleanup", and another
spawn, three times over, with the cause recorded nowhere.

These tests pin the one frame that holds both halves of the evidence.
"""

from __future__ import annotations

import inspect
import logging
import re

from kiro_crew.providers import acp as acp_provider


def _setup_guard_source() -> str:
    """The body of the post-spawn guard in the kiro start path."""
    source = inspect.getsource(acp_provider.AcpProvider._start_kiro_runtime_impl)
    marker = "failed session setup cleanup"
    assert marker in source, "the guard this suite describes is no longer here"
    return source


def test_the_setup_guard_binds_the_exception_it_catches() -> None:
    """A bare ``except BaseException:`` cannot log what it caught.

    Pinned structurally rather than by running a start: the guard wraps the whole
    of session setup, and the binding is what makes the cause reachable at all.
    """
    source = _setup_guard_source()
    assert re.search(
        r"except BaseException as \w+:", source
    ), "the guard must bind its exception so the failure can be reported"
    assert not re.search(r"except BaseException:\s*\n\s*# No provider owns", source)


def test_the_setup_guard_logs_the_cause_at_warning() -> None:
    """INFO is below what a running gateway retains, so the line must be WARNING."""
    source = _setup_guard_source()
    guard = source[source.index("except BaseException as") :]
    head = guard[: guard.index("chat_share_lease is not None")]
    assert (
        "logger.warning(" in head
    ), "the cause must be logged at WARNING, before the cleanup that follows"
    assert "exc_info=True" in head, "the traceback is the part that names the failing call"


def test_the_setup_failure_line_carries_the_childs_stderr() -> None:
    """The exception alone cannot tell a dead host from a refused answer.

    The child's stderr is the only evidence that separates them, and the
    deliberate cleanup kill demotes the death line that carries it below the
    level a running gateway retains.
    """
    source = _setup_guard_source()
    guard = source[source.index("except BaseException as") :]
    head = guard[: guard.index("chat_share_lease is not None")]
    assert "redacted_stderr_tail()" in head, (
        "the child's stderr tail must ride the failure line; the death line that "
        "carried it is emitted at INFO on a deliberate kill"
    )


def test_the_failure_line_names_the_runtime_and_the_session() -> None:
    """A burst places several starts at once, so a line that names neither the
    pid nor the session cannot be matched to the spawn it belongs to."""
    source = _setup_guard_source()
    guard = source[source.index("except BaseException as") :]
    head = guard[: guard.index("chat_share_lease is not None")]
    assert "runtime.pid" in head and "session" in head


def test_the_stderr_tail_reader_redacts_before_handing_bytes_over() -> None:
    """Child stderr is untrusted output that can echo a credential, and the
    failure line is a new consumer of it."""
    from kiro_crew.acp.runtime import AcpRuntime

    source = inspect.getsource(AcpRuntime.redacted_stderr_tail)
    assert "redact_credentials" in source and "redact_exfiltration_urls" in source


def test_a_composition_failure_does_not_replace_the_original_exception() -> None:
    """A diagnostic that raises would mask the failure it describes, and this arm
    runs while an exception is already propagating."""
    source = _setup_guard_source()
    guard = source[source.index("except BaseException as") :]
    head = guard[: guard.index("chat_share_lease is not None")]
    assert "logger.debug(" in head, "the warning must itself be guarded"


def test_the_gateway_log_level_assumption_this_suite_rests_on() -> None:
    """The reason INFO is not enough, stated as a check rather than a comment."""
    assert logging.WARNING > logging.INFO
