"""Complete Slack Block Kit inventory (as of Slack platform 2026).

Source of truth: docs.slack.dev/reference/block-kit + python-slack-sdk models.
Telly integration status is tracked so we can keep expanding deliberately.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# BLOCKS — visual components for messages, modals, App Home
# ---------------------------------------------------------------------------

BLOCKS: dict[str, dict] = {
    "actions": {
        "description": "Row of interactive elements (buttons, menus, pickers). Max 25 elements.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",  # stop / regenerate / fork / home actions
    },
    "alert": {
        "description": "Status banner with level: default|info|success|warning|error. MODAL-ONLY in current Slack docs.",
        "surfaces": ["modals"],
        "telly": "modal_only",  # message-safe stand-in: build_message_status_block
    },
    "call": {
        "description": "Embed a Slack call object (call_id).",
        "surfaces": ["messages"],
        "telly": "n/a",  # coding agent does not host Slack calls
    },
    "card": {
        "description": "Content card with icon, title, subtitle, body, optional actions/hero image.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "integrated",
    },
    "carousel": {
        "description": "Horizontally scrollable set of card blocks.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "integrated",
    },
    "context": {
        "description": "Muted metadata row (images + mrkdwn/plain_text). Max 10 elements.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",  # footers, breadcrumbs
    },
    "context_actions": {
        "description": "Lightweight action row for AI responses (feedback_buttons, icon_button).",
        "surfaces": ["messages"],
        "telly": "used",  # feedback + icon buttons
    },
    "data_visualization": {
        "description": "Native charts: pie, bar, area, line.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",  # slack-visualization fences
    },
    "divider": {
        "description": "Horizontal rule between content sections.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",
    },
    "file": {
        "description": "Remote file reference (external_id + source). Not for local uploads.",
        "surfaces": ["messages"],
        "telly": "partial",  # files uploaded via files_upload_v2 instead
    },
    "header": {
        "description": "Large plain_text title (max 150 chars).",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",
    },
    "image": {
        "description": "Image via public image_url or slack_file.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "integrated",
    },
    "input": {
        "description": "Label + input element. Primarily modals/Home; can dispatch in messages.",
        "surfaces": ["modals", "home", "messages"],
        "telly": "used",  # feedback modal
    },
    "markdown": {
        "description": "Standard Markdown → Slack formatting (AI-oriented). 12k cumulative.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",
    },
    "plan": {
        "description": "Groups task_card tasks into a multi-step plan.",
        "surfaces": ["messages"],
        "telly": "used",
    },
    "rich_text": {
        "description": "Composer-grade rich text: sections, lists, quotes, preformatted.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",  # task details/output
    },
    "section": {
        "description": "Text + optional fields (2-col) + optional accessory element.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",
    },
    "table": {
        "description": "Structured table (raw_text / raw_number / rich_text cells). Max 100 rows.",
        "surfaces": ["messages", "modals", "home"],
        "telly": "used",
    },
    "task_card": {
        "description": "Single agent task with status, details, output, sources.",
        "surfaces": ["messages"],
        "telly": "used",
    },
    "video": {
        "description": "Embedded video (video_url + thumbnail_url + title + alt_text).",
        "surfaces": ["messages", "modals", "home"],
        "telly": "integrated",
    },
    # Documented on platform; may require newer workspaces
    "data_table": {
        "description": "Rich interactive data table (pagination/sort/filter; work objects).",
        "surfaces": ["messages", "modals", "home"],
        "telly": "passthrough",  # accept fenced JSON if agent emits valid payload
    },
}

# ---------------------------------------------------------------------------
# BLOCK ELEMENTS — interactive components inside blocks
# ---------------------------------------------------------------------------

ELEMENTS: dict[str, dict] = {
    # Buttons
    "button": {"description": "Clickable button; styles primary|danger.", "telly": "used"},
    "link_button": {"description": "Button that opens a URL.", "telly": "integrated"},
    "workflow_button": {"description": "Triggers a Slack workflow.", "telly": "n/a"},
    "icon_button": {"description": "Icon-only button (e.g. trash) in context_actions.", "telly": "integrated"},
    "feedback_buttons": {"description": "👍/👎 style AI feedback pair.", "telly": "used"},
    # Selects
    "static_select": {"description": "Dropdown of static options.", "telly": "used"},
    "external_select": {"description": "Dropdown loaded from app options API.", "telly": "available"},
    "users_select": {"description": "User picker.", "telly": "integrated"},
    "conversations_select": {"description": "Conversation picker.", "telly": "available"},
    "channels_select": {"description": "Public channel picker.", "telly": "available"},
    "multi_static_select": {"description": "Multi static select.", "telly": "available"},
    "multi_external_select": {"description": "Multi external select.", "telly": "available"},
    "multi_users_select": {"description": "Multi user select.", "telly": "available"},
    "multi_conversations_select": {"description": "Multi conversation select.", "telly": "available"},
    "multi_channels_select": {"description": "Multi channel select.", "telly": "available"},
    # Other interactive
    "overflow": {"description": "⋯ overflow menu of options.", "telly": "integrated"},
    "datepicker": {"description": "Date picker.", "telly": "available"},
    "timepicker": {"description": "Time picker.", "telly": "available"},
    "datetimepicker": {"description": "Combined date+time picker.", "telly": "available"},
    "checkboxes": {"description": "Checkbox group.", "telly": "integrated"},
    "radio_buttons": {"description": "Radio button group.", "telly": "integrated"},
    # Inputs
    "plain_text_input": {"description": "Single/multi-line text input.", "telly": "used"},
    "rich_text_input": {"description": "Formatted rich text input.", "telly": "available"},
    "url_text_input": {"description": "URL text input.", "telly": "available"},
    "email_text_input": {"description": "Email text input.", "telly": "available"},
    "number_input": {"description": "Numeric input.", "telly": "available"},
    "file_input": {"description": "File upload input (modals; files:read).", "telly": "available"},
    # Display
    "image": {"description": "Inline image element (accessory).", "telly": "integrated"},
    "url": {"description": "URL source element on task cards.", "telly": "used"},  # url source
}

# Rich-text sub-elements (inside rich_text block)
RICH_TEXT_PARTS = [
    "rich_text_section",
    "rich_text_list",
    "rich_text_preformatted",
    "rich_text_quote",
    # inline: text, link, emoji, user, usergroup, channel, team, broadcast, color, date
]

# ---------------------------------------------------------------------------
# COMPOSITION OBJECTS
# ---------------------------------------------------------------------------

COMPOSITION_OBJECTS: dict[str, str] = {
    "text": "plain_text or mrkdwn text object",
    "confirm": "confirmation dialog for dangerous actions",
    "option": "select/radio/checkbox/overflow option",
    "option_group": "grouped options in a select",
    "dispatch_action_config": "when input elements dispatch block_actions",
    "filter": "conversation filter for conversation selects",
    "slack_file": "Slack-hosted file reference for images",
    "workflow": "workflow trigger payload for workflow_button",
    "feedback_button": "one side of feedback_buttons",
    "url_source": "citation/source URL on task cards",
}

# Surfaces
SURFACES = ["messages", "modals", "app_home"]


def inventory_summary() -> str:
    lines = ["# Slack Block Kit inventory (Telly)", ""]
    lines.append("## Blocks")
    for name, meta in sorted(BLOCKS.items()):
        lines.append(f"- `{name}` — {meta['description']} [{meta['telly']}]")
    lines.append("")
    lines.append("## Elements")
    for name, meta in sorted(ELEMENTS.items()):
        lines.append(f"- `{name}` — {meta['description']} [{meta['telly']}]")
    lines.append("")
    lines.append("## Composition objects")
    for name, desc in sorted(COMPOSITION_OBJECTS.items()):
        lines.append(f"- `{name}` — {desc}")
    return "\n".join(lines)


def all_block_types() -> list[str]:
    return sorted(BLOCKS)


def all_element_types() -> list[str]:
    return sorted(ELEMENTS)
