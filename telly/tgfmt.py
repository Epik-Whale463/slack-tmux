"""Telegram rendering: agent Markdown + Block Kit fences → Telegram HTML, media, keyboards.

Telegram has no Block Kit, so telly's native Slack blocks are re-rendered into
Telegram idioms:
- markdown      → HTML (parse_mode="HTML"; only <, >, & need escaping)
- table         → aligned monospace <pre>
- plan          → checklist lines with status glyphs
- data charts   → monospace data table (no native charts in Telegram)
- card/carousel → titled text blocks
- alert         → emoji-prefixed bold line
- image/video   → real Telegram photo/video messages
Everything else degrades to readable text rather than being dropped.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from . import slackfmt

# Telegram text cap is 4096 chars after entity parsing; leave headroom.
TG_TEXT_LIMIT = 4000
# Captions are capped at 1024.
TG_CAPTION_LIMIT = 1000
_MAX_ACTIVITY_STEPS = 8
_CODE_PLACEHOLDER = "\x00TGCODE{}\x00"

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+.#-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]*(.+?)[ \t]*$")
_DIVIDER_RE = re.compile(r"(?m)^[ \t]*([-*_])\1{2,}[ \t]*$")
_INLINE_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)")
_LIST_RE = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")

_STATUS_GLYPH = {
    "pending": "\u25cb",       # ○
    "in_progress": "\u25d0",   # ◐
    "complete": "\u2713",      # ✓
    "error": "\u2717",         # ✗
}
_ALERT_LEVEL_ICON = {
    "default": "\u2139\ufe0f",
    "info": "\u2139\ufe0f",
    "success": "\u2705",
    "warning": "\u26a0\ufe0f",
    "error": "\u274c",
}


@dataclass
class Media:
    kind: str  # "photo" | "video" | "document"
    url: str | None = None
    path: str | None = None
    caption: str = ""
    title: str = ""


@dataclass
class RenderResult:
    html_chunks: list[str] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)


def esc(text: str) -> str:
    """Escape text for Telegram HTML (only <, >, & need escaping)."""
    return html.escape(str(text), quote=False)


# --------------------------------------------------------------------------- #
# Markdown → Telegram HTML
# --------------------------------------------------------------------------- #

def markdown_to_html(md: str) -> str:
    if not md:
        return ""
    code_blocks: list[str] = []

    def stash(match: re.Match) -> str:
        lang = (match.group(1) or "").strip()
        body = esc(match.group(2))
        if lang:
            blocks = f'<pre><code class="language-{esc(lang)}">{body}</code></pre>'
        else:
            blocks = f"<pre>{body}</pre>"
        code_blocks.append(blocks)
        return _CODE_PLACEHOLDER.format(len(code_blocks) - 1)

    text = _FENCE_RE.sub(stash, md)

    quote_blocks: list[str] = []
    text = _stash_blockquotes(text, quote_blocks)

    text = esc(text)

    inline_codes: list[str] = []

    def stash_inline(match: re.Match) -> str:
        inline_codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00TGINLINE{len(inline_codes) - 1}\x00"

    text = _INLINE_CODE_RE.sub(stash_inline, text)
    text = _INLINE_IMAGE_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1) or "image"}</a>', text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_UNDER_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _DIVIDER_RE.sub("\u2014\u2014\u2014\u2014\u2014", text)
    text = _LIST_RE.sub("\u2022 ", text)
    text = re.sub(r"\x00TGINLINE(\d+)\x00", lambda m: inline_codes[int(m.group(1))], text)
    text = re.sub(r"\x00TGQUOTE(\d+)\x00", lambda m: quote_blocks[int(m.group(1))], text)

    def unstash(match: re.Match) -> str:
        return code_blocks[int(match.group(1))]

    text = re.sub(r"\x00TGCODE(\d+)\x00", unstash, text)
    return text.strip()


def _stash_blockquotes(text: str, store: list[str]) -> str:
    """Replace runs of ``> quoted`` lines with placeholders (rendered later).

    Runs before HTML escaping so the emitted ``<blockquote>`` tags survive.
    """
    out: list[str] = []
    quote: list[str] = []

    def flush() -> None:
        if quote:
            store.append(f"<blockquote>{markdown_to_html(chr(10).join(quote))}</blockquote>")
            out.append(f"\x00TGQUOTE{len(store) - 1}\x00")
            quote.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith(">"):
            quote.append(re.sub(r"^\s*>\s?", "", line))
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Reply rendering (markdown + native blocks → HTML chunks + media)
# --------------------------------------------------------------------------- #

def render_reply(reply: str, *, limit: int = TG_TEXT_LIMIT) -> RenderResult:
    """Render an agent reply into Telegram HTML chunks plus media to attach."""
    cleaned, blocks = slackfmt.extract_native_content(reply)
    result = RenderResult()

    for chunk in _split_markdown(cleaned, limit):
        html_chunk = markdown_to_html(chunk)
        if html_chunk:
            result.html_chunks.append(html_chunk)

    for block in blocks:
        rendered, media = render_block(block)
        if rendered:
            for chunk in _chunk_html(rendered, limit):
                result.html_chunks.append(chunk)
        result.media.extend(media)

    if not result.html_chunks:
        result.html_chunks = [""]
    return result


def _split_markdown(text: str, limit: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > limit and buf:
            chunks.append(buf)
            buf = para
        else:
            buf = candidate
        while len(buf) > limit:
            chunks.append(buf[:limit])
            buf = buf[limit:]
    if buf.strip():
        chunks.append(buf)
    return chunks


def _chunk_html(html_text: str, limit: int) -> list[str]:
    """Split rendered block HTML without cutting tags.

    Oversized ``<pre>`` blocks (tables/charts) are re-wrapped into several
    balanced ``<pre>`` chunks rather than sliced mid-tag.
    """
    if len(html_text) <= limit:
        return [html_text]
    if html_text.startswith("<pre>") and html_text.endswith("</pre>"):
        inner = html_text[len("<pre>") : -len("</pre>")]
        overhead = len("<pre></pre>")
        chunks: list[str] = []
        buf: list[str] = []
        size = overhead
        for line in inner.split("\n"):
            if buf and size + len(line) + 1 > limit:
                chunks.append("<pre>" + "\n".join(buf) + "</pre>")
                buf, size = [], overhead
            buf.append(line)
            size += len(line) + 1
        if buf:
            chunks.append("<pre>" + "\n".join(buf) + "</pre>")
        return chunks

    chunks = []
    buf = ""
    for line in html_text.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def _cell_text(cell: object) -> str:
    if isinstance(cell, dict):
        return str(cell.get("text") or cell.get("number") or "")
    return str(cell or "")


def _pre_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    def disp(s: str) -> int:
        return len(s)

    col_w = [max(disp(r[i]) for r in rows) for i in range(width)]
    lines = []
    for idx, row in enumerate(rows):
        cells = [row[i].ljust(col_w[i]) for i in range(width)]
        lines.append("  ".join(cells).rstrip())
        if idx == 0 and len(rows) > 1:
            lines.append("  ".join("-" * col_w[i] for i in range(width)).rstrip())
    return f"<pre>{esc(chr(10).join(lines))}</pre>"


def render_block(block: object) -> tuple[str, list[Media]]:
    if not isinstance(block, dict):
        return "", []
    btype = block.get("type")
    if btype == "table":
        return _render_table(block), []
    if btype == "plan":
        return _render_plan(block), []
    if btype == "data_visualization":
        return _render_chart(block), []
    if btype == "alert":
        return _render_alert(block), []
    if btype == "card":
        return _render_card(block), []
    if btype == "carousel":
        return _render_carousel(block), []
    if btype == "image":
        return "", _render_image(block)
    if btype == "video":
        return "", _render_video(block)
    return _render_generic(block), []


def _render_table(block: dict) -> str:
    rows_in = block.get("rows") or []
    rows: list[list[str]] = []
    for row in rows_in:
        if isinstance(row, list):
            rows.append([_cell_text(c) for c in row])
    return _pre_table(rows)


def _render_plan(block: dict) -> str:
    title = str(block.get("title") or "Plan")
    lines = [f"<b>{esc(title)}</b>"]
    tasks = block.get("tasks") or []
    completed = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "pending")
        glyph = _STATUS_GLYPH.get(status, "\u25cb")
        if status == "complete":
            completed += 1
        lines.append(f"{glyph} {esc(str(task.get('title') or 'Task'))}")
    lines.append(f"<i>{completed}/{len(tasks)} done</i>")
    return "\n".join(lines)


def _render_chart(block: dict) -> str:
    title = str(block.get("title") or "Chart")
    chart = block.get("chart") if isinstance(block.get("chart"), dict) else {}
    ctype = chart.get("type")
    if ctype == "pie":
        segs = chart.get("segments") or []
        total = sum(float(s.get("value") or 0) for s in segs if isinstance(s, dict)) or 1.0
        rows = [["Segment", "Value", "Share"]]
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            value = float(seg.get("value") or 0)
            rows.append([str(seg.get("label") or ""), f"{value:g}", f"{value / total * 100:.0f}%"])
        return f"<b>{esc(title)}</b>\n{_pre_table(rows)}"
    series = chart.get("series") or []
    categories = ((chart.get("axis_config") or {}).get("categories")) or []
    names = [str(s.get("name") or "Series") for s in series if isinstance(s, dict)]
    rows = [["Category", *names]]
    for i, cat in enumerate(categories):
        cells = [str(cat)]
        for s in series:
            data = s.get("data") or []
            cells.append(f"{data[i].get('value'):g}" if i < len(data) and isinstance(data[i], dict) else "")
        rows.append(cells)
    return f"<b>{esc(title)}</b>\n{_pre_table(rows)}"


def _render_alert(block: dict) -> str:
    level = str(block.get("level") or "info")
    icon = _ALERT_LEVEL_ICON.get(level, _ALERT_LEVEL_ICON["info"])
    text = block.get("text")
    if isinstance(text, dict):
        text = text.get("text")
    return f"{icon} <b>{esc(str(text or 'Notice'))}</b>"


def _card_lines(card: dict) -> list[str]:
    lines: list[str] = []
    title = card.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    if title:
        lines.append(f"<b>{esc(str(title))}</b>")
    for key in ("subtitle", "body", "subtext"):
        val = card.get(key)
        if isinstance(val, dict):
            val = val.get("text")
        if val:
            lines.append(esc(str(val)))
    return lines


def _render_card(block: dict) -> str:
    return "\n".join(_card_lines(block))


def _render_carousel(block: dict) -> str:
    sections = []
    for card in block.get("elements") or []:
        if isinstance(card, dict):
            lines = _card_lines(card)
            if lines:
                sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_image(block: dict) -> list[Media]:
    url = block.get("image_url")
    title = block.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    if url:
        return [Media(kind="photo", url=str(url), caption=_caption(str(title or block.get("alt_text") or "")))]
    return []


def _render_video(block: dict) -> list[Media]:
    video_url = block.get("video_url")
    title = block.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    if video_url:
        return [Media(kind="video", url=str(video_url), title=str(title or "Video"), caption=_caption(str(title or "")))]
    return []


def _render_generic(block: dict) -> str:
    """Best-effort text for block types without a dedicated Telegram form."""
    for key in ("text", "title", "body"):
        val = block.get(key)
        if isinstance(val, dict):
            val = val.get("text")
        if val:
            return esc(str(val))
    return ""


def _caption(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:TG_CAPTION_LIMIT]


# --------------------------------------------------------------------------- #
# In-flight activity (the "working…" message)
# --------------------------------------------------------------------------- #

_TOOL_ICON = {
    "Read": "\U0001f4d6", "Glob": "\U0001f50d", "Grep": "\U0001f50d",
    "Edit": "\u270f\ufe0f", "Write": "\u270f\ufe0f", "NotebookEdit": "\u270f\ufe0f",
    "WebFetch": "\U0001f310", "WebSearch": "\U0001f310", "Task": "\U0001f916",
    "Bash": "\u26a1", "shell_command": "\u26a1", "read_file": "\U0001f4d6",
    "edit_file": "\u270f\ufe0f", "write_file": "\u270f\ufe0f", "grep": "\U0001f50d",
    "glob": "\U0001f50d", "web_search": "\U0001f310", "web_fetch": "\U0001f310",
    "agent": "\U0001f916",
}


def tool_label(name: str, tool_input: dict) -> str:
    """Short human label for one tool call (no backticks/mrkdwn)."""
    name = name or "tool"
    if name in ("Bash", "shell_command"):
        cmd = str(tool_input.get("command") or "")
        return f"$ {_ellipsis(cmd, 80)}" if cmd else name
    for key in ("file_path", "path"):
        if tool_input.get(key):
            return f"{name} {_ellipsis(str(tool_input[key]), 80)}"
    for key in ("url", "query", "pattern", "prompt", "description"):
        if tool_input.get(key):
            return f"{name} {_ellipsis(str(tool_input[key]), 80)}"
    return name


def _ellipsis(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def render_activity(tool_events: list[dict] | None, partial_text: str = "", *, header: str = "Working on it\u2026") -> str:
    """Render the live 'thinking steps' view for the in-flight message."""
    events = tool_events or []
    lines = [f"<b>{esc(header)}</b>"]
    for event in events[-_MAX_ACTIVITY_STEPS:]:
        name = str(event.get("name") or "tool")
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        status = str(event.get("status") or "running")
        glyph = {"running": "\u25d0", "done": "\u2713", "error": "\u2717", "pending": "\u25cb"}.get(status, "\u25d0")
        icon = _TOOL_ICON.get(name, "\U0001f527")
        lines.append(f"{glyph} {icon} {esc(tool_label(name, tool_input))}")
    if not events and partial_text.strip():
        lines.append(esc(_ellipsis(partial_text.strip()[-200:], 200)))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #

def approve_deny_keyboard(thread_key: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "\u2705 Approve", "callback_data": f"confirm:approve:{thread_key}"},
            {"text": "\U0001f6ab Deny", "callback_data": f"confirm:deny:{thread_key}"},
        ]]
    }


def feedback_keyboard(thread_key: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "\U0001f44d", "callback_data": f"fb:up:{thread_key}"},
            {"text": "\U0001f44e", "callback_data": f"fb:down:{thread_key}"},
            {"text": "\U0001f501 Regenerate", "callback_data": f"regen:{thread_key}"},
            {"text": "\u2702\ufe0f Fork", "callback_data": f"fork:{thread_key}"},
        ]]
    }


def stop_keyboard(thread_key: str) -> dict:
    return {"inline_keyboard": [[{"text": "\u23f9 Stop", "callback_data": f"stop:{thread_key}"}]]}


def feedback_category_keyboard(thread_key: str) -> dict:
    cats = [
        ("Wrong answer", "wrong_answer"),
        ("Broke something", "broke_something"),
        ("Too slow", "too_slow"),
        ("Unsafe / risky", "unsafe"),
        ("Other", "other"),
    ]
    rows = [[{"text": label, "callback_data": f"fbc:{value}:{thread_key}"}] for label, value in cats]
    return {"inline_keyboard": rows}


def prompts_keyboard(prompts: list[dict]) -> dict:
    rows = [[{"text": p["title"], "callback_data": f"prompt:{i}"}] for i, p in enumerate(prompts)]
    return {"inline_keyboard": rows}


HELP_TEXT = (
    "<b>telly</b> — a local coding agent you drive from chat.\n\n"
    "Just send a message to ask for anything: inspect code, debug an error, "
    "review changes, summarize an uploaded file, run a browser task.\n\n"
    "<b>Commands</b>\n"
    "/new — start a fresh conversation\n"
    "/status — show the active agent CLI\n"
    "/sessions — list recent conversations\n"
    "/stop — stop the running turn\n"
    "/triggers — list scheduled triggers\n"
    "/help — this message\n\n"
    "Each chat keeps its own resumable agent session."
)
