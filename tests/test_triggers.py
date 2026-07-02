import unittest
from unittest.mock import patch

from telly.app import _format_trigger_note, _handle_trigger_command, _run_due_triggers
from telly.config import Config
from telly.state import ThreadStore


class FakeSlackClient:
    def __init__(self):
        self.messages = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)


class TriggerCommandTest(unittest.TestCase):
    def test_add_list_remove_and_clear_triggers(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            store = ThreadStore(__import__("pathlib").Path(tmp))
            client = FakeSlackClient()

            handled = _handle_trigger_command(
                store,
                client,
                "C1",
                "T1",
                "add trigger amma jio: When I say check latest, inspect WhatsApp Amma Jio and suggest a reply.",
            )
            self.assertTrue(handled)
            self.assertEqual(
                store.get("T1").triggers,
                {
                    "amma jio": {
                        "instruction": "When I say check latest, inspect WhatsApp Amma Jio and suggest a reply.",
                        "interval_seconds": None,
                        "enabled": False,
                        "next_run_at": None,
                    }
                },
            )

            handled = _handle_trigger_command(store, client, "C1", "T1", "start trigger amma jio every 5m")
            self.assertTrue(handled)
            self.assertEqual(store.get("T1").triggers["amma jio"]["interval_seconds"], 300)
            self.assertTrue(store.get("T1").triggers["amma jio"]["enabled"])

            handled = _handle_trigger_command(store, client, "C1", "T1", "stop trigger amma jio")
            self.assertTrue(handled)
            self.assertFalse(store.get("T1").triggers["amma jio"]["enabled"])

            handled = _handle_trigger_command(store, client, "C1", "T1", "list triggers")
            self.assertTrue(handled)
            self.assertIn("amma jio", client.messages[-1]["text"])

            handled = _handle_trigger_command(store, client, "C1", "T1", "remove trigger amma jio")
            self.assertTrue(handled)
            self.assertEqual(store.get("T1").triggers, {})

            handled = _handle_trigger_command(store, client, "C1", "T1", "clear triggers")
            self.assertTrue(handled)
            self.assertEqual(store.get("T1").triggers, {})

    def test_format_trigger_note(self):
        note = _format_trigger_note(
            {
                "amma jio": {
                    "instruction": "Keep the WhatsApp chat context in mind.",
                    "interval_seconds": 300,
                    "enabled": True,
                    "next_run_at": None,
                }
            }
        )

        self.assertIn("Live triggers active", note)
        self.assertIn("amma jio", note)
        self.assertIn("Keep the WhatsApp chat context in mind.", note)
        self.assertIn("running every 5m", note)

    @patch("telly.app._kickoff")
    def test_run_due_triggers_dispatches_background_trigger(self, kickoff):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            store = ThreadStore(Path(tmp))
            store.update(
                "T1",
                channel="C1",
                user="U1",
                status="complete",
                triggers={
                    "amma jio": {
                        "instruction": "Check latest WhatsApp message and reply if needed.",
                        "interval_seconds": 300,
                        "enabled": True,
                        "next_run_at": 0,
                    }
                },
            )
            cfg = Config(
                slack_bot_token="x",
                slack_app_token="y",
                slack_signing_secret="z",
                main_agent_cmd="opencode",
                tmux_session_prefix="slacktask",
                telly_home=Path(tmp),
                allowed_slack_user_ids=frozenset(),
            )

            _run_due_triggers(cfg, store, object())

            kickoff.assert_called_once()
            self.assertTrue(kickoff.call_args.kwargs["background"])
            self.assertIn("Scheduled trigger: amma jio", kickoff.call_args.args[7])
            self.assertGreater(store.get("T1").triggers["amma jio"]["next_run_at"], 0)


if __name__ == "__main__":
    unittest.main()
