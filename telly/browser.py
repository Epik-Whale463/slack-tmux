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
import time
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_TIMEOUT_S = 45
DEFAULT_VIEWPORT = "1440,1000"
DEFAULT_TEXT_LIMIT = 20000
DEFAULT_REMOTE_DEBUGGING_PORT = 9222
SESSION_ROOT_DIR = "~/.telly/browser-sessions"
DEFAULT_SESSION_NAME = "default"
DEFAULT_PROFILE_DIR = "~/.telly/chrome-profile"
DEFAULT_CHROME_PROFILE_NAME = "Default"
MCP_OUTPUT_DIR = "~/.telly/playwright-mcp"
MCP_CONFIG_FILENAME = "config.json"
PROFILE_LOCK_FILES = ("SingletonLock", "SingletonCookie")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def playwright_mcp_config(client: str) -> dict:
    """Return a Playwright MCP config block for the target agent client."""
    config_path = str(_write_playwright_mcp_config())
    args = [
        "-y",
        "@playwright/mcp@latest",
        "--config",
        config_path,
    ]

    if client == "opencode":
        return {
            "type": "local",
            "command": ["npx", *args],
            "enabled": True,
            "timeout": 10000,
        }

    return {
        "command": "npx",
        "args": args,
    }


def _write_playwright_mcp_config() -> Path:
    """Write the MCP config required to expose Chrome over CDP for live streaming."""
    output_dir = Path(MCP_OUTPUT_DIR).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    launch_args = []
    remote_debugging_arg = _remote_debugging_arg()
    if remote_debugging_arg:
        launch_args.append(remote_debugging_arg)
    if os.geteuid() == 0:
        launch_args.append("--no-sandbox")
    launch_options: dict = {
        "headless": True,
        "args": launch_args,
    }
    path = chrome_path()
    if path:
        launch_options["executablePath"] = path
    config = {
        "browser": {
            "browserName": "chromium",
            "userDataDir": str(_default_profile_dir()),
            "launchOptions": launch_options,
        },
        "capabilities": ["core", "pdf", "devtools"],
        "outputDir": str(output_dir),
        "imageResponses": "allow",
    }
    config_path = output_dir / MCP_CONFIG_FILENAME
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


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


def remote_debugging_port() -> int | None:
    value = os.environ.get("TELLY_BROWSER_REMOTE_DEBUGGING_PORT", str(DEFAULT_REMOTE_DEBUGGING_PORT)).strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return None
    try:
        port = int(value)
    except ValueError as e:
        raise BrowserError("TELLY_BROWSER_REMOTE_DEBUGGING_PORT must be an integer port or 0/off") from e
    if port < 1 or port > 65535:
        raise BrowserError("TELLY_BROWSER_REMOTE_DEBUGGING_PORT must be between 1 and 65535")
    return port


def _remote_debugging_arg() -> str | None:
    port = remote_debugging_port()
    return f"--remote-debugging-port={port}" if port else None


def capability_text() -> str:
    path = chrome_path()
    if not path:
        return "Headless browser: unavailable; no Chrome/Chromium binary found."
    py = sys.executable
    profile = _default_profile_dir()
    automation = "Playwright automation is installed" if _has_playwright() else "Chrome capture fallback is available"
    mcp = "Playwright MCP is injected into spawned Claude/OpenCode turns" if shutil.which("npx") else "Playwright MCP unavailable; `npx` not found"
    return (
        f"Browser automation available via local Chrome ({automation}; {mcp}). Never use the user's normal Chrome profile unless explicitly asked. For each browser job, choose a descriptive persistent session name such as `linkedin`, `whatsapp`, or `gmail`, and keep all work for that site in that named telly session under `{_session_root_dir()}`. Use Playwright MCP first only for interactive debugging in the default telly session `{profile}`; for reusable authenticated site work, prefer the `telly.browser` CLI with `--session NAME`. Live browser sharing is handled by telly through Chrome remote debugging; do not look for `browser_start_video`/`browser_stop_video` tools.\n"
        f"- `{py} -m telly.browser --session linkedin text URL` for readable rendered page text in the `linkedin` session\n"
        f"- `{py} -m telly.browser --session linkedin dom URL` for rendered HTML/DOM\n"
        f"- `{py} -m telly.browser --session linkedin metadata URL` for title, final URL, and text summary\n"
        f"- `{py} -m telly.browser --session linkedin screenshot URL --output PATH.png` for a screenshot\n"
        f"- `{py} -m telly.browser --session linkedin pdf URL --output PATH.pdf` for a PDF capture\n"
        f"- `{py} -m telly.browser --session linkedin script URL --steps '[{{\"action\":\"fill\",\"selector\":\"input[name=q]\",\"text\":\"query\"}},{{\"action\":\"press\",\"selector\":\"input[name=q]\",\"key\":\"Enter\"}},{{\"action\":\"text\"}}]'` for clicks, forms, waits, screenshots, PDFs, and JS evaluation\n"
        f"- `{py} -m telly.browser sessions` to list saved named sessions\n"
        f"For login/CAPTCHA, run `--headed --session SESSION login URL --success-url REGEX`; the login helper closes after success and saves the browser state in that named session for future Slack chats. "
        "Useful flags: `--session NAME` for a named telly browser session, `--headed` to show Chrome, `--profile PATH` only for an explicit Chrome user-data directory escape hatch, `--profile-name NAME` to choose Default/Profile 1/etc, `--ephemeral` for a fresh throwaway browser, `--wait-ms 3000`, `--viewport 390,844` for mobile, `--user-agent VALUE`, `--timeout 60`. "
        "Use `--storage-state PATH.json` with `script --ephemeral` only for explicit Playwright state files. "
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
    return _session_profile_dir(os.environ.get("TELLY_BROWSER_SESSION", DEFAULT_SESSION_NAME))


def _session_root_dir() -> Path:
    return Path(os.environ.get("TELLY_BROWSER_SESSION_ROOT", SESSION_ROOT_DIR)).expanduser().resolve()


def _validate_session_name(name: str) -> str:
    name = name.strip()
    if not SESSION_NAME_RE.fullmatch(name):
        raise BrowserError("session name must be 1-64 chars: letters, numbers, dot, underscore, or hyphen")
    if name in {".", ".."}:
        raise BrowserError("session name cannot be . or ..")
    return name


def _session_profile_dir(name: str) -> Path:
    return (_session_root_dir() / _validate_session_name(name)).resolve()


def _profile_dir(args: argparse.Namespace) -> Path | None:
    if getattr(args, "ephemeral", False):
        return None
    profile = getattr(args, "profile", "")
    session = getattr(args, "session", "")
    if profile and session:
        raise BrowserError("use either --profile PATH or --session NAME, not both")
    if session:
        return _session_profile_dir(session)
    profile = getattr(args, "profile", "")
    return Path(profile).expanduser().resolve() if profile else _default_profile_dir()


def _list_sessions() -> list[dict[str, str | bool]]:
    root = _session_root_dir()
    if not root.exists():
        return []
    sessions = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir() or not SESSION_NAME_RE.fullmatch(path.name):
            continue
        sessions.append({
            "name": path.name,
            "path": str(path),
            "active": _profile_lock_path(path) is not None,
        })
    return sessions


def _profile_lock_path(profile: Path) -> Path | None:
    for name in PROFILE_LOCK_FILES:
        lock = profile / name
        if lock.exists() or lock.is_symlink():
            return lock
    return None


def _ensure_profile_available(profile: Path) -> None:
    lock = _profile_lock_path(profile)
    if lock is None:
        return
    raise BrowserError(
        f"Chrome profile is already in use: {profile}. "
        "Reuse the active browser session and open a new tab, or pass --ephemeral/--profile for a separate session. "
        f"Lock file: {lock}"
    )


@contextmanager
def _chrome_user_data_dir(args: argparse.Namespace):
    profile = _profile_dir(args)
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        _ensure_profile_available(profile)
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
        _ensure_profile_available(profile)
        launch_args = []
        remote_debugging_arg = _remote_debugging_arg()
        if remote_debugging_arg:
            launch_args.append(remote_debugging_arg)
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

    launch_args = []
    remote_debugging_arg = _remote_debugging_arg()
    if remote_debugging_arg:
        launch_args.append(remote_debugging_arg)
    browser = pw.chromium.launch(executable_path=path, headless=not getattr(args, "headed", False), args=launch_args)
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


def _login_success(page, args: argparse.Namespace, start_url: str) -> tuple[bool, str]:
    if args.success_url and re.search(args.success_url, page.url):
        return True, "success_url"
    if args.success_text:
        text = page.locator("body").inner_text(timeout=1000)
        if args.success_text.lower() in text.lower():
            return True, "success_text"
    if not args.success_url and not args.success_text and page.url != start_url:
        parsed = urlparse(page.url)
        login_markers = ("login", "signin", "sign-in", "checkpoint", "challenge", "uas")
        haystack = f"{parsed.path}?{parsed.query}".lower()
        if not any(marker in haystack for marker in login_markers):
            return True, "url_changed_off_login"
    return False, ""


def _wait_for_login(url: str, args: argparse.Namespace) -> dict:
    _validate_url(url)
    pw, browser, context, page = _playwright_context(args)
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            start_url = page.url
            deadline = time.monotonic() + args.timeout
            last_url = page.url
            while time.monotonic() < deadline:
                ok, reason = _login_success(page, args, start_url)
                if ok:
                    return {"authenticated": True, "reason": reason, "url": page.url, "title": page.title()}
                last_url = page.url
                page.wait_for_timeout(args.poll_ms)
            return {"authenticated": False, "reason": "timeout", "url": last_url, "title": page.title()}
        except Exception as e:
            if "closed" in str(e).lower():
                return {"authenticated": False, "reason": "browser_closed", "url": "", "title": ""}
            raise
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
    parser.add_argument("--session", default="", help="named persistent telly browser session, e.g. linkedin or whatsapp")
    parser.add_argument("--profile", default="", help="persistent Chrome user-data directory; defaults to telly's dedicated profile")
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
    login = sub.add_parser("login")
    login.add_argument("url")
    login.add_argument("--success-url", default="", help="regex that must match the authenticated page URL")
    login.add_argument("--success-text", default="", help="text that must appear on the authenticated page")
    login.add_argument("--poll-ms", type=int, default=1000, help="how often to check authentication state")
    sub.add_parser("sessions")
    sub.add_parser("capabilities")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            print(capability_text())
            return 0
        if args.command == "sessions":
            print(json.dumps({"root": str(_session_root_dir()), "sessions": _list_sessions()}, indent=2))
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
        if args.command == "login":
            result = _wait_for_login(args.url, args)
            print(json.dumps(result, indent=2))
            return 0 if result["authenticated"] else 3
    except BrowserError as e:
        print(f"browser error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
