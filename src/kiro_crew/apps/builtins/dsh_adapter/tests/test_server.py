"""Tests for the adapter's Python launcher.

The launcher is small on purpose, and the two things it does that can be wrong
without being visible are covered here: reading a config file that may not exist
yet, and building the child environment. A key it did not read must stay UNSET
rather than becoming an empty string, because the Node side reports an unset key
as a configuration gap and would read an empty one as a real value.
"""
from __future__ import annotations

import json

from kiro_crew.apps.builtins.dsh_adapter import server


def test_missing_config_reads_as_empty(tmp_path):
    """A first run has no config file, which is a normal state, not an error."""
    assert server.read_config(tmp_path / "nope.json") == {}


def test_malformed_config_reads_as_empty(tmp_path):
    """A truncated or hand-edited config must not raise into the spawn path."""
    path = tmp_path / "config.json"
    path.write_text("{not json")
    assert server.read_config(path) == {}


def test_non_object_config_reads_as_empty(tmp_path):
    """A JSON list is valid JSON and still not a config."""
    path = tmp_path / "config.json"
    path.write_text("[1, 2]")
    assert server.read_config(path) == {}


def test_config_maps_onto_the_child_environment(tmp_path):
    """Every configured key reaches the Node process under its own variable."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "checkout": "/srv/checkout",
        "gateway": "http://127.0.0.1:9999",
        "token": "t0ken",
        "units": ["kiro", "review-agent"],
    }))
    env = server.build_env(server.read_config(path), base={"PORT": "8080"})
    assert env["DSH_ADAPTER_CHECKOUT"] == "/srv/checkout"
    assert env["DSH_ADAPTER_GATEWAY"] == "http://127.0.0.1:9999"
    assert env["DSH_ADAPTER_TOKEN"] == "t0ken"
    assert env["DSH_ADAPTER_UNITS"] == "kiro,review-agent"
    assert env["PORT"] == "8080", "the platform's own variables survive"


def test_absent_keys_stay_unset_rather_than_empty():
    """An unset key must be absent, so the child reports it as missing."""
    env = server.build_env({}, base={})
    for name in ("DSH_ADAPTER_CHECKOUT", "DSH_ADAPTER_GATEWAY", "DSH_ADAPTER_TOKEN",
                 "DSH_ADAPTER_UNITS"):
        assert name not in env


def test_empty_values_are_treated_as_unset():
    """An empty string in the config is a gap, not a value."""
    env = server.build_env({"checkout": "", "token": "", "units": []}, base={})
    assert "DSH_ADAPTER_CHECKOUT" not in env
    assert "DSH_ADAPTER_TOKEN" not in env
    assert "DSH_ADAPTER_UNITS" not in env


def test_units_of_the_wrong_shape_are_ignored():
    """A hand-edited `units` string must not become one bogus unit id."""
    env = server.build_env({"units": "kiro"}, base={})
    assert "DSH_ADAPTER_UNITS" not in env


def test_node_entry_points_at_the_shipped_adapter():
    """The launcher execs the entry that ships inside this package."""
    entry = server.node_entry()
    assert entry.is_file(), entry
    assert entry.name == "server.mjs"
    assert entry.parent.name == "node"
