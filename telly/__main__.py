from .app import run
from .config import Config, setup_logging


def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level)
    run(cfg)


if __name__ == "__main__":
    main()