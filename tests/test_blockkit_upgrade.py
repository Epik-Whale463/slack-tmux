import unittest

from telly import blockkit_catalog, slackfmt


class CatalogTest(unittest.TestCase):
    def test_catalog_lists_all_core_blocks(self):
        types = set(blockkit_catalog.all_block_types())
        for required in (
            "actions", "alert", "card", "carousel", "context", "context_actions",
            "data_visualization", "divider", "header", "image", "input", "markdown",
            "plan", "rich_text", "section", "table", "task_card", "video",
        ):
            self.assertIn(required, types)

    def test_catalog_lists_core_elements(self):
        types = set(blockkit_catalog.all_element_types())
        for required in (
            "button", "feedback_buttons", "icon_button", "overflow", "static_select",
            "plain_text_input", "checkboxes", "radio_buttons", "datepicker",
        ):
            self.assertIn(required, types)


class NewBlockExtractionTest(unittest.TestCase):
    def test_alert_fence(self):
        text = 'Note\n```slack-alert\n{"text":"Disk almost full","level":"warning"}\n```'
        cleaned, blocks = slackfmt.extract_alert_blocks(text)
        self.assertEqual(cleaned, "Note")
        self.assertEqual(blocks[0]["type"], "alert")
        self.assertEqual(blocks[0]["level"], "warning")

    def test_card_and_carousel(self):
        text = (
            '```slack-carousel\n'
            '{"elements":[{"title":"A","body":"one"},{"title":"B","body":"two"}]}\n'
            "```"
        )
        cleaned, blocks = slackfmt.extract_carousel_blocks(text)
        self.assertEqual(cleaned, "")
        self.assertEqual(blocks[0]["type"], "carousel")
        self.assertEqual(len(blocks[0]["elements"]), 2)

    def test_image_fence_and_markdown_image(self):
        text = (
            'Shot:\n'
            '```slack-image\n{"image_url":"https://example.com/a.png","alt_text":"A"}\n```\n'
            "![B](https://example.com/b.png)\n"
            "done"
        )
        cleaned, blocks = slackfmt.extract_native_content(text)
        types = [b["type"] for b in blocks]
        self.assertIn("image", types)
        self.assertEqual(types.count("image"), 2)
        self.assertIn("done", cleaned)
        self.assertNotIn("example.com/a.png", cleaned)

    def test_video_fence(self):
        text = (
            '```slack-video\n'
            '{"video_url":"https://example.com/embed/1","thumbnail_url":"https://example.com/t.jpg","title":"Demo"}\n'
            "```"
        )
        cleaned, blocks = slackfmt.extract_video_blocks(text)
        self.assertEqual(blocks[0]["type"], "video")
        self.assertEqual(blocks[0]["title"]["text"], "Demo")

    def test_generic_passthrough_block(self):
        text = '```slack-block\n{"type":"divider"}\n```'
        cleaned, blocks = slackfmt.extract_generic_blocks(text)
        self.assertEqual(blocks[0]["type"], "divider")


class UiBuildersTest(unittest.TestCase):
    def test_error_blocks_are_message_safe(self):
        blocks = slackfmt.build_error_blocks("1.2", "boom")
        # alert is modal-only; messages use section stand-in
        self.assertEqual(blocks[0]["type"], "section")
        self.assertNotIn("alert", {b["type"] for b in blocks})
        safe = slackfmt.sanitize_blocks_for_messages(blocks)
        self.assertTrue(all(b.get("type") != "alert" for b in safe))

    def test_feedback_includes_icon_button(self):
        block = slackfmt.build_feedback_block("1.2")
        types = [e["type"] for e in block["elements"]]
        self.assertIn("feedback_buttons", types)
        self.assertIn("icon_button", types)

    def test_actions_include_overflow(self):
        block = slackfmt.build_actions_block("1.2")
        types = [e["type"] for e in block["elements"]]
        self.assertIn("button", types)
        self.assertIn("overflow", types)

    def test_home_view_uses_reliable_sections(self):
        class T:
            thread_ts = "1.2"
            last_message = "hello"
            status = "complete"
            agent_cmd = "claude"
            session_id = "abc12345"
            video_stream_id = None

        view = slackfmt.build_home_view([T()])
        types = [b["type"] for b in view["blocks"]]
        self.assertIn("header", types)
        self.assertIn("section", types)
        self.assertIn("actions", types)
        self.assertNotIn("alert", types)

    def test_permission_blocks_have_approve_deny(self):
        blocks = slackfmt.build_permission_blocks("1.2", "git push origin main")
        actions = next(b for b in blocks if b["type"] == "actions")
        ids = [e["action_id"] for e in actions["elements"]]
        self.assertEqual(ids, ["telly_confirm_approve", "telly_confirm_deny"])

    def test_extract_confirm_marker(self):
        text = "About to push.\nSLACK_CONFIRM: git push origin main\n"
        cleaned, summary = slackfmt.extract_confirm_request(text)
        self.assertEqual(summary, "git push origin main")
        self.assertIn("About to push.", cleaned)
        self.assertNotIn("SLACK_CONFIRM", cleaned)

    def test_sanitize_strips_alert_from_messages(self):
        blocks = [slackfmt.build_alert_block("x", level="error"), {"type": "divider"}]
        safe = slackfmt.sanitize_blocks_for_messages(blocks)
        self.assertEqual(safe[0]["type"], "section")
        self.assertEqual(safe[1]["type"], "divider")

    def test_card_rejects_emoji_icon_silently(self):
        card = slackfmt.build_card_block(title="Hi", body="body text", icon=":robot_face:")
        self.assertNotIn("icon", card)
        self.assertEqual(card["type"], "card")


if __name__ == "__main__":
    unittest.main()
