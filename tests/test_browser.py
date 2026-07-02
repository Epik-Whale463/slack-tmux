from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from telly.browser import BrowserError, _default_profile_dir, _ensure_profile_available, _list_sessions, _load_steps, _page_title, _profile_dir, _session_profile_dir, _text_from_dom, _viewport, build_parser, capability_text, playwright_mcp_config


class BrowserTest(unittest.TestCase):
    def test_text_from_dom_removes_markup_and_scripts(self):
        dom = "<html><head><script>bad()</script></head><body><h1>Hello</h1><p>A &amp; B</p></body></html>"

        self.assertEqual(_text_from_dom(dom), "Hello\nA & B")

    def test_page_title(self):
        self.assertEqual(_page_title("<title> Hello   world </title>"), "Hello world")

    def test_capabilities_mentions_cli(self):
        self.assertIn("-m telly.browser", capability_text())

    def test_capabilities_mentions_mcp_first_and_cli_fallback(self):
        text = capability_text()

        self.assertIn("--session NAME", text)
        self.assertIn("telly.browser sessions", text)
        self.assertIn("user's normal Chrome profile", text)

    def test_playwright_mcp_config_for_opencode(self):
        config = playwright_mcp_config("opencode")

        self.assertEqual(config["type"], "local")
        self.assertEqual(config["command"][:3], ["npx", "-y", "@playwright/mcp@latest"])
        self.assertIn("--browser=chrome", config["command"])
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in config["command"]))

    def test_playwright_mcp_config_for_claude(self):
        config = playwright_mcp_config("claude")

        self.assertEqual(config["command"], "npx")
        self.assertEqual(config["args"][:2], ["-y", "@playwright/mcp@latest"])

    def test_parser_accepts_screenshot_output(self):
        args = build_parser().parse_args(["screenshot", "https://example.com", "--output", "out.png"])

        self.assertEqual(args.command, "screenshot")
        self.assertEqual(args.output, "out.png")

    def test_parser_defaults_to_persistent_profile(self):
        args = build_parser().parse_args(["text", "https://example.com"])

        self.assertEqual(_profile_dir(args), _default_profile_dir())
        self.assertTrue(str(_profile_dir(args)).endswith(".telly/browser-sessions/default"))
        self.assertEqual(args.profile_name, "Default")

    def test_parser_accepts_named_session(self):
        args = build_parser().parse_args(["--session", "linkedin", "text", "https://example.com"])

        self.assertEqual(_profile_dir(args), _session_profile_dir("linkedin"))

    def test_parser_rejects_profile_and_session_together(self):
        args = build_parser().parse_args([
            "--session",
            "linkedin",
            "--profile",
            "/tmp/custom-profile",
            "text",
            "https://example.com",
        ])

        with self.assertRaises(BrowserError):
            _profile_dir(args)

    def test_sessions_command_lists_named_sessions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TELLY_BROWSER_SESSION_ROOT": tmp}):
            Path(tmp, "linkedin").mkdir()
            Path(tmp, "whatsapp").mkdir()

            self.assertEqual([item["name"] for item in _list_sessions()], ["linkedin", "whatsapp"])

    def test_parser_accepts_headed_and_ephemeral(self):
        args = build_parser().parse_args(["--headed", "--ephemeral", "text", "https://example.com"])

        self.assertTrue(args.headed)
        self.assertIsNone(_profile_dir(args))

    def test_profile_lock_reports_reuse_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "SingletonLock").write_text("locked")

            with self.assertRaises(BrowserError) as ctx:
                _ensure_profile_available(profile)

        self.assertIn("Reuse the active browser session", str(ctx.exception))

    def test_parser_accepts_script_steps(self):
        args = build_parser().parse_args([
            "script",
            "https://example.com",
            "--steps",
            '[{"action":"text"}]',
            "--storage-state",
            "state.json",
        ])

        self.assertEqual(_load_steps(args), [{"action": "text"}])
        self.assertEqual(args.storage_state, "state.json")

    def test_parser_accepts_login_success_checks(self):
        args = build_parser().parse_args([
            "--headed",
            "login",
            "https://www.linkedin.com/messaging/",
            "--success-url",
            "/messaging",
            "--success-text",
            "Messaging",
        ])

        self.assertEqual(args.command, "login")
        self.assertTrue(args.headed)
        self.assertEqual(args.success_url, "/messaging")
        self.assertEqual(args.success_text, "Messaging")
        self.assertEqual(args.poll_ms, 1000)

    def test_viewport_parsing(self):
        self.assertEqual(_viewport("390,844"), {"width": 390, "height": 844})


if __name__ == "__main__":
    unittest.main()
