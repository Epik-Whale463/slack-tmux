import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ThreadState:
    thread_ts: str
    channel: str = ""
    user: str = ""
    session_id: str | None = None
    last_message: str | None = None
    agent_cmd: str | None = None
    cwd: str | None = None
    status: str = "idle"
    uploaded_files: list[str] = field(default_factory=list)
    triggers: dict[str, dict[str, Any]] = field(default_factory=dict)
    video_stream_id: str | None = None
    updated_at: float = field(default_factory=time.time)


def _normalize_triggers(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name, payload in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip()
        if isinstance(payload, str):
            normalized[key] = {
                "instruction": payload,
                "interval_seconds": None,
                "enabled": False,
                "next_run_at": None,
            }
            continue
        if not isinstance(payload, dict):
            continue
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            continue
        normalized[key] = {
            "instruction": instruction.strip(),
            "interval_seconds": payload.get("interval_seconds"),
            "enabled": bool(payload.get("enabled", False)),
            "next_run_at": payload.get("next_run_at"),
        }
    return normalized


class ThreadStore:
    def __init__(self, telly_home: Path):
        self.root = telly_home
        self.path = telly_home / "threads.json"
        self.feedback_path = telly_home / "feedback.jsonl"
        self._threads: dict[str, ThreadState] = {}
        self.load()

    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text())
            threads = raw.get("threads", raw)
            for thread_ts, payload in threads.items():
                if isinstance(payload, dict):
                    state_payload = {k: v for k, v in payload.items() if k != "thread_ts"}
                    state_payload["triggers"] = _normalize_triggers(state_payload.get("triggers"))
                    self._threads[thread_ts] = ThreadState(thread_ts=thread_ts, **state_payload)
        except Exception:
            log.exception("thread state load failed")

    def save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"threads": {k: asdict(v) for k, v in self._threads.items()}}
            self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except Exception:
            log.exception("thread state save failed")

    def get(self, thread_ts: str) -> ThreadState | None:
        return self._threads.get(thread_ts)

    def ensure(self, thread_ts: str, **updates: Any) -> ThreadState:
        state = self._threads.get(thread_ts)
        if state is None:
            state = ThreadState(thread_ts=thread_ts)
            self._threads[thread_ts] = state
        self.update(thread_ts, save=False, **updates)
        return state

    def update(self, thread_ts: str, save: bool = True, **updates: Any) -> ThreadState:
        state = self._threads.setdefault(thread_ts, ThreadState(thread_ts=thread_ts))
        for key, value in updates.items():
            if hasattr(state, key) and value is not None:
                setattr(state, key, value)
        state.updated_at = time.time()
        if save:
            self.save()
        return state

    def delete(self, thread_ts: str) -> None:
        self._threads.pop(thread_ts, None)
        self.save()

    def list_recent(self, limit: int = 20) -> list[ThreadState]:
        return sorted(self._threads.values(), key=lambda s: s.updated_at, reverse=True)[:limit]

    def session_map(self) -> dict[str, str]:
        return {k: v.session_id for k, v in self._threads.items() if v.session_id}

    def log_feedback(self, record: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.feedback_path.open("a") as f:
                f.write(json.dumps({**record, "created_at": time.time()}) + "\n")
        except Exception:
            log.exception("feedback log failed")
