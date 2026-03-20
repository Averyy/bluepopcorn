from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).parent.parent.parent


@dataclass
class Settings:
    # Seerr
    seerr_url: str
    seerr_api_key: str

    # Bot
    allowed_senders: list[str]

    # LLM
    model: str = "haiku"
    fallback_model: str = "sonnet"
    llm_timeout: int = 30

    # Polling
    poll_interval: float = 0.5
    debounce_delay: float = 0.3

    # Digest
    digest_time: str = "07:30"

    # Location
    timezone: str = "America/Toronto"

    # Paths
    poster_dir: str = "~/Pictures/bluepopcorn"
    chat_db_path: str = "~/Library/Messages/chat.db"
    data_dir: str = "data"
    memory_dir: str = "data/memory"
    log_path: str = "bluepopcorn.log"

    # Messages
    max_message_length: int = 1200
    history_window: int = 20
    conversation_gap_hours: float = 2.0

    # Notifications
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"

    # Webhooks
    webhook_port: int = 8095
    webhook_secret: str = ""

    # HTTP
    http_timeout: int = 15

    # Logging
    log_level: str = "INFO"

    def resolve_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p


def load_settings(
    env_path: str = ".env",
    config_path: str = "config.toml",
) -> Settings:
    """Load settings from .env and config.toml."""
    # Load .env
    env = dotenv_values(env_path)

    # Load config.toml
    config_file = Path(config_path)
    config: dict = {}
    if config_file.exists():
        with open(config_file, "rb") as f:
            config = tomllib.load(f)

    # Required env vars
    seerr_url = env.get("SEERR_URL", "")
    seerr_api_key = env.get("SEERR_API_KEY", "")
    allowed_raw = env.get("ALLOWED_SENDERS", "")
    allowed_senders = [s.strip() for s in allowed_raw.split(",") if s.strip()]

    # Flatten config.toml sections
    llm = config.get("llm", {})
    polling = config.get("polling", {})
    digest = config.get("digest", {})
    location = config.get("location", {})
    paths = config.get("paths", {})
    messages = config.get("messages", {})
    notifications = config.get("notifications", {})
    webhooks = config.get("webhooks", {})
    logging_cfg = config.get("logging", {})

    # Validate required env vars
    missing = []
    if not seerr_url:
        missing.append("SEERR_URL")
    if not seerr_api_key:
        missing.append("SEERR_API_KEY")
    if not allowed_senders:
        missing.append("ALLOWED_SENDERS")
    if missing:
        raise ValueError(f"Missing required .env variables: {', '.join(missing)}")

    return Settings(
        seerr_url=seerr_url,
        seerr_api_key=seerr_api_key,
        allowed_senders=allowed_senders,
        model=llm.get("model", "haiku"),
        fallback_model=llm.get("fallback_model", "sonnet"),
        llm_timeout=llm.get("timeout", 30),
        poll_interval=polling.get("interval", 0.5),
        debounce_delay=polling.get("debounce_delay", 0.3),
        digest_time=digest.get("time", "07:30"),
        timezone=location.get("timezone", "America/Toronto"),
        poster_dir=paths.get("poster_dir", "~/Pictures/bluepopcorn"),
        chat_db_path=paths.get("chat_db_path", "~/Library/Messages/chat.db"),
        data_dir=paths.get("data_dir", "data"),
        memory_dir=paths.get("memory_dir", "data/memory"),
        log_path=paths.get("log_path", "bluepopcorn.log"),
        max_message_length=messages.get("max_length", 1200),
        history_window=messages.get("history_window", 20),
        conversation_gap_hours=messages.get("conversation_gap_hours", 2.0),
        quiet_start=notifications.get("quiet_start", "22:00"),
        quiet_end=notifications.get("quiet_end", "07:00"),
        webhook_port=webhooks.get("port", 8095),
        webhook_secret=env.get("WEBHOOK_SECRET", ""),
        http_timeout=config.get("http", {}).get("timeout", 15),
        log_level=logging_cfg.get("level", "INFO"),
    )
