"""Format conversions from Markdown (what the agent emits) to Slack surfaces,
plus helpers for chunking long replies and building agent UI blocks.

Prefer native Slack agent blocks when available:
- `markdown` blocks accept standard Markdown (preferred for final answers)
- `table` blocks for structured tabular data
- `plan` / `task_card` for multi-step and activity UI
- section + mrkdwn remains the fallback when newer blocks are rejected

Legacy mrkdwn notes (fallback path only):
- *bold* (single asterisk), not **bold**
- _italic_, not *italic*
- ~strike~, not ~~strike~~
- <url|text>, not [text](url)
- no `# headings` — render as bold instead
"""

import json
import re
from urllib.parse import urlparse

SLACK_MAX_TEXT = 2800   # section blocks cap text at 3000 chars; leave room for mrkdwn changes.
SLACK_MAX_MARKDOWN = 10000  # markdown blocks allow up to 12k cumulative; leave headroom.
_BOLD_PLACEHOLDER = "\x01BOLD\x01"
_MAX_TABLE_ROWS = 100
_MAX_TABLE_COLS = 20
_MAX_TASK_SOURCES = 5
_VALID_TASK_STATUSES = frozenset({"pending", "in_progress", "complete", "error"})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_FENCE_LANG_RE = re.compile(r"^```([a-zA-Z0-9_+-]+)\s*$", re.MULTILINE)
_VISUALIZATION_FENCE_RE = re.compile(
    r"```(?:slack-visualization|slack_visualization|slack-data-visualization|slack_data_visualization)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_TABLE_FENCE_RE = re.compile(
    r"```(?:slack-table|slack_table)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_PLAN_FENCE_RE = re.compile(
    r"```(?:slack-plan|slack_plan)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_ALERT_FENCE_RE = re.compile(
    r"```(?:slack-alert|slack_alert)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_CARD_FENCE_RE = re.compile(
    r"```(?:slack-card|slack_card)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_CAROUSEL_FENCE_RE = re.compile(
    r"```(?:slack-carousel|slack_carousel)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_IMAGE_FENCE_RE = re.compile(
    r"```(?:slack-image|slack_image)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_VIDEO_FENCE_RE = re.compile(
    r"```(?:slack-video|slack_video)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_GENERIC_BLOCK_FENCE_RE = re.compile(
    r"```(?:slack-block|slack_block)\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
# Note: do not use \s in the separator class — it matches newlines and swallows rows.
_MD_TABLE_RE = re.compile(
    r"(?:^|\n)("
    r"\|[^\n]*\|\n"
    r"\|[ \t:|\-]+\|\n?"
    r"(?:\|[^\n]*\|\n?)*"
    r")",
)
_MD_IMAGE_RE = re.compile(r"(?:^|\n)!\[([^\]]*)\]\((https?://[^\s)]+)\)(?:\n|$)")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ALERT_LEVELS = frozenset({"default", "info", "success", "warning", "error"})
# Blocks that must NOT be posted in channel/DM messages (surface restrictions).
_MESSAGE_UNSAFE_BLOCK_TYPES = frozenset({
    "alert",  # docs: alert is currently only supported in modals
    "input",  # primarily modals/home; avoid in agent message body
})
# Newer/optional blocks — drop these on invalid_blocks fallback.
_MESSAGE_OPTIONAL_BLOCK_TYPES = frozenset({
    "card", "carousel", "video", "data_table", "data_visualization", "plan", "task_card",
    "markdown",  # fall back to section mrkdwn if workspace rejects
})
_KNOWN_BLOCK_TYPES = frozenset({
    "actions", "alert", "call", "card", "carousel", "context", "context_actions",
    "data_visualization", "data_table", "divider", "file", "header", "image",
    "input", "markdown", "plan", "rich_text", "section", "table", "task_card", "video",
})
_CONFIRM_MARKER_RE = re.compile(
    r"^\s*SLACK_CONFIRM:\s*(?P<summary>.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CONFIRM_SOFT_RE = re.compile(
    r"(?is)\b("
    r"should i (proceed|continue|go ahead|do that|run that)|"
    r"do you (want me to|approve|confirm)|"
    r"may i (proceed|continue|go ahead)|"
    r"can i (proceed|continue|go ahead)|"
    r"awaiting (your )?confirmation|"
    r"confirm (before|that|this|if)|"
    r"reply (with )?(yes|'?y'?|approve)|"
    r"please confirm|"
    r"need your (approval|permission|go-ahead|go ahead)"
    r")\b"
)


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
    if not text:
        return [""]
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


def extract_native_content(text: str) -> tuple[str, list[dict]]:
    """Extract all native Slack content blocks from agent reply text.

    Order: visualizations → alerts → cards → carousels → images → videos →
    plans → generic blocks → tables (fenced + MD) → standalone MD images.
    """
    cleaned, blocks = extract_visualization_blocks(text)
    cleaned, alert_blocks = extract_alert_blocks(cleaned)
    blocks.extend(alert_blocks)
    cleaned, card_blocks = extract_card_blocks(cleaned)
    blocks.extend(card_blocks)
    cleaned, carousel_blocks = extract_carousel_blocks(cleaned)
    blocks.extend(carousel_blocks)
    cleaned, image_blocks = extract_image_blocks(cleaned)
    blocks.extend(image_blocks)
    cleaned, video_blocks = extract_video_blocks(cleaned)
    blocks.extend(video_blocks)
    cleaned, plan_blocks = extract_plan_blocks(cleaned)
    blocks.extend(plan_blocks)
    cleaned, generic_blocks = extract_generic_blocks(cleaned)
    blocks.extend(generic_blocks)
    cleaned, table_blocks = extract_table_blocks(cleaned)
    blocks.extend(table_blocks)
    cleaned, md_images = extract_markdown_images(cleaned)
    blocks.extend(md_images)
    return cleaned, blocks


def extract_visualization_blocks(text: str) -> tuple[str, list[dict]]:
    """Extract native Slack data_visualization blocks from fenced JSON.

    Assistants can emit:
    ```slack-visualization
    {"type":"data_visualization", ...}
    ```

    Valid blocks are removed from visible text. Invalid fences are left intact so
    the user can see and debug the payload instead of silently losing content.
    """
    blocks: list[dict] = []

    def replace(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group(1))
            candidates = parsed if isinstance(parsed, list) else [parsed]
            validated = [_validate_visualization_block(candidate) for candidate in candidates]
        except (TypeError, ValueError, json.JSONDecodeError):
            return match.group(0)
        blocks.extend(validated)
        return ""

    cleaned = _VISUALIZATION_FENCE_RE.sub(replace, text).strip()
    cleaned, raw_blocks = _extract_raw_visualization_json(cleaned)
    blocks.extend(raw_blocks)
    return cleaned, blocks


def extract_table_blocks(text: str) -> tuple[str, list[dict]]:
    """Extract native table blocks from fenced JSON and GFM Markdown tables.

    Fenced form:
    ```slack-table
    {"headers":["A","B"],"rows":[["1","2"]]}
    ```
    or a full `{"type":"table","rows":[...]}` payload.
    """
    blocks: list[dict] = []

    def replace_fence(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group(1))
            candidates = parsed if isinstance(parsed, list) else [parsed]
            validated = [_validate_table_block(candidate) for candidate in candidates]
        except (TypeError, ValueError, json.JSONDecodeError):
            return match.group(0)
        blocks.extend(validated)
        return ""

    cleaned = _TABLE_FENCE_RE.sub(replace_fence, text)
    cleaned, md_tables = _extract_markdown_tables(cleaned)
    blocks.extend(md_tables)
    return cleaned.strip(), blocks


def extract_plan_blocks(text: str) -> tuple[str, list[dict]]:
    """Extract plan blocks from fenced JSON.

    ```slack-plan
    {"title":"Deploy","tasks":[{"task_id":"1","title":"Run tests","status":"complete"}]}
    ```
    """
    blocks: list[dict] = []

    def replace(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group(1))
            candidates = parsed if isinstance(parsed, list) else [parsed]
            validated = [_validate_plan_block(candidate) for candidate in candidates]
        except (TypeError, ValueError, json.JSONDecodeError):
            return match.group(0)
        blocks.extend(validated)
        return ""

    cleaned = _PLAN_FENCE_RE.sub(replace, text).strip()
    return cleaned, blocks


def _extract_fenced_json_blocks(
    text: str,
    pattern: re.Pattern,
    validator,
) -> tuple[str, list[dict]]:
    blocks: list[dict] = []

    def replace(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group(1))
            candidates = parsed if isinstance(parsed, list) else [parsed]
            validated = [validator(candidate) for candidate in candidates]
        except (TypeError, ValueError, json.JSONDecodeError):
            return match.group(0)
        blocks.extend(validated)
        return ""

    cleaned = pattern.sub(replace, text).strip()
    return cleaned, blocks


def extract_alert_blocks(text: str) -> tuple[str, list[dict]]:
    return _extract_fenced_json_blocks(text, _ALERT_FENCE_RE, _validate_alert_block)


def extract_card_blocks(text: str) -> tuple[str, list[dict]]:
    return _extract_fenced_json_blocks(text, _CARD_FENCE_RE, _validate_card_block)


def extract_carousel_blocks(text: str) -> tuple[str, list[dict]]:
    return _extract_fenced_json_blocks(text, _CAROUSEL_FENCE_RE, _validate_carousel_block)


def extract_image_blocks(text: str) -> tuple[str, list[dict]]:
    return _extract_fenced_json_blocks(text, _IMAGE_FENCE_RE, _validate_image_block)


def extract_video_blocks(text: str) -> tuple[str, list[dict]]:
    return _extract_fenced_json_blocks(text, _VIDEO_FENCE_RE, _validate_video_block)


def extract_generic_blocks(text: str) -> tuple[str, list[dict]]:
    """Passthrough for any valid Block Kit block type via ```slack-block."""
    return _extract_fenced_json_blocks(text, _GENERIC_BLOCK_FENCE_RE, _validate_passthrough_block)


def extract_markdown_images(text: str) -> tuple[str, list[dict]]:
    """Convert standalone Markdown images into native image blocks."""
    blocks: list[dict] = []

    def replace(match: re.Match) -> str:
        alt = (match.group(1) or "image").strip() or "image"
        url = match.group(2).strip()
        try:
            blocks.append(_validate_image_block({"image_url": url, "alt_text": alt}))
        except (TypeError, ValueError):
            return match.group(0)
        return "\n"

    cleaned = _MD_IMAGE_RE.sub(replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, blocks


def build_alert_block(text: str, *, level: str = "info", block_id: str | None = None) -> dict:
    """Modal-only alert block (do not put in channel/DM messages)."""
    return _validate_alert_block({"text": text, "level": level, "block_id": block_id})


def build_message_status_block(text: str, *, level: str = "info") -> dict:
    """Message-safe stand-in for alert (section + emoji). Alert is modal-only."""
    icons = {
        "success": ":white_check_mark:",
        "warning": ":warning:",
        "error": ":x:",
        "info": ":information_source:",
        "default": ":information_source:",
    }
    icon = icons.get(level, icons["info"])
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"{icon} {_truncate(text, 2900)}"},
    }


def build_card_block(
    *,
    title: str,
    body: str | None = None,
    subtitle: str | None = None,
    icon: str | None = None,
    icon_url: str | None = None,
    actions: list[dict] | None = None,
    block_id: str | None = None,
) -> dict:
    payload: dict = {"title": title}
    if body:
        payload["body"] = body
    if subtitle:
        payload["subtitle"] = subtitle
    if icon_url:
        payload["icon"] = icon_url
    elif icon and icon.startswith("http"):
        payload["icon"] = icon
    # emoji icons are NOT valid on card.icon (must be image element) — omit
    if actions:
        payload["actions"] = actions
    if block_id:
        payload["block_id"] = block_id
    return _validate_card_block(payload)


def extract_confirm_request(text: str) -> tuple[str, str | None]:
    """Pull SLACK_CONFIRM marker out of agent reply. Returns (cleaned, summary|None)."""
    if not text:
        return text, None
    match = _CONFIRM_MARKER_RE.search(text)
    if not match:
        return text, None
    summary = (match.group("summary") or "").strip()
    cleaned = _CONFIRM_MARKER_RE.sub("", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, summary or "Proceed with the requested action?"


def looks_like_confirmation_request(text: str) -> bool:
    """Heuristic when the agent asks for approval without the SLACK_CONFIRM marker."""
    if not text or len(text) > 2500:
        return False
    return bool(_CONFIRM_SOFT_RE.search(text))


def build_permission_blocks(thread_ts: str, summary: str) -> list[dict]:
    """Approve / Deny buttons for destructive or externally-visible actions."""
    summary = _truncate(summary, 500)
    return [
        build_message_status_block(
            f"*Permission needed*\n{summary}\n\nApprove to let telly proceed, or deny to cancel.",
            level="warning",
        ),
        {
            "type": "actions",
            "block_id": f"telly_permission_{_safe_id(thread_ts)}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "telly_confirm_approve",
                    "value": f"{thread_ts}|{summary[:200]}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "telly_confirm_deny",
                    "value": f"{thread_ts}|{summary[:200]}",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "Telly is paused on this action until you choose. You can also reply in the thread.",
            }],
        },
    ]


def sanitize_blocks_for_messages(blocks: list[dict] | None) -> list[dict]:
    """Drop surface-illegal blocks before posting to messages."""
    if not blocks:
        return []
    out: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in _MESSAGE_UNSAFE_BLOCK_TYPES:
            # Convert alert → message-safe section
            if btype == "alert":
                text = block.get("text")
                if isinstance(text, dict):
                    text = text.get("text")
                out.append(build_message_status_block(str(text or "Notice"), level=str(block.get("level") or "info")))
            continue
        # Fix common card mistakes in place
        if btype == "card":
            try:
                out.append(_validate_card_block(block))
            except (TypeError, ValueError):
                title = block.get("title")
                if isinstance(title, dict):
                    title = title.get("text")
                body = block.get("body")
                if isinstance(body, dict):
                    body = body.get("text")
                out.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{_truncate(title or 'Card', 80)}*\n{_truncate(body or '', 500)}"},
                })
            continue
        out.append(block)
    return out


def block_fallback_tiers(blocks: list[dict], *, reply_text: str = "") -> list[list[dict]]:
    """Ordered attempts from richest → safest for chat.postMessage / stopStream."""
    safe = sanitize_blocks_for_messages(blocks)
    tiers: list[list[dict]] = [safe]
    # Drop optional/new agent blocks
    core = [b for b in safe if b.get("type") not in _MESSAGE_OPTIONAL_BLOCK_TYPES]
    if core != safe:
        tiers.append(core)
    # Convert markdown blocks to section mrkdwn
    classic: list[dict] = []
    for b in core:
        if b.get("type") == "markdown":
            classic.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": to_mrkdwn(str(b.get("text") or ""))[:3000]},
                "expand": True,
            })
        elif b.get("type") in {"context_actions"}:
            # keep feedback if possible
            classic.append(b)
        elif b.get("type") in {"actions", "context", "section", "divider", "header", "image"}:
            classic.append(b)
        # drop table/plan/task if still present
    if classic and classic not in tiers:
        tiers.append(classic)
    # Minimal last resort
    minimal: list[dict] = []
    if reply_text:
        minimal.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": to_mrkdwn(reply_text)[:3000]},
            "expand": True,
        })
    for b in classic:
        if b.get("type") in {"actions", "context_actions"}:
            minimal.append(b)
    if minimal:
        tiers.append(minimal)
    # Deduplicate identical tiers
    uniq: list[list[dict]] = []
    seen: set[str] = set()
    for tier in tiers:
        key = json.dumps(tier, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            uniq.append(tier)
    return uniq or [[]]


def build_image_block(image_url: str, *, alt_text: str = "image", title: str | None = None) -> dict:
    payload: dict = {"image_url": image_url, "alt_text": alt_text}
    if title:
        payload["title"] = title
    return _validate_image_block(payload)


def build_video_block(
    *,
    video_url: str,
    thumbnail_url: str,
    title: str,
    alt_text: str | None = None,
    title_url: str | None = None,
    description: str | None = None,
) -> dict:
    payload: dict = {
        "video_url": video_url,
        "thumbnail_url": thumbnail_url,
        "title": title,
        "alt_text": alt_text or title,
    }
    if title_url:
        payload["title_url"] = title_url
    if description:
        payload["description"] = description
    return _validate_video_block(payload)


def build_header_block(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": _limit_text(text, 150, "Telly")}}


def build_divider_block() -> dict:
    return {"type": "divider"}


def build_link_button(text: str, url: str, *, action_id: str = "telly_link", style: str | None = None) -> dict:
    btn: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": _limit_text(text, 75, "Open")},
        "url": url[:3000],
        "action_id": action_id,
    }
    if style in {"primary", "danger"}:
        btn["style"] = style
    return btn


def _mrkdwn_text_obj(text: str, limit: int = 3000) -> dict:
    return {"type": "mrkdwn", "text": _truncate(str(text), limit)}


def _plain_text_obj(text: str, limit: int = 150) -> dict:
    return {"type": "plain_text", "text": _limit_text(text, limit, " "), "emoji": True}


def _validate_alert_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("alert must be an object")
    text = block.get("text") or block.get("message")
    if isinstance(text, dict):
        text = text.get("text")
    if not text:
        raise ValueError("alert text required")
    level = str(block.get("level") or "info").lower()
    if level not in _ALERT_LEVELS:
        level = "info"
    out: dict = {
        "type": "alert",
        "level": level,
        "text": _mrkdwn_text_obj(str(text), 3000),
    }
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    return out


def _validate_card_block(block: object) -> dict:
    """Card schema per docs: icon/hero_image are image elements; body max 200."""
    if not isinstance(block, dict):
        raise ValueError("card must be an object")
    title = block.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    body_raw = block.get("body")
    if isinstance(body_raw, dict):
        body_raw = body_raw.get("text")
    if not title and not body_raw and not block.get("hero_image") and not block.get("actions"):
        raise ValueError("card needs title, body, hero_image, or actions")
    out: dict = {"type": "card"}
    if title:
        out["title"] = _mrkdwn_text_obj(str(title), 150)
    for key, limit in (("subtitle", 150), ("body", 200), ("subtext", 200)):
        val = block.get(key)
        if isinstance(val, dict):
            val = val.get("text")
        if val:
            out[key] = _mrkdwn_text_obj(str(val), limit)
    # icon must be an image element, not an emoji string
    icon = block.get("icon")
    if isinstance(icon, dict) and icon.get("type") == "image":
        out["icon"] = {
            "type": "image",
            "image_url": str(icon.get("image_url") or "")[:3000],
            "alt_text": _limit_text(icon.get("alt_text") or "icon", 2000, "icon"),
        }
    elif isinstance(icon, str) and _is_http_url(icon):
        out["icon"] = {"type": "image", "image_url": icon[:3000], "alt_text": "icon"}
    # ignore emoji icons like :robot_face: — invalid for card.icon
    hero = block.get("hero_image")
    if isinstance(hero, dict) and hero.get("type") == "image":
        out["hero_image"] = {
            "type": "image",
            "image_url": str(hero.get("image_url") or "")[:3000],
            "alt_text": _limit_text(hero.get("alt_text") or "image", 2000, "image"),
        }
    elif isinstance(hero, str) and _is_http_url(hero):
        out["hero_image"] = {"type": "image", "image_url": hero[:3000], "alt_text": "image"}
    if isinstance(block.get("actions"), list):
        # docs: max 3 buttons
        out["actions"] = [a for a in block["actions"] if isinstance(a, dict)][:3]
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    if block.get("slack_icon") and isinstance(block["slack_icon"], dict):
        out["slack_icon"] = block["slack_icon"]
    return out


def _validate_carousel_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("carousel must be an object")
    elements_in = block.get("elements") or block.get("cards") or []
    if not isinstance(elements_in, list) or not elements_in:
        raise ValueError("carousel needs cards")
    elements: list[dict] = []
    for card in elements_in[:10]:
        if not isinstance(card, dict):
            raise ValueError("carousel card must be an object")
        elements.append(_validate_card_block(card))
    for el in elements:
        el["type"] = "card"
    out: dict = {"type": "carousel", "elements": elements}
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    return out


def _validate_image_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("image must be an object")
    image_url = block.get("image_url") or block.get("url")
    if not image_url or not _is_http_url(str(image_url)):
        # allow slack_file
        if not isinstance(block.get("slack_file"), dict):
            raise ValueError("image_url or slack_file required")
    alt = _limit_text(block.get("alt_text") or block.get("alt") or "image", 2000, "image")
    out: dict = {"type": "image", "alt_text": alt}
    if image_url and _is_http_url(str(image_url)):
        out["image_url"] = str(image_url)[:3000]
    if isinstance(block.get("slack_file"), dict):
        out["slack_file"] = block["slack_file"]
    title = block.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    if title:
        out["title"] = _plain_text_obj(str(title), 2000)
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    return out


def _validate_video_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("video must be an object")
    video_url = block.get("video_url") or block.get("url")
    thumbnail_url = block.get("thumbnail_url") or block.get("thumbnail")
    title = block.get("title")
    if isinstance(title, dict):
        title = title.get("text")
    if not video_url or not thumbnail_url or not title:
        raise ValueError("video needs video_url, thumbnail_url, and title")
    if not _is_http_url(str(video_url)) or not _is_http_url(str(thumbnail_url)):
        raise ValueError("video URLs must be http(s)")
    out: dict = {
        "type": "video",
        "video_url": str(video_url)[:3000],
        "thumbnail_url": str(thumbnail_url)[:3000],
        "title": _plain_text_obj(str(title), 200),
        "alt_text": _limit_text(block.get("alt_text") or title, 200, str(title)),
    }
    if block.get("title_url") and _is_http_url(str(block["title_url"])):
        out["title_url"] = str(block["title_url"])[:3000]
    desc = block.get("description")
    if isinstance(desc, dict):
        desc = desc.get("text")
    if desc:
        out["description"] = _plain_text_obj(str(desc), 200)
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    if block.get("provider_icon_url") and _is_http_url(str(block["provider_icon_url"])):
        out["provider_icon_url"] = str(block["provider_icon_url"])[:3000]
    if block.get("provider_name"):
        out["provider_name"] = _limit_text(block["provider_name"], 50, "Telly")
    if block.get("author_name"):
        out["author_name"] = _limit_text(block["author_name"], 50, "Telly")
    return out


def _validate_passthrough_block(block: object) -> dict:
    if not isinstance(block, dict) or block.get("type") not in _KNOWN_BLOCK_TYPES:
        raise ValueError("unknown or invalid block type")
    # Re-validate known types we fully support
    t = block["type"]
    if t == "alert":
        return _validate_alert_block(block)
    if t == "card":
        return _validate_card_block(block)
    if t == "carousel":
        return _validate_carousel_block(block)
    if t == "image":
        return _validate_image_block(block)
    if t == "video":
        return _validate_video_block(block)
    if t == "table":
        return _validate_table_block(block)
    if t == "plan":
        return _validate_plan_block(block)
    if t == "data_visualization":
        return _validate_visualization_block(block)
    # Passthrough for advanced types (data_table, etc.) with type present
    return dict(block)


def _extract_markdown_tables(text: str) -> tuple[str, list[dict]]:
    blocks: list[dict] = []
    if not text:
        return text, blocks

    def replace(match: re.Match) -> str:
        table_text = match.group(1).strip("\n")
        try:
            block = _markdown_table_to_block(table_text)
        except (TypeError, ValueError):
            return match.group(0)
        blocks.append(block)
        return "\n"

    cleaned = _MD_TABLE_RE.sub(replace, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, blocks


def _markdown_table_to_block(table_text: str) -> dict:
    lines = [line.strip() for line in table_text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("table needs header and separator")
    if not _is_table_separator(lines[1]):
        raise ValueError("second row must be a separator")
    header = _split_md_table_row(lines[0])
    if not header:
        raise ValueError("empty header")
    width = min(len(header), _MAX_TABLE_COLS)
    header = header[:width]
    data_rows: list[list[str]] = []
    for line in lines[2:_MAX_TABLE_ROWS + 1]:
        cells = _split_md_table_row(line)
        if not cells:
            continue
        # Pad / trim to header width
        row = (cells + [""] * width)[:width]
        data_rows.append(row)
    rows = [header, *data_rows]
    return _rows_to_table_block(rows)


def _is_table_separator(line: str) -> bool:
    cells = _split_md_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _split_md_table_row(line: str) -> list[str]:
    text = line.strip()
    if not text.startswith("|"):
        return []
    if text.endswith("|"):
        text = text[1:-1]
    else:
        text = text[1:]
    return [cell.strip() for cell in text.split("|")]


def _rows_to_table_block(rows: list[list[str]], column_settings: list[dict] | None = None) -> dict:
    if not rows or not rows[0]:
        raise ValueError("table rows required")
    width = min(max(len(row) for row in rows), _MAX_TABLE_COLS)
    if width < 1:
        raise ValueError("table needs at least one column")
    table_rows: list[list[dict]] = []
    char_count = 0
    for row in rows[:_MAX_TABLE_ROWS]:
        cells = (list(row) + [""] * width)[:width]
        cell_objs: list[dict] = []
        for cell in cells:
            cell_obj = _table_cell(cell)
            char_count += len(str(cell))
            if char_count > 10000:
                raise ValueError("table exceeds character limit")
            cell_objs.append(cell_obj)
        table_rows.append(cell_objs)
    block: dict = {"type": "table", "rows": table_rows}
    if column_settings:
        block["column_settings"] = column_settings[:width]
    return block


def _table_cell(value: object) -> dict:
    """Build a table cell. Prefer raw_text for broad client compatibility."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        text = str(value)
    else:
        text = str(value if value is not None else "").replace("\n", " ").strip()
    return {"type": "raw_text", "text": text[:500] or " "}


def _validate_table_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("table must be an object")

    if block.get("type") == "table" and isinstance(block.get("rows"), list):
        rows_in = block["rows"]
        if not rows_in:
            raise ValueError("table rows required")
        # Full Block Kit rows already
        if rows_in and isinstance(rows_in[0], list) and rows_in[0] and isinstance(rows_in[0][0], dict):
            validated_rows: list[list[dict]] = []
            for row in rows_in[:_MAX_TABLE_ROWS]:
                if not isinstance(row, list):
                    raise ValueError("table row must be a list")
                cells = []
                for cell in row[:_MAX_TABLE_COLS]:
                    if not isinstance(cell, dict) or cell.get("type") not in {"raw_text", "raw_number", "rich_text"}:
                        raise ValueError("invalid table cell")
                    # Normalize raw_number-ish payloads into raw_text when only a scalar is given
                    if cell.get("type") == "raw_number" and "text" not in cell and "number" in cell:
                        cells.append({"type": "raw_text", "text": str(cell["number"])})
                    else:
                        cells.append(cell)
                if cells:
                    validated_rows.append(cells)
            if not validated_rows:
                raise ValueError("table rows required")
            out: dict = {"type": "table", "rows": validated_rows}
            if isinstance(block.get("column_settings"), list):
                out["column_settings"] = block["column_settings"][:_MAX_TABLE_COLS]
            if block.get("block_id"):
                out["block_id"] = _safe_id(str(block["block_id"]))
            return out
        # rows as list of lists of scalars
        scalar_rows = [[str(c) if not isinstance(c, (int, float)) else c for c in row] for row in rows_in if isinstance(row, list)]
        settings = block.get("column_settings") if isinstance(block.get("column_settings"), list) else None
        return _rows_to_table_block(scalar_rows, settings)

    headers = block.get("headers") or block.get("columns")
    data = block.get("rows") or block.get("data")
    if not isinstance(headers, list) or not headers:
        raise ValueError("table headers required")
    if not isinstance(data, list):
        data = []
    header_row = [str(h) for h in headers[:_MAX_TABLE_COLS]]
    rows = [header_row]
    for row in data[: _MAX_TABLE_ROWS - 1]:
        if isinstance(row, dict):
            rows.append([row.get(h, "") for h in header_row])
        elif isinstance(row, list):
            rows.append(list(row)[: len(header_row)])
        else:
            continue
    settings = block.get("column_settings") if isinstance(block.get("column_settings"), list) else None
    return _rows_to_table_block(rows, settings)


def _validate_plan_block(block: object) -> dict:
    if not isinstance(block, dict):
        raise ValueError("plan must be an object")
    if block.get("type") not in {None, "plan"}:
        raise ValueError("expected plan block")
    title = _limit_text(block.get("title"), 150, "Plan")
    tasks_in = block.get("tasks") or []
    if not isinstance(tasks_in, list) or not tasks_in:
        raise ValueError("plan tasks required")
    tasks: list[dict] = []
    for i, task in enumerate(tasks_in[:20]):
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        task_id = _safe_id(str(task.get("task_id") or task.get("id") or f"task_{i + 1}"))
        status = str(task.get("status") or "pending")
        if status not in _VALID_TASK_STATUSES:
            status = "pending"
        item: dict = {
            "task_id": task_id,
            "title": _limit_text(task.get("title") or task.get("text"), 150, f"Task {i + 1}"),
            "status": status,
        }
        details = task.get("details")
        if isinstance(details, str) and details.strip():
            item["details"] = _rich_text(_truncate(details, 500))
        elif isinstance(details, dict):
            item["details"] = details
        output = task.get("output")
        if isinstance(output, str) and output.strip():
            item["output"] = _rich_text(_truncate(output, 500))
        elif isinstance(output, dict):
            item["output"] = output
        sources = task.get("sources")
        if isinstance(sources, list):
            validated_sources = _validate_sources(sources)
            if validated_sources:
                item["sources"] = validated_sources
        tasks.append(item)
    out = {"type": "plan", "title": title, "tasks": tasks}
    if block.get("block_id"):
        out["block_id"] = _safe_id(str(block["block_id"]))
    return out


def _validate_sources(sources: object) -> list[dict]:
    if not isinstance(sources, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen or not _is_http_url(url):
            continue
        seen.add(url)
        text = _limit_text(source.get("text") or _source_label(url), 75, _source_label(url))
        out.append({"type": "url", "url": url[:2000], "text": text})
        if len(out) >= _MAX_TASK_SOURCES:
            break
    return out


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _source_label(url: str) -> str:
    try:
        host = urlparse(url).netloc or url
    except Exception:
        host = url
    return _limit_text(host, 75, "source")


def collect_task_sources(
    tool_events: list[dict] | None = None,
) -> list[dict]:
    """Build URL source elements from tool events."""
    sources: list[dict] = []

    for event in tool_events or []:
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        name = str(event.get("name") or "")
        candidates: list[tuple[str, str]] = []
        for key in ("url", "uri", "href"):
            val = tool_input.get(key)
            if isinstance(val, str) and _is_http_url(val):
                label = "Browser" if "browser" in name.lower() or name.startswith("playwright") else _source_label(val)
                if name in {"WebFetch", "WebSearch"}:
                    label = "Web" if name == "WebSearch" else _source_label(val)
                candidates.append((val, label))
        # Scan string values for URLs (navigate args, bash curl, etc.)
        if not candidates:
            for val in tool_input.values():
                if not isinstance(val, str):
                    continue
                for match in _HTTP_URL_RE.findall(val):
                    url = match.rstrip(").,;\"'")
                    if _is_http_url(url):
                        candidates.append((url, _source_label(url)))
        for url, label in candidates:
            sources.append({"type": "url", "url": url, "text": label})

    return _validate_sources(sources)


def file_paths_from_tool_events(tool_events: list[dict] | None = None) -> list[str]:
    """Collect unique file paths touched by file tools (for task output text)."""
    paths: list[str] = []
    seen: set[str] = set()
    for event in tool_events or []:
        name = str(event.get("name") or "")
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        if name not in {"Read", "Edit", "Write", "NotebookEdit"}:
            continue
        path = tool_input.get("file_path") or tool_input.get("path")
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= 5:
            break
    return paths


def _extract_raw_visualization_json(text: str) -> tuple[str, list[dict]]:
    blocks: list[dict] = []
    spans: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            parsed, end_offset = decoder.raw_decode(text[start:])
            block = _validate_visualization_block(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            idx = start + 1
            continue
        blocks.append(block)
        spans.append((start, start + end_offset))
        idx = start + end_offset

    if not spans:
        return text, []
    kept: list[str] = []
    last = 0
    for start, end in spans:
        kept.append(text[last:start])
        last = end
    kept.append(text[last:])
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    return cleaned, blocks


def _validate_visualization_block(block: object) -> dict:
    if not isinstance(block, dict) or block.get("type") != "data_visualization":
        raise ValueError("expected data_visualization block")
    block = _normalize_visualization_block(block)
    title = _limit_text(block.get("title"), 50, "Visualization")
    chart = block.get("chart")
    if not isinstance(chart, dict):
        raise ValueError("chart must be an object")

    chart_type = chart.get("type")
    if chart_type == "pie":
        validated_chart = {"type": "pie", "segments": _validate_segments(chart.get("segments"))}
    elif chart_type in {"bar", "area", "line"}:
        categories = _validate_categories(((chart.get("axis_config") or {}).get("categories")))
        validated_chart = {
            "type": chart_type,
            "series": _validate_series(chart.get("series"), categories),
            "axis_config": _validate_axis_config(chart.get("axis_config"), categories),
        }
    else:
        raise ValueError("unsupported chart type")

    validated = {"type": "data_visualization", "title": title, "chart": validated_chart}
    if block.get("block_id"):
        validated["block_id"] = _safe_id(str(block["block_id"]))
    return validated


def _normalize_visualization_block(block: dict) -> dict:
    chart = block.get("chart")
    if isinstance(chart, dict) and "series" in chart and "axis_config" in chart:
        return block

    if isinstance(chart, dict) and chart.get("type") in {"line", "bar", "area"} and isinstance(chart.get("data"), list):
        return _normalize_tabular_visualization(
            block,
            chart_type=chart.get("type"),
            data=chart.get("data"),
            x_key=chart.get("x"),
            y_key=chart.get("y"),
            title=block.get("title") or chart.get("title"),
            x_label=chart.get("x_label") or chart.get("x"),
            y_label=chart.get("y_label") or chart.get("y"),
        )

    chart_type = block.get("chart_type")
    data = block.get("data")
    if chart_type not in {"line", "bar", "area"} or not isinstance(data, list) or not data:
        return block

    return _normalize_tabular_visualization(
        block,
        chart_type=chart_type,
        data=data,
        x_key=None,
        y_key=None,
        title=block.get("title"),
        x_label=block.get("x_axis"),
        y_label=block.get("y_axis"),
    )


def _normalize_tabular_visualization(
    block: dict,
    *,
    chart_type: str,
    data: list,
    x_key: object,
    y_key: object,
    title: object,
    x_label: object,
    y_label: object,
) -> dict:
    first = data[0]
    if not isinstance(first, dict):
        return block
    x_key = str(x_key) if x_key else ("date" if "date" in first else next((k for k, v in first.items() if not isinstance(v, (int, float))), None))
    y_key = str(y_key) if y_key else next((k for k, v in first.items() if k != x_key and isinstance(v, (int, float)) and not isinstance(v, bool)), None)
    if not x_key or not y_key:
        return block

    points: list[dict] = []
    categories: list[str] = []
    for row in data[:20]:
        if not isinstance(row, dict) or x_key not in row or y_key not in row:
            continue
        label = _limit_text(row[x_key], 20, "")
        value = row[y_key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        categories.append(label)
        points.append({"label": label, "value": value})
    if not points:
        return block

    normalized = dict(block)
    normalized["title"] = _limit_text(title, 50, "Visualization")
    normalized["chart"] = {
        "type": chart_type,
        "series": [{"name": _limit_text(y_label or y_key, 20, "Series"), "data": points}],
        "axis_config": {
            "categories": categories,
            "x_label": _limit_text(x_label or x_key, 50, ""),
            "y_label": _limit_text(y_label or y_key, 50, ""),
        },
    }
    return normalized


def _limit_text(value: object, limit: int, fallback: str) -> str:
    text = str(value or fallback).replace("\n", " ").strip()
    return text[:limit] or fallback


def _number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value must be numeric")
    return value


def _validate_segments(segments: object) -> list[dict]:
    if not isinstance(segments, list) or not 1 <= len(segments) <= 6:
        raise ValueError("pie segments must contain 1-6 items")
    validated = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("segment must be an object")
        value = _number(segment.get("value"))
        if value <= 0:
            raise ValueError("pie segment value must be greater than 0")
        validated.append({"label": _limit_text(segment.get("label"), 20, "Segment"), "value": value})
    return validated


def _validate_categories(categories: object) -> list[str]:
    if not isinstance(categories, list) or not categories:
        raise ValueError("categories must be a non-empty list")
    return [_limit_text(category, 20, "Category") for category in categories[:20]]


def _validate_axis_config(axis_config: object, categories: list[str]) -> dict:
    if not isinstance(axis_config, dict):
        raise ValueError("axis_config must be an object")
    validated: dict = {"categories": categories}
    if axis_config.get("x_label"):
        validated["x_label"] = _limit_text(axis_config.get("x_label"), 50, "")
    if axis_config.get("y_label"):
        validated["y_label"] = _limit_text(axis_config.get("y_label"), 50, "")
    return validated


def _validate_series(series: object, categories: list[str]) -> list[dict]:
    if not isinstance(series, list) or not 1 <= len(series) <= 6:
        raise ValueError("series must contain 1-6 items")
    category_set = set(categories)
    names: set[str] = set()
    validated = []
    for item in series:
        if not isinstance(item, dict):
            raise ValueError("series item must be an object")
        name = _limit_text(item.get("name"), 20, "Series")
        if name in names:
            raise ValueError("series names must be unique")
        names.add(name)
        data = item.get("data")
        if not isinstance(data, list) or len(data) != len(categories):
            raise ValueError("series data must match categories")
        points = []
        seen_labels: set[str] = set()
        for point in data:
            if not isinstance(point, dict):
                raise ValueError("data point must be an object")
            label = _limit_text(point.get("label"), 20, "")
            if label not in category_set:
                raise ValueError("data point label must match categories")
            seen_labels.add(label)
            points.append({"label": label, "value": _number(point.get("value"))})
        if seen_labels != category_set:
            raise ValueError("series must include every category")
        validated.append({"name": name, "data": points})
    return validated


def build_sources_block(sources: list[dict] | None) -> dict | None:
    """Compact citations row for the foot of a reply: `Sources · <link> · <link>`."""
    validated = _validate_sources(sources or [])
    if not validated:
        return None
    links = " · ".join(f"<{s['url']}|{s['text']}>" for s in validated)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Sources · {links}"}]}


def _truncate(value: object, limit: int = 100) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Bash invocations whose first token is one of these are pure exploration.
_NOISY_BASH_CMDS = {
    "ls", "cat", "head", "tail", "pwd", "which", "find", "grep", "rg",
    "wc", "stat", "file", "du", "df", "tree", "echo", "printf",
}


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


_TOOL_ICONS = {
    "Read": ":mag:", "Glob": ":mag:", "Grep": ":mag:",
    "Edit": ":pencil2:", "Write": ":pencil2:", "NotebookEdit": ":pencil2:",
    "WebFetch": ":globe_with_meridians:", "WebSearch": ":globe_with_meridians:",
    "Task": ":robot_face:",
    "Bash": ":zap:",
}


def phase_for_tool(name: str, status: str) -> tuple[str, str]:
    if status == "done":
        return ":white_check_mark:", "wrapping up"
    if name in {"Read", "Glob", "Grep"}:
        return ":mag:", "searching workspace"
    if name in {"Edit", "Write", "NotebookEdit"}:
        return ":pencil2:", "editing files"
    if name in {"WebFetch", "WebSearch"} or name.startswith("playwright_browser"):
        return ":globe_with_meridians:", "using browser"
    if name == "Task":
        return ":robot_face:", "delegating to sub-agent"
    if name == "Bash":
        return ":zap:", "running command"
    return ":gear:", "using tools"


# Back-compat alias
_phase_for_tool = phase_for_tool


def build_activity_blocks(
    thread_ts: str,
    tool_events: list[dict] | None = None,
    partial_text: str = "",
) -> list[dict]:
    """Build the in-flight Slack agent card with live activity details."""
    events = tool_events or []
    current = next((event for event in reversed(events) if event.get("status") == "running"), None)
    current = current or (events[-1] if events else None)
    phase_icon, phase = _phase_for_tool(current.get("name", "") if current else "", current.get("status", "") if current else "")
    if not current and partial_text:
        phase_icon, phase = ":speech_balloon:", "drafting reply"
    elif not current:
        phase_icon, phase = ":hourglass_flowing_sand:", "starting"

    rows: list[str] = []
    for event in events[-6:]:
        name = str(event.get("name") or "tool")
        tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
        icon = _status_marker(str(event.get("status") or "running"), _tool_icon(name, tool_input))
        rows.append(f"{icon} {format_breadcrumb(name, tool_input)}")
    if not rows:
        rows.append(":small_blue_diamond: Preparing the local agent and workspace.")

    file_paths = file_paths_from_tool_events(events)
    if file_paths:
        rows.append(":page_facing_up: " + ", ".join(f"`{_truncate(p, 60)}`" for p in file_paths[:3]))

    details = "\n".join([f"{phase_icon} {phase}", *rows])
    if partial_text.strip():
        details = details + "\n\n" + _truncate(to_mrkdwn(partial_text.strip())[-1200:], 1200)

    sources = collect_task_sources(events)
    work_task: dict = {
        "task_id": f"{_safe_id(thread_ts)}_work",
        "title": f"{phase_icon} {phase}",
        "status": "in_progress",
        "details": _rich_text(details),
    }
    if sources:
        work_task["sources"] = sources

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
                work_task,
                {
                    "task_id": f"{sid}_answer",
                    "title": "Post the complete answer",
                    "status": "pending",
                },
            ],
        },
        build_stop_actions_block(thread_ts),
    ]

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
    """Initial in-flight UI before agent events arrive."""
    return build_activity_blocks(thread_ts)


def build_plain_loading_blocks(thread_ts: str) -> list[dict]:
    """Fallback for workspaces that reject newer agent blocks."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":hourglass_flowing_sand: *Working on your request...*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Posting the complete answer when ready."}]},
        build_stop_actions_block(thread_ts),
    ]


def build_feedback_block(thread_ts: str) -> dict:
    """context_actions with feedback_buttons + icon_button (copy session / delete)."""
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
            },
            {
                "type": "icon_button",
                "action_id": "telly_delete_message",
                "icon": "trash",
                "text": {"type": "plain_text", "text": "Delete"},
                "value": thread_ts,
                "confirm": {
                    "title": {"type": "plain_text", "text": "Delete this reply?"},
                    "text": {"type": "mrkdwn", "text": "This removes the message from the thread."},
                    "confirm": {"type": "plain_text", "text": "Delete"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                },
            },
        ],
    }


def build_feedback_modal(
    *,
    thread_ts: str,
    channel: str | None = None,
    message_ts: str | None = None,
) -> dict:
    """Modal opened when the user marks a response as bad."""
    metadata = {
        "thread_ts": thread_ts,
        "channel": channel or "",
        "message_ts": message_ts or "",
    }
    return {
        "type": "modal",
        "callback_id": "telly_feedback_modal",
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "What went wrong?"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "feedback_category",
                "label": {"type": "plain_text", "text": "Category"},
                "element": {
                    "type": "static_select",
                    "action_id": "category",
                    "placeholder": {"type": "plain_text", "text": "Select a category"},
                    "options": [
                        {"text": {"type": "plain_text", "text": "Wrong answer"}, "value": "wrong_answer"},
                        {"text": {"type": "plain_text", "text": "Broke something"}, "value": "broke_something"},
                        {"text": {"type": "plain_text", "text": "Too slow"}, "value": "too_slow"},
                        {"text": {"type": "plain_text", "text": "Unsafe / risky"}, "value": "unsafe"},
                        {"text": {"type": "plain_text", "text": "Other"}, "value": "other"},
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "feedback_notes",
                "optional": True,
                "label": {"type": "plain_text", "text": "Details (optional)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "notes",
                    "multiline": True,
                    "max_length": 1000,
                    "placeholder": {"type": "plain_text", "text": "What should telly do differently?"},
                },
            },
        ],
    }


def parse_feedback_modal_submission(view: dict) -> dict:
    """Extract category/notes/metadata from a feedback modal view submission."""
    meta_raw = view.get("private_metadata") or "{}"
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else {}
    except json.JSONDecodeError:
        meta = {}
    values = (view.get("state") or {}).get("values") or {}
    category = (
        ((values.get("feedback_category") or {}).get("category") or {})
        .get("selected_option") or {}
    ).get("value") or "other"
    notes = ((values.get("feedback_notes") or {}).get("notes") or {}).get("value") or ""
    return {
        "thread_ts": meta.get("thread_ts") or "",
        "channel": meta.get("channel") or "",
        "message_ts": meta.get("message_ts") or "",
        "category": str(category),
        "notes": str(notes).strip()[:1000],
    }


def build_error_blocks(thread_ts: str, error: str) -> list[dict]:
    short = _truncate(error, 700)
    return [
        # alert is modal-only — use message-safe status section
        build_message_status_block(f"*Telly hit a problem*\n`{short}`", level="error"),
        {
            "type": "task_card",
            "task_id": f"{_safe_id(thread_ts)}_error",
            "title": "Telly hit a problem",
            "status": "error",
            "output": _rich_text(short),
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "I preserved the thread state. You can retry, fork, or inspect logs.",
            },
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
    """Buttons + overflow menu under each reply."""
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
            {
                "type": "overflow",
                "action_id": "telly_overflow",
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "Stop running turn"},
                        "value": f"stop:{thread_ts}",
                    },
                    {
                        "text": {"type": "plain_text", "text": "List triggers"},
                        "value": f"list_triggers:{thread_ts}",
                    },
                    {
                        "text": {"type": "plain_text", "text": "Open App Home"},
                        "value": f"home:{thread_ts}",
                    },
                ],
            },
        ],
    }


def build_session_card(state: object) -> dict:
    """Card summarizing one telly thread for App Home / carousels.

    Card.icon must be an image element (URL), not an emoji — we omit icon.
    """
    thread_ts = getattr(state, "thread_ts", "")
    title = _truncate(getattr(state, "last_message", None) or "Untitled thread", 80)
    status = str(getattr(state, "status", "idle") or "idle")
    agent = str(getattr(state, "agent_cmd", "") or "")
    session = getattr(state, "session_id", None)
    body_parts = [f"*Status:* {status}"]
    if agent:
        body_parts.append(f"*Agent:* `{_truncate(agent, 40)}`")
    if session:
        body_parts.append(f"*Session:* `{str(session)[:8]}`")
    body = _truncate(" · ".join(body_parts), 200)
    return build_card_block(
        title=title,
        subtitle=_truncate(thread_ts, 150),
        body=body,
        actions=[
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Stop"},
                "style": "danger",
                "action_id": "telly_stop",
                "value": thread_ts,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Fork"},
                "action_id": "telly_fork",
                "value": thread_ts,
            },
        ],
        block_id=f"session_card_{_safe_id(thread_ts)}",
    )


def build_home_view(threads: list[object]) -> dict:
    """App Home — prefer reliable section/actions; optional cards when valid."""
    blocks: list[dict] = [
        build_header_block("Telly Control Center"),
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Monitor agent threads, recover sessions, and control background work.\n"
                    "Use *messages* with telly for the agent chat surface."
                ),
            },
        },
        build_divider_block(),
    ]
    if not threads:
        blocks.append(build_message_status_block(
            "No telly threads yet. Mention me or DM to start.",
            level="info",
        ))
    else:
        # Reliable section rows first (always work)
        for state in threads[:10]:
            thread_ts = getattr(state, "thread_ts", "")
            title = _truncate(getattr(state, "last_message", None) or "No message", 80)
            status = getattr(state, "status", "idle")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*\n`{thread_ts}` · {status}"},
                "accessory": {
                    "type": "overflow",
                    "action_id": "telly_overflow",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Stop"}, "value": f"stop:{thread_ts}"},
                        {"text": {"type": "plain_text", "text": "Fork"}, "value": f"fork:{thread_ts}"},
                    ],
                },
            })
            blocks.append({
                "type": "actions",
                "block_id": f"home_actions_{_safe_id(thread_ts)}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Stop"},
                        "style": "danger",
                        "action_id": "telly_stop",
                        "value": thread_ts,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Fork"},
                        "action_id": "telly_fork",
                        "value": thread_ts,
                    },
                ],
            })
    blocks.append(build_divider_block())
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                "AI-generated output can be wrong. Review commands and code changes before relying on them. "
                "Approve/Deny buttons appear when telly needs permission for a destructive action."
            ),
        }],
    })
    return {"type": "home", "blocks": blocks[:100]}


def build_blocks(
    reply_chunk: str,
    *,
    completion_block: dict | None = None,
    breadcrumbs_block: dict | None = None,
    visualization_blocks: list[dict] | None = None,
    content_blocks: list[dict] | None = None,
    footer: dict | None = None,
    actions_block: dict | None = None,
    feedback_block: dict | None = None,
    use_markdown_block: bool = True,
) -> list[dict]:
    """Block Kit payload for one message chunk. Agent UI goes on top, controls at the bottom.

    Prefer the native `markdown` block for LLM answers (standard Markdown). When
    `use_markdown_block` is False, fall back to section + converted mrkdwn.
    `content_blocks` / `visualization_blocks` may include tables, plans, and charts.
    """
    blocks: list[dict] = []
    if completion_block:
        blocks.append(completion_block)
    if breadcrumbs_block:
        blocks.append(breadcrumbs_block)
    if reply_chunk:
        if use_markdown_block:
            blocks.append({"type": "markdown", "text": reply_chunk})
        else:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": to_mrkdwn(reply_chunk)},
                "expand": True,
            })
    extras: list[dict] = []
    if content_blocks:
        extras.extend(content_blocks)
    if visualization_blocks:
        extras.extend(visualization_blocks)
    if extras:
        blocks.extend(extras[:12])
    if actions_block:
        blocks.append(actions_block)
    if feedback_block:
        blocks.append(feedback_block)
    if footer:
        blocks.append(footer)
    return blocks
