import tempfile
import unittest
from pathlib import Path

from telly.state import ThreadStore


class ThreadStoreTest(unittest.TestCase):
    def test_persists_session_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThreadStore(Path(tmp))
            store.update("111.222", channel="C1", user="U1", session_id="ses_123")

            reloaded = ThreadStore(Path(tmp))

            self.assertEqual(reloaded.session_map(), {"111.222": "ses_123"})

    def test_latest_for_channel_user_returns_recent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ThreadStore(Path(tmp))
            store.update("old", channel="D1", user="U1", session_id="ses_old")
            store.update("new", channel="D1", user="U1", session_id="ses_new")

            state = store.latest_for_channel_user("D1", "U1")

            self.assertIsNotNone(state)
            self.assertEqual(state.thread_ts, "new")


if __name__ == "__main__":
    unittest.main()
