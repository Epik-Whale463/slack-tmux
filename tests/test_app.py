import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from telly.app import _kickoff
from telly.config import Config
from telly.state import ThreadStore


class AppBrowserVideoTest(unittest.TestCase):
    def test_kickoff_reuses_same_stream_id_for_instruction_and_state(self):
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
            self.assertIsNotNone(state.video_stream_id)
            self.assertTrue(submit.called)

            # kwargs form: prompt is positional arg index 8 (text/prompt)
            args = submit.call_args.args
            kwargs = submit.call_args.kwargs
            submitted_prompt = args[8] if len(args) > 8 else kwargs.get("text")
            # _executor.submit(_handle_brain_turn, cfg, store, client, channel, thread_ts, msg_ts, user_id, prompt, ...)
            self.assertIsNotNone(submitted_prompt)
            self.assertIn(f"/stream/{state.video_stream_id}", submitted_prompt)
            self.assertIn("Live Browser Sharing", submitted_prompt)
            self.assertIn("Do not call `browser_start_video`", submitted_prompt)


if __name__ == "__main__":
    unittest.main()
