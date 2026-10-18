# Slack Block Kit — full inventory + Telly status

Generated from Slack platform docs + `python-slack-sdk` models (2026).

Legend: **used** already in production paths · **integrated** newly wired · **available** builder/passthrough ready · **n/a** not applicable · **partial** alternate path

## Blocks

| Type | Description | Telly |
|---|---|---|
| `actions` | Interactive row (buttons, selects, overflow, pickers) | used |
| `alert` | Banner: default/info/success/warning/error | integrated |
| `call` | Slack call embed | n/a |
| `card` | Icon/title/subtitle/body + actions | integrated |
| `carousel` | Scrollable cards | integrated |
| `context` | Muted metadata row | used |
| `context_actions` | AI feedback / icon buttons | used |
| `data_visualization` | pie/bar/area/line charts | used |
| `data_table` | Rich interactive table (work objects) | passthrough via `slack-block` |
| `divider` | Horizontal rule | used |
| `file` | Remote file object | partial (files_upload_v2) |
| `header` | Large plain_text title | used |
| `image` | Public URL or slack_file image | integrated |
| `input` | Labeled form field | used (modals) |
| `markdown` | Standard Markdown (AI) | used |
| `plan` | Multi-task plan | used |
| `rich_text` | Lists/quotes/preformatted | used |
| `section` | Text + fields + accessory | used |
| `table` | Structured table | used |
| `task_card` | Agent task step | used |
| `video` | Embedded video player | integrated |

## Block elements

| Type | Description | Telly |
|---|---|---|
| `button` | Click action / link via `url` | used |
| `link_button` | Explicit link button | integrated (helper) |
| `workflow_button` | Trigger workflow | n/a |
| `icon_button` | Icon action (trash, etc.) | integrated |
| `feedback_buttons` | 👍/👎 | used |
| `static_select` | Static dropdown | used |
| `external_select` | App-loaded options | available |
| `users_select` | User picker | available |
| `conversations_select` | Conversation picker | available |
| `channels_select` | Channel picker | available |
| `multi_*_select` | Multi variants of above | available |
| `overflow` | ⋯ menu | integrated |
| `datepicker` | Date | available |
| `timepicker` | Time | available |
| `datetimepicker` | Date+time | available |
| `checkboxes` | Checkbox group | available |
| `radio_buttons` | Radio group | available |
| `plain_text_input` | Text field | used |
| `rich_text_input` | Rich text field | available |
| `url_text_input` | URL field | available |
| `email_text_input` | Email field | available |
| `number_input` | Number field | available |
| `file_input` | File upload (modal) | available |
| `image` | Accessory image | integrated |
| `url` (source) | Task citation | used |

## Composition objects

`text` (plain_text / mrkdwn) · `confirm` · `option` · `option_group` · `dispatch_action_config` · `filter` · `slack_file` · `workflow` · `feedback_button` · `url_source`

## Agent fences Telly understands

```
```slack-visualization | slack-table | slack-plan
```slack-alert | slack-card | slack-carousel
```slack-image | slack-video | slack-block
```

Plus auto: GFM pipe tables → `table`, `![alt](url)` → `image`.

## Surfaces

- **Messages** — streaming answers, alerts, cards, charts, tables, feedback
- **Modals** — feedback form (expandable to confirm/destructive flows)
- **App Home** — header, alerts, session cards/carousel, overflow quick actions
