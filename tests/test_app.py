import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from telly.app import _kickoff
from telly.config import Config
from telly.state import ThreadStore


class AppKickoffTest(unittest.TestCase):
    def test_kickoff_submits_prompt_without_live_browser_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                slack_bot_token="x",
                slack_app_token="x",
                slack_signing_secret="x",
                main_agent_cmd="opencode",
                tmux_session_prefix="slacktask",
                telly_home=Path(tmp),
                allowed_slack_user_ids=frozenset(),
            )
            store = ThreadStore(Path(tmp))
            client = Mock()

            with patch("telly.app._executor.submit") as submit:
                _kickoff(cfg, store, client, "C123", "T123", "M123", "U123", "visit example.com")

            state = store.get("T123")
            self.assertIsNotNone(state)
            # The live-feed feature is gone; no stream id is tracked.
            self.assertFalse(hasattr(state, "video_stream_id"))
            self.assertTrue(submit.called)

            args = submit.call_args.args
            submitted_prompt = args[8] if len(args) > 8 else submit.call_args.kwargs.get("text")
            self.assertEqual(submitted_prompt, "visit example.com")


if __name__ == "__main__":
    unittest.main()
