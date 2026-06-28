import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .browser import capability_text as browser_capability_text
from .discovery import discover_agents, format_for_brain

log = logging.getLogger(__name__)

AGENT_TURN_TIMEOUT_S = 600


class BrainCancelled(Exception):
    """Raised when a turn was killed via cancel_thread()."""


@dataclass
class BrainTurn:
    reply: str
    session_id: str | None
    cost_usd: float | None
    duration_ms: int | None
    resumed: bool
    tool_breadcrumbs: list[dict] = field(default_factory=list)  # [{name, input}]
    tool_events: list[dict] = field(default_factory=list)       # [{id, name, input, status}]


# In-memory state. Lives for the bot's process lifetime; a restart wipes
# thread continuity, which is acceptable for v1.
_thread_sessions: dict[str, str] = {}        # slack thread_ts -> agent session_id
_thread_last_message: dict[str, str] = {}    # slack thread_ts -> last user message text
_thread_locks: dict[str, threading.Lock] = {}
_locks_mu = threading.Lock()
_agents_block_cache: str | None = None

# In-flight subprocess registry, so a Slack Stop button can kill a running turn.
_thread_procs: dict[str, subprocess.Popen] = {}
_thread_cancelled: set[str] = set()
_procs_mu = threading.Lock()


def cancel_thread(thread_ts: str) -> bool:
    """Kill the in-flight agent turn for this thread, if any. Returns True if a proc was killed."""
    with _procs_mu:
        proc = _thread_procs.get(thread_ts)
        if proc is None or proc.poll() is not None:
            return False
        _thread_cancelled.add(thread_ts)
    try:
        proc.kill()
    except Exception:
        log.exception("kill failed for thread %s", thread_ts)
    return True


def restore_sessions(sessions: dict[str, str]) -> None:
    _thread_sessions.update({k: v for k, v in sessions.items() if v})


def set_session_for_thread(thread_ts: str, session_id: str | None) -> None:
    if session_id:
        _thread_sessions[thread_ts] = session_id


def session_for_thread(thread_ts: str) -> str | None:
    return _thread_sessions.get(thread_ts)


def _agents_block() -> str:
    global _agents_block_cache
    if _agents_block_cache is None:
        _agents_block_cache = format_for_brain(discover_agents())
    return _agents_block_cache


def _system_prompt() -> str:
    return (
        "You are telly, a Slack-facing coding agent. Each user message arrived "
        "from a Slack channel; your reply will be posted back into the Slack "
        "thread, so be concise and answer directly — long status narration "
        "becomes noise in Slack. You have full filesystem access on this "
        "machine and can spawn other coding CLIs in tmux sessions when useful. "
        "Before destructive or externally visible actions (delete files, git push, "
        "force operations, package installs, editing outside the current project, "
        "or credential/auth changes), pause and ask the Slack user for explicit "
        "confirmation instead of proceeding silently. When you create a local file "
        "that should be shown in Slack, include a line exactly like "
        "`SLACK_UPLOAD: /absolute/path/to/file` in your final reply; telly will "
        "upload it into the thread and hide that marker from the visible text. "
        "When a Slack-native chart is useful, include a fenced "
        "```slack-visualization JSON block containing one Slack "
        "`data_visualization` block with the native `chart` payload. Supported "
        "chart types are pie, bar, area, and line; telly will render the chart "
        "natively and hide the JSON from the visible text.\n\n"
        f"Local coding CLIs available on this host:\n{_agents_block()}\n\n"
        f"{browser_capability_text()}"
    )


def _lock_for(thread_ts: str) -> threading.Lock:
    with _locks_mu:
        lock = _thread_locks.get(thread_ts)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[thread_ts] = lock
        return lock


def _build_cmd(agent_cmd: str, thread_ts: str, message: str) -> list[str]:
    agent_name = Path(agent_cmd).name
    existing = _thread_sessions.get(thread_ts)

    if agent_name == "opencode":
        prompt = f"{_system_prompt()}\n\nUser message from Slack:\n\n{message}"
        cmd: list[str] = [
            agent_cmd,
            "run",
            "--format", "json",
            "--dangerously-skip-permissions",
        ]
        if existing:
            cmd += ["--session", existing]
        cmd.append(prompt)
        return cmd

    cmd = [
        agent_cmd,
        "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--append-system-prompt", _system_prompt(),
    ]
    if existing:
        cmd += ["--resume", existing]
    cmd.append(message)
    return cmd


def _log_turn(turn_log_path: Path, record: dict) -> None:
    try:
        turn_log_path.parent.mkdir(parents=True, exist_ok=True)
        with turn_log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.exception("turn log write failed")


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text_from_content(item) for item in content)
    if not isinstance(content, dict):
        return ""

    ctype = content.get("type")
    if ctype in ("text", "output_text"):
        return str(content.get("text") or content.get("content") or "")
    if "content" in content:
        return _text_from_content(content["content"])
    if "parts" in content:
        return _text_from_content(content["parts"])
    return ""


def _first_present(*values: object) -> object | None:
    for value in values:
        if value is not None:
            return value
    return None


def _parse_event(event: dict, state: dict) -> None:
    """Mutate state based on one JSON event from Claude or OpenCode."""
    etype = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}

    if etype == "system" and event.get("subtype") == "init":
        state["session_id"] = event.get("session_id")
        return

    if etype == "text":
        text = str(part.get("text") or event.get("text") or "")
        if text.strip():
            state["assistant_texts"].append(text.strip())

    if etype == "assistant":
        msg = event.get("message") or {}
        text = _text_from_content(msg.get("content") or event.get("content"))
        if text.strip():
            state["assistant_texts"].append(text.strip())
        for block in msg.get("content") or []:
            if block.get("type") == "tool_use":
                te = {
                    "id": block.get("id"),
                    "name": block.get("name", "tool"),
                    "input": block.get("input") or {},
                    "status": "running",
                }
                state["tool_events"].append(te)
                state["tool_breadcrumbs"].append({"name": te["name"], "input": te["input"]})

    elif etype == "user":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id")
                is_error = bool(block.get("is_error"))
                for te in state["tool_events"]:
                    if te["id"] == tool_id and te["status"] == "running":
                        te["status"] = "error" if is_error else "done"
                        break

    elif etype == "result":
        state["reply"] = (event.get("result") or "").strip()
        state["session_id"] = event.get("session_id") or state.get("session_id")
        state["cost_usd"] = event.get("total_cost_usd")
        state["duration_ms"] = event.get("duration_ms")
        return

    state["session_id"] = (
        event.get("session_id")
        or event.get("sessionID")
        or event.get("sessionId")
        or part.get("sessionID")
        or part.get("session_id")
        or state.get("session_id")
    )
    state["cost_usd"] = _first_present(
        event.get("total_cost_usd"),
        event.get("cost_usd"),
        part.get("cost"),
        state.get("cost_usd"),
    )
    state["duration_ms"] = _first_present(
        event.get("duration_ms"),
        event.get("durationMS"),
        state.get("duration_ms"),
    )

    role = event.get("role") or (event.get("message") or {}).get("role")
    if role == "assistant" and etype != "assistant":
        text = _text_from_content(
            event.get("content")
            or event.get("part")
            or (event.get("message") or {}).get("content")
        )
        if text.strip():
            state["assistant_texts"].append(text.strip())


def _parse_output(stdout: str, duration_ms: int) -> dict:
    state: dict = {
        "reply": "",
        "session_id": None,
        "cost_usd": None,
        "duration_ms": duration_ms,
        "tool_breadcrumbs": [],
        "tool_events": [],
        "assistant_texts": [],
    }

    parsed_any = False
    stripped = stdout.strip()
    if stripped:
        try:
            loaded = json.loads(stripped)
            for event in loaded if isinstance(loaded, list) else [loaded]:
                if isinstance(event, dict):
                    parsed_any = True
                    _parse_event(event, state)
        except json.JSONDecodeError:
            for line in stripped.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    parsed_any = True
                    _parse_event(event, state)

    if not state["reply"] and state["assistant_texts"]:
        state["reply"] = "\n\n".join(state["assistant_texts"]).strip()
    if not state["reply"] and not parsed_any:
        state["reply"] = stripped
    return state


def _run_agent(cmd: list[str], thread_ts: str) -> dict:
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None

    with _procs_mu:
        _thread_procs[thread_ts] = proc

    try:
        try:
            stdout, stderr = proc.communicate(timeout=AGENT_TURN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(f"agent timed out after {AGENT_TURN_TIMEOUT_S}s")

        with _procs_mu:
            was_cancelled = thread_ts in _thread_cancelled
            _thread_cancelled.discard(thread_ts)
        if was_cancelled:
            raise BrainCancelled()
        rc = proc.returncode
        if rc != 0:
            raise RuntimeError(f"agent exited {rc}: {stderr.strip()[-500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        with _procs_mu:
            if _thread_procs.get(thread_ts) is proc:
                del _thread_procs[thread_ts]
            _thread_cancelled.discard(thread_ts)

    duration_ms = int((time.monotonic() - started) * 1000)
    return _parse_output(stdout, duration_ms)


def ask_brain(
    agent_cmd: str,
    thread_ts: str,
    message: str,
    turn_log_path: Path,
) -> BrainTurn:
    """Run one agent turn for a Slack thread. Returns a BrainTurn with reply + metadata."""
    with _lock_for(thread_ts):
        is_resume = thread_ts in _thread_sessions
        cmd = _build_cmd(agent_cmd, thread_ts, message)
        log.info("agent turn cmd=%s thread=%s resume=%s", Path(agent_cmd).name, thread_ts, is_resume)

        state = _run_agent(cmd, thread_ts)

        sid = state["session_id"]
        if sid:
            _thread_sessions[thread_ts] = sid
        _thread_last_message[thread_ts] = message

        turn = BrainTurn(
            reply=state["reply"] or "(empty reply — see ~/.telly/turns.jsonl)",
            session_id=sid,
            cost_usd=state["cost_usd"],
            duration_ms=state["duration_ms"],
            resumed=is_resume,
            tool_breadcrumbs=state["tool_breadcrumbs"],
            tool_events=state["tool_events"],
        )

        _log_turn(
            turn_log_path,
            {
                "thread_ts": thread_ts,
                "session_id": sid,
                "resume": is_resume,
                "message": message,
                "reply": turn.reply,
                "cost_usd": turn.cost_usd,
                "duration_ms": turn.duration_ms,
                "tool_calls": turn.tool_breadcrumbs,
            },
        )

        return turn


def has_session_for_thread(thread_ts: str) -> bool:
    return thread_ts in _thread_sessions


def last_message_for_thread(thread_ts: str) -> str | None:
    return _thread_last_message.get(thread_ts)


def fork_thread(thread_ts: str) -> None:
    """Forget the agent session for this Slack thread; the next message starts fresh."""
    _thread_sessions.pop(thread_ts, None)
