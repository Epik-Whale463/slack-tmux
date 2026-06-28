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

Telly includes a local Chrome/Playwright helper for rendered page text, DOM, screenshots, PDFs, and scripted interactions:

```bash
python -m telly.browser text https://example.com
python -m telly.browser screenshot https://example.com --output page.png
```

The browser helper uses a dedicated Chrome profile by default so normal Chrome sessions are not disturbed.

## Slack Native Visualizations

Telly can render Slack-native chart blocks from assistant replies.

![Slack native visualization preview](slack-native-visualization-preview.png)
