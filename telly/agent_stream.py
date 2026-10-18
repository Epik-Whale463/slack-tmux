"""Slack agent text-streaming loop (chat.startStream / appendStream / stopStream).

Primary path for in-progress tool tasks and final answers on modern Slack agent
surfaces. Falls back to the legacy placeholder + chat.update path when streaming
is unavailable for the workspace/app.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import slackfmt

log = logging.getLogger(__name__)

PROGRESS_PLACEHOLDER_TEXT = "telly is working..."
PROGRESS_UPDATE_INTERVAL_S = 1.0
TASK_TITLE_LIMIT = 150
TASK_DETAIL_LIMIT = 240

LOADING_MESSAGES = [
    "Reading your request…",
    "Spinning up the local coding agent…",
    "Working through the plan…",
    "Checking the workspace…",
    "Drafting a careful reply…",
]


def _safe_task_id(value: object, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]", "_", str(value or fallback))[:80]
    return text or fallback


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def tool_event_to_task_chunk(event: dict) -> dict:
    """Map one brain tool_event into a chat stream task_update chunk."""
    name = str(event.get("name") or "tool")
    tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
    status = str(event.get("status") or "running")
    slack_status = {
        "running": "in_progress",
        "done": "complete",
        "error": "error",
        "pending": "pending",
        "in_progress": "in_progress",
        "complete": "complete",
    }.get(status, "in_progress")

    title = _clip(slackfmt.format_breadcrumb(name, tool_input), TASK_TITLE_LIMIT)
    if not title.startswith(name) and name not in title:
        title = _clip(f"{name}: {title}", TASK_TITLE_LIMIT)

    chunk: dict[str, Any] = {
        "type": "task_update",
        "id": _safe_task_id(event.get("id") or f"{name}_{hash(title) & 0xffff}", "task"),
        "title": title,
        "status": slack_status,
    }
    chunk["details"] = _clip(title, TASK_DETAIL_LIMIT)

    sources = slackfmt.collect_task_sources([event])
    if sources:
        chunk["sources"] = sources
    return chunk


def build_plan_seed_chunks(thread_ts: str) -> list[dict]:
    """Single in-progress step so the timeline shows activity immediately."""
    sid = _safe_task_id(thread_ts, "thread")
    return [
        {
            "type": "task_update",
            "id": f"{sid}_work",
            "title": "Working on your request",
            "status": "in_progress",
            "details": "Spinning up the local agent…",
        },
    ]


@dataclass
class AgentReplyStream:
    """Manages one streamed agent reply (or legacy placeholder fallback)."""

    client: Any
    channel: str
    thread_ts: str
    user_id: str | None = None
    team_id: str | None = None
    task_display_mode: str = "timeline"

    stream_ts: str | None = None
    legacy_placeholder_ts: str | None = None
    stop_control_ts: str | None = None
    using_stream: bool = False
    failed: bool = False

    _streamer: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_sent: float = 0.0
    _last_signature: tuple | None = None
    _task_state: dict[str, str] = field(default_factory=dict)
    _markdown_sent: str = ""
    _seeded: bool = False

    def start(self) -> str | None:
        """Start stream or legacy placeholder. Returns the message ts if known."""
        if self._try_start_stream():
            self._post_stop_control()
            return self.stream_ts
        self.using_stream = False
        self.legacy_placeholder_ts = self._post_legacy_placeholder()
        return self.legacy_placeholder_ts

    def _try_start_stream(self) -> bool:
        try:
            kwargs: dict[str, Any] = {
                "channel": self.channel,
                "thread_ts": self.thread_ts,
                "task_display_mode": self.task_display_mode,
                "buffer_size": 1,  # flush task chunks immediately
            }
            if self.user_id:
                kwargs["recipient_user_id"] = self.user_id
            if self.team_id:
                kwargs["recipient_team_id"] = self.team_id
            # Prefer stoppable streams when the workspace supports them.
            kwargs["is_stoppable"] = True

            streamer = self.client.chat_stream(**kwargs)
            seed = build_plan_seed_chunks(self.thread_ts)
            resp = streamer.append(chunks=seed)
            self._streamer = streamer
            self.using_stream = True
            self._seeded = True
            for chunk in seed:
                self._task_state[chunk["id"]] = chunk["status"]
            # stream ts may be on the streamer after first flush
            self.stream_ts = getattr(streamer, "_stream_ts", None) or (
                resp.get("ts") if isinstance(resp, dict) else None
            )
            if not self.stream_ts and hasattr(resp, "get"):
                self.stream_ts = resp.get("ts")
            log.info(
                "agent stream started channel=%s thread=%s ts=%s mode=%s",
                self.channel,
                self.thread_ts,
                self.stream_ts,
                self.task_display_mode,
            )
            return True
        except Exception as e:
            log.info("agent stream unavailable, using legacy progress UI: %s", e)
            self.failed = True
            self.using_stream = False
            self._streamer = None
            return False

    def _post_legacy_placeholder(self) -> str | None:
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=self.thread_ts,
                text=PROGRESS_PLACEHOLDER_TEXT,
                blocks=slackfmt.build_loading_blocks(self.thread_ts),
            )
            return resp["ts"]
        except Exception:
            log.exception("agent placeholder post failed; falling back to basic blocks")
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=self.thread_ts,
                text=PROGRESS_PLACEHOLDER_TEXT,
                blocks=slackfmt.build_plain_loading_blocks(self.thread_ts),
            )
            return resp["ts"]
        except Exception:
            log.exception("placeholder fallback post failed")
            return None

    def _post_stop_control(self) -> None:
        """Separate stop button while the stream is live (stream body has no actions)."""
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel,
                thread_ts=self.thread_ts,
                text="Telly is working — you can stop this turn.",
                blocks=[slackfmt.build_stop_actions_block(self.thread_ts)],
            )
            self.stop_control_ts = resp.get("ts")
        except Exception as e:
            log.debug("stop control post failed: %s", e)

    def _delete_stop_control(self) -> None:
        if not self.stop_control_ts:
            return
        try:
            self.client.chat_delete(channel=self.channel, ts=self.stop_control_ts)
        except Exception as e:
            log.debug("stop control delete failed: %s", e)
        self.stop_control_ts = None

    def make_progress_callback(self) -> Callable[[dict], None]:
        def on_progress(state: dict) -> None:
            self.on_progress(state)

        return on_progress

    def on_progress(self, state: dict) -> None:
        tool_events = state.get("tool_events") if isinstance(state.get("tool_events"), list) else []
        partial_text = str(state.get("partial_text") or "")
        signature = (
            len(tool_events),
            tool_events[-1].get("status", "") if tool_events else "",
            partial_text[-200:],
        )
        now = time.monotonic()
        with self._lock:
            if signature == self._last_signature or now - self._last_sent < PROGRESS_UPDATE_INTERVAL_S:
                return
            self._last_signature = signature
            self._last_sent = now

        if self.using_stream and self._streamer is not None:
            self._stream_progress(tool_events, partial_text)
            return
        if self.legacy_placeholder_ts:
            try:
                self.client.chat_update(
                    channel=self.channel,
                    ts=self.legacy_placeholder_ts,
                    text=PROGRESS_PLACEHOLDER_TEXT,
                    blocks=slackfmt.build_activity_blocks(
                        self.thread_ts,
                        tool_events,
                        partial_text,
                    ),
                )
            except Exception:
                log.debug("legacy progress chat.update failed", exc_info=True)

    def _stream_progress(self, tool_events: list[dict], partial_text: str) -> None:
        # Timeline mode: each tool call is its own step that moves
        # in_progress → complete. Full answer markdown is sent on stopStream
        # so we never risk duplicated partial drafts.
        chunks: list[dict] = []
        for event in tool_events:
            chunk = tool_event_to_task_chunk(event)
            task_id = chunk["id"]
            prev = self._task_state.get(task_id)
            if prev == chunk["status"] and prev is not None:
                if chunk["status"] != "in_progress":
                    continue
            self._task_state[task_id] = chunk["status"]
            chunks.append(chunk)

        # Before any tool call fires, show the draft on the seed work step so
        # the timeline isn't empty while the model thinks.
        if not tool_events and partial_text.strip():
            work_id = f"{_safe_task_id(self.thread_ts, 'thread')}_work"
            detail = _clip(partial_text.strip()[-200:], TASK_DETAIL_LIMIT)
            if self._task_state.get(work_id) != detail:
                self._task_state[work_id] = detail
                chunks.append({
                    "type": "task_update",
                    "id": work_id,
                    "title": "Drafting reply",
                    "status": "in_progress",
                    "details": detail,
                })

        if not chunks:
            return
        try:
            resp = self._streamer.append(chunks=chunks)
            if not self.stream_ts:
                self.stream_ts = getattr(self._streamer, "_stream_ts", None) or (
                    resp.get("ts") if resp is not None and hasattr(resp, "get") else None
                )
        except Exception:
            log.exception("stream append failed; switching to legacy progress")
            self.using_stream = False
            if not self.legacy_placeholder_ts:
                self.legacy_placeholder_ts = self._post_legacy_placeholder()

    def discard(self) -> None:
        """Remove in-progress UI without posting a final answer (NO_REPLY)."""
        self._delete_stop_control()
        if self.using_stream and self.stream_ts:
            try:
                if self._streamer is not None and getattr(self._streamer, "_state", None) != "completed":
                    self._streamer.stop(markdown_text="")
                self.client.chat_delete(channel=self.channel, ts=self.stream_ts)
            except Exception as e:
                log.debug("stream discard failed: %s", e)
            return
        if self.legacy_placeholder_ts:
            try:
                self.client.chat_delete(channel=self.channel, ts=self.legacy_placeholder_ts)
            except Exception as e:
                log.debug("legacy placeholder delete failed: %s", e)

    def finish_cancelled(self) -> None:
        self._delete_stop_control()
        text = ":octagonal_sign: _stopped by user_"
        if self.using_stream and self._streamer is not None:
            try:
                work_id = f"{_safe_task_id(self.thread_ts, 'thread')}_work"
                self._streamer.stop(
                    markdown_text="Stopped by user.",
                    chunks=[{
                        "type": "task_update",
                        "id": work_id,
                        "title": "Stopped by user",
                        "status": "error",
                        "output": "Turn cancelled.",
                    }],
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                )
                return
            except Exception:
                log.exception("stream cancel stop failed")
        self._legacy_update_or_post(text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])

    def finish_error(self, err: Exception) -> None:
        self._delete_stop_control()
        text = f":warning: brain error: `{err}`"
        blocks = slackfmt.sanitize_blocks_for_messages(slackfmt.build_error_blocks(self.thread_ts, str(err)))
        fallback = slackfmt.build_basic_error_blocks(self.thread_ts, str(err))
        if self.using_stream and self._streamer is not None:
            for tier in slackfmt.block_fallback_tiers(blocks, reply_text=text):
                try:
                    work_id = f"{_safe_task_id(self.thread_ts, 'thread')}_work"
                    self._streamer.stop(
                        markdown_text=f"Telly hit a problem: {err}",
                        chunks=[{
                            "type": "task_update",
                            "id": work_id,
                            "title": "Telly hit a problem",
                            "status": "error",
                            "output": _clip(str(err), TASK_DETAIL_LIMIT),
                        }],
                        blocks=tier,
                    )
                    return
                except Exception as e:
                    log.warning("stream error stop tier failed: %s", e)
            try:
                self._streamer.stop(markdown_text=text)
                return
            except Exception:
                log.exception("stream bare error stop failed")
        self._legacy_update_or_post(text, blocks, fallback)

    def finish_success(
        self,
        *,
        markdown_text: str,
        blocks: list[dict],
        fallback_blocks: list[dict],
        notify_text: str,
        tool_events: list[dict] | None = None,
    ) -> None:
        self._delete_stop_control()
        # Mark work complete in the plan/timeline
        complete_chunks: list[dict] = []
        work_id = f"{_safe_task_id(self.thread_ts, 'thread')}_work"
        complete_chunks.append({
            "type": "task_update",
            "id": work_id,
            "title": "Work complete",
            "status": "complete",
            "output": "Answer ready.",
        })
        answer_id = f"{_safe_task_id(self.thread_ts, 'thread')}_answer"
        complete_chunks.append({
            "type": "task_update",
            "id": answer_id,
            "title": "Post the complete answer",
            "status": "complete",
        })
        # Close any still-running tool tasks
        for event in tool_events or []:
            if str(event.get("status") or "") == "running":
                event = {**event, "status": "done"}
            chunk = tool_event_to_task_chunk(event)
            if chunk["status"] == "in_progress":
                chunk["status"] = "complete"
            complete_chunks.append(chunk)

        tiers = slackfmt.block_fallback_tiers(blocks, reply_text=markdown_text or notify_text)
        # Always try the explicit fallback tier last
        if fallback_blocks:
            tiers.append(slackfmt.sanitize_blocks_for_messages(fallback_blocks))

        if self.using_stream and self._streamer is not None:
            last_err: Exception | None = None
            for i, tier in enumerate(tiers):
                try:
                    # Only pass markdown/chunks on first attempt — failed stops leave
                    # text buffered on the streamer; re-passing would duplicate.
                    self._streamer.stop(
                        markdown_text=(markdown_text or None) if i == 0 else None,
                        chunks=complete_chunks if i == 0 else None,
                        blocks=tier,
                    )
                    if i > 0:
                        log.warning("stream stop succeeded with fallback block tier %s", i)
                    return
                except Exception as e:
                    last_err = e
                    log.warning("stream stop failed on block tier %s: %s", i, e)
            # Last ditch: stop stream with no blocks so the message isn't stuck mid-stream
            try:
                self._streamer.stop(markdown_text=markdown_text or notify_text or "Done.")
                # Then post interactive blocks as a follow-up
                self._post_blocks_with_tiers(notify_text, tiers)
                return
            except Exception:
                log.exception("stream bare stop failed: %s", last_err)

        # Legacy finalize with tiered blocks
        ts = self.legacy_placeholder_ts
        if ts:
            for i, tier in enumerate(tiers):
                try:
                    self.client.chat_update(channel=self.channel, ts=ts, text=notify_text, blocks=tier)
                    return
                except Exception as e:
                    log.warning("chat.update tier %s failed: %s", i, e)
        self._post_blocks_with_tiers(notify_text, tiers)

    def _post_blocks_with_tiers(self, text: str, tiers: list[list[dict]]) -> None:
        for i, tier in enumerate(tiers):
            try:
                self.client.chat_postMessage(
                    channel=self.channel,
                    thread_ts=self.thread_ts,
                    text=text,
                    blocks=tier or None,
                )
                if i > 0:
                    log.warning("postMessage succeeded with fallback block tier %s", i)
                return
            except Exception as e:
                log.warning("postMessage tier %s failed: %s", i, e)
        # Absolute last resort — plain text
        try:
            self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=text)
        except Exception:
            log.exception("plain text postMessage failed")

    def _legacy_update_or_post(
        self,
        text: str,
        blocks: list[dict],
        fallback_blocks: list[dict] | None = None,
    ) -> None:
        ts = self.legacy_placeholder_ts or self.stream_ts
        if ts:
            try:
                self.client.chat_update(channel=self.channel, ts=ts, text=text, blocks=blocks)
                return
            except Exception:
                log.exception("legacy update failed")
            if fallback_blocks is not None:
                try:
                    self.client.chat_update(channel=self.channel, ts=ts, text=text, blocks=fallback_blocks)
                    return
                except Exception:
                    log.exception("legacy fallback update failed")
        try:
            self.client.chat_postMessage(channel=self.channel, thread_ts=self.thread_ts, text=text, blocks=blocks)
        except Exception:
            if fallback_blocks is not None:
                self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts, text=text, blocks=fallback_blocks
                )
