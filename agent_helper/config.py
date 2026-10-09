"""Settings read from environment variables. See config/app.env.example."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

MIN_ADMIN_SECRET_LENGTH = 32
NOTIFY_EVENTS = ("request.created", "request.message", "report.created", "board.posted", "directory.published")
DEFAULT_NOTIFY_EVENTS = frozenset({"request.created", "request.message", "report.created"})

log = logging.getLogger("agent_helper")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _events(name: str, default: frozenset[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    chosen = frozenset(e.strip() for e in raw.split(",") if e.strip())
    unknown = chosen - set(NOTIFY_EVENTS)
    if unknown:
        log.warning("ignoring unknown %s entries: %s", name, ", ".join(sorted(unknown)))
    return chosen & set(NOTIFY_EVENTS)


def _webhook_url(raw: str | None) -> str | None:
    if not raw:
        return None
    if not raw.lower().startswith(("https://", "http://")):
        log.warning("NOTIFY_WEBHOOK_URL must start with https:// or http://; notifications are disabled")
        return None
    return raw


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    public_base_url: str = "http://localhost:8080"
    data_dir: Path = Path("./data")
    admin_secret: str | None = None
    trust_proxy_headers: bool = False
    max_body_bytes: int = 16 * 1024
    write_per_minute: int = 10
    read_per_minute: int = 120
    global_write_per_minute: int = 60
    max_messages_per_request: int = 200
    capabilities_file: Path | None = None
    log_level: str = "info"
    max_mailbox_messages: int = 500
    mail_retention_days: int = 90
    notify_webhook_url: str | None = None
    notify_webhook_secret: str | None = None
    notify_events: frozenset[str] = DEFAULT_NOTIFY_EVENTS
    notify_include_preview: bool = False
    notify_max_per_minute: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent-helper.db"

    @property
    def admin_enabled(self) -> bool:
        return self.admin_secret is not None and len(self.admin_secret) >= MIN_ADMIN_SECRET_LENGTH

    @classmethod
    def from_env(cls) -> Settings:
        caps = os.environ.get("CAPABILITIES_FILE")
        settings = cls(
            public_base_url=os.environ.get("PUBLIC_BASE_URL", cls.public_base_url).rstrip("/"),
            data_dir=Path(os.environ.get("DATA_DIR", str(cls.data_dir))),
            admin_secret=os.environ.get("ADMIN_AUTH_SECRET") or None,
            trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", cls.trust_proxy_headers),
            max_body_bytes=_int("MAX_BODY_BYTES", cls.max_body_bytes),
            write_per_minute=_int("RATE_LIMIT_WRITE_PER_MIN", cls.write_per_minute),
            read_per_minute=_int("RATE_LIMIT_READ_PER_MIN", cls.read_per_minute),
            global_write_per_minute=_int("RATE_LIMIT_GLOBAL_WRITE_PER_MIN", cls.global_write_per_minute),
            max_messages_per_request=_int("MAX_MESSAGES_PER_REQUEST", cls.max_messages_per_request),
            capabilities_file=Path(caps) if caps else None,
            log_level=os.environ.get("LOG_LEVEL", cls.log_level),
            max_mailbox_messages=_int("MAX_MAILBOX_MESSAGES", cls.max_mailbox_messages),
            mail_retention_days=_int("MAIL_RETENTION_DAYS", cls.mail_retention_days),
            notify_webhook_url=_webhook_url(os.environ.get("NOTIFY_WEBHOOK_URL")),
            notify_webhook_secret=os.environ.get("NOTIFY_WEBHOOK_SECRET") or None,
            notify_events=_events("NOTIFY_EVENTS", cls.notify_events),
            notify_include_preview=_bool("NOTIFY_INCLUDE_PREVIEW", cls.notify_include_preview),
            notify_max_per_minute=_int("NOTIFY_MAX_PER_MIN", cls.notify_max_per_minute),
        )
        if settings.admin_secret is not None and not settings.admin_enabled:
            log.warning(
                "ADMIN_AUTH_SECRET is shorter than %d characters; admin API is disabled",
                MIN_ADMIN_SECRET_LENGTH,
            )
        return settings
