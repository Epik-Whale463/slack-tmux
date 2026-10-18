import logging
import os
from dataclasses import dataclass, field
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
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_telegram_chat_ids: frozenset[str] = field(default_factory=frozenset)
    log_level: str = "INFO"

    @property
    def run_slack(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token and self.slack_signing_secret)

    @property
    def run_telegram(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def turn_log_path(self) -> Path:
        return self.telly_home / "turns.jsonl"

    @classmethod
    def from_env(cls) -> "Config":
        slack_bot = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        slack_app = os.environ.get("SLACK_APP_TOKEN", "").strip()
        slack_secret = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
        telegram = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

        slack_values = {
            "SLACK_BOT_TOKEN": slack_bot,
            "SLACK_APP_TOKEN": slack_app,
            "SLACK_SIGNING_SECRET": slack_secret,
        }
        present = [name for name, value in slack_values.items() if value]
        if present and not all(slack_values.values()):
            missing = ", ".join(name for name, value in slack_values.items() if not value)
            raise RuntimeError(
                f"Slack is partially configured — missing {missing}. "
                f"Set all three SLACK_* vars, or none to run Telegram-only."
            )
        if not slack_bot and not telegram:
            raise RuntimeError(
                "No backend configured. Set SLACK_BOT_TOKEN/ SLACK_APP_TOKEN/ "
                "SLACK_SIGNING_SECRET (Socket Mode) and/or TELEGRAM_BOT_TOKEN."
            )

        return cls(
            slack_bot_token=slack_bot,
            slack_app_token=slack_app,
            slack_signing_secret=slack_secret,
            main_agent_cmd=os.environ.get("MAIN_AGENT_CMD", "opencode"),
            tmux_session_prefix=os.environ.get("TMUX_SESSION_PREFIX", "slacktask"),
            telly_home=Path(os.environ.get("TELLY_HOME", "~/.telly")).expanduser(),
            allowed_slack_user_ids=frozenset(_split_csv(os.environ.get("ALLOWED_SLACK_USER_IDS", ""))),
            telegram_bot_token=telegram,
            allowed_telegram_user_ids=frozenset(_split_csv(os.environ.get("ALLOWED_TELEGRAM_USER_IDS", ""))),
            allowed_telegram_chat_ids=frozenset(_split_csv(os.environ.get("ALLOWED_TELEGRAM_CHAT_IDS", ""))),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
