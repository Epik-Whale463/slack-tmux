"""Live video streaming for browser automation tasks."""

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PLAYWRIGHT_OUTPUT_DIR = Path("~/.telly/playwright-mcp").expanduser().resolve()
STREAM_PORT = 9999
STREAM_HOST = "127.0.0.1"


class VideoStreamSession:
    """Manages a browser recording/streaming session using Playwright MCP."""

    def __init__(self, stream_id: str, task_name: str = "browser-task"):
        self.stream_id = stream_id
        self.task_name = task_name
        self.video_filename = f"{stream_id}.mp4"
        self.video_path = PLAYWRIGHT_OUTPUT_DIR / self.video_filename
        self.status_file = PLAYWRIGHT_OUTPUT_DIR / f"{stream_id}.status.json"

    def start(self) -> None:
        """Start recording session."""
        PLAYWRIGHT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._write_status("recording", {"started_at": time.time()})
        log.info("Video stream session started: %s (will save to %s)", self.stream_id, self.video_filename)

    def stop(self) -> None:
        """Stop recording."""
        self._write_status("stopped", {"stopped_at": time.time()})
        log.info("Video stream session stopped: %s", self.stream_id)

    def get_url(self) -> str:
        """Get the URL to stream this video."""
        return f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{self.stream_id}"

    def get_video_filename(self) -> str:
        """Get the filename to use when calling browser_start_video."""
        return self.video_filename

    def cleanup(self, delay_s: float = 30.0) -> None:
        """Delete video file after a delay."""
        def _cleanup():
            time.sleep(delay_s)
            for f in [self.video_path, self.status_file]:
                if f.exists():
                    try:
                        f.unlink()
                        log.info("Cleaned up video file: %s", f.name)
                    except Exception as e:
                        log.error("Failed to cleanup video file %s: %s", f.name, e)

        threading.Thread(target=_cleanup, daemon=True).start()

    def _write_status(self, state: str, meta: dict) -> None:
        """Write session status file."""
        status = {
            "stream_id": self.stream_id,
            "task_name": self.task_name,
            "video_filename": self.video_filename,
            "state": state,
            "meta": meta,
        }
        try:
            self.status_file.write_text(json.dumps(status, indent=2))
        except Exception as e:
            log.error("Failed to write status file: %s", e)


class VideoStreamHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for video streaming."""

    # Class-level dict to track active streams
    active_streams = {}

    def do_GET(self) -> None:
        """Handle GET requests for video streams."""
        if self.path.startswith("/stream/"):
            stream_id = self.path[8:].split("/")[0]
            self._serve_stream(stream_id)
        elif self.path == "/health":
            self._send_json({"status": "ok"})
        elif self.path == "/streams":
            self._send_json({"streams": list(self.active_streams.keys())})
        else:
            self._send_error(404, "Not found")

    def do_POST(self) -> None:
        """Handle POST requests for stream control."""
        if self.path == "/api/start":
            self._handle_start_stream()
        elif self.path == "/api/stop":
            self._handle_stop_stream()
        else:
            self._send_error(404, "Not found")

    def _serve_stream(self, stream_id: str) -> None:
        """Serve HTML page with video player for a stream."""
        status_file = PLAYWRIGHT_OUTPUT_DIR / f"{stream_id}.status.json"

        # Read status to find video filename
        status = {}
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
            except Exception:
                pass

        if not status:
            self._send_error(404, "Stream not found")
            return

        state = status.get("state", "unknown")
        task_name = status.get("task_name", "Browser Task")
        video_filename = status.get("video_filename", f"{stream_id}.mp4")
        video_path = PLAYWRIGHT_OUTPUT_DIR / video_filename

        # If video exists, serve it; otherwise serve HTML status page
        if video_path.exists():
            self._serve_file(video_path, "video/mp4")
        else:
            html = self._build_stream_html(stream_id, state, task_name, video_path.exists())
            self._send_html(html)

    def _build_stream_html(self, stream_id: str, state: str, task_name: str, has_video: bool) -> str:
        """Build HTML for stream status page."""
        video_url = f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}"

        status_html = {
            "recording": f'<div style="color: #ff6b6b; font-weight: bold;">🔴 Recording in progress...</div>',
            "stopped": f'<div style="color: #51cf66; font-weight: bold;">✅ Recording stopped</div>',
            "unknown": f'<div style="color: #ffd43b;">⏳ Status unknown</div>',
        }.get(state, '<div>Unknown state</div>')

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Live Browser Task - {task_name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #1a1a1a; color: #fff; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ margin-bottom: 20px; }}
        .status {{ margin: 20px 0; padding: 15px; background: #2a2a2a; border-radius: 8px; }}
        video {{ width: 100%; max-width: 100%; border-radius: 8px; background: #000; }}
        .info {{ margin-top: 20px; padding: 15px; background: #2a2a2a; border-radius: 8px; }}
        .live {{ animation: pulse 1.5s infinite; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.7; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 {task_name}</h1>
            <p>Live browser automation stream</p>
        </div>

        <div class="status">
            {status_html}
        </div>

        {'<video controls><source src="' + video_url + '" type="video/mp4">Your browser does not support the video tag.</video>' if has_video else '<p style="color: #999;">Video recording in progress... Refresh to see updates.</p>'}

        <div class="info">
            <h3>Stream ID: <code>{stream_id}</code></h3>
            <p>This stream will auto-delete after the task completes.</p>
        </div>

        <script>
            if (!"{has_video}".includes("True")) {{
                setTimeout(() => location.reload(), 2000);
            }}
        </script>
    </div>
</body>
</html>"""
        return html

    def _serve_file(self, filepath: Path, content_type: str) -> None:
        """Serve a file with proper headers."""
        try:
            file_size = filepath.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            with open(filepath, "rb") as f:
                self.wfile.write(f.read())
        except Exception as e:
            log.error("Error serving file: %s", e)
            self._send_error(500, "Error serving file")

    def _handle_start_stream(self) -> None:
        """Handle POST /api/start."""
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode()
            data = json.loads(body) if body else {}

            stream_id = data.get("stream_id") or str(uuid.uuid4())[:8]
            self.active_streams[stream_id] = {"started": time.time()}

            self._send_json({
                "stream_id": stream_id,
                "url": f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}",
            })
        except Exception as e:
            log.error("Error starting stream: %s", e)
            self._send_error(400, "Bad request")

    def _handle_stop_stream(self) -> None:
        """Handle POST /api/stop."""
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode()
            data = json.loads(body) if body else {}

            stream_id = data.get("stream_id")
            if stream_id and stream_id in self.active_streams:
                del self.active_streams[stream_id]
                self._send_json({"status": "stopped"})
            else:
                self._send_error(404, "Stream not found")
        except Exception as e:
            log.error("Error stopping stream: %s", e)
            self._send_error(400, "Bad request")

    def _send_json(self, data: dict) -> None:
        """Send JSON response."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _send_html(self, html: str) -> None:
        """Send HTML response."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_error(self, code: int, message: str) -> None:
        """Send error response."""
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"{code} {message}".encode())

    def log_message(self, format: str, *args) -> None:
        """Suppress verbose logging."""
        if "HTTP" not in format:
            log.debug(format, *args)


class VideoStreamServer:
    """Background HTTP server for video streaming."""

    _instance: Optional["VideoStreamServer"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "VideoStreamServer":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        """Initialize the server (called once)."""
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._started = False

    def start(self) -> None:
        """Start the HTTP server in background."""
        if self._started:
            return

        with self._lock:
            if self._started:
                return

            try:
                self.server = HTTPServer((STREAM_HOST, STREAM_PORT), VideoStreamHTTPHandler)
                self.thread = threading.Thread(target=self._run, daemon=True, name="video-stream-server")
                self.thread.start()
                self._started = True
                log.info("Video stream server started at %s:%d", STREAM_HOST, STREAM_PORT)
            except Exception as e:
                log.error("Failed to start video stream server: %s", e)
                self._started = False

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None

    def _run(self) -> None:
        """Run the server (blocking)."""
        try:
            if self.server:
                self.server.serve_forever()
        except Exception as e:
            log.error("Video stream server error: %s", e)


def get_stream_server() -> VideoStreamServer:
    """Get or create the singleton video stream server."""
    return VideoStreamServer()
