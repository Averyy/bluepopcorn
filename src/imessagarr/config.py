from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class Settings:
    # Seerr
    seerr_url: str
    seerr_email: str
    seerr_password: str

    # Bot
    bot_apple_id: str
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
    latitude: float = 43.1594
    longitude: float = -79.2469
    location_name: str = "St. Catharines"
    timezone: str = "America/Toronto"

    # Paths
    poster_dir: str = "~/Pictures/imessagarr"
    db_path: str = "~/.local/share/imessagarr/bot.db"
    chat_db_path: str = "~/Library/Messages/chat.db"
    log_path: str = "imessagarr.log"

    # Messages
    max_message_length: int = 1200
    history_window: int = 20
    history_gap_hours: float = 1.0

    # Notifications
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"

    # Webhooks
    webhook_port: int = 8095

    # HTTP
    http_timeout: int = 15

    # Logging
    log_level: str = "INFO"

    def resolve_path(self, path: str) -> Path:
        return Path(path).expanduser()


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
    seerr_email = env.get("SEERR_EMAIL", "")
    seerr_password = env.get("SEERR_PASSWORD", "")
    bot_apple_id = env.get("BOT_APPLE_ID", "")
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
    if not seerr_email:
        missing.append("SEERR_EMAIL")
    if not seerr_password:
        missing.append("SEERR_PASSWORD")
    if not bot_apple_id:
        missing.append("BOT_APPLE_ID")
    if not allowed_senders:
        missing.append("ALLOWED_SENDERS")
    if missing:
        raise ValueError(f"Missing required .env variables: {', '.join(missing)}")

    return Settings(
        seerr_url=seerr_url,
        seerr_email=seerr_email,
        seerr_password=seerr_password,
        bot_apple_id=bot_apple_id,
        allowed_senders=allowed_senders,
        model=llm.get("model", "haiku"),
        fallback_model=llm.get("fallback_model", "sonnet"),
        llm_timeout=llm.get("timeout", 30),
        poll_interval=polling.get("interval", 0.5),
        debounce_delay=polling.get("debounce_delay", 0.3),
        digest_time=digest.get("time", "07:30"),
        latitude=location.get("latitude", 43.1594),
        longitude=location.get("longitude", -79.2469),
        location_name=location.get("name", "St. Catharines"),
        timezone=location.get("timezone", "America/Toronto"),
        poster_dir=paths.get("poster_dir", "~/Pictures/imessagarr"),
        db_path=paths.get("db_path", "~/.local/share/imessagarr/bot.db"),
        chat_db_path=paths.get("chat_db_path", "~/Library/Messages/chat.db"),
        log_path=paths.get("log_path", "imessagarr.log"),
        max_message_length=messages.get("max_length", 1200),
        history_window=messages.get("history_window", 20),
        history_gap_hours=messages.get("history_gap_hours", 1.0),
        quiet_start=notifications.get("quiet_start", "22:00"),
        quiet_end=notifications.get("quiet_end", "07:00"),
        webhook_port=webhooks.get("port", 8095),
        http_timeout=config.get("http", {}).get("timeout", 15),
        log_level=logging_cfg.get("level", "INFO"),
    )
