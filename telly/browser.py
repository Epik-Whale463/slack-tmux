import argparse
from contextlib import contextmanager
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_TIMEOUT_S = 45
DEFAULT_VIEWPORT = "1440,1000"
DEFAULT_TEXT_LIMIT = 20000
DEFAULT_PROFILE_DIR = "~/.telly/chrome-profile"
DEFAULT_CHROME_PROFILE_NAME = "Default"


class BrowserError(RuntimeError):
    pass


def chrome_path() -> str | None:
    env_path = os.environ.get("TELLY_CHROME_PATH")
    if env_path:
        return env_path
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def capability_text() -> str:
    path = chrome_path()
    if not path:
        return "Headless browser: unavailable; no Chrome/Chromium binary found."
    py = sys.executable
    profile = _default_profile_dir()
    automation = "Playwright automation is installed" if _has_playwright() else "Chrome capture fallback is available"
    return (
        f"Browser automation available via local Chrome ({automation}). It reuses telly's dedicated profile `{profile}` by default, so the user can keep normal Chrome open. Use these commands when a Slack task needs rendered web pages:\n"
        f"- `{py} -m telly.browser text URL` for readable rendered page text\n"
        f"- `{py} -m telly.browser dom URL` for rendered HTML/DOM\n"
        f"- `{py} -m telly.browser metadata URL` for title, final URL, and text summary\n"
        f"- `{py} -m telly.browser screenshot URL --output PATH.png` for a screenshot\n"
        f"- `{py} -m telly.browser pdf URL --output PATH.pdf` for a PDF capture\n"
        f"- `{py} -m telly.browser script URL --steps '[{{\"action\":\"fill\",\"selector\":\"input[name=q]\",\"text\":\"query\"}},{{\"action\":\"press\",\"selector\":\"input[name=q]\",\"key\":\"Enter\"}},{{\"action\":\"text\"}}]'` for clicks, forms, waits, screenshots, PDFs, and JS evaluation\n"
        f"For login/CAPTCHA, run with `--headed --profile {DEFAULT_PROFILE_DIR}`, ask the user to complete the browser challenge, then retry normally; cookies/session remain saved in that profile. "
        "Useful flags: `--headed` to show Chrome, `--profile PATH` to choose a Chrome user-data directory, `--profile-name NAME` to choose Default/Profile 1/etc, `--ephemeral` for a fresh throwaway browser, `--wait-ms 3000`, `--viewport 390,844` for mobile, `--user-agent VALUE`, `--timeout 60`. "
        "Use the default telly profile first; use `--storage-state PATH.json` with `script --ephemeral` only for explicit Playwright state files. "
        "To show screenshots/PDFs in Slack, include `SLACK_UPLOAD: /absolute/path` in the final reply."
    )


def _has_playwright() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "file", "data"):
        raise BrowserError("URL must start with http://, https://, file://, or data:")
    return url


def _default_profile_dir() -> Path:
    env_profile = os.environ.get("TELLY_BROWSER_PROFILE")
    if env_profile:
        return Path(env_profile).expanduser().resolve()
    return Path(DEFAULT_PROFILE_DIR).expanduser().resolve()


def _profile_dir(args: argparse.Namespace) -> Path | None:
    if getattr(args, "ephemeral", False):
        return None
    profile = getattr(args, "profile", "")
    return Path(profile).expanduser().resolve() if profile else _default_profile_dir()


@contextmanager
def _chrome_user_data_dir(args: argparse.Namespace):
    profile = _profile_dir(args)
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        yield str(profile)
        return
    with tempfile.TemporaryDirectory(prefix="telly-chrome-") as tmp:
        yield tmp


def _chrome_base_args(args: argparse.Namespace, user_data_dir: str) -> list[str]:
    path = chrome_path()
    if not path:
        raise BrowserError("Chrome/Chromium is not installed or TELLY_CHROME_PATH is invalid")
    cmd = [
        path,
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={user_data_dir}",
        f"--window-size={args.viewport}",
    ]
    if not getattr(args, "headed", False):
        cmd.insert(1, "--headless=new")
    if os.geteuid() == 0:
        cmd.append("--no-sandbox")
    if args.user_agent:
        cmd.append(f"--user-agent={args.user_agent}")
    if getattr(args, "profile_name", ""):
        cmd.append(f"--profile-directory={args.profile_name}")
    if args.wait_ms:
        cmd.append(f"--virtual-time-budget={args.wait_ms}")
    return cmd


def _run_chrome(chrome_args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(chrome_args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BrowserError(f"Chrome timed out after {timeout}s") from e
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise BrowserError(f"Chrome exited {result.returncode}: {err[-1000:]}")
    return result


def _dump_dom(url: str, args: argparse.Namespace) -> str:
    _validate_url(url)
    with _chrome_user_data_dir(args) as tmp:
        cmd = _chrome_base_args(args, tmp) + ["--dump-dom", url]
        return _run_chrome(cmd, args.timeout).stdout


def _text_from_dom(dom: str) -> str:
    dom = re.sub(r"(?is)<(script|style|noscript|svg|canvas).*?</\1>", " ", dom)
    dom = re.sub(r"(?is)<br\s*/?>", "\n", dom)
    dom = re.sub(r"(?is)</(p|div|section|article|main|header|footer|li|h[1-6]|tr|blockquote)>", "\n", dom)
    text = re.sub(r"(?s)<[^>]+>", " ", dom)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _write_capture(url: str, args: argparse.Namespace, flag: str) -> Path:
    _validate_url(url)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with _chrome_user_data_dir(args) as tmp:
        cmd = _chrome_base_args(args, tmp) + [f"{flag}={output}", url]
        _run_chrome(cmd, args.timeout)
    if not output.exists() or output.stat().st_size == 0:
        raise BrowserError(f"Chrome did not create {output}")
    return output


def _page_title(dom: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", dom)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()


def _viewport(value: str) -> dict[str, int]:
    try:
        width, height = value.split(",", 1)
        return {"width": int(width), "height": int(height)}
    except ValueError as e:
        raise BrowserError("viewport must be WIDTH,HEIGHT") from e


def _playwright_context(args: argparse.Namespace):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserError("Playwright is not installed") from e

    path = chrome_path()
    if not path:
        raise BrowserError("Chrome/Chromium is not installed or TELLY_CHROME_PATH is invalid")
    pw = sync_playwright().start()
    context_kwargs: dict = {
        "viewport": _viewport(args.viewport),
        "ignore_https_errors": True,
    }
    if args.user_agent:
        context_kwargs["user_agent"] = args.user_agent
    storage_state = getattr(args, "storage_state", None)
    profile = _profile_dir(args)
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        launch_args = []
        if getattr(args, "profile_name", ""):
            launch_args.append(f"--profile-directory={args.profile_name}")
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=path,
            headless=not getattr(args, "headed", False),
            args=launch_args,
            **context_kwargs,
        )
        context.set_default_timeout(args.timeout * 1000)
        page = context.pages[0] if context.pages else context.new_page()
        return pw, None, context, page

    browser = pw.chromium.launch(executable_path=path, headless=not getattr(args, "headed", False))
    if storage_state and Path(storage_state).expanduser().exists():
        context_kwargs["storage_state"] = str(Path(storage_state).expanduser())
    context = browser.new_context(**context_kwargs)
    context.set_default_timeout(args.timeout * 1000)
    page = context.new_page()
    return pw, browser, context, page


def _with_page(url: str, args: argparse.Namespace, fn):
    _validate_url(url)
    pw, browser, context, page = _playwright_context(args)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        if args.wait_ms:
            page.wait_for_timeout(args.wait_ms)
        return fn(page, context)
    finally:
        storage_state = getattr(args, "storage_state", None)
        if storage_state:
            Path(storage_state).expanduser().parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(Path(storage_state).expanduser()))
        context.close()
        if browser is not None:
            browser.close()
        pw.stop()


def _dump_dom_best(url: str, args: argparse.Namespace) -> str:
    if _has_playwright():
        return _with_page(url, args, lambda page, _context: page.content())
    return _dump_dom(url, args)


def _write_capture_best(url: str, args: argparse.Namespace, kind: str) -> Path:
    if not _has_playwright():
        return _write_capture(url, args, "--screenshot" if kind == "screenshot" else "--print-to-pdf")
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    def capture(page, _context):
        if kind == "screenshot":
            page.screenshot(path=str(output), full_page=getattr(args, "full_page", True))
        else:
            page.pdf(path=str(output), print_background=True)
        return output

    return _with_page(url, args, capture)


def _load_steps(args: argparse.Namespace) -> list[dict]:
    if args.steps_file:
        raw = Path(args.steps_file).expanduser().read_text()
    elif args.steps:
        raw = args.steps
    else:
        raw = "[]"
    steps = json.loads(raw)
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise BrowserError("steps must be a JSON array of objects")
    return steps


def _run_script(url: str, args: argparse.Namespace) -> dict:
    _validate_url(url)
    steps = _load_steps(args)
    pw, browser, context, page = _playwright_context(args)
    results: list[dict] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        if args.wait_ms:
            page.wait_for_timeout(args.wait_ms)
        for step in steps:
            action = step.get("action")
            selector = step.get("selector")
            if action == "goto":
                page.goto(_validate_url(step["url"]), wait_until=step.get("wait_until", "domcontentloaded"))
            elif action == "click":
                page.click(selector)
            elif action == "fill":
                page.fill(selector, str(step.get("text", step.get("value", ""))))
            elif action == "type":
                page.type(selector, str(step.get("text", "")))
            elif action == "press":
                page.press(selector, str(step["key"]))
            elif action == "wait":
                page.wait_for_timeout(int(step.get("ms", args.wait_ms or 1000)))
            elif action == "wait_for_selector":
                page.wait_for_selector(selector)
            elif action == "evaluate":
                results.append({"action": action, "result": page.evaluate(str(step["expression"]))})
            elif action == "screenshot":
                output = Path(step["output"]).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output), full_page=bool(step.get("full_page", True)))
                results.append({"action": action, "path": str(output)})
            elif action == "pdf":
                output = Path(step["output"]).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                page.pdf(path=str(output), print_background=True)
                results.append({"action": action, "path": str(output)})
            elif action == "text":
                text = page.locator("body").inner_text(timeout=args.timeout * 1000)
                results.append({"action": action, "text": text[: int(step.get("limit", DEFAULT_TEXT_LIMIT))]})
            elif action == "dom":
                results.append({"action": action, "html": page.content()[: int(step.get("limit", DEFAULT_TEXT_LIMIT))]})
            elif action == "metadata":
                results.append({"action": action, "url": page.url, "title": page.title()})
            else:
                raise BrowserError(f"unknown script action: {action}")
        return {"url": page.url, "title": page.title(), "results": results}
    finally:
        if args.storage_state:
            Path(args.storage_state).expanduser().parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(Path(args.storage_state).expanduser()))
        context.close()
        if browser is not None:
            browser.close()
        pw.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Use local headless Chrome for browser tasks.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Chrome timeout in seconds")
    parser.add_argument("--wait-ms", type=int, default=3000, help="virtual time budget for JS/network settling")
    parser.add_argument("--viewport", default=DEFAULT_VIEWPORT, help="viewport/window size, e.g. 1440,1000")
    parser.add_argument("--user-agent", default="", help="override browser user agent")
    parser.add_argument("--profile", default="", help="persistent Chrome user-data directory; defaults to your real Chrome profile if found")
    parser.add_argument("--profile-name", default=DEFAULT_CHROME_PROFILE_NAME, help="Chrome profile directory name inside user-data dir, e.g. Default or 'Profile 1'")
    parser.add_argument("--ephemeral", action="store_true", help="use a fresh temporary browser profile")
    parser.add_argument("--headed", action="store_true", help="show Chrome so the user can solve CAPTCHA/login manually")
    parser.add_argument("--storage-state", default="", help="Playwright cookies/local storage JSON path")

    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dom", "text", "metadata"):
        p = sub.add_parser(name)
        p.add_argument("url")
        if name == "text":
            p.add_argument("--limit", type=int, default=DEFAULT_TEXT_LIMIT)
    shot = sub.add_parser("screenshot")
    shot.add_argument("url")
    shot.add_argument("--output", required=True)
    shot.add_argument("--full-page", action=argparse.BooleanOptionalAction, default=True)
    pdf = sub.add_parser("pdf")
    pdf.add_argument("url")
    pdf.add_argument("--output", required=True)
    script = sub.add_parser("script")
    script.add_argument("url")
    script.add_argument("--steps", help="JSON array of automation steps")
    script.add_argument("--steps-file", help="path to JSON array of automation steps")
    script.add_argument("--storage-state", default="", help="cookies/local storage JSON path for this script")
    sub.add_parser("capabilities")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            print(capability_text())
            return 0
        if args.command == "dom":
            print(_dump_dom_best(args.url, args), end="")
            return 0
        if args.command == "text":
            text = _text_from_dom(_dump_dom_best(args.url, args))
            print(text[: args.limit], end="")
            if len(text) > args.limit:
                print("\n[truncated]", end="")
            return 0
        if args.command == "metadata":
            dom = _dump_dom_best(args.url, args)
            text = _text_from_dom(dom)
            print(json.dumps({"url": args.url, "title": _page_title(dom), "text": text[:2000]}, indent=2))
            return 0
        if args.command == "screenshot":
            print(_write_capture_best(args.url, args, "screenshot"))
            return 0
        if args.command == "pdf":
            print(_write_capture_best(args.url, args, "pdf"))
            return 0
        if args.command == "script":
            print(json.dumps(_run_script(args.url, args), indent=2))
            return 0
    except BrowserError as e:
        print(f"browser error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
