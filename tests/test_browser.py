from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from telly.browser import DEFAULT_REMOTE_DEBUGGING_PORT, BrowserError, _default_profile_dir, _ensure_profile_available, _list_sessions, _load_steps, _page_title, _profile_dir, _session_profile_dir, _text_from_dom, _viewport, build_parser, capability_text, playwright_mcp_config, remote_debugging_port


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
        with tempfile.TemporaryDirectory() as tmp, patch("telly.browser.MCP_OUTPUT_DIR", tmp):
            config = playwright_mcp_config("opencode")
            self.assertEqual(config["type"], "local")
            self.assertEqual(config["command"][:3], ["npx", "-y", "@playwright/mcp@latest"])
            self.assertIn("--config", config["command"])
            self.assertNotIn(f"--args=--remote-debugging-port={DEFAULT_REMOTE_DEBUGGING_PORT}", config["command"])
            config_path = Path(config["command"][config["command"].index("--config") + 1])
            mcp_config = json.loads(config_path.read_text())
            self.assertIn(f"--remote-debugging-port={DEFAULT_REMOTE_DEBUGGING_PORT}", mcp_config["browser"]["launchOptions"]["args"])
            self.assertEqual(mcp_config["capabilities"], ["core", "pdf", "devtools"])

    def test_playwright_mcp_config_for_claude(self):
        with tempfile.TemporaryDirectory() as tmp, patch("telly.browser.MCP_OUTPUT_DIR", tmp):
            config = playwright_mcp_config("claude")
            self.assertEqual(config["command"], "npx")
            self.assertEqual(config["args"][:2], ["-y", "@playwright/mcp@latest"])
            self.assertIn("--config", config["args"])

    def test_remote_debugging_port_env_override(self):
        with patch.dict(os.environ, {"TELLY_BROWSER_REMOTE_DEBUGGING_PORT": "9333"}):
            self.assertEqual(remote_debugging_port(), 9333)

        with patch.dict(os.environ, {"TELLY_BROWSER_REMOTE_DEBUGGING_PORT": "off"}):
            self.assertIsNone(remote_debugging_port())

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
