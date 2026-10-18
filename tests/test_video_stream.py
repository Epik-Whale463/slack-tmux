import unittest
from unittest.mock import Mock

from telly.video_stream import VideoStreamHTTPHandler


class VideoStreamTest(unittest.TestCase):
    def test_stream_html_uses_live_endpoint(self):
        handler = object.__new__(VideoStreamHTTPHandler)

        html = handler._build_stream_html("abc123", "recording", "Task", has_video=False)

        self.assertIn('/stream/abc123/live', html)
        self.assertIn('Live browser feed', html)
        self.assertIn('setInterval(refreshFeed, 400)', html)

    def test_live_route_ignores_query_string(self):
        handler = object.__new__(VideoStreamHTTPHandler)
        handler.path = "/stream/abc123/live?t=123"
        handler._serve_live_stream = Mock()
        handler._serve_stream_video = Mock()
        handler._serve_stream = Mock()
        handler._send_json = Mock()
        handler._send_error = Mock()

        handler.do_GET()

        handler._serve_live_stream.assert_called_once_with("abc123")
        handler._serve_stream_video.assert_not_called()
        handler._serve_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
