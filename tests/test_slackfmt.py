import unittest

from telly import slackfmt


class SlackFormatTest(unittest.TestCase):
    def test_extracts_native_pie_visualization(self):
        text, blocks = slackfmt.extract_visualization_blocks(
            "Chart below\n"
            "```slack-visualization\n"
            '{"type":"data_visualization","title":"Candy","chart":{"type":"pie","segments":[{"label":"Kit Kat","value":45}]}}\n'
            "```\n"
            "Done"
        )

        self.assertEqual(text, "Chart below\n\nDone")
        self.assertEqual(blocks[0]["type"], "data_visualization")
        self.assertEqual(blocks[0]["chart"]["type"], "pie")

    def test_keeps_invalid_visualization_visible(self):
        source = "```slack-visualization\n{}\n```"

        text, blocks = slackfmt.extract_visualization_blocks(source)

        self.assertEqual(text, source)
        self.assertEqual(blocks, [])

    def test_build_blocks_includes_visualization_before_actions(self):
        visualization = {"type": "data_visualization", "title": "Candy", "chart": {"type": "pie", "segments": [{"label": "A", "value": 1}]}}
        actions = slackfmt.build_actions_block("123.456")

        blocks = slackfmt.build_blocks("See chart", visualization_blocks=[visualization], actions_block=actions)

        self.assertEqual(blocks[0]["type"], "markdown")
        self.assertEqual(blocks[1], visualization)
        self.assertEqual(blocks[2]["type"], "actions")

    def test_activity_blocks_show_phase_tool_and_partial_reply(self):
        blocks = slackfmt.build_activity_blocks(
            "123.456",
            [{"name": "Edit", "input": {"file_path": "/tmp/app.py"}, "status": "running"}],
            "Drafting answer",
        )

        rendered = str(blocks)
        self.assertEqual(blocks[0]["type"], "plan")
        self.assertIn("editing files", rendered)
        self.assertIn("edit `/tmp/app.py`", rendered)
        self.assertIn("Drafting answer", rendered)
        self.assertEqual(blocks[-1]["type"], "actions")


if __name__ == "__main__":
    unittest.main()
