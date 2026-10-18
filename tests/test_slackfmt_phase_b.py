import json
import unittest

from telly import slackfmt


class MarkdownBlockTest(unittest.TestCase):
    def test_build_blocks_uses_markdown_block_by_default(self):
        blocks = slackfmt.build_blocks("**Hello** world")
        self.assertEqual(blocks[0]["type"], "markdown")
        self.assertEqual(blocks[0]["text"], "**Hello** world")

    def test_build_blocks_fallback_uses_section_mrkdwn(self):
        blocks = slackfmt.build_blocks("**Hello**", use_markdown_block=False)
        self.assertEqual(blocks[0]["type"], "section")
        self.assertEqual(blocks[0]["text"]["type"], "mrkdwn")
        self.assertIn("*Hello*", blocks[0]["text"]["text"])


class TableExtractionTest(unittest.TestCase):
    def test_extracts_fenced_slack_table(self):
        text = (
            "Results:\n"
            "```slack-table\n"
            '{"headers":["Name","Score"],"rows":[["Ada",10],["Bob",7]]}\n'
            "```\n"
            "Done"
        )
        cleaned, blocks = slackfmt.extract_table_blocks(text)
        self.assertEqual(cleaned, "Results:\n\nDone")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual(blocks[0]["rows"][0][0]["text"], "Name")
        self.assertEqual(blocks[0]["rows"][1][0]["text"], "Ada")
        self.assertEqual(blocks[0]["rows"][1][1]["text"], "10")

    def test_extracts_markdown_pipe_table(self):
        text = (
            "Here:\n"
            "| File | Status |\n"
            "| --- | --- |\n"
            "| a.py | ok |\n"
            "| b.py | fail |\n"
            "\nThanks"
        )
        cleaned, blocks = slackfmt.extract_table_blocks(text)
        self.assertIn("Here:", cleaned)
        self.assertIn("Thanks", cleaned)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual(len(blocks[0]["rows"]), 3)
        self.assertEqual(blocks[0]["rows"][2][0]["text"], "b.py")

    def test_keeps_invalid_fenced_table_visible(self):
        source = "```slack-table\n{}\n```"
        cleaned, blocks = slackfmt.extract_table_blocks(source)
        self.assertEqual(cleaned, source)
        self.assertEqual(blocks, [])


class PlanExtractionTest(unittest.TestCase):
    def test_extracts_fenced_slack_plan(self):
        text = (
            "Plan:\n"
            "```slack-plan\n"
            '{"title":"Ship it","tasks":['
            '{"task_id":"t1","title":"Tests","status":"complete"},'
            '{"task_id":"t2","title":"Deploy","status":"pending"}'
            "]}\n"
            "```"
        )
        cleaned, blocks = slackfmt.extract_plan_blocks(text)
        self.assertEqual(cleaned, "Plan:")
        self.assertEqual(blocks[0]["type"], "plan")
        self.assertEqual(blocks[0]["title"], "Ship it")
        self.assertEqual(blocks[0]["tasks"][0]["status"], "complete")
        self.assertEqual(blocks[0]["tasks"][1]["title"], "Deploy")


class NativeContentPipelineTest(unittest.TestCase):
    def test_extract_native_content_combines_plan_table_viz(self):
        text = (
            "Summary\n"
            "```slack-plan\n"
            '{"title":"Work","tasks":[{"task_id":"1","title":"A","status":"complete"}]}\n'
            "```\n"
            "| Col |\n"
            "| --- |\n"
            "| 1 |\n"
            "```slack-visualization\n"
            '{"type":"data_visualization","title":"Candy","chart":{"type":"pie","segments":[{"label":"A","value":1}]}}\n'
            "```"
        )
        cleaned, blocks = slackfmt.extract_native_content(text)
        types = [b["type"] for b in blocks]
        self.assertEqual(cleaned, "Summary")
        self.assertIn("data_visualization", types)
        self.assertIn("plan", types)
        self.assertIn("table", types)


class TaskSourcesTest(unittest.TestCase):
    def test_collects_browser_and_fetch_urls(self):
        sources = slackfmt.collect_task_sources(
            [
                {"name": "WebFetch", "input": {"url": "https://example.com/docs"}},
                {"name": "playwright_browser_navigate", "input": {"url": "https://app.local/login"}},
            ],
        )
        urls = {s["url"] for s in sources}
        self.assertIn("https://example.com/docs", urls)
        self.assertIn("https://app.local/login", urls)

    def test_activity_blocks_include_sources_and_file_paths(self):
        blocks = slackfmt.build_activity_blocks(
            "123.456",
            [
                {"name": "Edit", "input": {"file_path": "/tmp/app.py"}, "status": "running"},
                {"name": "WebFetch", "input": {"url": "https://example.com"}, "status": "done"},
            ],
        )
        work = blocks[0]["tasks"][1]
        self.assertIn("sources", work)
        urls = {s["url"] for s in work["sources"]}
        self.assertIn("https://example.com", urls)
        self.assertIn("/tmp/app.py", str(work["details"]))


class FeedbackModalTest(unittest.TestCase):
    def test_build_feedback_modal_shape(self):
        view = slackfmt.build_feedback_modal(thread_ts="1.2", channel="C1", message_ts="3.4")
        self.assertEqual(view["type"], "modal")
        self.assertEqual(view["callback_id"], "telly_feedback_modal")
        meta = json.loads(view["private_metadata"])
        self.assertEqual(meta["thread_ts"], "1.2")
        self.assertEqual(meta["channel"], "C1")
        block_ids = [b["block_id"] for b in view["blocks"]]
        self.assertIn("feedback_category", block_ids)
        self.assertIn("feedback_notes", block_ids)

    def test_parse_feedback_modal_submission(self):
        view = {
            "private_metadata": json.dumps({"thread_ts": "9.9", "channel": "C9", "message_ts": "8.8"}),
            "state": {
                "values": {
                    "feedback_category": {
                        "category": {"selected_option": {"value": "too_slow"}},
                    },
                    "feedback_notes": {"notes": {"value": " took forever "}},
                }
            },
        }
        parsed = slackfmt.parse_feedback_modal_submission(view)
        self.assertEqual(parsed["category"], "too_slow")
        self.assertEqual(parsed["notes"], "took forever")
        self.assertEqual(parsed["thread_ts"], "9.9")


class ContentBlocksPlacementTest(unittest.TestCase):
    def test_content_blocks_before_actions(self):
        table = {"type": "table", "rows": [[{"type": "raw_text", "text": "A"}]]}
        actions = slackfmt.build_actions_block("1.2")
        blocks = slackfmt.build_blocks("See table", content_blocks=[table], actions_block=actions)
        self.assertEqual(blocks[0]["type"], "markdown")
        self.assertEqual(blocks[1]["type"], "table")
        self.assertEqual(blocks[2]["type"], "actions")


if __name__ == "__main__":
    unittest.main()
