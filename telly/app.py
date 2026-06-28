import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import brain, slackfmt
from .config import Config
from .state import ThreadStore

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
NO_REPLY_TOKEN = "NO_REPLY"
PROGRESS_PLACEHOLDER_TEXT = "telly is working..."
LONG_REPLY_FILE_THRESHOLD = 9000
UPLOAD_MARKER_RE = re.compile(r"^\s*(?:SLACK_UPLOAD|ATTACH_FILE):\s*(.+?)\s*$", re.MULTILINE)

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


def _handle_brain_turn(
    cfg: Config, store: ThreadStore, client, channel: str, thread_ts: str, msg_ts: str | None,
    user_id: str, text: str, placeholder_ts: str | None, success_reaction: str = "white_check_mark",
) -> None:
    prior = store.get(thread_ts)
    if prior and prior.session_id:
        brain.set_session_for_thread(thread_ts, prior.session_id)
    store.update(thread_ts, channel=channel, user=user_id, last_message=text, agent_cmd=cfg.main_agent_cmd, status="running")
    _set_status(client, channel, thread_ts, "Working on your request...")
    try:
        turn = brain.ask_brain(cfg.main_agent_cmd, thread_ts, text, cfg.turn_log_path)
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


def _kickoff(cfg: Config, store: ThreadStore, client, channel: str, thread_ts: str, msg_ts: str | None, user_id: str, text: str, file_paths: list[str] | None = None) -> None:
    if msg_ts:
        _swap_reaction(client, channel, msg_ts, None, "hourglass_flowing_sand")
    prompt = _augment_prompt(text, file_paths or [])
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
    placeholder_ts = _post_placeholder(client, channel, thread_ts)
    _executor.submit(_handle_brain_turn, cfg, store, client, channel, thread_ts, msg_ts, user_id, prompt, placeholder_ts)


def build_app(cfg: Config) -> App:
    app = App(token=cfg.slack_bot_token, signing_secret=cfg.slack_signing_secret)
    store = ThreadStore(cfg.telly_home)
    brain.restore_sessions(store.session_map())

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
