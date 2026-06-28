import unittest

from telly.brain import _parse_output


class ParseOutputTest(unittest.TestCase):
    def test_opencode_text_event(self):
        stdout = """{"type":"step_start","sessionID":"ses_123","part":{"type":"step-start","sessionID":"ses_123"}}
{"type":"text","sessionID":"ses_123","part":{"type":"text","text":"PONG","sessionID":"ses_123"}}
{"type":"step_finish","sessionID":"ses_123","part":{"type":"step-finish","sessionID":"ses_123","cost":0}}
"""

        state = _parse_output(stdout, duration_ms=1234)

        self.assertEqual(state["reply"], "PONG")
        self.assertEqual(state["session_id"], "ses_123")
        self.assertEqual(state["cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
