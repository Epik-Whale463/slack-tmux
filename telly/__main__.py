import logging
import threading
from typing import Callable

from .config import Config, setup_logging

log = logging.getLogger(__name__)


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)

    runners: list[tuple[str, Callable[[Config], None]]] = []
    if cfg.run_slack:
        from .app import run as run_slack

        runners.append(("slack", run_slack))
    if cfg.run_telegram:
        from .telegram import run as run_telegram

        runners.append(("telegram", run_telegram))
    if not runners:
        raise RuntimeError("Nothing to run: configure Slack and/or Telegram.")

    if len(runners) == 1:
        name, run = runners[0]
        log.info("starting %s backend", name)
        run(cfg)
        return

    threads = [
        threading.Thread(target=run, args=(cfg,), name=f"telly-{name}", daemon=True)
        for name, run in runners
    ]
    log.info("starting backends: %s", ", ".join(name for name, _ in runners))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
