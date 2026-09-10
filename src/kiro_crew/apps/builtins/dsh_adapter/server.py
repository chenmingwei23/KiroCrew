"""Launcher for the adapter backend: replaces itself with the Node process.

The app platform spawns a builtin app's backend as ``python -m <module>``, and a
builtin cannot use a file ``backend.entryPoint`` at all: the spawn resolves the
entry against ``app_dir(name)`` under the Kiro Crew home, while a builtin's files
only ever exist in the installed package. So the declared entry point is this
module, and its whole job is to become the Node process with ``os.execv`` --
which keeps the PID the gateway recorded, so its health check, its log capture
and its shutdown all address the process that is actually doing the work.

Configuration comes from a JSON file in the app's own data directory rather than
from the environment, because ``minimal_env()`` strips everything the platform
does not explicitly pass through::

    ~/.kiro/crew/apps/dsh-adapter/data/config.json
    {
      "checkout": "/abs/path/to/the/plugin/checkout",
      "gateway":  "http://127.0.0.1:5476",
      "units":    ["kiro", "review-agent"]
    }

No credential belongs in that file: the platform already hands a backend its own
app secret as ``KIROCREW_PROXY_SECRET``, and the adapter exchanges that at
``POST /api/apps/<name>/token`` for the app-scoped token it calls with. A
``token`` key is still honoured for a run against a standalone contract server
that mints none. Absent any of it the adapter still starts and still reports
health, saying what is missing -- a backend that exited instead would be
restarted forever over a configuration gap.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

#: The app's name, which is also its projection namespace under section 2 of the
#: contribution protocol.
APP_NAME = "dsh-adapter"

#: Config keys read from ``config.json``, mapped to the environment variable the
#: Node process reads each one from.
_CONFIG_ENV = {
    "checkout": "DSH_ADAPTER_CHECKOUT",
    "gateway": "DSH_ADAPTER_GATEWAY",
    "token": "DSH_ADAPTER_TOKEN",
}


def node_entry() -> Path:
    """Return the adapter's Node entry point inside this package."""
    return Path(__file__).resolve().parent / "node" / "server.mjs"


def config_path() -> Path:
    """Return the adapter's config file path inside the app data directory."""
    override = os.environ.get("DSH_ADAPTER_CONFIG")
    if override:
        return Path(override)
    from kiro_crew.apps.manager import app_data_dir

    return Path(app_data_dir(APP_NAME)) / "config.json"


def read_config(path: Path | None = None) -> dict:
    """Read the adapter config, tolerating an absent or malformed file.

    A missing config is a normal first-run state, not an error: the adapter
    reports it through its health endpoint.
    """
    target = path or config_path()
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_env(config: dict, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment for the Node process.

    Only the platform's own variables and the adapter's configuration travel; an
    unset config key is left unset so the Node side reports it as missing rather
    than reading an empty string as a value.
    """
    env = dict(base if base is not None else os.environ)
    for key, name in _CONFIG_ENV.items():
        value = config.get(key)
        if isinstance(value, str) and value:
            env[name] = value
    units = config.get("units")
    if isinstance(units, list):
        joined = ",".join(str(unit) for unit in units if str(unit))
        if joined:
            env["DSH_ADAPTER_UNITS"] = joined
    return env


def find_node() -> str | None:
    """Return a usable ``node`` binary, preferring the one the platform resolves."""
    from kiro_crew.apps.backend import _find_node_binary

    try:
        found = _find_node_binary()
    except Exception:  # noqa: BLE001 - a resolver failure must not mask the real cause
        found = None
    return found or shutil.which("node")


def main(argv: list[str] | None = None) -> int:
    """Replace this process with the Node adapter.

    Returns a nonzero exit code only when the process could not be replaced;
    on success ``os.execve`` does not return.
    """
    del argv
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    entry = node_entry()
    if not entry.is_file():
        logger.error("%s: adapter entry point missing: %s", APP_NAME, entry)
        return 1
    node = find_node()
    if not node:
        logger.error("%s: no node binary found; searched nvm and PATH", APP_NAME)
        return 1
    config = read_config()
    env = build_env(config)
    logger.info(
        "%s: exec %s %s (checkout %s)",
        APP_NAME,
        node,
        entry,
        env.get("DSH_ADAPTER_CHECKOUT", "<unset>"),
    )
    # Same PID, so the gateway's health check, log capture and shutdown all keep
    # addressing the process that does the work.
    os.execve(node, [node, str(entry)], env)
    return 1  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
