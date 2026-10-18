import unittest
from unittest.mock import MagicMock, Mock, patch

from telly.agent_stream import (
    AgentReplyStream,
    build_plan_seed_chunks,
    tool_event_to_task_chunk,
)


class ToolEventChunkTest(unittest.TestCase):
    def test_maps_running_edit_to_in_progress_task(self):
        chunk = tool_event_to_task_chunk(
            {"id": "tool_1", "name": "Edit", "input": {"file_path": "/tmp/a.py"}, "status": "running"}
        )
        self.assertEqual(chunk["type"], "task_update")
        self.assertEqual(chunk["status"], "in_progress")
        self.assertIn("a.py", chunk["title"])

    def test_includes_live_browser_source(self):
        chunk = tool_event_to_task_chunk(
            {"id": "t2", "name": "WebFetch", "input": {"url": "https://example.com"}, "status": "done"},
            live_browser_url="http://127.0.0.1:9999/stream/abc",
        )
        self.assertEqual(chunk["status"], "complete")
        urls = {s["url"] for s in chunk.get("sources", [])}
        self.assertIn("https://example.com", urls)
        self.assertIn("http://127.0.0.1:9999/stream/abc", urls)


class PlanSeedTest(unittest.TestCase):
    def test_seed_has_understand_and_work(self):
        chunks = build_plan_seed_chunks("123.456")
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["status"], "complete")
        self.assertEqual(chunks[1]["status"], "in_progress")


class AgentReplyStreamTest(unittest.TestCase):
    def test_start_uses_chat_stream_when_available(self):
        client = Mock()
        streamer = MagicMock()
        streamer._stream_ts = "S.1"
        streamer.append.return_value = {"ts": "S.1"}
        client.chat_stream.return_value = streamer
        client.chat_postMessage.return_value = {"ts": "stop.1"}

        session = AgentReplyStream(
            client=client,
            channel="C1",
            thread_ts="T1",
            user_id="U1",
            team_id="TM1",
        )
        ts = session.start()

        self.assertTrue(session.using_stream)
        self.assertEqual(ts, "S.1")
        client.chat_stream.assert_called_once()
        kwargs = client.chat_stream.call_args.kwargs
        self.assertEqual(kwargs["task_display_mode"], "plan")
        self.assertEqual(kwargs["recipient_user_id"], "U1")
        streamer.append.assert_called()
        # stop control message posted
        self.assertEqual(session.stop_control_ts, "stop.1")

    def test_start_falls_back_to_legacy_placeholder(self):
        client = Mock()
        client.chat_stream.side_effect = Exception("no stream")
        client.chat_postMessage.return_value = {"ts": "P.1"}

        session = AgentReplyStream(client=client, channel="C1", thread_ts="T1", user_id="U1")
        ts = session.start()

        self.assertFalse(session.using_stream)
        self.assertEqual(ts, "P.1")
        self.assertEqual(session.legacy_placeholder_ts, "P.1")

    def test_progress_appends_task_chunks(self):
        client = Mock()
        streamer = MagicMock()
        streamer._stream_ts = "S.1"
        streamer.append.return_value = {"ts": "S.1"}
        client.chat_stream.return_value = streamer
        client.chat_postMessage.return_value = {"ts": "stop.1"}

        session = AgentReplyStream(client=client, channel="C1", thread_ts="T1", user_id="U1")
        session.start()
        session._last_sent = 0  # force throttle open
        session.on_progress({
            "tool_events": [
                {"id": "x", "name": "Bash", "input": {"command": "ls"}, "status": "running"},
            ],
            "partial_text": "",
        })
        # first append is seed; later append is progress
        self.assertGreaterEqual(streamer.append.call_count, 2)
        last_kwargs = streamer.append.call_args.kwargs
        self.assertIn("chunks", last_kwargs)
        self.assertTrue(any(c.get("type") == "task_update" for c in last_kwargs["chunks"]))

    def test_finish_success_stops_stream_with_blocks(self):
        client = Mock()
        streamer = MagicMock()
        streamer._stream_ts = "S.1"
        streamer.append.return_value = {"ts": "S.1"}
        client.chat_stream.return_value = streamer
        client.chat_postMessage.return_value = {"ts": "stop.1"}

        session = AgentReplyStream(client=client, channel="C1", thread_ts="T1", user_id="U1")
        session.start()
        blocks = [{"type": "context_actions", "elements": []}]
        session.finish_success(
            markdown_text="**hello**",
            blocks=blocks,
            fallback_blocks=blocks,
            notify_text="hello",
            tool_events=[],
        )
        streamer.stop.assert_called_once()
        stop_kwargs = streamer.stop.call_args.kwargs
        self.assertEqual(stop_kwargs["markdown_text"], "**hello**")
        self.assertEqual(stop_kwargs["blocks"], blocks)
        # stop control deleted
        client.chat_delete.assert_called()


if __name__ == "__main__":
    unittest.main()
