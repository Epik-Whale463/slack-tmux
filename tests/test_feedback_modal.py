import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from telly import slackfmt
from telly.state import ThreadStore


class FeedbackModalAppTest(unittest.TestCase):
    def test_negative_feedback_modal_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThreadStore(Path(tmp))
            client = Mock()
            thread_ts = "9.0"
            channel = "C1"
            message_ts = "10.0"
            value = f"negative:{thread_ts}"

            store.log_feedback({
                "user": "U1",
                "value": value,
                "polarity": "negative",
                "thread_ts": thread_ts,
                "message_ts": message_ts,
            })
            client.views_open(
                trigger_id="trig-1",
                view=slackfmt.build_feedback_modal(
                    thread_ts=thread_ts,
                    channel=channel,
                    message_ts=message_ts,
                ),
            )
            client.views_open.assert_called_once()
            view = client.views_open.call_args.kwargs["view"]
            self.assertEqual(view["callback_id"], "telly_feedback_modal")
            meta = json.loads(view["private_metadata"])
            self.assertEqual(meta["thread_ts"], thread_ts)
            self.assertTrue(store.feedback_path.exists())


if __name__ == "__main__":
    unittest.main()