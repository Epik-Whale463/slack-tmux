import unittest

from telly.browser import _default_profile_dir, _load_steps, _page_title, _profile_dir, _text_from_dom, _viewport, build_parser, capability_text


class BrowserTest(unittest.TestCase):
    def test_text_from_dom_removes_markup_and_scripts(self):
        dom = "<html><head><script>bad()</script></head><body><h1>Hello</h1><p>A &amp; B</p></body></html>"

        self.assertEqual(_text_from_dom(dom), "Hello\nA & B")

    def test_page_title(self):
        self.assertEqual(_page_title("<title> Hello   world </title>"), "Hello world")

    def test_capabilities_mentions_cli(self):
        self.assertIn("-m telly.browser", capability_text())

    def test_parser_accepts_screenshot_output(self):
        args = build_parser().parse_args(["screenshot", "https://example.com", "--output", "out.png"])

        self.assertEqual(args.command, "screenshot")
        self.assertEqual(args.output, "out.png")

    def test_parser_defaults_to_persistent_profile(self):
        args = build_parser().parse_args(["text", "https://example.com"])

        self.assertEqual(_profile_dir(args), _default_profile_dir())
        self.assertTrue(str(_profile_dir(args)).endswith(".telly/chrome-profile"))
        self.assertEqual(args.profile_name, "Default")

    def test_parser_accepts_headed_and_ephemeral(self):
        args = build_parser().parse_args(["--headed", "--ephemeral", "text", "https://example.com"])

        self.assertTrue(args.headed)
        self.assertIsNone(_profile_dir(args))

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

    def test_viewport_parsing(self):
        self.assertEqual(_viewport("390,844"), {"width": 390, "height": 844})


if __name__ == "__main__":
    unittest.main()
