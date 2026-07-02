import unittest

from telly.app import _fetch_discussion_context, _wants_discussion_context


class FakeSlackClient:
    def __init__(self):
        self.replies_called = False
        self.history_called = False

    def conversations_replies(self, **kwargs):
        self.replies_called = True
        return {
            "messages": [
                {"ts": "1", "user": "U1", "text": "We should use the smaller context window."},
                {"ts": "2", "user": "U2", "text": "Agreed, load history only on demand."},
            ]
        }

    def conversations_history(self, **kwargs):
        self.history_called = True
        return {"messages": [{"ts": "3", "user": "U3", "text": "Based on our discussion, what do you think?"}]}


class DiscussionContextTest(unittest.TestCase):
    def test_wants_discussion_context_for_prior_discussion_prompt(self):
        self.assertTrue(_wants_discussion_context("Based on our discussion, what do you think?"))

    def test_does_not_want_discussion_context_for_direct_command(self):
        self.assertFalse(_wants_discussion_context("Run the tests and tell me what failed."))

    def test_fetch_discussion_context_formats_thread_and_channel_messages(self):
        client = FakeSlackClient()

        context = _fetch_discussion_context(client, "C1", "1", "3")

        self.assertTrue(client.replies_called)
        self.assertTrue(client.history_called)
        self.assertIn("Slack discussion context loaded", context)
        self.assertIn("<@U1>: We should use the smaller context window.", context)
        self.assertIn("<@U3> (current message): Based on our discussion", context)


if __name__ == "__main__":
    unittest.main()
