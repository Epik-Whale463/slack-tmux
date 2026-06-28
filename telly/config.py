import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str
    main_agent_cmd: str
    tmux_session_prefix: str
    telly_home: Path
    allowed_slack_user_ids: frozenset[str]
    log_level: str = "INFO"

    @property
    def turn_log_path(self) -> Path:
        return self.telly_home / "turns.jsonl"

    @classmethod
    def from_env(cls) -> "Config":
        missing = [
            name
            for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise RuntimeError(
                f"Missing required env vars: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill them in."
            )

        return cls(
            slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
            slack_app_token=os.environ["SLACK_APP_TOKEN"],
            slack_signing_secret=os.environ["SLACK_SIGNING_SECRET"],
            main_agent_cmd=os.environ.get("MAIN_AGENT_CMD", "opencode"),
            tmux_session_prefix=os.environ.get("TMUX_SESSION_PREFIX", "slacktask"),
            telly_home=Path(os.environ.get("TELLY_HOME", "~/.telly")).expanduser(),
            allowed_slack_user_ids=frozenset(
                _split_csv(os.environ.get("ALLOWED_SLACK_USER_IDS", ""))
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
