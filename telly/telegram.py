"""Telegram backend for telly: the same local coding agent, driven from Telegram.

Feature parity with the Slack backend, expressed the Telegram way:

- Socket Mode            -> getUpdates long polling (no public URL, runs on your laptop)
- per-thread session     -> per-chat (+ forum topic) resumable agent session
- text streaming         -> a "working" message edited live with thinking steps
- loading status         -> sendChatAction("typing") + the working message
- Block Kit blocks       -> Telegram HTML, <pre> tables, real photo/video messages
- feedback buttons/modal -> inline 👍/👎 keyboard + a category keyboard on 👎
- Approve/Deny           -> inline callback buttons
- regenerate / fork      -> inline callback buttons
- Stop                   -> inline Stop button + /stop
- reactions              -> ⏳ on receipt, ✅ when done
- /telly commands        -> /status /sessions /stop /new /triggers /help
- suggested prompts      -> /start welcome with inline prompt buttons
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from . import brain, slackfmt, tgfmt
from .config import Config
from .state import ThreadStore

log = logging.getLogger(__name__)

TYPING_REFRESH_S = 4.5
EDIT_INTERVAL_S = 1.2
LONG_REPLY_DOC_THRESHOLD = 8000
NO_REPLY_TOKEN = "NO_REPLY"
UPLOAD_MARKER_RE = re.compile(r"^\s*(?:SLACK_UPLOAD|ATTACH_FILE):\s*(.+?)\s*$", re.MULTILINE)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TRIGGER_ADD_RE = re.compile(
    r"^\s*(?:add|set)\s+trigger(?:\s+(?:named\s+)?)?\s+(?P<name>[^:\n=>]+?)\s*(?::|=>)\s*(?P<instruction>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TRIGGER_REMOVE_RE = re.compile(r"^\s*(?:remove|delete)\s+trigger\s+(?P<name>.+?)\s*$", re.IGNORECASE)
_TRIGGER_START_RE = re.compile(
    r"^\s*start\s+trigger\s+(?P<name>.+?)\s+every\s+(?P<interval>.+?)\s*$", re.IGNORECASE
)
_TRIGGER_STOP_RE = re.compile(r"^\s*stop\s+trigger\s+(?P<name>.+?)\s*$", re.IGNORECASE)
_TRIGGER_LIST_RE = re.compile(r"^\s*list\s+triggers\s*$", re.IGNORECASE)
_TRIGGER_CLEAR_RE = re.compile(r"^\s*clear\s+triggers\s*$", re.IGNORECASE)

SUGGESTED_PROMPTS = [
    {"title": "Analyze this project", "message": "Analyze this project and suggest the highest-impact improvements."},
    {"title": "Review recent changes", "message": "Review the recent changes and call out risks."},
    {"title": "Debug an error", "message": "Help me debug this error. I'll paste logs or upload a file."},
    {"title": "Run a browser task", "message": "Use the browser to look something up and report back."},
]

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="telly-tg")
_thread_procs_lock = threading.Lock()


class TelegramError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #

class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, *, files: dict | None = None, http_timeout: int = 30, **params: Any) -> Any:
        url = f"{self.base}/{method}"
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            if files:
                # multipart/form-data: nested objects must be JSON strings.
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in clean.items()}
                resp = self.session.post(url, data=data, files=files, timeout=http_timeout)
            else:
                resp = self.session.post(url, json=clean, timeout=http_timeout)
        except requests.RequestException as e:
            log.warning("telegram %s transport error: %s", method, e)
            return None
        try:
            payload = resp.json()
        except ValueError:
            log.warning("telegram %s returned non-JSON: %s", method, resp.text[:200])
            return None
        if not payload.get("ok"):
            if payload.get("error_code") == 429:
                retry = int((payload.get("parameters") or {}).get("retry_after") or 1)
                log.warning("telegram %s rate-limited; sleeping %ss", method, retry)
                time.sleep(min(retry, 15))
            else:
                log.warning("telegram %s failed: %s", method, payload.get("description"))
            return None
        return payload.get("result")

    def get_me(self) -> Any:
        return self.call("getMe")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        result = self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "edited_message", "callback_query"],
        )
        return result or []

    def set_my_commands(self, commands: list[dict]) -> Any:
        return self.call("setMyCommands", commands=commands)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        reply_markup: dict | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        disable_notification: bool = False,
        link_preview: bool = False,
    ) -> dict | None:
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
            link_preview_options={"is_disabled": True} if not link_preview else None,
            reply_parameters={"message_id": reply_to_message_id} if reply_to_message_id else None,
            disable_notification=disable_notification or None,
        )

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        reply_markup: dict | None = None,
        link_preview: bool = False,
    ) -> dict | None:
        return self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True} if not link_preview else None,
        )

    def edit_message_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict | None) -> dict | None:
        return self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)

    def delete_message(self, chat_id: int | str, message_id: int) -> Any:
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def send_chat_action(self, chat_id: int | str, action: str = "typing", message_thread_id: int | None = None) -> Any:
        return self.call("sendChatAction", chat_id=chat_id, action=action, message_thread_id=message_thread_id)

    def set_reaction(self, chat_id: int | str, message_id: int, emoji: str | None) -> Any:
        reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
        return self.call("setMessageReaction", chat_id=chat_id, message_id=message_id, reaction=reaction)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def _send_media(self, method: str, field: str, chat_id, source, *, caption="", reply_markup=None,
                    reply_to_message_id=None, message_thread_id=None) -> dict | None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption or None,
            "parse_mode": "HTML" if caption else None,
            "reply_markup": reply_markup,
            "reply_to_message_id": reply_to_message_id,
            "message_thread_id": message_thread_id,
        }
        if source.startswith("http://") or source.startswith("https://"):
            return self.call(method, **{field: source}, **params)
        path = Path(source)
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            return self.call(method, files={field: (path.name, fh)}, **params)

    def send_photo(self, chat_id, source, **kw) -> dict | None:
        return self._send_media("sendPhoto", "photo", chat_id, source, **kw)

    def send_video(self, chat_id, source, **kw) -> dict | None:
        return self._send_media("sendVideo", "video", chat_id, source, **kw)

    def send_document(self, chat_id, source, **kw) -> dict | None:
        return self._send_media("sendDocument", "document", chat_id, source, **kw)

    def get_file(self, file_id: str) -> str | None:
        result = self.call("getFile", file_id=file_id)
        if isinstance(result, dict):
            return result.get("file_path")
        return None

    def download(self, file_path: str, dest: Path) -> bool:
        try:
            resp = self.session.get(f"{self.file_base}/{file_path}", timeout=60)
            resp.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            return True
        except requests.RequestException as e:
            log.warning("telegram file download failed: %s", e)
            return False


# --------------------------------------------------------------------------- #
# In-flight progress (the live "working" message)
# --------------------------------------------------------------------------- #

class TelegramProgress:
    def __init__(self, client: TelegramClient, chat_id, thread_key: str, *, message_thread_id=None, reply_to=None):
        self.client = client
        self.chat_id = chat_id
        self.thread_key = thread_key
        self.message_thread_id = message_thread_id
        self.reply_to = reply_to
        self.message_id: int | None = None
        self._last_sent = 0.0
        self._last_text = ""
        self._typing_at = 0.0

    def start(self) -> None:
        self._typing()
        res = self.client.send_message(
            self.chat_id,
            "<i>Working on it\u2026</i>",
            reply_markup=tgfmt.stop_keyboard(self.thread_key),
            reply_to_message_id=self.reply_to,
            message_thread_id=self.message_thread_id,
        )
        if res:
            self.message_id = res.get("message_id")

    def _typing(self) -> None:
        now = time.monotonic()
        if now - self._typing_at >= TYPING_REFRESH_S:
            self._typing_at = now
            self.client.send_chat_action(self.chat_id, "typing", self.message_thread_id)

    def on_progress(self, state: dict) -> None:
        self._typing()
        if not self.message_id:
            return
        now = time.monotonic()
        if now - self._last_sent < EDIT_INTERVAL_S:
            return
        html = tgfmt.render_activity(state.get("tool_events"), state.get("partial_text"))
        if html == self._last_text:
            return
        self._last_sent = now
        self._last_text = html
        self.client.edit_message_text(
            self.chat_id, self.message_id, html, reply_markup=tgfmt.stop_keyboard(self.thread_key)
        )

    def make_callback(self):
        return lambda state: self.on_progress(state)

    def finish(self, html: str, *, reply_markup: dict | None) -> bool:
        """Replace the working message with the final answer. True if edited."""
        if not self.message_id:
            return False
        return self.client.edit_message_text(
            self.chat_id, self.message_id, html, reply_markup=reply_markup
        ) is not None

    def discard(self) -> None:
        if self.message_id:
            self.client.delete_message(self.chat_id, self.message_id)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

class TelegramBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = TelegramClient(cfg.telegram_bot_token)
        # Separate store file so Telegram and Slack backends never race on writes.
        self.store = ThreadStore(cfg.telly_home, filename="telegram-threads.json")
        self.upload_root = cfg.telly_home / "telegram-uploads"
        self._offset: int | None = None
        self._running = True

    # -- access control ----------------------------------------------------- #

    def _allowed(self, user_id: Any, chat_id: Any) -> bool:
        uid, cid = str(user_id), str(chat_id)
        if self.cfg.allowed_telegram_user_ids and uid not in self.cfg.allowed_telegram_user_ids:
            return False
        if self.cfg.allowed_telegram_chat_ids and cid not in self.cfg.allowed_telegram_chat_ids:
            return False
        return True

    # -- run loop ----------------------------------------------------------- #

    def run(self) -> None:
        me = self.client.get_me()
        if not me:
            raise TelegramError("could not authenticate with Telegram (check TELEGRAM_BOT_TOKEN)")
        log.info("telegram bot @%s (%s)", me.get("username"), me.get("id"))
        self.client.set_my_commands([
            {"command": "start", "description": "Start a conversation with telly"},
            {"command": "new", "description": "Start a fresh conversation"},
            {"command": "status", "description": "Show the active agent CLI"},
            {"command": "sessions", "description": "List recent conversations"},
            {"command": "stop", "description": "Stop the running turn"},
            {"command": "triggers", "description": "List scheduled triggers"},
            {"command": "help", "description": "How to use telly"},
        ])
        brain.restore_sessions(self.store.session_map())
        threading.Thread(target=self._trigger_loop, name="telly-tg-triggers", daemon=True).start()

        while self._running:
            try:
                updates = self.client.get_updates(self._offset, timeout=30)
            except Exception:
                log.exception("getUpdates failed; backing off")
                time.sleep(3)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                try:
                    self._dispatch(update)
                except Exception:
                    log.exception("update dispatch failed: %s", update.get("update_id"))

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    # -- inbound messages --------------------------------------------------- #

    def _on_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = message.get("from") or {}
        user_id = user.get("id")
        if chat_id is None or user_id is None or user.get("is_bot"):
            return
        if not self._allowed(user_id, chat_id):
            log.warning("rejecting telegram message from disallowed user=%s chat=%s", user_id, chat_id)
            self.client.send_message(chat_id, "Sorry, you're not on telly's allowlist on this host.")
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        thread_key = self._thread_key(chat_id, message.get("message_thread_id"))
        message_thread_id = message.get("message_thread_id")
        message_id = message.get("message_id")

        if text.startswith("/"):
            if self._handle_command(chat_id, thread_key, message_thread_id, text):
                return

        if self._handle_trigger_command(chat_id, thread_key, text):
            return

        file_paths = self._download_attachments(message, thread_key)
        if not text and not file_paths:
            return

        prompt = self._build_prompt(text, file_paths, message)
        self._kickoff(chat_id, thread_key, message_id, message_thread_id, str(user_id), text or "(attachment)", prompt)

    def _thread_key(self, chat_id: Any, message_thread_id: Any) -> str:
        base = f"tg:{chat_id}"
        return f"{base}:{message_thread_id}" if message_thread_id else base

    def _build_prompt(self, text: str, file_paths: list[str], message: dict) -> str:
        parts = [text.strip()]
        if file_paths:
            parts.append("Attached Telegram files downloaded locally:\n" + "\n".join(f"- {p}" for p in file_paths))
        reply = message.get("reply_to_message") or {}
        reply_text = (reply.get("text") or reply.get("caption") or "").strip()
        if reply_text:
            parts.append(f"Context — the user replied to this earlier message:\n{reply_text}")
        return "\n\n".join(p for p in parts if p)

    def _download_attachments(self, message: dict, thread_key: str) -> list[str]:
        target = self.upload_root / re.sub(r"[^0-9A-Za-z_.-]", "_", thread_key)
        saved: list[str] = []
        doc = message.get("document")
        if doc:
            saved += self._save_file(doc.get("file_id"), target, doc.get("file_name") or "document")
        photo = message.get("photo")
        if photo:
            best = photo[-1]
            saved += self._save_file(best.get("file_id"), target, f"photo-{message.get('message_id')}.jpg")
        return saved

    def _save_file(self, file_id: str | None, target: Path, name: str) -> list[str]:
        if not file_id:
            return []
        file_path = self.client.get_file(file_id)
        if not file_path:
            return []
        dest = target / self._safe_filename(name)
        if self.client.download(file_path, dest):
            return [str(dest)]
        return []

    @staticmethod
    def _safe_filename(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")
        return cleaned[:120] or "upload"

    # -- inbound callbacks -------------------------------------------------- #

    def _on_callback(self, cq: dict) -> None:
        data = str(cq.get("data") or "")
        cq_id = cq.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        user = cq.get("from") or {}
        user_id = user.get("id")
        message_id = message.get("message_id")
        message_thread_id = message.get("message_thread_id")
        if chat_id is None or user_id is None:
            return
        if not self._allowed(user_id, chat_id):
            self.client.answer_callback(cq_id, "Not allowed.")
            return

        parts = data.split(":")
        kind = parts[0] if parts else ""

        if kind == "prompt":
            self.client.answer_callback(cq_id)
            idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
            if 0 <= idx < len(SUGGESTED_PROMPTS):
                prompt = SUGGESTED_PROMPTS[idx]["message"]
                tk = self._thread_key(chat_id, message_thread_id)
                self._kickoff(chat_id, tk, message_id, message_thread_id, str(user_id), prompt, prompt)
            return

        if kind == "stop":
            tk = ":".join(parts[1:])
            killed = brain.cancel_thread(tk)
            self.store.update(tk, status="cancelled")
            self.client.answer_callback(cq_id, "Stopped." if killed else "Nothing running.")
            if message_id:
                self.client.edit_message_reply_markup(chat_id, message_id, None)
            return

        if kind in ("confirm",) and len(parts) >= 3:
            decision = parts[1]
            tk = ":".join(parts[2:])
            approve = decision == "approve"
            self.client.answer_callback(cq_id, "Approved." if approve else "Denied.")
            if message_id:
                self.client.edit_message_reply_markup(chat_id, message_id, None)
            prompt = (
                "[Chat permission] The user clicked Approve for the pending action. "
                "Proceed now and report what you did."
                if approve
                else "[Chat permission] The user clicked Deny. Do NOT perform that action; acknowledge briefly."
            )
            self._kickoff(chat_id, tk, message_id, message_thread_id, str(user_id), prompt, prompt)
            return

        if kind == "fb" and len(parts) >= 3:
            polarity, tk = parts[1], ":".join(parts[2:])
            self.client.answer_callback(cq_id, "Thanks for the feedback!")
            self.store.log_feedback({"user": str(user_id), "value": polarity, "polarity": polarity, "thread_ts": tk})
            if polarity == "down" and message_id:
                self.client.send_message(
                    chat_id,
                    "What went wrong?",
                    reply_markup=tgfmt.feedback_category_keyboard(tk),
                    reply_to_message_id=message_id,
                    message_thread_id=message_thread_id,
                )
            return

        if kind == "fbc" and len(parts) >= 3:
            category, tk = parts[1], ":".join(parts[2:])
            self.client.answer_callback(cq_id, "Noted.")
            if message_id:
                self.client.edit_message_reply_markup(chat_id, message_id, None)
            self.store.log_feedback({"user": str(user_id), "value": "negative", "polarity": "negative",
                                     "category": category, "thread_ts": tk, "source": "category"})
            return

        if kind in ("regen", "fork") and len(parts) >= 2:
            tk = ":".join(parts[1:])
            self.client.answer_callback(cq_id)
            if kind == "fork":
                brain.fork_thread(tk)
                self.store.update(tk, session_id=None, status="forked")
                self.client.send_message(chat_id, "\u2702\ufe0f Forked \u2014 your next message starts a fresh conversation.",
                                         message_thread_id=message_thread_id)
                return
            last = brain.last_message_for_thread(tk)
            if not last:
                self.client.send_message(chat_id, "(nothing to regenerate)", message_thread_id=message_thread_id)
                return
            prompt = f"The user asked earlier:\n\n\"\"\"\n{last}\n\"\"\"\n\nProduce a different or improved answer."
            self._kickoff(chat_id, tk, message_id, message_thread_id, str(user_id), prompt, prompt)
            return

        self.client.answer_callback(cq_id)

    # -- commands ----------------------------------------------------------- #

    def _handle_command(self, chat_id, thread_key, message_thread_id, text: str) -> bool:
        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/help"):
            self.client.send_message(
                chat_id, tgfmt.HELP_TEXT, reply_markup=tgfmt.prompts_keyboard(SUGGESTED_PROMPTS),
                message_thread_id=message_thread_id,
            )
            return True
        if command == "/new":
            brain.fork_thread(thread_key)
            self.store.update(thread_key, session_id=None, status="forked")
            self.client.send_message(chat_id, "\U0001f195 Fresh conversation started.", message_thread_id=message_thread_id)
            return True
        if command == "/status":
            self.client.send_message(
                chat_id, f"telly is using <code>{tgfmt.esc(self.cfg.main_agent_cmd)}</code>.", message_thread_id=message_thread_id
            )
            return True
        if command == "/sessions":
            rows = [f"{i+1}. <b>{tgfmt.esc((s.last_message or 'no message')[:60])}</b> \u2014 {tgfmt.esc(s.status)}"
                    for i, s in enumerate(self.store.list_recent(10))]
            self.client.send_message(chat_id, "\n".join(rows) or "No conversations yet.", message_thread_id=message_thread_id)
            return True
        if command == "/stop":
            killed = brain.cancel_thread(thread_key)
            self.store.update(thread_key, status="cancelled")
            self.client.send_message(chat_id, "Stopped." if killed else "Nothing running.", message_thread_id=message_thread_id)
            return True
        if command == "/triggers":
            state = self.store.get(thread_key)
            triggers = (state.triggers if state else None) or {}
            if not triggers:
                self.client.send_message(chat_id, "No triggers in this chat.", message_thread_id=message_thread_id)
            else:
                lines = [f"\u2022 <b>{tgfmt.esc(name)}</b>: {tgfmt.esc(str(t.get('instruction', '')))[:120]}"
                         for name, t in triggers.items()]
                self.client.send_message(chat_id, "Triggers:\n" + "\n".join(lines), message_thread_id=message_thread_id)
            return True
        return False

    # -- triggers (same grammar as Slack) ----------------------------------- #

    def _handle_trigger_command(self, chat_id, thread_key: str, text: str) -> bool:
        add = _TRIGGER_ADD_RE.match(text)
        if add:
            name = re.sub(r"\s+", " ", add.group("name")).strip()
            state = self.store.get(thread_key)
            triggers = dict(state.triggers if state else {})
            previous = triggers.get(name) or {}
            triggers[name] = {
                "instruction": add.group("instruction").strip(),
                "interval_seconds": previous.get("interval_seconds"),
                "enabled": bool(previous.get("enabled", False)),
                "next_run_at": previous.get("next_run_at"),
            }
            self.store.update(thread_key, triggers=triggers)
            self.client.send_message(chat_id, f"Saved trigger `{name}` for this chat.")
            return True
        start = _TRIGGER_START_RE.match(text)
        if start:
            name = re.sub(r"\s+", " ", start.group("name")).strip()
            interval = _parse_interval_seconds(start.group("interval"))
            if interval is None:
                self.client.send_message(chat_id, "Could not parse the interval. Example: `start trigger x every 5m`.")
                return True
            state = self.store.get(thread_key)
            triggers = dict(state.triggers if state else {})
            trigger = dict(triggers.get(name) or {})
            if not str(trigger.get("instruction") or "").strip():
                self.client.send_message(chat_id, f"No saved trigger named `{name}`. Add it first.")
                return True
            trigger.update({"interval_seconds": interval, "enabled": True, "next_run_at": time.time()})
            triggers[name] = trigger
            self.store.update(thread_key, triggers=triggers)
            self.client.send_message(chat_id, f"Started trigger `{name}` every `{_format_interval(interval)}`.")
            return True
        remove = _TRIGGER_REMOVE_RE.match(text)
        if remove:
            name = re.sub(r"\s+", " ", remove.group("name")).strip()
            state = self.store.get(thread_key)
            triggers = dict(state.triggers if state else {})
            triggers.pop(name, None)
            self.store.update(thread_key, triggers=triggers)
            self.client.send_message(chat_id, f"Removed trigger `{name}`.")
            return True
        stop = _TRIGGER_STOP_RE.match(text)
        if stop:
            name = re.sub(r"\s+", " ", stop.group("name")).strip()
            state = self.store.get(thread_key)
            triggers = dict(state.triggers if state else {})
            trigger = dict(triggers.get(name) or {})
            trigger.update({"enabled": False, "next_run_at": None})
            triggers[name] = trigger
            self.store.update(thread_key, triggers=triggers)
            self.client.send_message(chat_id, f"Stopped trigger `{name}`.")
            return True
        if _TRIGGER_LIST_RE.match(text):
            return self._handle_command(chat_id, thread_key, None, "/triggers")
        if _TRIGGER_CLEAR_RE.match(text):
            self.store.update(thread_key, triggers={})
            self.client.send_message(chat_id, "Cleared all triggers for this chat.")
            return True
        return False

    def _trigger_loop(self) -> None:
        while self._running:
            try:
                self._run_due_triggers()
            except Exception:
                log.exception("telegram trigger loop failed")
            time.sleep(2.0)

    def _run_due_triggers(self) -> None:
        now = time.time()
        for state in self.store.list_recent(999):
            triggers = dict(state.triggers or {})
            changed = False
            for name, trigger in triggers.items():
                if not isinstance(trigger, dict) or not trigger.get("enabled"):
                    continue
                interval = trigger.get("interval_seconds")
                nxt = trigger.get("next_run_at")
                if not isinstance(interval, (int, float)):
                    continue
                if isinstance(nxt, (int, float)) and nxt > now:
                    continue
                trigger["next_run_at"] = now + int(interval)
                changed = True
                self.store.update(state.thread_ts, triggers=triggers, save=True)
                prompt = (
                    f"[Scheduled trigger: {name}] Running automatically in the background.\n\n"
                    f"Instruction:\n{trigger.get('instruction')}\n\n"
                    f"Check now and act if needed. If there is nothing to report, reply exactly {NO_REPLY_TOKEN}."
                )
                chat_id = self._chat_id_from_key(state.thread_ts)
                if chat_id is not None:
                    self._kickoff(chat_id, state.thread_ts, None, None, state.user, prompt, prompt)
                break
            if changed:
                continue

    @staticmethod
    def _chat_id_from_key(thread_key: str) -> str | None:
        parts = thread_key.split(":")
        return parts[1] if len(parts) >= 2 and parts[0] == "tg" else None

    # -- turn orchestration ------------------------------------------------- #

    def _kickoff(self, chat_id, thread_key: str, message_id, message_thread_id, user_id: str, text: str, prompt: str) -> None:
        if message_id:
            self.client.set_reaction(chat_id, message_id, "\u23f3")
        self.store.update(
            thread_key, channel=str(chat_id), user=user_id, last_message=text,
            agent_cmd=self.cfg.main_agent_cmd, status="queued",
        )
        _executor.submit(self._handle_turn, chat_id, thread_key, message_id, message_thread_id, user_id, prompt)

    def _handle_turn(self, chat_id, thread_key: str, message_id, message_thread_id, user_id: str, text: str) -> None:
        prior = self.store.get(thread_key)
        if prior and prior.session_id:
            brain.set_session_for_thread(thread_key, prior.session_id)
        self.store.update(thread_key, status="running")

        progress = TelegramProgress(self.client, chat_id, thread_key,
                                    message_thread_id=message_thread_id, reply_to=message_id)
        progress.start()

        try:
            turn = brain.ask_brain(self.cfg.main_agent_cmd, thread_key, text, self.cfg.turn_log_path,
                                   on_progress=progress.make_callback())
        except brain.BrainCancelled:
            progress.finish("<i>Stopped by user.</i>", reply_markup=None)
            if message_id:
                self.client.set_reaction(chat_id, message_id, "\U0001f6d1")
            self.store.update(thread_key, status="cancelled")
            return
        except Exception as e:  # noqa: BLE001
            log.exception("telegram turn failed")
            progress.finish(f"\u26a0\ufe0f <b>telly hit a problem</b>\n<code>{tgfmt.esc(str(e))[:500]}</code>", reply_markup=None)
            if message_id:
                self.client.set_reaction(chat_id, message_id, "\u274c")
            self.store.update(thread_key, status="error")
            return

        self.store.update(thread_key, session_id=turn.session_id, status="complete")
        if message_id:
            self.client.set_reaction(chat_id, message_id, "\u2705")
        self._deliver(chat_id, thread_key, message_thread_id, message_id, turn, progress)

    def _deliver(self, chat_id, thread_key: str, message_thread_id, reply_to, turn: brain.BrainTurn,
                 progress: TelegramProgress) -> None:
        reply = turn.reply or ""
        if reply.strip() == NO_REPLY_TOKEN:
            progress.discard()
            return

        reply, confirm_summary = slackfmt.extract_confirm_request(reply)
        if not confirm_summary and slackfmt.looks_like_confirmation_request(reply):
            confirm_summary = reply.strip()[-300:]
        reply, upload_paths = self._extract_uploads(reply)

        result = tgfmt.render_reply(reply)
        keyboard = tgfmt.approve_deny_keyboard(thread_key) if confirm_summary else tgfmt.feedback_keyboard(thread_key)

        # Very long answers become a Markdown attachment instead of a message wall.
        if sum(len(c) for c in result.html_chunks) > LONG_REPLY_DOC_THRESHOLD:
            self._attach_document(chat_id, thread_key, reply, message_thread_id)
            chunks = ["\U0001f4c4 The answer was long, so I attached it as a Markdown file."]
        else:
            chunks = [c for c in result.html_chunks if c] or ["(no output)"]

        last_id = self._replace_placeholder(chat_id, message_thread_id, reply_to, progress, chunks[0])
        for chunk in chunks[1:]:
            res = self.client.send_message(chat_id, chunk, message_thread_id=message_thread_id)
            if res:
                last_id = res.get("message_id")

        for media in result.media:
            caption = tgfmt.esc(media.caption)
            res = None
            if media.kind == "photo" and media.url:
                res = self.client.send_photo(chat_id, media.url, caption=caption, message_thread_id=message_thread_id)
            elif media.kind == "video" and media.url:
                res = self.client.send_video(chat_id, media.url, caption=caption, message_thread_id=message_thread_id)
            if res:
                last_id = res.get("message_id")

        up_id = self._send_uploads(chat_id, upload_paths, message_thread_id)
        if up_id:
            last_id = up_id

        if last_id:
            self.client.edit_message_reply_markup(chat_id, last_id, keyboard)

    def _replace_placeholder(self, chat_id, message_thread_id, reply_to, progress: TelegramProgress, first: str) -> int | None:
        first = first or "(no output)"
        if progress.message_id and progress.finish(first, reply_markup=None):
            return progress.message_id
        if progress.message_id:
            progress.discard()
        res = self.client.send_message(chat_id, first, reply_to_message_id=reply_to, message_thread_id=message_thread_id)
        return res.get("message_id") if res else None

    def _attach_document(self, chat_id, thread_key: str, text: str, message_thread_id) -> None:
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", thread_key)
        path = self.cfg.telly_home / f"telegram-reply-{safe}.md"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            log.exception("could not write long reply document")
            return
        self.client.send_document(chat_id, str(path), message_thread_id=message_thread_id)

    def _extract_uploads(self, text: str) -> tuple[str, list[Path]]:
        paths: list[Path] = []
        kept: list[str] = []
        for line in text.splitlines():
            match = UPLOAD_MARKER_RE.match(line)
            if match:
                raw = match.group(1).strip().strip("`'")
                if raw:
                    paths.append(Path(raw).expanduser())
                continue
            kept.append(line)
        return "\n".join(kept).strip(), paths

    def _send_uploads(self, chat_id, paths: list[Path], message_thread_id) -> int | None:
        last: int | None = None
        for path in paths:
            if not path.is_file():
                continue
            sender = self.client.send_photo if path.suffix.lower() in _IMAGE_EXT else self.client.send_document
            res = sender(chat_id, str(path), message_thread_id=message_thread_id)
            if res:
                last = res.get("message_id")
        return last


# --------------------------------------------------------------------------- #
# Trigger interval helpers (same grammar as the Slack backend)
# --------------------------------------------------------------------------- #

def _parse_interval_seconds(text: str) -> int | None:
    match = re.fullmatch(r"(\d+)\s*([a-z]+)?", text.strip().lower())
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


def _format_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def run(cfg: Config) -> None:
    TelegramBot(cfg).run()
