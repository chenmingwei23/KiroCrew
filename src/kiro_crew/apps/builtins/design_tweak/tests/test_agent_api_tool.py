"""Tests for the ``design_tweak_update_thread`` MCP tool's authorization story.

The tool is the ONLY credentialed path from an agent session to the Design
Tweak app's ``POST /thread`` route (the ``ops_mission_control_api`` /
``issue_radar_record_investigation`` precedent: the MCP server process holds
the internal secret; the agent never sees a credential). Three planes must
stay mutually consistent, and each has a failure mode this module pins:

- the **schema** in ``validation.py`` — id grammar on both ids, ``status``
  restricted to the one forward-progress value, and ``text or status``
  required, all enforced before any HTTP happens;
- the **handler** in ``mcp_tools/apps.py`` — the proxied URL it builds, the
  ``{role, text, status}`` body it posts, and redaction on the way in and out;
- the **gateway's mixed-internal path set** admitting EXACTLY ``/thread`` for
  internal-secret callers, and never the app's state-mutating routes
  (``/submit``, ``/send``, ``/clear``, ``/delete``, …) nor the bare app prefix.
"""

import unittest
from unittest import mock

from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import internal_path_matches
from kiro_crew.mcp_tools import apps
from kiro_crew.validation import (
    DESIGN_TWEAK_UPDATE_THREAD_SCHEMA,
    ValidationError,
    validate_tool_args,
)

_THREAD_PATH = "/api/apps/design-tweak/api/thread"


class TestSchema(unittest.TestCase):
    def _validate(self, **kwargs):
        return validate_tool_args(kwargs, DESIGN_TWEAK_UPDATE_THREAD_SCHEMA)

    def test_a_text_note_passes(self):
        cleaned = self._validate(request_id="r-1", comment_id="c.2", text="editing")
        self.assertEqual(cleaned["request_id"], "r-1")
        self.assertEqual(cleaned["comment_id"], "c.2")
        self.assertEqual(cleaned["text"], "editing")

    def test_a_status_only_done_passes(self):
        cleaned = self._validate(request_id="r-1", status="done")
        self.assertEqual(cleaned["status"], "done")

    def test_request_level_note_without_comment_id_passes(self):
        cleaned = self._validate(request_id="r-1", text="rebuilding, one moment")
        self.assertEqual(cleaned["comment_id"], "")

    def test_text_or_status_is_required(self):
        """The backend requires ``text or status``; refuse an empty call here."""
        with self.assertRaises(ValidationError):
            self._validate(request_id="r-1")

    def test_status_is_restricted_to_done(self):
        """Forward progress only — no clear/dismiss/resolve status.

        ``new``/``sent`` are the app's own lifecycle, not an agent report;
        anything else is simply off-surface.
        """
        for bad in ("resolve", "clear", "new", "sent", "dismiss", "DONE"):
            with self.assertRaises(ValidationError):
                self._validate(request_id="r-1", status=bad)

    def test_ids_match_the_backend_grammar(self):
        """Both ids are value-position only; no '/', '?', '#', or spaces.

        A '/' in an id is what would let the query rewrite the single
        ``/thread`` route the tool posts to.
        """
        for bad_req in ("../etc", "a/b", "a?b", "a#b", "a b", "a&b"):
            with self.assertRaises(ValidationError):
                self._validate(request_id=bad_req, text="x")
        for bad_cid in ("a/b", "a?b", "a b"):
            with self.assertRaises(ValidationError):
                self._validate(request_id="ok", comment_id=bad_cid, text="x")

    def test_request_id_is_required(self):
        with self.assertRaises(ValidationError):
            self._validate(text="x")


class TestHandler(unittest.TestCase):
    """The handler builds the proxied POST and redacts; it holds no identity."""

    def _call(self, args, resp=None):
        captured = {}

        def _fake_post(url, body=None, **kwargs):
            captured["url"] = url
            captured["body"] = body
            captured["kwargs"] = kwargs
            return resp if resp is not None else {"ok": True}

        with mock.patch.object(apps.mcp_core, "_post", _fake_post):
            out = apps.design_tweak_update_thread("design_tweak_update_thread", args)
        return out, captured

    def test_per_comment_url_carries_id_and_cid(self):
        _out, cap = self._call(
            {"request_id": "req-1", "comment_id": "cmt-2", "text": "editing"}
        )
        self.assertEqual(cap["url"], f"{_THREAD_PATH}?id=req-1&cid=cmt-2")

    def test_request_level_url_omits_cid(self):
        _out, cap = self._call({"request_id": "req-1", "text": "rebuilding"})
        self.assertEqual(cap["url"], f"{_THREAD_PATH}?id=req-1")

    def test_body_is_role_agent_with_text_and_status(self):
        _out, cap = self._call(
            {"request_id": "r", "comment_id": "c", "text": "done!", "status": "done"}
        )
        self.assertEqual(cap["body"]["role"], "agent")
        self.assertEqual(cap["body"]["text"], "done!")
        self.assertEqual(cap["body"]["status"], "done")

    def test_status_only_body_has_no_text_key(self):
        _out, cap = self._call({"request_id": "r", "comment_id": "c", "status": "done"})
        self.assertNotIn("text", cap["body"])
        self.assertEqual(cap["body"]["status"], "done")

    def test_handler_passes_no_session_key(self):
        """The thread post needs no caller identity; the resolver must stay out.

        A ``session_key`` kwarg here would mean the handler resolved a caller,
        which this route deliberately does not.
        """
        _out, cap = self._call({"request_id": "r", "text": "x"})
        self.assertNotIn("session_key", cap["kwargs"])

    def test_outgoing_text_is_redacted(self):
        """A credential quoted into a note is scrubbed BEFORE it is posted."""
        secret = "ghp_" + "A" * 36
        _out, cap = self._call({"request_id": "r", "text": f"token {secret}"})
        self.assertNotIn(secret, cap["body"]["text"])

    def test_response_is_redacted(self):
        secret = "ghp_" + "B" * 36
        out, _cap = self._call(
            {"request_id": "r", "text": "x"},
            resp={"ok": True, "echo": f"leak {secret}"},
        )
        self.assertNotIn(secret, out)


class TestGatewayAdmission(unittest.TestCase):
    """The gateway admits exactly ``/thread``, and no mutating sibling."""

    @staticmethod
    def _admitted(path):
        return internal_path_matches(path, _MIXED_INTERNAL_API_PATHS)

    def test_thread_route_is_admitted(self):
        self.assertTrue(self._admitted(_THREAD_PATH))

    def test_mutating_and_prefix_routes_are_not_admitted(self):
        """Only ``/thread`` is agent-reachable; everything else stays UI-only.

        Admitting the app prefix would prefix-match these state-mutating routes
        to anything holding the internal secret.
        """
        for path in (
            "/api/apps/design-tweak",
            "/api/apps/design-tweak/api",
            "/api/apps/design-tweak/api/submit",
            "/api/apps/design-tweak/api/send",
            "/api/apps/design-tweak/api/clear",
            "/api/apps/design-tweak/api/delete",
            "/api/apps/design-tweak/api/delete-comment",
            "/api/apps/design-tweak/api/projects",
            "/api/apps/design-tweak/api/dev-server/start",
        ):
            self.assertFalse(
                self._admitted(path),
                f"{path} must not be reachable with the internal secret",
            )


class TestToolRegistry(unittest.TestCase):
    def test_tool_is_advertised_and_handled(self):
        names = {s["name"] for s in apps.schemas()}
        self.assertIn("design_tweak_update_thread", names)
        self.assertIn("design_tweak_update_thread", apps.HANDLERS)


if __name__ == "__main__":
    unittest.main()
