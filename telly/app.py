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
from .agent_stream import AgentReplyStream, LOADING_MESSAGES
from .config import Config
from .state import ThreadStore
from .video_stream import get_stream_server, VideoStreamSession, STREAM_HOST, STREAM_PORT

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
NO_REPLY_TOKEN = "NO_REPLY"
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


def _set_status(
    client,
    channel: str,
    thread_ts: str,
    status: str,
    *,
    loading_messages: list[str] | None = None,
) -> None:
    """Set assistant/agent status. Empty status clears the indicator."""
    kwargs: dict[str, Any] = {
        "channel_id": channel,
        "thread_ts": thread_ts,
        "status": status,
    }
    if status and loading_messages:
        kwargs["loading_messages"] = loading_messages
    _call_assistant(client, "assistant_threads_setStatus", **kwargs)


def _set_title(client, channel: str, thread_ts: str, text: str) -> None:
    title = re.sub(r"\s+", " ", text).strip()[:80] or "Telly thread"
    _call_assistant(client, "assistant_threads_setTitle", channel_id=channel, thread_ts=thread_ts, title=title)


DEFAULT_SUGGESTED_PROMPTS = [
    {"title": "Analyze this project", "message": "Analyze this project and suggest the highest-impact improvements."},
    {"title": "Review recent changes", "message": "Review the recent changes and call out risks."},
    {"title": "Debug an error", "message": "Help me debug this error. I will paste logs or upload a file."},
    {"title": "Summarize files", "message": "Summarize the files I upload and tell me what matters."},
]


def _set_suggested_prompts(
    client,
    channel: str,
    thread_ts: str | None = None,
    *,
    context_channel_id: str | None = None,
) -> None:
    """Set suggested prompts for assistant_view (per-thread) or agent_view (Messages tab)."""
    prompts = list(DEFAULT_SUGGESTED_PROMPTS)
    if context_channel_id:
        prompts = [
            {
                "title": "Summarize this channel",
                "message": f"Summarize recent discussion in <#{context_channel_id}> and call out open questions.",
            },
            *prompts[:3],
        ]
    kwargs: dict[str, Any] = {
        "channel_id": channel,
        "title": "Try telly with:",
        "prompts": prompts[:4],
    }
    # agent_view pins prompts at the top of Messages and does not require thread_ts.
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    _call_assistant(client, "assistant_threads_setSuggestedPrompts", **kwargs)


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


def _build_final_message_parts(
    turn: brain.BrainTurn,
    thread_ts: str,
    *,
    live_browser_url: str | None = None,
) -> tuple[str, str, list[dict], list[dict], list[dict], list[Path], str, str | None]:
    """Return display_md, notify, trailing, legacy, fallback, uploads, full_md, confirm_summary."""
    reply, upload_paths = _extract_upload_markers(turn.reply)
    reply, confirm_summary = slackfmt.extract_confirm_request(reply)
    reply, content_blocks = slackfmt.extract_native_content(reply)
    content_blocks = slackfmt.sanitize_blocks_for_messages(content_blocks)

    if not confirm_summary and slackfmt.looks_like_confirmation_request(reply):
        # Soft-detect "should I proceed?" style asks and surface real buttons
        confirm_summary = _truncate_confirm_summary(reply)

    empty_placeholder = (
        "Here's the structured result."
        if content_blocks
        else ("Uploaded requested file." if upload_paths else "Done.")
    )
    markdown_text = (reply or empty_placeholder).strip()
    notify_text = slackfmt.to_mrkdwn(markdown_text)

    breadcrumbs_block = slackfmt.build_breadcrumb_block(turn.tool_breadcrumbs)
    footer_block = slackfmt.build_footer_block(turn.session_id, turn.cost_usd, turn.duration_ms)
    # When awaiting permission, only show Approve/Deny — not regenerate/fork noise
    actions_block = None if confirm_summary else slackfmt.build_actions_block(thread_ts)
    feedback_block = None if confirm_summary else slackfmt.build_feedback_block(thread_ts)
    tool_events = turn.tool_events or turn.tool_breadcrumbs
    sources = slackfmt.collect_task_sources(tool_events, live_browser_url=live_browser_url)
    file_paths = slackfmt.file_paths_from_tool_events(tool_events)
    completion_block = slackfmt.build_completion_task_block(
        thread_ts,
        turn.duration_ms,
        sources=sources,
        file_paths=file_paths,
    )

    # Message-safe leading chrome (never use modal-only alert in messages)
    leading: list[dict] = []
    if confirm_summary:
        leading.extend(slackfmt.build_permission_blocks(thread_ts, confirm_summary))
    else:
        leading.append(slackfmt.build_message_status_block("Response ready.", level="success"))
        try:
            leading.append(slackfmt.build_completion_card(
                thread_ts, turn.duration_ms, sources=sources, file_paths=file_paths,
            ))
        except (TypeError, ValueError):
            pass

    display_markdown = markdown_text
    if len(markdown_text) > LONG_REPLY_FILE_THRESHOLD:
        display_markdown = "The answer was long, so I attached the full response as a Markdown file."

    trailing_blocks = slackfmt.sanitize_blocks_for_messages(
        leading + slackfmt.build_blocks(
            "",
            completion_block=None if confirm_summary else completion_block,
            breadcrumbs_block=breadcrumbs_block,
            content_blocks=content_blocks,
            actions_block=actions_block,
            feedback_block=feedback_block,
            footer=footer_block,
        )
    )
    legacy_blocks = slackfmt.sanitize_blocks_for_messages(
        leading + slackfmt.build_blocks(
            display_markdown,
            completion_block=None if confirm_summary else completion_block,
            breadcrumbs_block=breadcrumbs_block,
            content_blocks=content_blocks,
            actions_block=actions_block,
            feedback_block=feedback_block,
            footer=footer_block,
        )
    )
    # Ultra-safe fallback: section + permission buttons OR section + actions
    if confirm_summary:
        legacy_fallback = slackfmt.build_permission_blocks(thread_ts, confirm_summary)
        if display_markdown:
            legacy_fallback = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": slackfmt.to_mrkdwn(display_markdown)[:3000]},
                    "expand": True,
                },
                *legacy_fallback,
            ]
    else:
        legacy_fallback = slackfmt.build_blocks(
            display_markdown,
            breadcrumbs_block=breadcrumbs_block,
            actions_block=slackfmt.build_actions_block(thread_ts),
            footer=footer_block,
            use_markdown_block=False,
        )
    return (
        display_markdown,
        notify_text[:3000] or display_markdown,
        trailing_blocks,
        legacy_blocks,
        legacy_fallback,
        upload_paths,
        markdown_text,
        confirm_summary,
    )


def _truncate_confirm_summary(text: str) -> str:
    # Prefer last non-empty line as the ask
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return "Proceed with the requested action?"
    # If the last lines look like the question, use them
    tail = " ".join(lines[-3:])
    return tail[:400]


def _finalize_reply(
    client,
    channel: str,
    thread_ts: str,
    reply_stream: AgentReplyStream | None,
    turn: brain.BrainTurn,
    *,
    live_browser_url: str | None = None,
) -> None:
    (
        display_markdown,
        notify_text,
        trailing_blocks,
        legacy_blocks,
        legacy_fallback,
        upload_paths,
        full_markdown,
        confirm_summary,
    ) = _build_final_message_parts(turn, thread_ts, live_browser_url=live_browser_url)

    if len(full_markdown) > LONG_REPLY_FILE_THRESHOLD:
        _try_upload_long_reply(client, channel, thread_ts, full_markdown)

    tool_events = turn.tool_events or turn.tool_breadcrumbs
    if reply_stream:
        reply_stream.finish_success(
            markdown_text=display_markdown if not confirm_summary else (display_markdown or "Permission needed."),
            blocks=trailing_blocks if reply_stream.using_stream else legacy_blocks,
            fallback_blocks=legacy_fallback,
            notify_text=notify_text,
            tool_events=tool_events,
        )
    else:
        for tier in slackfmt.block_fallback_tiers(legacy_blocks, reply_text=display_markdown):
            try:
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=notify_text, blocks=tier)
                break
            except Exception:
                continue
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=notify_text, blocks=legacy_fallback)

    if confirm_summary:
        log.info("permission UI posted thread=%s summary=%s", thread_ts, confirm_summary[:80])

    upload_failures = _upload_reply_files(client, channel, thread_ts, upload_paths)
    if upload_failures:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Could not upload file attachment:\n" + "\n".join(f"- {failure}" for failure in upload_failures),
        )

    if not (reply_stream and reply_stream.using_stream) and len(full_markdown) <= LONG_REPLY_FILE_THRESHOLD and not confirm_summary:
        chunks = slackfmt.chunk_for_slack(full_markdown, limit=slackfmt.SLACK_MAX_MARKDOWN)
        for chunk in chunks[1:]:
            for tier in slackfmt.block_fallback_tiers(
                slackfmt.build_blocks(chunk),
                reply_text=chunk,
            ):
                try:
                    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk[:3000], blocks=tier)
                    break
                except Exception:
                    continue


def _is_browser_task(text: str) -> bool:
    """Detect if the prompt likely involves browser automation."""
    keywords = (
        "browser", "navigate", "visit", "screenshot", "click", "fill", "form",
        "web", "page", "link", "url", "scrape", "automat", "interact", "submit"
    )
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)


def _build_live_browser_instruction(stream_id: str) -> str:
    """Build instruction for agent-visible live browser sharing."""
    return (
        f"[Live Browser Sharing] A live browser feed is already prepared for this task.\n"
        f"Use the available Playwright MCP tools or `telly.browser` CLI normally; telly captures the active Chrome page through remote debugging.\n"
        f"Do not call `browser_start_video`, `browser_video_show_actions`, or `browser_stop_video`; current Playwright MCP does not expose those tools.\n"
        f"The live feed is at: http://{STREAM_HOST}:{STREAM_PORT}/stream/{stream_id}\n"
        f"Keep the browser page active while doing visible browser work so the user can watch it live."
    )


def _handle_brain_turn(
    cfg: Config,
    store: ThreadStore,
    client,
    channel: str,
    thread_ts: str,
    msg_ts: str | None,
    user_id: str,
    text: str,
    *,
    team_id: str | None = None,
    background: bool = False,
    success_reaction: str = "white_check_mark",
) -> None:
    prior = store.get(thread_ts)
    if prior and prior.session_id:
        brain.set_session_for_thread(thread_ts, prior.session_id)
    store.update(thread_ts, channel=channel, user=user_id, last_message=text, agent_cmd=cfg.main_agent_cmd, status="running")
    _set_status(
        client,
        channel,
        thread_ts,
        "is working on your request…",
        loading_messages=LOADING_MESSAGES,
    )

    # Detect browser tasks and start video streaming
    stream_id = prior.video_stream_id if prior and _is_browser_task(text) else None
    stream_session = None
    live_browser_url: str | None = None
    if _is_browser_task(text):
        if stream_id is None:
            stream_id = str(uuid.uuid4())[:12]
            store.update(thread_ts, video_stream_id=stream_id)
        try:
            stream_session = VideoStreamSession(stream_id, task_name=text[:60])
            stream_session.start()
            live_browser_url = stream_session.get_url()
            store.update(thread_ts, video_stream_id=stream_id)
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"🎬 *Live browser session:* <{live_browser_url}|Watch live>",
                )
            except Exception as e:
                log.warning("Failed to post video stream link: %s", e)
        except Exception as e:
            log.warning("Failed to start video streaming: %s", e)
            stream_id = None
            stream_session = None
            live_browser_url = None

    reply_stream: AgentReplyStream | None = None
    if not background:
        reply_stream = AgentReplyStream(
            client=client,
            channel=channel,
            thread_ts=thread_ts,
            user_id=user_id,
            team_id=team_id,
            task_display_mode="plan",
            live_browser_url=live_browser_url,
        )
        reply_stream.start()
        if reply_stream.stream_ts or reply_stream.legacy_placeholder_ts:
            store.update(
                thread_ts,
                status="running",
            )

    on_progress = reply_stream.make_progress_callback() if reply_stream else None
    try:
        turn = brain.ask_brain(cfg.main_agent_cmd, thread_ts, text, cfg.turn_log_path, on_progress=on_progress)
    except brain.BrainCancelled:
        log.info("brain turn cancelled by user thread=%s", thread_ts)
        store.update(thread_ts, status="cancelled")
        _set_status(client, channel, thread_ts, "")
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", "octagonal_sign")
        if reply_stream:
            reply_stream.finish_cancelled()
        return
    except Exception as e:
        log.exception("brain failure")
        store.update(thread_ts, status="error")
        _set_status(client, channel, thread_ts, "")
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", "x")
        if reply_stream:
            reply_stream.finish_error(e)
        else:
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f":warning: brain error: `{e}`",
                    blocks=slackfmt.build_basic_error_blocks(thread_ts, str(e)),
                )
            except Exception:
                log.exception("error post failed")
        return
    finally:
        if stream_session:
            try:
                stream_session.stop()
                stream_session.cleanup(delay_s=5.0)
            except Exception as e:
                log.debug("Error during video stream cleanup: %s", e)

    if turn.reply.strip() == NO_REPLY_TOKEN:
        log.info("brain returned NO_REPLY — staying silent")
        if reply_stream:
            reply_stream.discard()
        if msg_ts:
            _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", success_reaction)
        store.update(thread_ts, session_id=turn.session_id, status="silent")
        _set_status(client, channel, thread_ts, "")
        return

    store.update(thread_ts, session_id=turn.session_id, status="complete")
    _set_status(client, channel, thread_ts, "")
    if msg_ts:
        _swap_reaction(client, channel, msg_ts, "hourglass_flowing_sand", success_reaction)
    _finalize_reply(
        client,
        channel,
        thread_ts,
        reply_stream,
        turn,
        live_browser_url=live_browser_url,
    )


def _kickoff(
    cfg: Config,
    store: ThreadStore,
    client,
    channel: str,
    thread_ts: str,
    msg_ts: str | None,
    user_id: str,
    text: str,
    file_paths: list[str] | None = None,
    background: bool = False,
    team_id: str | None = None,
) -> None:
    if msg_ts:
        _swap_reaction(client, channel, msg_ts, None, "hourglass_flowing_sand")
    context_note = _fetch_discussion_context(client, channel, thread_ts, msg_ts) if _wants_discussion_context(text) else None
    state = store.get(thread_ts)
    trigger_note = _format_trigger_note(state.triggers if state else None)
    is_browser_task = _is_browser_task(text)
    stream_id = str(uuid.uuid4())[:12] if is_browser_task else None

    notes = [context_note, trigger_note]
    if stream_id:
        notes.append(_build_live_browser_instruction(stream_id))

    prompt = _augment_prompt(text, file_paths or [], "\n\n".join(note for note in notes if note))
    store.update(
        thread_ts,
        channel=channel,
        user=user_id,
        last_message=text,
        agent_cmd=cfg.main_agent_cmd,
        uploaded_files=file_paths or None,
        video_stream_id=stream_id,
        status="queued",
    )
    _set_title(client, channel, thread_ts, text)
    # Status opens the agent thread and shows loading immediately (official agent loop).
    _set_status(
        client,
        channel,
        thread_ts,
        "is working on your request…",
        loading_messages=LOADING_MESSAGES,
    )
    _executor.submit(
        _handle_brain_turn,
        cfg,
        store,
        client,
        channel,
        thread_ts,
        msg_ts,
        user_id,
        prompt,
        team_id=team_id,
        background=background,
    )


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
        _kickoff(
            cfg, store, client, channel, thread_ts, msg_ts, user_id, text, file_paths,
            team_id=event.get("team") or event.get("team_id"),
        )

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
        prior_dm = store.latest_for_channel_user(channel, user_id) if is_dm else None
        thread_ts = event.get("thread_ts") or (prior_dm.thread_ts if prior_dm else None) or (msg_ts if is_dm else None)
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
        # Prefer app_context from agent_view message.im payloads when present.
        app_context = event.get("app_context") or event.get("context") or {}
        context_channel = None
        if isinstance(app_context, dict):
            entities = app_context.get("entities") or []
            for ent in entities:
                if isinstance(ent, dict) and ent.get("type") == "slack#/types/channel_id":
                    context_channel = ent.get("value")
                    break
            context_channel = context_channel or app_context.get("channel_id")
        if context_channel:
            store.update(thread_ts, channel=context_channel)
        _kickoff(
            cfg, store, client, channel, thread_ts, msg_ts, user_id, clean, file_paths,
            team_id=event.get("team") or event.get("team_id"),
        )

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
        tab = event.get("tab") or "home"
        # agent_view: opening the Messages tab is the DM open signal (replaces assistant_thread_started).
        if tab == "messages":
            channel = event.get("channel")
            context = event.get("context") or {}
            context_channel = context.get("channel_id") if isinstance(context, dict) else None
            if channel:
                _set_suggested_prompts(
                    client,
                    channel,
                    thread_ts=None,
                    context_channel_id=context_channel,
                )
            return
        try:
            client.views_publish(user_id=user_id, view=slackfmt.build_home_view(store.list_recent()))
        except Exception:
            log.exception("app home publish failed")

    @app.event("app_context_changed")
    def on_app_context_changed(event, client, ack):
        """agent_view: user changed what they're looking at while the agent is open."""
        ack()
        user_id = event.get("user") or event.get("user_id", "")
        if user_id and not _allowed(user_id):
            return
        context = event.get("context") or {}
        entities = context.get("entities") if isinstance(context, dict) else None
        channel_id = None
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict) and ent.get("type") == "slack#/types/channel_id":
                    channel_id = ent.get("value")
                    break
        if channel_id:
            log.debug("app_context_changed user=%s channel=%s", user_id, channel_id)
            # Refresh prompts for the DM messages surface when we know the channel.
            dm = event.get("channel")
            if dm:
                _set_suggested_prompts(client, dm, thread_ts=None, context_channel_id=channel_id)

    @app.event("assistant_thread_started")
    def on_assistant_thread_started(event, client, ack):
        """assistant_view (legacy) + still useful while migrating to agent_view."""
        ack()
        assistant_thread = event.get("assistant_thread", {}) or {}
        channel = assistant_thread.get("channel_id") or event.get("channel")
        thread_ts = assistant_thread.get("thread_ts") or event.get("thread_ts") or event.get("ts")
        user_id = event.get("user") or event.get("user_id", "")
        context = assistant_thread.get("context") or {}
        if not channel or not thread_ts or (user_id and not _allowed(user_id)):
            return
        store.ensure(thread_ts, channel=channel, user=user_id, status="started")
        _set_suggested_prompts(
            client,
            channel,
            thread_ts,
            context_channel_id=context.get("channel_id") if isinstance(context, dict) else None,
        )
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="Ask me to inspect code, debug an error, review changes, or summarize uploaded files.",
            )
        except Exception as e:
            log.debug("assistant welcome failed: %s", e)

    @app.event("assistant_thread_context_changed")
    def on_assistant_context_changed(event, client, ack):
        ack()
        assistant_thread = event.get("assistant_thread", {}) or {}
        thread_ts = assistant_thread.get("thread_ts")
        context = assistant_thread.get("context", {}) or {}
        channel = assistant_thread.get("channel_id")
        if thread_ts:
            store.update(thread_ts, channel=context.get("channel_id"), status="context_changed")
        if channel and context.get("channel_id"):
            _set_suggested_prompts(
                client,
                channel,
                thread_ts,
                context_channel_id=context.get("channel_id"),
            )

    @app.event("message_stream_stopped")
    def on_message_stream_stopped(event, client, ack):
        """User stopped a stoppable chat stream from the Slack UI."""
        ack()
        user_id = event.get("user") or event.get("user_id", "")
        if user_id and not _allowed(user_id):
            return
        thread_ts = event.get("thread_ts") or event.get("message_ts")
        if not thread_ts:
            return
        killed = brain.cancel_thread(thread_ts)
        if killed:
            store.update(thread_ts, status="cancelled")
        log.info("message_stream_stopped by %s thread=%s killed=%s", user_id, thread_ts, killed)

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
        polarity = value.split(":", 1)[0] if ":" in value else value
        log.info("feedback from %s value=%s thread=%s", actor, value, thread_ts)
        store.log_feedback({
            "user": actor,
            "value": value,
            "polarity": polarity,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
        })

        if polarity in {"negative", "bad"} and body.get("trigger_id") and thread_ts:
            try:
                client.views_open(
                    trigger_id=body["trigger_id"],
                    view=slackfmt.build_feedback_modal(
                        thread_ts=thread_ts,
                        channel=channel,
                        message_ts=message_ts,
                    ),
                )
            except Exception as e:
                log.debug("feedback modal open failed: %s", e)
            return

        if channel and message_ts:
            try:
                client.reactions_add(channel=channel, timestamp=message_ts, name="+1")
            except Exception as e:
                log.debug("feedback acknowledgement reaction failed: %s", e)

    @app.view("telly_feedback_modal")
    def on_feedback_modal(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        view = body.get("view") or {}
        parsed = slackfmt.parse_feedback_modal_submission(view)
        log.info(
            "feedback modal from %s category=%s thread=%s",
            actor,
            parsed.get("category"),
            parsed.get("thread_ts"),
        )
        store.log_feedback({
            "user": actor,
            "value": "negative",
            "polarity": "negative",
            "category": parsed.get("category"),
            "notes": parsed.get("notes"),
            "thread_ts": parsed.get("thread_ts"),
            "message_ts": parsed.get("message_ts"),
            "source": "modal",
        })
        channel = parsed.get("channel")
        message_ts = parsed.get("message_ts")
        if channel and message_ts:
            try:
                client.reactions_add(channel=channel, timestamp=message_ts, name="memo")
            except Exception as e:
                log.debug("feedback modal reaction failed: %s", e)

    @app.action("telly_confirm_approve")
    def on_confirm_approve(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        action = (body.get("actions") or [{}])[0]
        raw = action.get("value") or ""
        thread_ts, _, summary = raw.partition("|")
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        if not channel or not thread_ts:
            return
        # Disable buttons by updating the message
        try:
            if message_ts:
                client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=f":white_check_mark: Approved by <@{actor}>: {summary or 'action'}",
                    blocks=[{
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":white_check_mark: *Approved* by <@{actor}>\n{summary or 'Proceeding.'}",
                        },
                    }],
                )
        except Exception as e:
            log.debug("confirm approve message update failed: %s", e)
        prompt = (
            f"[Slack permission] <@{actor}> clicked *Approve* for this action:\n"
            f"{summary or 'the pending action'}\n\n"
            "Proceed with the approved action now. Do not ask for confirmation again for this step. "
            "Report what you did when finished."
        )
        log.info("permission APPROVED by %s thread=%s", actor, thread_ts)
        _kickoff(cfg, store, client, channel, thread_ts, None, actor, prompt)

    @app.action("telly_confirm_deny")
    def on_confirm_deny(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        action = (body.get("actions") or [{}])[0]
        raw = action.get("value") or ""
        thread_ts, _, summary = raw.partition("|")
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        if not channel or not thread_ts:
            return
        try:
            if message_ts:
                client.chat_update(
                    channel=channel,
                    ts=message_ts,
                    text=f":no_entry_sign: Denied by <@{actor}>: {summary or 'action'}",
                    blocks=[{
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":no_entry_sign: *Denied* by <@{actor}>\n{summary or 'Action cancelled.'}",
                        },
                    }],
                )
        except Exception as e:
            log.debug("confirm deny message update failed: %s", e)
        prompt = (
            f"[Slack permission] <@{actor}> clicked *Deny* for this action:\n"
            f"{summary or 'the pending action'}\n\n"
            "Do NOT perform that action. Acknowledge briefly that it was cancelled."
        )
        log.info("permission DENIED by %s thread=%s", actor, thread_ts)
        _kickoff(cfg, store, client, channel, thread_ts, None, actor, prompt)

    @app.action("telly_delete_message")
    def on_delete_message(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        if channel and message_ts:
            try:
                client.chat_delete(channel=channel, ts=message_ts)
            except Exception as e:
                log.debug("delete message failed: %s", e)

    @app.action("telly_overflow")
    def on_overflow(ack, body, client):
        ack()
        actor = body.get("user", {}).get("id", "")
        if not _allowed(actor):
            return
        action = (body.get("actions") or [{}])[0]
        selected = (action.get("selected_option") or {}).get("value") or ""
        channel = body.get("channel", {}).get("id")
        if ":" not in selected:
            return
        kind, thread_ts = selected.split(":", 1)
        if kind == "stop" and thread_ts:
            killed = brain.cancel_thread(thread_ts)
            if killed:
                store.update(thread_ts, status="cancelled")
            if channel:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="Stopped." if killed else "(nothing running for that thread)",
                )
        elif kind == "fork" and thread_ts:
            brain.fork_thread(thread_ts)
            store.update(thread_ts, session_id=None, status="forked")
            if channel:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=":fork_and_knife: Forked — next message starts a fresh conversation.",
                )
        elif kind == "list_triggers" and thread_ts and channel:
            state = store.get(thread_ts)
            triggers = (state.triggers if state else None) or {}
            if not triggers:
                text = "No triggers on this thread."
            else:
                lines = [f"• *{name}*: {payload.get('instruction', '')[:120]}" for name, payload in triggers.items()]
                text = "Triggers:\n" + "\n".join(lines)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        elif kind == "home" and actor:
            try:
                client.views_publish(user_id=actor, view=slackfmt.build_home_view(store.list_recent()))
            except Exception as e:
                log.debug("overflow home publish failed: %s", e)

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
