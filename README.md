# slack-tmux

Slack-facing coding agent that runs local coding CLIs in resumable tmux-backed sessions.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in the Slack credentials in `.env` before running.

## Run

```bash
python -m telly
```

## Browser Automation

Telly gives spawned Claude/OpenCode turns a Playwright MCP server when `npx` is available. MCP is the first choice for interactive browser debugging, form flows, console/network inspection, and persistent page state. It uses telly's dedicated Chrome profile at `~/.telly/chrome-profile`, not the user's normal Chrome profile, and writes MCP artifacts under `~/.telly/playwright-mcp`.

Telly also includes a local Chrome/Playwright helper for deterministic rendered page text, DOM, screenshots, PDFs, and scripted interactions:

```bash
python -m telly.browser text https://example.com
python -m telly.browser screenshot https://example.com --output page.png
```

The helper is the fallback when MCP cannot complete a task or when a deterministic capture/action is better. It uses the same dedicated Chrome profile by default so normal Chrome sessions are not disturbed. If that profile is locked by MCP, use a task-specific `--profile PATH` for the login/action flow. Set `TELLY_PLAYWRIGHT_MCP=0` to disable MCP injection and keep only the helper commands.

## Slack Native Visualizations

Telly can render Slack-native chart blocks from assistant replies.

![Slack native visualization preview](slack-native-visualization-preview.png)
