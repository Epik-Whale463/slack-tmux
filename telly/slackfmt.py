"""Format conversions from Markdown (what the agent emits) to Slack mrkdwn,
plus helpers for chunking long replies and building context blocks.

Slack's mrkdwn is NOT real Markdown:
- *bold* (single asterisk), not **bold**
- _italic_, not *italic*
- ~strike~, not ~~strike~~
- <url|text>, not [text](url)
- no `# headings` — render as bold instead
- code fences work, but a language tag (```python) renders as literal text
"""

import re

SLACK_MAX_TEXT = 2800   # section blocks cap text at 3000 chars; leave room for mrkdwn changes.
_BOLD_PLACEHOLDER = "\x01BOLD\x01"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_FENCE_LANG_RE = re.compile(r"^```([a-zA-Z0-9_+-]+)\s*$", re.MULTILINE)


def to_mrkdwn(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn. Conservative: doesn't try italic
    conversion because single-asterisk patterns produce too many false positives."""
    if not text:
        return text

    # 1. Bold first, via a placeholder so we don't touch the inner content twice.
    text = _BOLD_RE.sub(lambda m: f"{_BOLD_PLACEHOLDER}{m.group(1)}{_BOLD_PLACEHOLDER}", text)

    # 2. Strip code-fence language tags (```python -> ```).
    text = _FENCE_LANG_RE.sub("```", text)

    # 3. Strikethrough.
    text = _STRIKE_RE.sub(r"~\1~", text)

    # 4. Inline links.
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)

    # 5. Headings → bold line (Slack has no native heading in plain mrkdwn).
    text = _HEADING_RE.sub(lambda m: f"{_BOLD_PLACEHOLDER}{m.group(2)}{_BOLD_PLACEHOLDER}", text)

    # 6. Restore bold marker.
    text = text.replace(_BOLD_PLACEHOLDER, "*")
    return text


def chunk_for_slack(text: str, limit: int = SLACK_MAX_TEXT) -> list[str]:
    """Split long text into Slack-postable chunks, preferring newline breaks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:   # no useful newline; hard-split
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def build_footer_block(session_id: str | None, cost_usd: float | None, duration_ms: int | None) -> dict | None:
    """Return a Slack `context` block summarizing the turn, or None if no metadata."""
    parts: list[str] = []
    if session_id:
        parts.append(f"session `{session_id[:8]}`")
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")
    if not parts:
        return None
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": " · ".join(parts)}],
    }


def _truncate(value: object, limit: int = 100) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Tools whose calls are pure exploration — hidden from the Slack trace.
_NOISY_TOOLS = {"Read", "Glob", "Grep"}
# Bash invocations whose first token is one of these are also pure exploration.
_NOISY_BASH_CMDS = {
    "ls", "cat", "head", "tail", "pwd", "which", "find", "grep", "rg",
    "wc", "stat", "file", "du", "df", "tree", "echo", "printf",
}


def _is_noisy(name: str, tool_input: dict) -> bool:
    if name in _NOISY_TOOLS:
        return True
    if name == "Bash":
        cmd = (tool_input.get("command") or "").lstrip()
        first = cmd.split(maxsplit=1)[0] if cmd else ""
        return first in _NOISY_BASH_CMDS
    return False


def format_breadcrumb(name: str, tool_input: dict) -> str:
    """Render one tool call as a one-line Slack mrkdwn breadcrumb."""
    if name == "Bash":
        cmd = tool_input.get("command", "")
        return f"`$ {_truncate(cmd, 140)}`"
    if name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path") or tool_input.get("path") or "?"
        return f"{name.lower()} `{_truncate(path)}`"
    if name == "Glob":
        return f"glob `{_truncate(tool_input.get('pattern', '?'))}`"
    if name == "Grep":
        return f"grep `{_truncate(tool_input.get('pattern', '?'))}`"
    if name == "WebFetch":
        return f"fetch `{_truncate(tool_input.get('url', '?'))}`"
    if name == "WebSearch":
        return f"web search `{_truncate(tool_input.get('query', '?'))}`"
    if name == "Task":
        return f"agent: {_truncate(tool_input.get('description', tool_input.get('prompt', '?')))}"
    # Generic fallback
    first_val = next(iter(tool_input.values()), "") if tool_input else ""
    return f"{name} `{_truncate(first_val)}`"


_MAX_BREADCRUMBS = 4


def build_breadcrumb_block(breadcrumbs: list[dict]) -> dict | None:
    """Render tool calls as a single Slack `context` block above the reply.

    Filters out read-only exploration (Read/Glob/Grep, plus `ls`/`cat`-style
    Bash) and collapses consecutive same-tool calls into `… ×N`. Full trace
    is preserved in the turn jsonl regardless.
    """
    if not breadcrumbs:
        return None

    visible = [c for c in breadcrumbs if not _is_noisy(c["name"], c["input"])]
    hidden = len(breadcrumbs) - len(visible)

    collapsed: list[tuple[str, int]] = []
    for crumb in visible:
        rendered = format_breadcrumb(crumb["name"], crumb["input"])
        if collapsed and collapsed[-1][0] == rendered:
            text, n = collapsed[-1]
            collapsed[-1] = (text, n + 1)
        else:
            collapsed.append((rendered, 1))

    elements: list[dict] = []
    for text, n in collapsed[:_MAX_BREADCRUMBS]:
        elements.append({"type": "mrkdwn", "text": text if n == 1 else f"{text} ×{n}"})
    overflow = len(collapsed) - _MAX_BREADCRUMBS
    extras: list[str] = []
    if overflow > 0:
        extras.append(f"+{overflow} more")
    if hidden > 0:
        extras.append(f"+{hidden} read")
    if extras:
        elements.append({"type": "mrkdwn", "text": f"_{' · '.join(extras)}_"})

    if not elements:
        return None
    return {"type": "context", "elements": elements}


_TOOL_ICONS = {
    "Read": ":mag:", "Glob": ":mag:", "Grep": ":mag:",
    "Edit": ":pencil2:", "Write": ":pencil2:", "NotebookEdit": ":pencil2:",
    "WebFetch": ":globe_with_meridians:", "WebSearch": ":globe_with_meridians:",
    "Task": ":robot_face:",
    "Bash": ":zap:",
}

def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:80]


def _rich_text(text: str) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text}],
            }
        ],
    }


def _tool_icon(name: str, tool_input: dict) -> str:
    if name == "Bash":
        cmd = (tool_input.get("command") or "").lstrip()
        first = cmd.split(maxsplit=1)[0] if cmd else ""
        return ":mag:" if first in _NOISY_BASH_CMDS else ":zap:"
    return _TOOL_ICONS.get(name, ":hammer_and_wrench:")


def _status_marker(status: str, icon: str) -> str:
    if status == "done":
        return ":white_check_mark:"
    if status == "error":
        return ":x:"
    return icon  # running


def build_loading_blocks(thread_ts: str) -> list[dict]:
    """Agent-style loading UI using Slack's plan and task_card blocks."""
    sid = _safe_id(thread_ts)
    return [
        {
            "type": "plan",
            "block_id": f"telly_plan_{sid}",
            "title": "Working on your request",
            "tasks": [
                {
                    "task_id": f"{sid}_understand",
                    "title": "Understand the Slack request",
                    "status": "complete",
                    "output": _rich_text("Thread context received."),
                },
                {
                    "task_id": f"{sid}_work",
                    "title": "Use local workspace and tools",
                    "status": "in_progress",
                    "details": _rich_text("Reading context and running the agent without streaming partial text."),
                },
                {
                    "task_id": f"{sid}_answer",
                    "title": "Post the complete answer",
                    "status": "pending",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "No token streaming. This card will be replaced with the full response."}],
        },
        build_stop_actions_block(thread_ts),
    ]


def build_plain_loading_blocks(thread_ts: str) -> list[dict]:
    """Fallback for workspaces that reject newer agent blocks."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: *Working on your request...*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Posting the complete answer when ready."}]},
        build_stop_actions_block(thread_ts),
    ]


def build_completion_task_block(thread_ts: str, duration_ms: int | None) -> dict:
    sid = _safe_id(thread_ts)
    detail = "Complete answer generated."
    if duration_ms is not None:
        detail = f"Complete answer generated in {duration_ms / 1000:.1f}s."
    return {
        "type": "task_card",
        "task_id": f"{sid}_complete",
        "title": "Telly response ready",
        "status": "complete",
        "output": _rich_text(detail),
    }


def build_feedback_block(thread_ts: str) -> dict:
    return {
        "type": "context_actions",
        "block_id": f"telly_feedback_{_safe_id(thread_ts)}",
        "elements": [
            {
                "type": "feedback_buttons",
                "action_id": "telly_feedback",
                "positive_button": {
                    "text": {"type": "plain_text", "text": "Good"},
                    "value": f"positive:{thread_ts}",
                    "accessibility_label": "Mark this response as good",
                },
                "negative_button": {
                    "text": {"type": "plain_text", "text": "Bad"},
                    "value": f"negative:{thread_ts}",
                    "accessibility_label": "Mark this response as bad",
                },
            }
        ],
    }


def build_error_blocks(thread_ts: str, error: str) -> list[dict]:
    short = _truncate(error, 700)
    return [
        {
            "type": "task_card",
            "task_id": f"{_safe_id(thread_ts)}_error",
            "title": "Telly hit a problem",
            "status": "error",
            "output": _rich_text(short),
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "I preserved the thread state. You can retry, fork, or inspect logs."},
        },
        build_actions_block(thread_ts),
    ]


def build_basic_error_blocks(thread_ts: str, error: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *Telly hit a problem*\n`{_truncate(error, 700)}`"}},
        build_actions_block(thread_ts),
    ]


def build_stop_actions_block(thread_ts: str) -> dict:
    """Single Stop button — shown on the in-flight placeholder only."""
    return {
        "type": "actions",
        "block_id": f"telly_stop_{thread_ts}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Stop"},
                "action_id": "telly_stop",
                "style": "danger",
                "value": thread_ts,
            },
        ],
    }


def build_actions_block(thread_ts: str) -> dict:
    """Buttons that appear under each reply."""
    return {
        "type": "actions",
        "block_id": f"telly_actions_{thread_ts}",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Regenerate"},
                "action_id": "telly_regenerate",
                "value": thread_ts,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Fork"},
                "action_id": "telly_fork",
                "value": thread_ts,
            },
        ],
    }


def build_home_view(threads: list[object]) -> dict:
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Telly Control Center"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Monitor recent agent threads, recover sessions, and jump back into work."}},
        {"type": "divider"},
    ]
    if not threads:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No telly threads yet."}})
    for state in threads[:10]:
        thread_ts = getattr(state, "thread_ts", "")
        title = _truncate(getattr(state, "last_message", None) or "No message tracked", 120)
        meta = [getattr(state, "status", "idle")]
        if getattr(state, "agent_cmd", None):
            meta.append(str(getattr(state, "agent_cmd")))
        if getattr(state, "session_id", None):
            meta.append(f"session `{str(getattr(state, 'session_id'))[:8]}`")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n{' · '.join(meta)}"},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"home_actions_{_safe_id(thread_ts)}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Stop"}, "style": "danger", "action_id": "telly_stop", "value": thread_ts},
                {"type": "button", "text": {"type": "plain_text", "text": "Fork"}, "action_id": "telly_fork", "value": thread_ts},
            ],
        })
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "AI-generated output can be wrong. Review commands and code changes before relying on them."}]})
    return {"type": "home", "blocks": blocks[:100]}


def build_blocks(
    reply_chunk: str,
    *,
    completion_block: dict | None = None,
    breadcrumbs_block: dict | None = None,
    footer: dict | None = None,
    actions_block: dict | None = None,
    feedback_block: dict | None = None,
) -> list[dict]:
    """Block Kit payload for one message chunk. Agent UI goes on top, controls at the bottom."""
    blocks: list[dict] = []
    if completion_block:
        blocks.append(completion_block)
    if breadcrumbs_block:
        blocks.append(breadcrumbs_block)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": reply_chunk}, "expand": True})
    if actions_block:
        blocks.append(actions_block)
    if feedback_block:
        blocks.append(feedback_block)
    if footer:
        blocks.append(footer)
    return blocks
