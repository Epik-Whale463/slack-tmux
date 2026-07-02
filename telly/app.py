import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import brain, slackfmt
from .config import Config
from .state import ThreadStore
from .video_stream import get_stream_server, VideoStreamSession, PLAYWRIGHT_OUTPUT_DIR, STREAM_HOST, STREAM_PORT

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
NO_REPLY_TOKEN = "NO_REPLY"
PROGRESS_PLACEHOLDER_TEXT = "telly is working..."
PROGRESS_UPDATE_INTERVAL_S = 1.2
LONG_REPLY_FILE_THRESHOLD = 9000
UPLOAD_MARKER_RE = re.compile(r"^\s*(?:SLACK_UPLOAD|ATTACH_FILE):\s*(.+?)\s*$", re.MULTILINE)
DISCUSSION_CONTEXT_RE = re.compile(
    r"\b("
    r"based on (our|the|this) discussion|"
    r"based on (what|the things) we (discussed|said|talked about)|"
    r"what do you think|"
    r"where we landed|"
    r"what did we decide|"
    r"summari[sz]e (the )?(discussion|thread|conversation)|"
    r"our discussion|"
    r"the approach we discussed|"
    r"what did <@[UW][A-Z0-9]+> mean|"
    r"is this still blocked"
    r")\b",
    re.IGNORECASE,
)
TRIGGER_ADD_RE = re.compile(
    r"^\s*(?:add|set)\s+trigger(?:\s+(?:named\s+)?)?\s+(?P<name>[^:\n=>]+?)\s*(?::|=>)\s*(?P<instruction>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
TRIGGER_REMOVE_RE = re.compile(
    r"^\s*(?:remove|delete)\s+trigger\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
TRIGGER_START_RE = re.compile(
    r"^\s*start\s+trigger\s+(?P<name>.+?)\s+every\s+(?P<interval>.+?)\s*$",
    re.IGNORECASE,
)
TRIGGER_STOP_RE = re.compile(
    r"^\s*stop\s+trigger\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
TRIGGER_LIST_RE = re.compile(r"^\s*list\s+triggers\s*$", re.IGNORECASE)
TRIGGER_CLEAR_RE = re.compile(r"^\s*clear\s+triggers\s*$", re.IGNORECASE)
MAX_CONTEXT_MESSAGES = 30
MAX_CONTEXT_TEXT_CHARS = 9000
BACKGROUND_TRIGGER_POLL_INTERVAL_S = 2.0

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="telly-brain")

_bot_user_id: str | None = None
_bot_user_id_lock = threading.Lock()


def _strip_mentions(text: str) -> str:
    return MENTION_RE.sub("", text).strip()


def _get_bot_user_id(client) -> str | None:
    global _bot_user_id
    with _bot_user_id_lock:
        if _bot_user_id is None:
            try:
                _bot_user_id = client.auth_test()["user_id"]
                log.info("bot user id: %s", _bot_user_id)
            except Exception:
                log.exception("auth.test failed")
        return _bot_user_id


def _swap_reaction(client, channel: str, ts: str, remove: str | None, add: str | None) -> None:
    if remove:
        try:
            client.reactions_remove(channel=channel, timestamp=ts, name=remove)
        except Exception as e:
            log.debug("reactions_remove(%s) failed: %s", remove, e)
    if add:
        try:
            client.reactions_add(channel=channel, timestamp=ts, name=add)
        except Exception as e:
            log.debug("reactions_add(%s) failed: %s", add, e)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")
    return cleaned[:120] or "upload"


def _call_assistant(client, method: str, **kwargs) -> None:
    fn = getattr(client, method, None)
    if not fn:
        return
    try:
        fn(**kwargs)
    except Exception as e:
        log.debug("%s failed: %s", method, e)


def _set_status(client, channel: str, thread_ts: str, status: str) -> None:
    # We already post one in-thread plan/task card as the loading UI. In the
    # Slack agent container, assistant status renders as a second visible telly
    # surface, so keep status disabled to avoid duplicate in-progress messages.
    return


def _set_title(client, channel: str, thread_ts: str, text: str) -> None:
    title = re.sub(r"\s+", " ", text).strip()[:80] or "Telly thread"
    _call_assistant(client, "assistant_threads_setTitle", channel_id=channel, thread_ts=thread_ts, title=title)


def _set_suggested_prompts(client, channel: str, thread_ts: str) -> None:
    _call_assistant(
        client,
        "assistant_threads_setSuggestedPrompts",
        channel_id=channel,
        thread_ts=thread_ts,
        title="Try telly with:",
        prompts=[
            {"title": "Analyze this project", "message": "Analyze this project and suggest the highest-impact improvements."},
            {"title": "Review recent changes", "message": "Review the recent changes and call out risks."},
            {"title": "Debug an error", "message": "Help me debug this error. I will paste logs or upload a file."},
            {"title": "Summarize files", "message": "Summarize the files I upload and tell me what matters."},
        ],
    )


def _download_slack_files(cfg: Config, event: dict) -> list[str]:
    saved: list[str] = []
    files = event.get("files") or []
    if not files:
        return saved
    thread_ts = event.get("thread_ts") or event.get("ts") or "unknown"
    target_dir = cfg.telly_home / "uploads" / re.sub(r"[^0-9A-Za-z_.-]", "_", thread_ts)
    target_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        name = _safe_filename(f.get("name") or f.get("title") or f.get("id") or "upload")
        path = target_dir / name
        try:
            req = Request(url, headers={"Authorization": f"Bearer {cfg.slack_bot_token}"})
            with urlopen(req, timeout=30) as resp:
                path.write_bytes(resp.read())
            saved.append(str(path))
        except Exception:
            log.exception("failed to download Slack file %s", name)
    return saved


def _augment_prompt(text: str, file_paths: list[str], state_note: str | None = None) -> str:
    parts = [text.strip()]
    if file_paths:
        parts.append("Attached Slack files downloaded locally:\n" + "\n".join(f"- {p}" for p in file_paths))
    if state_note:
        parts.append(state_note)
    return "\n\n".join(p for p in parts if p)


def _format_trigger_note(triggers: dict[str, dict[str, Any]] | None) -> str | None:
    if not triggers:
        return None
    lines = ["Live triggers active for this Slack thread. Apply them to this and future requests in this thread unless the user removes them:"]
    for name, trigger in triggers.items():
        if not isinstance(trigger, dict):
            continue
        instruction = str(trigger.get("instruction") or "").strip()
        if not instruction:
            continue
        interval_seconds = trigger.get("interval_seconds")
        enabled = bool(trigger.get("enabled"))
        suffix = f" [running every {_format_interval_seconds(int(interval_seconds))}]" if enabled and isinstance(interval_seconds, (int, float)) else ""
        lines.append(f"- {name}: {instruction}{suffix}")
    return "\n".join(lines)


def _normalize_trigger_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _parse_interval_seconds(text: str) -> int | None:
    value = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([a-z]+)?", value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2) or "m"
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return amount
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return amount * 60
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return amount * 3600
    return None


def _format_interval_seconds(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}m"
    return f"{seconds}s"


def _trigger_summary(name: str, trigger: dict[str, Any]) -> str:
    instruction = str(trigger.get("instruction") or "").strip()
    interval_seconds = trigger.get("interval_seconds")
    enabled = bool(trigger.get("enabled"))
    schedule = "manual"
    if isinstance(interval_seconds, (int, float)):
        schedule = f"every {_format_interval_seconds(int(interval_seconds))}"
    status = "running" if enabled else "idle"
    return f"- `{name}` [{status}; {schedule}]: {instruction}"


def _handle_trigger_command(store: ThreadStore, client, channel: str, thread_ts: str, text: str) -> bool:
    add_match = TRIGGER_ADD_RE.match(text)
    if add_match:
        name = _normalize_trigger_name(add_match.group("name"))
        instruction = add_match.group("instruction").strip()
        state = store.get(thread_ts)
        triggers = dict(state.triggers if state else {})
        previous = triggers.get(name) or {}
        triggers[name] = {
            "instruction": instruction,
            "interval_seconds": previous.get("interval_seconds"),
            "enabled": bool(previous.get("enabled", False)),
            "next_run_at": previous.get("next_run_at"),
        }
        store.update(thread_ts, triggers=triggers)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"Saved trigger `{name}` for this chat.")
        return True

    start_match = TRIGGER_START_RE.match(text)
    if start_match:
        name = _normalize_trigger_name(start_match.group("name"))
        interval_seconds = _parse_interval_seconds(start_match.group("interval"))
        if interval_seconds is None:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Could not parse the interval. Example: `start trigger amma jio every 5m`.")
            return True
        state = store.get(thread_ts)
        triggers = dict(state.triggers if state else {})
        trigger = dict(triggers.get(name) or {})
        instruction = str(trigger.get("instruction") or "").strip()
        if not instruction:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"No saved trigger named `{name}`. First add it with `add trigger {name}: ...`.")
            return True
        trigger["interval_seconds"] = interval_seconds
        trigger["enabled"] = True
        trigger["next_run_at"] = time.time()
        triggers[name] = trigger
        store.update(thread_ts, triggers=triggers)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"Started trigger `{name}` every `{_format_interval_seconds(interval_seconds)}` for this chat.")
        return True

    remove_match = TRIGGER_REMOVE_RE.match(text)
    if remove_match:
        name = _normalize_trigger_name(remove_match.group("name"))
        state = store.get(thread_ts)
        triggers = dict(state.triggers if state else {})
        if name in triggers:
            del triggers[name]
            store.update(thread_ts, triggers=triggers)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"Removed trigger `{name}` from this chat.")
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"No trigger named `{name}` is active in this chat.")
        return True

    stop_match = TRIGGER_STOP_RE.match(text)
    if stop_match:
        name = _normalize_trigger_name(stop_match.group("name"))
        state = store.get(thread_ts)
        triggers = dict(state.triggers if state else {})
        trigger = dict(triggers.get(name) or {})
        instruction = str(trigger.get("instruction") or "").strip()
        if not instruction:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"No trigger named `{name}` is saved in this chat.")
            return True
        trigger["enabled"] = False
        trigger["next_run_at"] = None
        triggers[name] = trigger
        store.update(thread_ts, triggers=triggers)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f"Stopped trigger `{name}` for this chat.")
        return True

    if TRIGGER_LIST_RE.match(text):
        state = store.get(thread_ts)
        triggers = state.triggers if state else {}
        if not triggers:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="No active triggers in this chat.")
            return True
        lines = [_trigger_summary(name, trigger) for name, trigger in triggers.items()]
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Active triggers in this chat:\n" + "\n".join(lines))
        return True

    if TRIGGER_CLEAR_RE.match(text):
        store.update(thread_ts, triggers={})
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Cleared all triggers for this chat.")
        return True

    return False


def _build_trigger_prompt(name: str, trigger: dict[str, Any]) -> str:
    instruction = str(trigger.get("instruction") or "").strip()
    return (
        f"[Scheduled trigger: {name}] This trigger is running automatically in the background for this Slack thread.\n\n"
        f"Trigger instruction:\n{instruction}\n\n"
        f"Check now and act if needed. If there is nothing useful to report or do right now, reply with exactly {NO_REPLY_TOKEN}."
    )


def _run_due_triggers(cfg: Config, store: ThreadStore, client) -> None:
    now = time.time()
    for state in store.list_recent(999):
        if not state.channel or not state.user or state.status in {"running", "queued"}:
            continue
        triggers = dict(state.triggers or {})
        changed = False
        for name, trigger in triggers.items():
            if not isinstance(trigger, dict) or not trigger.get("enabled"):
                continue
            interval_seconds = trigger.get("interval_seconds")
            next_run_at = trigger.get("next_run_at")
            if not isinstance(interval_seconds, (int, float)):
                continue
            if isinstance(next_run_at, (int, float)) and next_run_at > now:
                continue
            trigger["next_run_at"] = now + int(interval_seconds)
            changed = True
            store.update(state.thread_ts, triggers=triggers, save=True)
            prompt = _build_trigger_prompt(name, trigger)
            _kickoff(cfg, store, client, state.channel, state.thread_ts, None, state.user, prompt, background=True)
            break
        if changed:
            continue


def _start_background_trigger_loop(cfg: Config, store: ThreadStore, client) -> None:
    def worker() -> None:
        while True:
            try:
                _run_due_triggers(cfg, store, client)
            except Exception:
                log.exception("background trigger loop failed")
            time.sleep(BACKGROUND_TRIGGER_POLL_INTERVAL_S)

    threading.Thread(target=worker, name="telly-trigger-loop", daemon=True).start()


def _wants_discussion_context(text: str) -> bool:
    return bool(DISCUSSION_CONTEXT_RE.search(text))


def _slack_message_text(message: dict) -> str:
    text = re.sub(r"\s+", " ", message.get("text", "") or "").strip()
    files = message.get("files") or []
    if files:
        names = [f.get("name") or f.get("title") or f.get("id") for f in files]
        text = (text + " " if text else "") + "[files: " + ", ".join(n for n in names if n) + "]"
    return text


def _format_context_messages(messages: list[dict], current_ts: str | None = None) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for msg in sorted(messages, key=lambda m: str(m.get("ts", ""))):
        ts = str(msg.get("ts") or "")
        if not ts or ts in seen:
            continue
        seen.add(ts)
        text = _slack_message_text(msg)
        if not text:
            continue
        speaker = msg.get("user") or msg.get("username") or msg.get("bot_id") or "unknown"
        marker = " (current message)" if current_ts and ts == current_ts else ""
        lines.append(f"- <@{speaker}>{marker}: {text}")
    return lines


def _fetch_discussion_context(client, channel: str, thread_ts: str, msg_ts: str | None) -> str | None:
    messages: list[dict] = []
    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=MAX_CONTEXT_MESSAGES)
        messages.extend(replies.get("messages", []) or [])
    except Exception as e:
        log.debug("thread context fetch failed: %s", e)

    try:
        history = client.conversations_history(
            channel=channel,
            latest=msg_ts or thread_ts,
            inclusive=True,
            limit=12,
        )
        messages.extend(history.get("messages", []) or [])
    except Exception as e:
        log.debug("channel context fetch failed: %s", e)

    lines = _format_context_messages(messages, current_ts=msg_ts)
    if not lines:
        return None

    body = "\n".join(lines)
    if len(body) > MAX_CONTEXT_TEXT_CHARS:
        body = body[-MAX_CONTEXT_TEXT_CHARS:]
    return (
        "Slack discussion context loaded because the user asked a context-dependent question. "
        "Use it to answer based on the thread/channel discussion. If it is insufficient, ask one concise clarifying question.\n\n"
        f"Channel: {channel}\nThread: {thread_ts}\nRecent discussion:\n{body}"
    )


def _post_placeholder(client, channel: str, thread_ts: str) -> str | None:
    try:
        resp = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=PROGRESS_PLACEHOLDER_TEXT,
            blocks=slackfmt.build_loading_blocks(thread_ts),
        )
        return resp["ts"]
    except Exception:
        log.exception("agent placeholder post failed; falling back to basic blocks")
    try:
        resp = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=PROGRESS_PLACEHOLDER_TEXT,
            blocks=slackfmt.build_plain_loading_blocks(thread_ts),
        )
        return resp["ts"]
    except Exception:
        log.exception("placeholder fallback post failed")
        return None


def _make_progress_callback(client, channel: str, thread_ts: str, placeholder_ts: str | None):
    if not placeholder_ts:
        return None

    last_sent = 0.0
    last_signature: tuple[int, str, str] | None = None
    lock = threading.Lock()

    def on_progress(state: dict) -> None:
        nonlocal last_sent, last_signature
        tool_events = state.get("tool_events") if isinstance(state.get("tool_events"), list) else []
        partial_text = str(state.get("partial_text") or "")
        signature = (len(tool_events), tool_events[-1].get("status", "") if tool_events else "", partial_text[-200:])
        now = time.monotonic()
        with lock:
            if signature == last_signature or now - last_sent < PROGRESS_UPDATE_INTERVAL_S:
                return
            last_signature = signature
            last_sent = now
        try:
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=PROGRESS_PLACEHOLDER_TEXT,
                blocks=slackfmt.build_activity_blocks(thread_ts, tool_events, partial_text),
            )
        except Exception:
            log.debug("progress chat.update failed", exc_info=True)

    return on_progress


def _post_or_update_final(
    client,
    channel: str,
    thread_ts: str,
    placeholder_ts: str | None,
    text: str,
    blocks: list[dict],
    fallback_blocks: list[dict],
) -> None:
    if placeholder_ts:
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text, blocks=blocks)
            return
        except Exception:
            log.exception("final chat.update with agent blocks failed; retrying basic blocks")
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text, blocks=fallback_blocks)
            return
        except Exception:
            log.exception("final chat.update fallback failed; falling back to postMessage")

    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)
    except Exception:
        log.exception("final postMessage with agent blocks failed; retrying basic blocks")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=fallback_blocks)


def _try_upload_long_reply(client, channel: str, thread_ts: str, text: str) -> bool:
    try:
        client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            filename=f"telly-answer-{thread_ts}.md",
            title="Full telly answer",
            content=text,
            initial_comment="Full answer attached because it is too long for a clean Slack message.",
        )
        return True
    except Exception as e:
        log.debug("long reply upload failed: %s", e)
        return False


def _extract_upload_markers(text: str) -> tuple[str, list[Path]]:
    paths: list[Path] = []
    kept_lines: list[str] = []
    for line in text.splitlines():
        match = UPLOAD_MARKER_RE.match(line)
        if match:
            raw = match.group(1).strip().strip("`'")
            if raw:
                paths.append(Path(raw).expanduser())
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip(), paths


def _upload_reply_files(client, channel: str, thread_ts: str, paths: list[Path]) -> list[str]:
    failures: list[str] = []
    for path in paths:
        try:
            if not path.is_file():
                failures.append(f"{path} (not found)")
                continue
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(path),
                filename=path.name,
                title=path.name,
            )
        except Exception as e:
            log.exception("reply file upload failed for %s", path)
            failures.append(_format_upload_failure(path, e))
    return failures


def _format_upload_failure(path: Path, err: Exception) -> str:
    response = getattr(err, "response", None)
    error = None
    needed = None
    if isinstance(response, dict):
        error = response.get("error")
        needed = response.get("needed")
    elif response is not None:
        try:
            error = response.get("error")
            needed = response.get("needed")
        except Exception:
            pass

    if error == "missing_scope":
        scope = needed or "files:write"
        return f"`{path}` — Slack app is missing `{scope}`; add the scope and reinstall the app."
    return f"`{path}` — upload failed; file is still available locally."


def _finalize_reply(client, channel: str, thread_ts: str, placeholder_ts: str | None, turn: brain.BrainTurn) -> None:
    reply, upload_paths = _extract_upload_markers(turn.reply)
    reply, visualization_blocks = slackfmt.extract_visualization_blocks(reply)
    text = slackfmt.to_mrkdwn(reply or ("Here's the visualization." if visualization_blocks else "Uploaded requested file."))
    if len(text) > LONG_REPLY_FILE_THRESHOLD and _try_upload_long_reply(client, channel, thread_ts, text):
        summary = "The answer was long, so I attached the full response as a Markdown file."
        actions_block = slackfmt.build_actions_block(thread_ts)
        feedback_block = slackfmt.build_feedback_block(thread_ts)
        footer_block = slackfmt.build_footer_block(turn.session_id, turn.cost_usd, turn.duration_ms)
        blocks = slackfmt.build_blocks(summary, actions_block=actions_block, feedback_block=feedback_block, footer=footer_block)
        fallback_blocks = slackfmt.build_blocks(summary, actions_block=actions_block, footer=footer_block)
        _post_or_update_final(client, channel, thread_ts, placeholder_ts, summary, blocks, fallback_blocks)
        return

    chunks = slackfmt.chunk_for_slack(text)
    breadcrumbs_block = slackfmt.build_breadcrumb_block(turn.tool_breadcrumbs)
    footer_block = slackfmt.build_footer_block(turn.session_id, turn.cost_usd, turn.duration_ms)
    actions_block = slackfmt.build_actions_block(thread_ts)
    feedback_block = slackfmt.build_feedback_block(thread_ts)
    completion_block = slackfmt.build_completion_task_block(thread_ts, turn.duration_ms)

    first_chunk = chunks[0]
    first_blocks = slackfmt.build_blocks(
        first_chunk,
        completion_block=completion_block,
        breadcrumbs_block=breadcrumbs_block,
        visualization_blocks=visualization_blocks,
        actions_block=actions_block if len(chunks) == 1 else None,
        feedback_block=feedback_block if len(chunks) == 1 else None,
        footer=footer_block if len(chunks) == 1 else None,
    )
    first_fallback_blocks = slackfmt.build_blocks(
        first_chunk,
        breadcrumbs_block=breadcrumbs_block,
        actions_block=actions_block if len(chunks) == 1 else None,
        footer=footer_block if len(chunks) == 1 else None,
    )

    _post_or_update_final(
        client, channel, thread_ts, placeholder_ts, first_chunk, first_blocks, first_fallback_blocks
    )

    upload_failures = _upload_reply_files(client, channel, thread_ts, upload_paths)
    if upload_failures:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Could not upload file attachment:\n" + "\n".join(f"- {failure}" for failure in upload_failures),
        )

    for i, chunk in enumerate(chunks[1:], start=1):
        is_last = i == len(chunks) - 1
        blocks = slackfmt.build_blocks(
            chunk,
            actions_block=actions_block if is_last else None,
            feedback_block=feedback_block if is_last else None,
            footer=footer_block if is_last else None,
        )
        fallback_blocks = slackfmt.build_blocks(
            chunk,
            actions_block=actions_block if is_last else None,
            footer=footer_block if is_last else None,
        )
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk, blocks=blocks)
        except Exception:
            log.exception("chunk postMessage with feedback failed; retrying basic blocks")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk, blocks=fallback_blocks)


def _finalize_cancelled(client, channel: str, thread_ts: str, placeholder_ts: str | None) -> None:
    text = ":octagonal_sign: _stopped by user_"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if placeholder_ts:
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text, blocks=blocks)
            return
        except Exception:
            log.exception("cancelled chat.update failed")
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)


def _finalize_error(client, channel: str, thread_ts: str, placeholder_ts: str | None, err: Exception) -> None:
    text = f":warning: brain error: `{err}`"
    blocks = slackfmt.build_error_blocks(thread_ts, str(err))
    fallback_blocks = slackfmt.build_basic_error_blocks(thread_ts, str(err))
    if placeholder_ts:
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text, blocks=blocks)
            return
        except Exception:
            log.exception("agent error chat.update failed; retrying basic blocks")
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text, blocks=fallback_blocks)
            return
        except Exception:
            log.exception("error chat.update fallback failed")
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)
    except Exception:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=fallback_blocks)


def _is_browser_task(text: str) -> bool:
    """Detect if the prompt likely involves browser automation."""
    keywords = (
        "browser", "navigate", "visit", "screenshot", "click", "fill", "form",
        "web", "page", "link", "url", "scrape", "automat", "interact", "submit"
    )
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def _build_video_recording_instruction(stream_id: str, video_filename: str) -> str:
    """Build instruction for agent to record browser activity."""
    return (
        f"[Video Recording] Please record this browser session for live sharing.\n"
        f"When you start using the browser:\n"
        f"  1. Call: browser_start_video with filename: {video_filename}\n"
        f"  2. Call: browser_video_show_actions to annotate your clicks/actions\n"
        f"When done with the browser:\n"
        f"  3. Call: browser_stop_video to finalize the recording\n"
        f"The video will be streamed at: http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}\n"
        f"Users can watch it live as you work."
    )


def _handle_brain_turn(
    cfg: Config, store: ThreadStore, client, channel: str, thread_ts: str, msg_ts: str | None,
    user_id: str, text: str, placeholder_ts: str | None, success_reaction: str = "white_check_mark",
) -> None:
    prior = store.get(thread_ts)
    if prior and prior.session_id:
        brain.set_session_for_thread(thread_ts, prior.session_id)
    store.update(thread_ts, channel=channel, user=user_id, last_message=text, agent_cmd=cfg.main_agent_cmd, status="running")
    _set_status(client, channel, thread_ts, "Working on your request...")

    # Detect browser tasks and start video streaming
    stream_id = None
    stream_session = None
    if _is_browser_task(text):
        stream_id = str(uuid.uuid4())[:12]
        try:
            stream_session = VideoStreamSession(stream_id, task_name=text[:60])
            stream_session.start()
            video_url = stream_session.get_url()
            store.update(thread_ts, video_stream_id=stream_id)
            # Post video stream link
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"🎬 *Live browser session:* <{video_url}|Watch live>",
                )
            except Exception as e:
                log.warning("Failed to post video stream link: %s", e)
        except Exception as e:
            log.warning("Failed to start video streaming: %s", e)
            stream_id = None
            stream_session = None

    on_progress = _make_progress_callback(client, channel, thread_ts, placeholder_ts)
    try:
        turn = brain.ask_brain(cfg.main_agent_cmd, thread_ts, text, cfg.turn_log_path, on_progress=on_progress)
    except brain.BrainCancelled:
        log.info("brain turn cancelled by user thread=%s", thread_ts)
        store.update(thread_ts, status="cancelled")
        _set_status(client, channel, thread_ts, "")
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", "octagonal_sign")
        _finalize_cancelled(client, channel, thread_ts, placeholder_ts)
        return
    except Exception as e:
        log.exception("brain failure")
        store.update(thread_ts, status="error")
        _set_status(client, channel, thread_ts, "")
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", "x")
        _finalize_error(client, channel, thread_ts, placeholder_ts, e)
        return
    finally:
        # Clean up video stream session
        if stream_session:
            try:
                stream_session.stop()
                stream_session.cleanup(delay_s=5.0)
            except Exception as e:
                log.debug("Error during video stream cleanup: %s", e)

    if turn.reply.strip() == NO_REPLY_TOKEN:
        log.info("brain returned NO_REPLY — staying silent")
        if placeholder_ts:
            try:
                client.chat_delete(channel=channel, ts=placeholder_ts)
            except Exception:
                log.debug("placeholder delete failed; leaving it in place")
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", success_reaction)
        store.update(thread_ts, session_id=turn.session_id, status="silent")
        _set_status(client, channel, thread_ts, "")
        return

    store.update(thread_ts, session_id=turn.session_id, status="complete")
    _set_status(client, channel, thread_ts, "")
    if msg_ts:
        _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", success_reaction)
    _finalize_reply(client, channel, thread_ts, placeholder_ts, turn)


def _kickoff(cfg: Config, store: ThreadStore, client, channel: str, thread_ts: str, msg_ts: str | None, user_id: str, text: str, file_paths: list[str] | None = None, background: bool = False) -> None:
    if msg_ts:
        _swap_reaction(client, channel, msg_ts, None, "hourglass_flowing_sand")
    context_note = _fetch_discussion_context(client, channel, thread_ts, msg_ts) if _wants_discussion_context(text) else None
    state = store.get(thread_ts)
    trigger_note = _format_trigger_note(state.triggers if state else None)

    # Add video recording instruction for browser tasks
    notes = [context_note, trigger_note]
    if _is_browser_task(text):
        stream_id = str(uuid.uuid4())[:12]
        video_filename = f"{stream_id}.mp4"
        notes.append(_build_video_recording_instruction(stream_id, video_filename))

    prompt = _augment_prompt(text, file_paths or [], "\n\n".join(note for note in notes if note))
    store.update(
        thread_ts,
        channel=channel,
        user=user_id,
        last_message=text,
        agent_cmd=cfg.main_agent_cmd,
        uploaded_files=file_paths or None,
        status="queued",
    )
    _set_title(client, channel, thread_ts, text)
    placeholder_ts = None if background else _post_placeholder(client, channel, thread_ts)
    _executor.submit(_handle_brain_turn, cfg, store, client, channel, thread_ts, msg_ts, user_id, prompt, placeholder_ts)


def build_app(cfg: Config) -> App:
    app = App(token=cfg.slack_bot_token, signing_secret=cfg.slack_signing_secret)
    store = ThreadStore(cfg.telly_home)
    brain.restore_sessions(store.session_map())
    _start_background_trigger_loop(cfg, store, app.client)

    # Start video streaming server for browser tasks
    get_stream_server().start()

    def _allowed(user_id: str) -> bool:
        return not cfg.allowed_slack_user_ids or user_id in cfg.allowed_slack_user_ids

    @app.event("app_mention")
    def on_mention(event, client, ack):
        ack()
        user_id = event.get("user", "")
        channel = event["channel"]
        msg_ts = event["ts"]
        thread_ts = event.get("thread_ts") or msg_ts
        text = _strip_mentions(event.get("text", ""))

        if not _allowed(user_id):
            log.warning("rejecting mention from non-allowlisted user %s", user_id)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                text=f"<@{user_id}> sorry, you're not on telly's allowlist on this host.")
            return
        if not text:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="(say something after the mention)")
            return

        if _handle_trigger_command(store, client, channel, thread_ts, text):
            return

        file_paths = _download_slack_files(cfg, event)
        log.info("mention from %s in %s thread=%s: %s", user_id, channel, thread_ts, text[:120])
        _kickoff(cfg, store, client, channel, thread_ts, msg_ts, user_id, text, file_paths)

    @app.event("message")
    def on_message(event, client, ack):
        ack()

        # Skip edits, deletes, channel joins, bot messages, etc.
        if event.get("subtype"):
            return
        if event.get("bot_id"):
            return

        user_id = event.get("user", "")
        if not user_id or not _allowed(user_id):
            return

        bot_uid = _get_bot_user_id(client)
        if bot_uid and user_id == bot_uid:
            return

        channel = event["channel"]
        msg_ts = event["ts"]
        is_dm = event.get("channel_type") == "im"
        thread_ts = event.get("thread_ts") or (msg_ts if is_dm else None)
        if not thread_ts:
            return  # non-DM messages are only continuations within an existing thread

        if not is_dm and not (brain.has_session_for_thread(thread_ts) or store.get(thread_ts)):
            return  # only threads telly has already joined

        text = event.get("text", "") or ""
        if not text:
            return

        # If user @mentioned telly, app_mention will handle it — don't double-fire.
        if bot_uid and f"<@{bot_uid}>" in text:
            return

        clean = _strip_mentions(text)
        if _handle_trigger_command(store, client, channel, thread_ts, clean):
            return
        file_paths = _download_slack_files(cfg, event)
        log.info("thread continuation from %s in %s thread=%s: %s", user_id, channel, thread_ts, clean[:120])
        _kickoff(cfg, store, client, channel, thread_ts, msg_ts, user_id, clean, file_paths)

    @app.event("reaction_added")
    def on_reaction_added(event, client, ack):
        ack()
        actor = event.get("user", "")
        if not _allowed(actor):
            return
        bot_uid = _get_bot_user_id(client)
        if not bot_uid or actor == bot_uid:
            return

        item = event.get("item", {}) or {}
        if item.get("type") != "message":
            return
        channel = item.get("channel")
        msg_ts = item.get("ts")
        emoji = event.get("reaction", "")
        if not channel or not msg_ts:
            return

        try:
            history = client.conversations_history(
                channel=channel, latest=msg_ts, oldest=msg_ts, inclusive=True, limit=1
            )
            messages = history.get("messages", [])
            if not messages:
                history = client.conversations_replies(channel=channel, ts=msg_ts, limit=1)
                messages = history.get("messages", [])
        except Exception:
            log.exception("could not fetch reacted message")
            return

        if not messages:
            return
        msg = messages[0]
        if msg.get("user") != bot_uid and msg.get("bot_id") is None:
            return

        thread_ts = msg.get("thread_ts") or msg.get("ts") or msg_ts
        original_text = msg.get("text", "")
        log.info("reaction :%s: from %s on telly msg in thread %s", emoji, actor, thread_ts)

        prompt = (
            f"[Slack reaction] <@{actor}> reacted with :{emoji}: to a previous message you sent "
            f"in this thread. The message they reacted to was:\n\n"
            f"\"\"\"\n{original_text}\n\"\"\"\n\n"
            f"Interpret the emoji as a signal from the user. If it asks you to do something "
            f"(redo, explain more, stop, save, follow up, etc.) — do it now and reply briefly. "
            f"If it's just passive acknowledgement (thumbs up, heart, eyes, etc.), reply with "
            f"exactly: {NO_REPLY_TOKEN}"
        )
        _kickoff(cfg, store, client, channel, thread_ts, None, actor, prompt)

    @app.event("app_home_opened")
    def on_app_home_opened(event, client, ack):
        ack()
        user_id = event.get("user")
        if not user_id or not _allowed(user_id):
            return
        try:
            client.views_publish(user_id=user_id, view=slackfmt.build_home_view(store.list_recent()))
        except Exception:
            log.exception("app home publish failed")

    @app.event("assistant_thread_started")
    def on_assistant_thread_started(event, client, ack):
        ack()
        assistant_thread = event.get("assistant_thread", {}) or {}
        channel = assistant_thread.get("channel_id") or event.get("channel")
        thread_ts = assistant_thread.get("thread_ts") or event.get("thread_ts") or event.get("ts")
        user_id = event.get("user") or event.get("user_id", "")
        if not channel or not thread_ts or (user_id and not _allowed(user_id)):
            return
        store.ensure(thread_ts, channel=channel, user=user_id, status="started")
        _set_suggested_prompts(client, channel, thread_ts)
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Ask me to inspect code, debug an error, review changes, or summarize uploaded files.",
            )
        except Exception as e:
            log.debug("assistant welcome failed: %s", e)

    @app.event("assistant_thread_context_changed")
    def on_assistant_context_changed(event, ack):
        ack()
        assistant_thread = event.get("assistant_thread", {}) or {}
        thread_ts = assistant_thread.get("thread_ts")
        context = assistant_thread.get("context", {}) or {}
        if thread_ts:
            store.update(thread_ts, channel=context.get("channel_id"), status="context_changed")

    @app.action("telly_stop")
    def on_stop(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        thread_ts = body["actions"][0].get("value")
        if not thread_ts:
            return
        killed = brain.cancel_thread(thread_ts)
        if killed:
            store.update(thread_ts, status="cancelled")
        log.info("stop from %s in thread %s (killed=%s)", actor, thread_ts, killed)
        if not killed:
            channel = body.get("channel", {}).get("id")
            if channel:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                    text="(nothing to stop — turn already finished)")

    @app.action("telly_feedback")
    def on_feedback(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        action = (body.get("actions") or [{}])[0]
        value = action.get("value") or action.get("selected_feedback") or ""
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        thread_ts = value.split(":", 1)[1] if ":" in value else body.get("message", {}).get("thread_ts")
        log.info("feedback from %s value=%s thread=%s", actor, value, thread_ts)
        store.log_feedback({"user": actor, "value": value, "thread_ts": thread_ts, "message_ts": message_ts})
        if channel and message_ts:
            try:
                client.reactions_add(channel=channel, timestamp=message_ts, name="memo")
            except Exception as e:
                log.debug("feedback acknowledgement reaction failed: %s", e)

    @app.action("telly_regenerate")
    def on_regenerate(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        channel = body.get("channel", {}).get("id")
        thread_ts = body["actions"][0].get("value")
        if not channel or not thread_ts:
            return
        last_msg = brain.last_message_for_thread(thread_ts)
        if not last_msg:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                text="(nothing to regenerate — no prior message tracked for this thread)")
            return
        prompt = (
            "The user clicked Regenerate. Produce a different or improved answer to their previous "
            f"question:\n\n\"\"\"\n{last_msg}\n\"\"\""
        )
        log.info("regenerate from %s in thread %s", actor, thread_ts)
        _kickoff(cfg, store, client, channel, thread_ts, None, actor, prompt)

    @app.action("telly_fork")
    def on_fork(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        channel = body.get("channel", {}).get("id")
        thread_ts = body["actions"][0].get("value")
        if not channel or not thread_ts:
            return
        brain.fork_thread(thread_ts)
        store.update(thread_ts, session_id=None, status="forked")
        log.info("fork from %s in thread %s", actor, thread_ts)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts,
            text=":fork_and_knife: Forked — your next message in this thread starts a fresh conversation.")

    @app.command("/telly")
    def on_telly_command(ack, command, client):
        ack()
        user_id = command.get("user_id", "")
        channel = command.get("channel_id", "")
        text = (command.get("text") or "").strip()
        if not _allowed(user_id):
            client.chat_postEphemeral(channel=channel, user=user_id, text="You're not on telly's allowlist.")
            return
        if text in ("", "help"):
            client.chat_postEphemeral(channel=channel, user=user_id, text="Commands: `/telly status`, `/telly sessions`, `/telly stop <thread_ts>`.")
            return
        if text == "status":
            client.chat_postEphemeral(channel=channel, user=user_id, text=f"telly is using `{cfg.main_agent_cmd}`; {len(store.list_recent(999))} threads tracked.")
            return
        if text == "sessions":
            lines = [f"- `{s.thread_ts}` {s.status}: {(s.last_message or '')[:80]}" for s in store.list_recent(10)]
            client.chat_postEphemeral(channel=channel, user=user_id, text="\n".join(lines) or "No sessions tracked.")
            return
        if text.startswith("stop "):
            thread_ts = text.split(maxsplit=1)[1]
            killed = brain.cancel_thread(thread_ts)
            if killed:
                store.update(thread_ts, status="cancelled")
            client.chat_postEphemeral(channel=channel, user=user_id, text="Stopped." if killed else "Nothing running for that thread.")
            return
        client.chat_postEphemeral(channel=channel, user=user_id, text="Unknown `/telly` command. Try `/telly help`.")

    @app.shortcut("telly_ask_about_this")
    def on_ask_about_this(ack, shortcut, client):
        ack()
        user_id = shortcut.get("user", {}).get("id", "")
        if not _allowed(user_id):
            return
        channel = shortcut.get("channel", {}).get("id")
        message = shortcut.get("message", {}) or {}
        msg_ts = message.get("ts")
        if not channel or not msg_ts:
            return
        thread_ts = message.get("thread_ts") or msg_ts
        prompt = "Explain this Slack message and suggest useful next actions:\n\n" + (message.get("text") or "")
        _kickoff(cfg, store, client, channel, thread_ts, None, user_id, prompt)

    return app


def run(cfg: Config) -> None:
    app = build_app(cfg)
    log.info("starting Socket Mode handler")
    SocketModeHandler(app, cfg.slack_app_token).start()
