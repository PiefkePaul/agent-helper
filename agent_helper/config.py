"""Settings read from environment variables. See config/app.env.example."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

MIN_ADMIN_SECRET_LENGTH = 32

log = logging.getLogger("agent_helper")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


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
    capabilities_file: Path | None = None
    log_level: str = "info"

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
            capabilities_file=Path(caps) if caps else None,
            log_level=os.environ.get("LOG_LEVEL", cls.log_level),
        )
        if settings.admin_secret is not None and not settings.admin_enabled:
            log.warning(
                "ADMIN_AUTH_SECRET is shorter than %d characters; admin API is disabled",
                MIN_ADMIN_SECRET_LENGTH,
            )
        return settings
