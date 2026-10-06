"""Selected-versus-running component version skew (issue #13086).

KiroCrew can *select* a new package while the desktop, gateway, gateway daemon,
and registered sidecars keep serving an earlier one. The gateway stays
reachable, so ordinary health reports the same visible state whether every
component runs the selected package or one is stale -- yet the two states need
different operator action (nothing, versus finish the update / restart the stale
component). This module adds the missing *effective-state* indicator: a local
comparison of the selected package identity against each managed component's
running identity, reported as ``aligned`` or ``version_skew``.

What this module is, and is NOT:

* It is a DIAGNOSTIC. It reads identities and compares them. It never restarts,
  stops, adopts, or replaces a process, and it does not touch update, ownership,
  restart, or recovery behaviour (acceptance criterion 5).
* The comparable identity is the pair ``(release, distribution)`` -- the
  release-clamped product version (:func:`kiro_crew.beacon.release`) and the
  distribution channel (:func:`kiro_crew.beacon.distribution`). Both are already
  the stable, low-cardinality, cross-install (packaged *and* source) identity
  the rest of the codebase trusts, so equality on the pair is a meaningful
  "same package" test. The issue's acceptance boundary leaves the *final* field
  choice to maintainers; this first slice reports skew immediately and exposes
  its age, leaving the warning threshold to the caller.

Exported shape (acceptance criteria 1 and 4). Every row is a
:class:`ComponentRow` carrying only:

* ``role`` -- a bounded role from :data:`COMPONENT_ROLES`.
* ``product_version`` -- the public, release-clamped version string.
* ``build_identity`` -- an equality-comparable string ``"<version>+<dist>"``.
* ``start_time`` -- process start time as an epoch float, or ``None``.
* ``relation`` -- ``selected`` / ``aligned`` / ``skewed`` / ``unknown``.

Nothing here carries an installation path, hostname, command line, username, or
process identifier. :func:`ComponentRow.as_dict` is the ONLY serialization and
it emits exactly those five fields, so a caller cannot accidentally export a
leaky attribute: there is no attribute to leak.

Mismatch age (acceptance criterion 2). The age is "how long has *this* skew been
observed", not "how old is the stale process", because the operator question is
whether the mismatch is a transient update window or a persistent one. The first
time a skew is seen a marker is written under ``config_dir()``; the age is
``now - marker``. An aligned result clears the marker, so a later skew starts a
fresh clock. The marker is best-effort: an unwritable config dir yields age
``0.0`` rather than failing the comparison.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from kiro_crew import __version__, beacon
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# Bounded component-role vocabulary (acceptance criterion 1). A probe returning
# a role outside this set is folded to ``_ROLE_OTHER`` so an unexpected value
# can never widen the exported vocabulary.
COMPONENT_ROLES = frozenset(
    {
        "desktop",
        "gateway",
        "gateway_daemon",
        "sidecar",
    }
)
_ROLE_OTHER = "other"

# Role used for the selected-package row (not a running component).
SELECTED_ROLE = "selected"

# Relations a running component can bear to the selected package.
REL_SELECTED = "selected"
REL_ALIGNED = "aligned"
REL_SKEWED = "skewed"
REL_UNKNOWN = "unknown"

# Overall results.
RESULT_ALIGNED = "aligned"
RESULT_SKEW = "version_skew"

# Persisted first-seen marker for mismatch age.
_SKEW_MARKER_FILE = "component-skew-first-seen.json"

# Sentinel version for a component whose identity could not be read
# (acceptance criterion 6: unreadable component identity).
UNREADABLE = "unreadable"


def _clamp_role(role: str) -> str:
    """Fold any role outside the bounded vocabulary to ``_ROLE_OTHER``."""
    raw = (role or "").strip().lower()
    return raw if raw in COMPONENT_ROLES else _ROLE_OTHER


def _build_identity(product_version: str, distribution: str) -> str:
    """Equality-comparable identity string for a (version, distribution) pair.

    ``"<release>+<distribution>"``. Two components are the "same package" iff
    their build identities are equal, which is the comparison the whole skew
    check turns on. An unreadable component carries :data:`UNREADABLE` as its
    version so it never compares equal to a real package.
    """
    return f"{product_version}+{distribution}"


@dataclass(frozen=True)
class ComponentIdentity:
    """Raw identity a probe reports for one component, before comparison.

    A probe returns this (or raises / returns ``None`` for a component whose
    identity cannot be read, or one that exited during collection). The module
    turns it into a :class:`ComponentRow` once the selected package is known.
    """

    role: str
    product_version: str
    distribution: str
    start_time: Optional[float] = None


@dataclass(frozen=True)
class ComponentRow:
    """One exported row: the selected package or one managed component.

    The ONLY serialization is :meth:`as_dict`, which emits exactly the five
    public fields. There is deliberately no field for a path, hostname, command
    line, username, or PID, so none can be exported (acceptance criterion 4).
    """

    role: str
    product_version: str
    build_identity: str
    start_time: Optional[float]
    relation: str

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "product_version": self.product_version,
            "build_identity": self.build_identity,
            "start_time": self.start_time,
            "relation": self.relation,
        }


@dataclass(frozen=True)
class SkewReport:
    """Result of comparing the selected package against every component."""

    #: :data:`RESULT_ALIGNED` or :data:`RESULT_SKEW`.
    result: str
    #: Human-readable, bounded reason (never carries a leaky identifier).
    reason: str
    #: Seconds since this skew was first observed; ``0.0`` when aligned.
    mismatch_age_seconds: float
    #: The selected-package row followed by one row per managed component.
    rows: tuple[ComponentRow, ...]

    def as_dict(self) -> dict:
        return {
            "result": self.result,
            "reason": self.reason,
            "mismatch_age_seconds": self.mismatch_age_seconds,
            "components": [r.as_dict() for r in self.rows],
        }


# A probe yields component identities. Each item is a ComponentIdentity, or
# None for a component that could not be read / exited during collection. A
# probe may also raise; the collector treats a raise the same as None but
# records an "unreadable" row so the operator sees the component was present.
ComponentProbe = Callable[[], Iterable[Optional[ComponentIdentity]]]


def selected_identity() -> ComponentIdentity:
    """Identity of the package this install has *selected*.

    This is the local package the running CLI module itself came from -- the
    release-clamped ``__version__`` and the baked/declared distribution. It is
    the reference every component row is compared against.
    """
    return ComponentIdentity(
        role=SELECTED_ROLE,
        product_version=beacon.release(__version__),
        distribution=beacon.distribution(),
        start_time=None,
    )


def _self_gateway_probe() -> list[Optional[ComponentIdentity]]:
    """Probe for the one component this process can always read: itself.

    The running Python process IS the gateway/CLI for a source or pip install,
    so its identity equals the selected package by construction. Richer probes
    (desktop supervisor spawn-time identities, gateway-daemon and sidecar
    census) are platform-specific and are injected by the caller on the
    platforms that have them; this base probe keeps the comparison meaningful
    on every platform and in tests.
    """
    sel = selected_identity()
    try:
        start = _process_start_time()
    except Exception:  # pragma: no cover - defensive
        start = None
    return [
        ComponentIdentity(
            role="gateway",
            product_version=sel.product_version,
            distribution=sel.distribution,
            start_time=start,
        )
    ]


def _process_start_time() -> Optional[float]:
    """Best-effort epoch start time of the current process, or ``None``.

    Uses ``psutil`` when available; otherwise returns ``None`` rather than
    guessing. A missing start time is a bounded "unknown", not an error.
    """
    try:
        import os

        import psutil  # type: ignore[import-not-found]

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return None


def _skew_marker_path():
    return config_dir() / _SKEW_MARKER_FILE


def _read_first_seen() -> Optional[float]:
    try:
        raw = _skew_marker_path().read_text(encoding="utf-8")
        value = json.loads(raw).get("first_seen")
        return float(value) if isinstance(value, (int, float)) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_first_seen(when: float) -> None:
    try:
        path = _skew_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"first_seen": when}), encoding="utf-8")
    except OSError as exc:
        logger.debug("could not persist skew first-seen marker: %s", exc)


def _clear_first_seen() -> None:
    try:
        _skew_marker_path().unlink()
    except OSError:
        pass


def _mismatch_age(now: float) -> float:
    """Seconds since the current skew was first observed.

    Persists a first-seen marker so the age is meaningful across separate
    ``status`` invocations (a transient update window versus a persistent skew).
    Best-effort: an unwritable config dir yields ``0.0`` rather than raising.
    """
    first = _read_first_seen()
    if first is None or first > now:
        _write_first_seen(now)
        return 0.0
    return max(0.0, now - first)


def compare(
    probes: Optional[Iterable[ComponentProbe]] = None,
    *,
    now: Optional[float] = None,
    _selected: Optional[ComponentIdentity] = None,
) -> SkewReport:
    """Compare the selected package against every managed component.

    ``probes`` is the set of component probes to run; when omitted, only the
    always-available self probe runs. Each probe yields
    :class:`ComponentIdentity` items (or ``None`` / a raise for a component that
    could not be read or exited during collection -- acceptance criterion 6).

    The reference package comes from :func:`selected_identity` (overridable via
    ``_selected`` for tests). The result is :data:`RESULT_SKEW` when any
    readable component's build identity differs from the selected one, else
    :data:`RESULT_ALIGNED`. An unreadable component does NOT by itself force a
    skew result -- its identity is unknown, not known-different -- but it is
    reported as its own row so the operator sees collection was incomplete.
    """
    clock = time.time() if now is None else now
    selected = _selected if _selected is not None else selected_identity()
    selected_build = _build_identity(selected.product_version, selected.distribution)

    rows: list[ComponentRow] = [
        ComponentRow(
            role=SELECTED_ROLE,
            product_version=selected.product_version,
            build_identity=selected_build,
            start_time=None,
            relation=REL_SELECTED,
        )
    ]

    active_probes = list(probes) if probes is not None else [_self_gateway_probe]
    any_skew = False
    any_unreadable = False

    for probe in active_probes:
        try:
            items = list(probe())
        except Exception as exc:
            # A probe that fails wholesale is recorded as one unreadable row
            # rather than aborting the comparison.
            logger.debug("component probe failed: %s", exc)
            any_unreadable = True
            rows.append(_unreadable_row())
            continue
        for item in items:
            if item is None:
                # Component exited during collection, or its identity was
                # unreadable: a bounded "unknown" row, not a crash.
                any_unreadable = True
                rows.append(_unreadable_row())
                continue
            build = _build_identity(item.product_version, item.distribution)
            if item.product_version == UNREADABLE:
                any_unreadable = True
                relation = REL_UNKNOWN
            elif build == selected_build:
                relation = REL_ALIGNED
            else:
                relation = REL_SKEWED
                any_skew = True
            rows.append(
                ComponentRow(
                    role=_clamp_role(item.role),
                    product_version=item.product_version,
                    build_identity=build,
                    start_time=item.start_time,
                    relation=relation,
                )
            )

    if any_skew:
        age = _mismatch_age(clock)
        skewed = sum(1 for r in rows if r.relation == REL_SKEWED)
        reason = f"{skewed} component(s) run a package other than the selected one"
        if any_unreadable:
            reason += "; one or more component identities were unreadable"
        return SkewReport(
            result=RESULT_SKEW,
            reason=reason,
            mismatch_age_seconds=age,
            rows=tuple(rows),
        )

    # No known skew. Clear the marker so a future skew starts a fresh clock.
    _clear_first_seen()
    if any_unreadable:
        reason = (
            "all readable components match the selected package; "
            "one or more component identities were unreadable"
        )
    else:
        reason = "all managed components run the selected package"
    return SkewReport(
        result=RESULT_ALIGNED,
        reason=reason,
        mismatch_age_seconds=0.0,
        rows=tuple(rows),
    )


def _unreadable_row() -> ComponentRow:
    return ComponentRow(
        role=_ROLE_OTHER,
        product_version=UNREADABLE,
        build_identity=_build_identity(UNREADABLE, ""),
        start_time=None,
        relation=REL_UNKNOWN,
    )
