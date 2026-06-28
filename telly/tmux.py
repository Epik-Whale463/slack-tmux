import logging
import subprocess

log = logging.getLogger(__name__)


class TmuxError(RuntimeError):
    pass


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("tmux %s", " ".join(args[1:]))
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise TmuxError(
            f"tmux {' '.join(args[1:])} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def has_session(name: str) -> bool:
    return _run(["tmux", "has-session", "-t", name], check=False).returncode == 0


def new_detached_session(name: str, command: str) -> None:
    if has_session(name):
        raise TmuxError(f"session {name!r} already exists")
    _run(["tmux", "new-session", "-d", "-s", name, command])


def send_text(name: str, text: str, submit: bool = True) -> None:
    # `-l` sends the literal string without interpreting key names.
    _run(["tmux", "send-keys", "-t", name, "-l", text])
    if submit:
        _run(["tmux", "send-keys", "-t", name, "Enter"])


def capture_pane(name: str, lines: int = 2000) -> str:
    # -p prints to stdout; -S -N gives the last N lines of scrollback.
    result = _run(["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"])
    return result.stdout


def kill_session(name: str) -> None:
    if has_session(name):
        _run(["tmux", "kill-session", "-t", name])
