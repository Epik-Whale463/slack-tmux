"""Live video streaming for browser automation tasks."""

import html
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .browser import remote_debugging_port

log = logging.getLogger(__name__)

PLAYWRIGHT_OUTPUT_DIR = Path("~/.telly/playwright-mcp").expanduser().resolve()
STREAM_PORT = 9999
STREAM_HOST = "127.0.0.1"
LIVE_FRAME_INTERVAL_S = 0.4
LIVE_JPEG_QUALITY = 55


class VideoStreamSession:
    """Manages a browser live-sharing session."""

    def __init__(self, stream_id: str, task_name: str = "browser-task"):
        self.stream_id = stream_id
        self.task_name = task_name
        self.video_filename = f"{stream_id}.mp4"
        self.video_path = PLAYWRIGHT_OUTPUT_DIR / self.video_filename
        self.status_file = PLAYWRIGHT_OUTPUT_DIR / f"{stream_id}.status.json"

    def start(self) -> None:
        """Start live sharing session."""
        PLAYWRIGHT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._write_status("recording", {"started_at": time.time()})
        log.info("Video stream session started: %s", self.stream_id)

    def stop(self) -> None:
        """Stop live sharing."""
        self._write_status("stopped", {"stopped_at": time.time()})
        log.info("Video stream session stopped: %s", self.stream_id)

    def get_url(self) -> str:
        """Get the URL to stream this video."""
        return f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{self.stream_id}"

    def get_video_filename(self) -> str:
        """Get the optional finalized recording filename for this stream."""
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
        path = urlsplit(self.path).path
        if path.startswith("/stream/"):
            stream_path = path[8:]
            parts = [part for part in stream_path.split("/") if part]
            stream_id = parts[0] if parts else ""
            if len(parts) > 1:
                if parts[1] == "video":
                    self._serve_stream_video(stream_id)
                    return
                if parts[1] == "live":
                    self._serve_live_stream(stream_id)
                    return
            self._serve_stream(stream_id)
        elif path == "/health":
            self._send_json({"status": "ok"})
        elif path == "/streams":
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
        """Serve HTML page with live-sharing status for a stream."""
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

        html = self._build_stream_html(stream_id, state, task_name, video_path.exists())
        self._send_html(html)

    def _serve_stream_video(self, stream_id: str) -> None:
        """Serve the finalized recording file for a stream."""
        status_file = PLAYWRIGHT_OUTPUT_DIR / f"{stream_id}.status.json"
        video_filename = f"{stream_id}.mp4"
        if status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                video_filename = status.get("video_filename", video_filename)
            except Exception:
                pass
        video_path = PLAYWRIGHT_OUTPUT_DIR / video_filename
        if not video_path.exists():
            self._send_error(404, "Video not ready")
            return
        self._serve_file(video_path, "video/mp4")

    def _build_stream_html(self, stream_id: str, state: str, task_name: str, has_video: bool) -> str:
        """Build HTML for stream status page."""
        live_url = f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}/live"
        video_url = f"http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}/video"
        safe_task_name = html.escape(task_name)
        safe_stream_id = html.escape(stream_id)

        status_html = {
            "recording": f'<div style="color: #ff6b6b; font-weight: bold;">🔴 Live feed active...</div>',
            "stopped": f'<div style="color: #51cf66; font-weight: bold;">✅ Live feed stopped</div>',
            "unknown": f'<div style="color: #ffd43b;">⏳ Status unknown</div>',
        }.get(state, '<div>Unknown state</div>')

        playback_html = ""
        if has_video:
            playback_html = (
                '<details style="margin-top: 20px;">'
                '<summary style="cursor: pointer; color: #ccc;">Show finalized recording</summary>'
                f'<video controls style="margin-top: 12px;"><source src="{video_url}" type="video/mp4">Your browser does not support the video tag.</video>'
                '</details>'
            )

        page_html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Live Browser Feed - {safe_task_name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #1a1a1a; color: #fff; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ margin-bottom: 20px; }}
        .status {{ margin: 20px 0; padding: 15px; background: #2a2a2a; border-radius: 8px; }}
        .feed {{ width: 100%; max-width: 100%; border-radius: 8px; background: #000; display: block; aspect-ratio: 16 / 10; object-fit: contain; }}
        video {{ width: 100%; max-width: 100%; border-radius: 8px; background: #000; }}
        .info {{ margin-top: 20px; padding: 15px; background: #2a2a2a; border-radius: 8px; }}
        .hint {{ color: #999; margin-top: 12px; }}
        .live {{ animation: pulse 1.5s infinite; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.7; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 {safe_task_name}</h1>
            <p>Live browser feed</p>
        </div>

        <div class="status">
            {status_html}
        </div>

        <img class="feed" id="live-feed" src="{live_url}" alt="Live browser feed" />
        <p class="hint">If the feed is blank for a moment, the browser is still launching. This view refreshes continuously while the task runs.</p>
        {playback_html}

        <div class="info">
            <h3>Stream ID: <code>{safe_stream_id}</code></h3>
            <p>This page shows a live frame stream from the active browser page.</p>
            <p>Temporary stream files auto-delete shortly after the task completes.</p>
        </div>

        <script>
            const liveFeed = document.getElementById("live-feed");
            const refreshFeed = () => {{
                liveFeed.src = "{live_url}?t=" + Date.now();
            }};
            liveFeed.addEventListener("error", () => {{
                setTimeout(refreshFeed, 1000);
            }});
            setInterval(refreshFeed, {int(LIVE_FRAME_INTERVAL_S * 1000)});
            refreshFeed();
        </script>
    </div>
</body>
</html>"""
        return page_html

    def _serve_live_stream(self, stream_id: str) -> None:
        """Serve a single live frame captured from the active browser page."""
        frame = _capture_live_frame_jpeg()
        if frame:
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return

        placeholder = _placeholder_live_frame(stream_id)
        self.send_response(200)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(placeholder)))
        self.end_headers()
        self.wfile.write(placeholder)

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
        self.server: Optional[ThreadingHTTPServer] = None
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
                self.server = ThreadingHTTPServer((STREAM_HOST, STREAM_PORT), VideoStreamHTTPHandler)
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


def _stream_state(stream_id: str) -> str:
    status_file = PLAYWRIGHT_OUTPUT_DIR / f"{stream_id}.status.json"
    if not status_file.exists():
        return "unknown"
    try:
        status = json.loads(status_file.read_text())
    except Exception:
        return "unknown"
    return str(status.get("state") or "unknown")


def _placeholder_live_frame(stream_id: str) -> bytes:
    state = html.escape(_stream_state(stream_id))
    safe_stream_id = html.escape(stream_id)
    svg = f"""<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 1280 800\">
<rect width=\"1280\" height=\"800\" fill=\"#111827\"/>
<rect x=\"80\" y=\"80\" width=\"1120\" height=\"640\" rx=\"24\" fill=\"#1f2937\" stroke=\"#374151\"/>
<text x=\"640\" y=\"320\" text-anchor=\"middle\" fill=\"#f9fafb\" font-size=\"42\" font-family=\"Arial, sans-serif\">Waiting for live browser frame</text>
<text x=\"640\" y=\"390\" text-anchor=\"middle\" fill=\"#9ca3af\" font-size=\"26\" font-family=\"Arial, sans-serif\">Stream: {safe_stream_id}</text>
<text x=\"640\" y=\"435\" text-anchor=\"middle\" fill=\"#9ca3af\" font-size=\"26\" font-family=\"Arial, sans-serif\">State: {state}</text>
<text x=\"640\" y=\"520\" text-anchor=\"middle\" fill=\"#6b7280\" font-size=\"22\" font-family=\"Arial, sans-serif\">The page auto-refreshes while the browser launches or reconnects.</text>
</svg>"""
    return svg.encode("utf-8")


def _capture_live_frame_jpeg() -> bytes | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    browser = None
    playwright = None
    try:
        playwright = sync_playwright().start()
        port = remote_debugging_port()
        if port is None:
            return None
        browser = playwright.chromium.connect_over_cdp(f"http://{STREAM_HOST}:{port}")
        page = _active_browser_page(browser)
        if page is None:
            return None
        return page.screenshot(type="jpeg", quality=LIVE_JPEG_QUALITY, animations="disabled")
    except Exception:
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def _active_browser_page(browser):
    contexts = list(browser.contexts)
    contexts.sort(key=lambda ctx: len(ctx.pages), reverse=True)
    pages = []
    for context in contexts:
        pages.extend(context.pages)
    pages = [page for page in pages if not page.is_closed()]
    preferred = [
        page for page in pages
        if page.url and not page.url.startswith("about:") and not page.url.startswith("chrome:")
    ]
    candidates = preferred or pages
    return candidates[-1] if candidates else None
