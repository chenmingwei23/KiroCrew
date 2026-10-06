"""Tests for selected-versus-running component version skew (issue #13086).

Drives the real :mod:`kiro_crew.component_identity` code -- the comparison, the
mismatch-age clock, and the export shape -- so drift in any of them fails here.
Covers the six cases the issue's acceptance criterion 6 requires: aligned,
transient skew, persistent skew, an unreadable component identity, and a
component exiting during collection; plus the no-leak export contract
(criterion 4) and the bounded role vocabulary (criterion 1).
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import component_identity as ci


@pytest.fixture(autouse=True)
def _isolate_marker(tmp_path, monkeypatch):
    """Point the skew first-seen marker at a temp dir so tests do not share it."""
    monkeypatch.setattr(ci, "_skew_marker_path", lambda: tmp_path / "skew.json")
    yield


def _selected(version="0.9.0", dist="source"):
    return ci.ComponentIdentity(
        role=ci.SELECTED_ROLE, product_version=version, distribution=dist
    )


def _component(role="gateway", version="0.9.0", dist="source", start=1000.0):
    def probe():
        return [
            ci.ComponentIdentity(
                role=role, product_version=version, distribution=dist, start_time=start
            )
        ]

    return probe


# ---------------------------------------------------------------------------
# Aligned (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_aligned_reports_aligned_without_warning():
    report = ci.compare([_component(version="0.9.0", dist="source")], _selected=_selected())
    assert report.result == ci.RESULT_ALIGNED
    assert report.mismatch_age_seconds == 0.0
    # The selected row plus the one component row, both aligned.
    relations = [r.relation for r in report.rows]
    assert relations == [ci.REL_SELECTED, ci.REL_ALIGNED]


# ---------------------------------------------------------------------------
# Skew with age (acceptance criterion 2)
# ---------------------------------------------------------------------------


def test_persistent_skew_reports_version_skew_with_both_identities():
    probe = _component(role="desktop", version="0.8.0", dist="source")
    report = ci.compare([probe], now=100.0, _selected=_selected(version="0.9.0"))
    assert report.result == ci.RESULT_SKEW
    skewed = [r for r in report.rows if r.relation == ci.REL_SKEWED]
    assert len(skewed) == 1
    # Both comparable identities are present: the selected row and the skewed one.
    selected_row = next(r for r in report.rows if r.relation == ci.REL_SELECTED)
    assert selected_row.build_identity == "0.9.0+source"
    assert skewed[0].build_identity == "0.8.0+source"


def test_distribution_difference_alone_is_skew():
    # Same version, different distribution channel -> still a different package.
    probe = _component(version="0.9.0", dist="homebrew")
    report = ci.compare([probe], _selected=_selected(version="0.9.0", dist="source"))
    assert report.result == ci.RESULT_SKEW


def test_transient_then_persistent_skew_age_grows_across_calls():
    probe = _component(version="0.8.0")
    # First observation: age starts at zero (transient).
    first = ci.compare([probe], now=1000.0, _selected=_selected(version="0.9.0"))
    assert first.result == ci.RESULT_SKEW
    assert first.mismatch_age_seconds == 0.0
    # A later call with the SAME skew: age is the elapsed time (persistent).
    later = ci.compare([probe], now=1300.0, _selected=_selected(version="0.9.0"))
    assert later.result == ci.RESULT_SKEW
    assert later.mismatch_age_seconds == pytest.approx(300.0)


def test_aligned_clears_the_skew_clock():
    skew_probe = _component(version="0.8.0")
    aligned_probe = _component(version="0.9.0")
    sel = _selected(version="0.9.0")
    ci.compare([skew_probe], now=1000.0, _selected=sel)
    # Alignment clears the marker.
    ci.compare([aligned_probe], now=1300.0, _selected=sel)
    # A fresh skew therefore starts a new clock at zero, not at 500s.
    again = ci.compare([skew_probe], now=1500.0, _selected=sel)
    assert again.mismatch_age_seconds == 0.0


# ---------------------------------------------------------------------------
# Unreadable component identity (acceptance criterion 6)
# ---------------------------------------------------------------------------


def test_probe_that_raises_yields_one_unreadable_row_not_a_crash():
    def boom():
        raise RuntimeError("process inspection denied")

    report = ci.compare([boom], _selected=_selected())
    unreadable = [r for r in report.rows if r.product_version == ci.UNREADABLE]
    assert len(unreadable) == 1
    assert unreadable[0].relation == ci.REL_UNKNOWN
    # An unreadable identity is unknown, not known-different: no false skew.
    assert report.result == ci.RESULT_ALIGNED
    assert "unreadable" in report.reason


def test_unreadable_identity_value_is_reported_unknown():
    def probe():
        return [
            ci.ComponentIdentity(
                role="sidecar", product_version=ci.UNREADABLE, distribution=""
            )
        ]

    report = ci.compare([probe], _selected=_selected())
    row = next(r for r in report.rows if r.role == "sidecar")
    assert row.relation == ci.REL_UNKNOWN


# ---------------------------------------------------------------------------
# Component exiting during collection (acceptance criterion 6)
# ---------------------------------------------------------------------------


def test_component_exiting_during_collection_yields_none_handled():
    def probe():
        # A readable component, then one that exited mid-census (None).
        return [
            ci.ComponentIdentity(
                role="gateway", product_version="0.9.0", distribution="source"
            ),
            None,
        ]

    report = ci.compare([probe], _selected=_selected(version="0.9.0"))
    unknown = [r for r in report.rows if r.relation == ci.REL_UNKNOWN]
    assert len(unknown) == 1
    # The surviving readable component still compares; the vanished one does not
    # fabricate a skew.
    assert report.result == ci.RESULT_ALIGNED


def test_skew_plus_unreadable_reports_skew_and_notes_unreadable():
    def probe():
        return [
            ci.ComponentIdentity(
                role="desktop", product_version="0.8.0", distribution="source"
            ),
            None,
        ]

    report = ci.compare([probe], now=5.0, _selected=_selected(version="0.9.0"))
    assert report.result == ci.RESULT_SKEW
    assert "unreadable" in report.reason


# ---------------------------------------------------------------------------
# No-leak export (acceptance criterion 4)
# ---------------------------------------------------------------------------


def test_exported_rows_carry_only_bounded_fields():
    report = ci.compare([_component()], _selected=_selected())
    allowed = {"role", "product_version", "build_identity", "start_time", "relation"}
    for row in report.rows:
        assert set(row.as_dict().keys()) == allowed
    top = report.as_dict()
    assert set(top.keys()) == {"result", "reason", "mismatch_age_seconds", "components"}


def test_export_is_json_serializable_and_leaks_nothing_sensitive():
    report = ci.compare([_component(start=1234.5)], _selected=_selected())
    blob = json.dumps(report.as_dict())
    # None of the forbidden identifier shapes may appear anywhere in the export.
    for forbidden in ("/home/", "/local/home", "pid", "hostname", "cmdline", "username"):
        assert forbidden not in blob.lower()


# ---------------------------------------------------------------------------
# Bounded role vocabulary (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_role_outside_vocabulary_is_folded_to_other():
    probe = _component(role="totally-made-up-role")
    report = ci.compare([probe], _selected=_selected())
    comp = next(r for r in report.rows if r.relation != ci.REL_SELECTED)
    assert comp.role == "other"


@pytest.mark.parametrize("role", sorted(ci.COMPONENT_ROLES))
def test_known_roles_survive(role):
    report = ci.compare([_component(role=role)], _selected=_selected())
    comp = next(r for r in report.rows if r.relation != ci.REL_SELECTED)
    assert comp.role == role


# ---------------------------------------------------------------------------
# Default self probe
# ---------------------------------------------------------------------------


def test_default_self_probe_is_aligned_with_selected():
    # With no probes supplied, only the self probe runs and it is, by
    # construction, the selected package -> aligned.
    report = ci.compare()
    assert report.result == ci.RESULT_ALIGNED
    assert any(r.role == "gateway" for r in report.rows)
