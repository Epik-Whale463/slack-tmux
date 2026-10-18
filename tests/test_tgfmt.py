import unittest

from telly import tgfmt


class MarkdownTest(unittest.TestCase):
    def test_inline_formatting(self):
        html = tgfmt.markdown_to_html("**bold** and *italic* and `code` and ~~gone~~")
        self.assertEqual(html, "<b>bold</b> and <i>italic</i> and <code>code</code> and <s>gone</s>")

    def test_escapes_html(self):
        self.assertEqual(tgfmt.markdown_to_html("a < b & c > d"), "a &lt; b &amp; c &gt; d")

    def test_fenced_code_block(self):
        html = tgfmt.markdown_to_html("```python\nprint(1)\n```")
        self.assertIn('class="language-python"', html)
        self.assertIn("print(1)", html)

    def test_asterisk_in_inline_code_is_not_italicised(self):
        self.assertEqual(tgfmt.markdown_to_html("`a*b*c`"), "<code>a*b*c</code>")

    def test_link_and_heading(self):
        self.assertEqual(tgfmt.markdown_to_html("[x](https://e.com)"), '<a href="https://e.com">x</a>')
        self.assertEqual(tgfmt.markdown_to_html("# Title"), "<b>Title</b>")

    def test_blockquote(self):
        self.assertEqual(tgfmt.markdown_to_html("> hello"), "<blockquote>hello</blockquote>")

    def test_bullet_list(self):
        self.assertEqual(tgfmt.markdown_to_html("- one"), "\u2022 one")


class ReplyRenderTest(unittest.TestCase):
    def test_markdown_answer_becomes_html(self):
        result = tgfmt.render_reply("Hello **world**")
        self.assertEqual(result.html_chunks, ["Hello <b>world</b>"])
        self.assertEqual(result.media, [])

    def test_table_fence_renders_as_pre(self):
        reply = (
            "Data:\n```slack-table\n"
            '{"headers":["Name","Score"],"rows":[["Ada",10],["Bob",7]]}\n```'
        )
        result = tgfmt.render_reply(reply)
        joined = "\n".join(result.html_chunks)
        self.assertIn("<pre>", joined)
        self.assertIn("Ada", joined)
        self.assertIn("Data:", joined)

    def test_plan_fence_renders_checklist(self):
        reply = (
            "```slack-plan\n"
            '{"title":"Ship","tasks":[{"task_id":"1","title":"Tests","status":"complete"},'
            '{"task_id":"2","title":"Deploy","status":"in_progress"}]}\n```'
        )
        joined = "\n".join(tgfmt.render_reply(reply).html_chunks)
        self.assertIn("Ship", joined)
        self.assertIn("\u2713 Tests", joined)  # ✓
        self.assertIn("\u25d0 Deploy", joined)  # ◐

    def test_chart_fence_renders_data_table(self):
        reply = (
            "```slack-visualization\n"
            '{"type":"data_visualization","title":"Candy","chart":{"type":"pie",'
            '"segments":[{"label":"A","value":3},{"label":"B","value":1}]}}\n```'
        )
        joined = "\n".join(tgfmt.render_reply(reply).html_chunks)
        self.assertIn("Candy", joined)
        self.assertIn("75%", joined)
        self.assertIn("<pre>", joined)

    def test_image_fence_becomes_photo_media(self):
        reply = '```slack-image\n{"image_url":"https://x.test/p.png","alt_text":"pic"}\n```'
        result = tgfmt.render_reply(reply)
        self.assertEqual(len(result.media), 1)
        self.assertEqual(result.media[0].kind, "photo")
        self.assertEqual(result.media[0].url, "https://x.test/p.png")

    def test_alert_fence_gets_level_icon(self):
        reply = '```slack-alert\n{"text":"careful","level":"warning"}\n```'
        joined = "\n".join(tgfmt.render_reply(reply).html_chunks)
        self.assertIn("\u26a0", joined)
        self.assertIn("careful", joined)

    def test_very_long_answer_is_split_into_chunks(self):
        result = tgfmt.render_reply("word " * 4000, limit=500)
        self.assertGreater(len(result.html_chunks), 1)
        for chunk in result.html_chunks:
            self.assertLessEqual(len(chunk), 500)


class ActivityTest(unittest.TestCase):
    def test_activity_lists_tool_steps(self):
        html = tgfmt.render_activity([
            {"name": "Read", "input": {"file_path": "/tmp/a.py"}, "status": "done"},
            {"name": "Bash", "input": {"command": "pytest"}, "status": "running"},
        ])
        self.assertIn("\u2713", html)  # done glyph
        self.assertIn("a.py", html)
        self.assertIn("pytest", html)

    def test_activity_shows_partial_text_when_no_tools(self):
        html = tgfmt.render_activity([], "thinking about it")
        self.assertIn("thinking about it", html)


class KeyboardTest(unittest.TestCase):
    def test_approve_deny_callback_data(self):
        kb = tgfmt.approve_deny_keyboard("tg:42")
        buttons = kb["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], "confirm:approve:tg:42")
        self.assertEqual(buttons[1]["callback_data"], "confirm:deny:tg:42")

    def test_feedback_callback_data(self):
        kb = tgfmt.feedback_keyboard("tg:42")
        datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
        self.assertIn("fb:up:tg:42", datas)
        self.assertIn("fb:down:tg:42", datas)
        self.assertIn("regen:tg:42", datas)
        self.assertIn("fork:tg:42", datas)

    def test_prompts_keyboard_indexes(self):
        kb = tgfmt.prompts_keyboard([{"title": "A", "message": "a"}, {"title": "B", "message": "b"}])
        self.assertEqual(kb["inline_keyboard"][1][0]["callback_data"], "prompt:1")


if __name__ == "__main__":
    unittest.main()
