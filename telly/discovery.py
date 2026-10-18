import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Coding agent CLIs we know how to look for. The brain decides which to use;
# we just report what's installed. "cmd" is the alias Command Code ships next
# to "command-code" and both resolve to the same binary, so they are
# de-duplicated. On Windows "cmd" is the Command Prompt (cmd.exe) and must
# never be mistaken for an agent, so the alias is only probed on POSIX.
KNOWN_AGENTS: tuple[str, ...] = (
    "claude",
    "opencode",
    "command-code",
    *(() if sys.platform == "win32" else ("cmd",)),
    "codex",
    "aider",
    "gemini",
    "cursor-agent",
    "crush",
    "amp",
)


@dataclass(frozen=True)
class DiscoveredAgent:
    name: str
    path: str
    version: str  # may be empty if version probe failed but binary exists


def _probe_version(path: str) -> str:
    for flag in ("--version", "-V", "-v"):
        try:
            result = subprocess.run(
                [path, flag],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return ""


def discover_agents(names: tuple[str, ...] = KNOWN_AGENTS) -> list[DiscoveredAgent]:
    found: list[DiscoveredAgent] = []
    seen_paths: set[str] = set()
    for name in names:
        path = shutil.which(name)
        if not path:
            continue
        resolved = os.path.realpath(path)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        version = _probe_version(path)
        found.append(DiscoveredAgent(name=name, path=path, version=version))
        log.info("discovered agent: %s @ %s (%s)", name, path, version or "no version")
    return found


def format_for_brain(agents: list[DiscoveredAgent]) -> str:
    if not agents:
        return "(none — only shell tools are available)"
    lines = [f"- {a.name} at {a.path}" + (f" — {a.version}" if a.version else "") for a in agents]
    return "\n".join(lines)
