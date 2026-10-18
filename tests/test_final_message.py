import unittest

from telly.app import _build_final_message_parts
from telly.brain import BrainTurn


def _turn(reply, **kw):
    return BrainTurn(
        reply=reply,
        session_id=kw.get("session_id", "sess_12345678"),
        cost_usd=kw.get("cost_usd"),
        duration_ms=kw.get("duration_ms", 1200),
        resumed=False,
        tool_breadcrumbs=kw.get("tool_breadcrumbs", []),
        tool_events=kw.get("tool_events", []),
    )


class FinalMessageLayoutTest(unittest.TestCase):
    def test_answer_first_and_no_duplicate_chrome(self):
        turn = _turn("Here is the answer.", duration_ms=1200)

        display, _, trailing, legacy, _, _, _, confirm = _build_final_message_parts(turn, "1.2")

        self.assertEqual(display, "Here is the answer.")
        self.assertIsNone(confirm)
        legacy_types = [b["type"] for b in legacy]
        self.assertEqual(legacy_types[0], "markdown")
        # The old status banner / completion card / task card are gone.
        self.assertNotIn("card", legacy_types)
        self.assertNotIn("task_card", legacy_types)
        self.assertNotIn("plan", legacy_types)
        # Streamed config keeps the answer out of the bottom blocks but keeps controls.
        trailing_types = [b["type"] for b in trailing]
        self.assertNotIn("markdown", trailing_types)
        self.assertIn("context_actions", trailing_types)

    def test_sources_render_as_context_links(self):
        turn = _turn(
            "See the docs.",
            tool_events=[
                {"id": "t", "name": "WebFetch", "input": {"url": "https://example.com/docs"}, "status": "done"},
            ],
        )

        _, _, _, legacy, _, _, _, _ = _build_final_message_parts(turn, "1.2")

        contexts = [b for b in legacy if b["type"] == "context"]
        self.assertTrue(contexts)
        text = contexts[0]["elements"][0]["text"]
        self.assertIn("Sources", text)
        self.assertIn("https://example.com/docs", text)

    def test_permission_confirmation_leads_with_buttons_only(self):
        turn = _turn("I'll remove /tmp/x.\nSLACK_CONFIRM: delete /tmp/x")

        _, _, trailing, _, _, _, _, confirm = _build_final_message_parts(turn, "1.2")

        self.assertIsNotNone(confirm)
        self.assertIn("delete /tmp/x", confirm)
        types = {b["type"] for b in trailing}
        self.assertIn("actions", types)          # Approve / Deny
        self.assertNotIn("context_actions", types)  # no regenerate/fork/feedback noise


if __name__ == "__main__":
    unittest.main()
