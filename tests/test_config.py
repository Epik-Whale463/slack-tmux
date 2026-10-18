import os
import unittest
from unittest.mock import patch

from telly.config import Config


def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_") and k != "TELEGRAM_BOT_TOKEN"}
    env.update(overrides)
    return env


class ConfigBackendsTest(unittest.TestCase):
    def test_telegram_only(self):
        with patch.dict(os.environ, _clean_env(TELEGRAM_BOT_TOKEN="123:abc"), clear=True):
            cfg = Config.from_env()

        self.assertTrue(cfg.run_telegram)
        self.assertFalse(cfg.run_slack)

    def test_slack_only(self):
        with patch.dict(
            os.environ,
            _clean_env(SLACK_BOT_TOKEN="xoxb", SLACK_APP_TOKEN="xapp", SLACK_SIGNING_SECRET="sec"),
            clear=True,
        ):
            cfg = Config.from_env()

        self.assertTrue(cfg.run_slack)
        self.assertFalse(cfg.run_telegram)

    def test_partial_slack_config_raises(self):
        with patch.dict(os.environ, _clean_env(SLACK_BOT_TOKEN="xoxb"), clear=True):
            with self.assertRaises(RuntimeError):
                Config.from_env()

    def test_no_backend_raises(self):
        with patch.dict(os.environ, _clean_env(), clear=True):
            with self.assertRaises(RuntimeError):
                Config.from_env()

    def test_telegram_allowlists_parsed(self):
        with patch.dict(
            os.environ,
            _clean_env(
                TELEGRAM_BOT_TOKEN="1:a",
                ALLOWED_TELEGRAM_USER_IDS="111, 222",
                ALLOWED_TELEGRAM_CHAT_IDS="-100999",
            ),
            clear=True,
        ):
            cfg = Config.from_env()

        self.assertEqual(cfg.allowed_telegram_user_ids, frozenset({"111", "222"}))
        self.assertEqual(cfg.allowed_telegram_chat_ids, frozenset({"-100999"}))


if __name__ == "__main__":
    unittest.main()
