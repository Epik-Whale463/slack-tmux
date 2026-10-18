import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from telly import telegram
from telly.config import Config


def _bare_bot() -> telegram.TelegramBot:
    return object.__new__(telegram.TelegramBot)


class BotHelpersTest(unittest.TestCase):
    def test_thread_key_includes_topic(self):
        bot = _bare_bot()
        self.assertEqual(bot._thread_key(42, None), "tg:42")
        self.assertEqual(bot._thread_key(42, 7), "tg:42:7")

    def test_chat_id_round_trip(self):
        self.assertEqual(telegram.TelegramBot._chat_id_from_key("tg:42:7"), "42")
        self.assertEqual(telegram.TelegramBot._chat_id_from_key("tg:42"), "42")
        self.assertIsNone(telegram.TelegramBot._chat_id_from_key("slack:1"))

    def test_interval_parsing(self):
        self.assertEqual(telegram._parse_interval_seconds("5m"), 300)
        self.assertEqual(telegram._parse_interval_seconds("2h"), 7200)
        self.assertEqual(telegram._parse_interval_seconds("30s"), 30)
        self.assertIsNone(telegram._parse_interval_seconds("soon"))
        self.assertEqual(telegram._format_interval(300), "5m")
        self.assertEqual(telegram._format_interval(7200), "2h")
        self.assertEqual(telegram._format_interval(45), "45s")

    def test_safe_filename(self):
        self.assertEqual(telegram.TelegramBot._safe_filename("my notes/../x?.md"), "my_notes_.._x_.md")

    def test_extract_uploads(self):
        bot = _bare_bot()
        text, paths = bot._extract_uploads("Answer body\nSLACK_UPLOAD: /tmp/report.md")
        self.assertEqual(text, "Answer body")
        self.assertEqual([str(p) for p in paths], ["/tmp/report.md"])

    def test_build_prompt_includes_files_and_reply_context(self):
        bot = _bare_bot()
        prompt = bot._build_prompt(
            "do it",
            ["/tmp/a.png"],
            {"reply_to_message": {"text": "earlier message"}},
        )
        self.assertIn("do it", prompt)
        self.assertIn("/tmp/a.png", prompt)
        self.assertIn("earlier message", prompt)


class ClientTest(unittest.TestCase):
    def _client(self):
        client = telegram.TelegramClient("123:abc")
        client.session = Mock()
        return client

    def test_get_updates_passes_long_poll_timeout_and_prunes_none(self):
        client = self._client()
        captured = {}

        def fake_post(url, json=None, data=None, files=None, timeout=None):
            captured["json"] = json
            captured["url"] = url
            return Mock(json=lambda: {"ok": True, "result": [{"update_id": 1}]})

        client.session.post.side_effect = fake_post

        updates = client.get_updates(None, timeout=25)

        self.assertEqual(updates, [{"update_id": 1}])
        self.assertIn("/getUpdates", captured["url"])
        self.assertEqual(captured["json"]["timeout"], 25)
        self.assertNotIn("offset", captured["json"])  # None pruned
        self.assertEqual(captured["json"]["allowed_updates"], ["message", "edited_message", "callback_query"])

    def test_send_message_sets_html_and_reply(self):
        client = self._client()
        captured = {}

        def fake_post(url, json=None, data=None, files=None, timeout=None):
            captured["json"] = json
            return Mock(json=lambda: {"ok": True, "result": {"message_id": 5}})

        client.session.post.side_effect = fake_post

        res = client.send_message(10, "<b>hi</b>", reply_to_message_id=3, reply_markup={"inline_keyboard": []})

        self.assertEqual(res["message_id"], 5)
        self.assertEqual(captured["json"]["parse_mode"], "HTML")
        self.assertEqual(captured["json"]["reply_parameters"], {"message_id": 3})
        self.assertTrue(captured["json"]["link_preview_options"]["is_disabled"])


class BotInitTest(unittest.TestCase):
    def test_uses_separate_store_file_from_slack(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                slack_bot_token="",
                slack_app_token="",
                slack_signing_secret="",
                main_agent_cmd="opencode",
                tmux_session_prefix="x",
                telly_home=Path(tmp),
                allowed_slack_user_ids=frozenset(),
                telegram_bot_token="1:a",
            )
            bot = telegram.TelegramBot(cfg)

            self.assertEqual(bot.store.path.name, "telegram-threads.json")
            self.assertTrue(bot.store.path.parent == Path(tmp))


if __name__ == "__main__":
    unittest.main()
