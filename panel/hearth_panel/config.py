"""Panel configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str | None
    cookie_secure: bool
    session_hours: int
    curseforge_api_key: str | None
    update_interval_minutes: int
    web_dir: Path
    user_agent: str
    http_timeout: float
    local_node_token: str | None = None
    local_node_url: str = "http://agent:8080"
    local_public_host: str = "localhost"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("HEARTH_PANEL_DATA", "/var/lib/hearth-panel"))
        return cls(
            database_url=os.environ.get("HEARTH_DATABASE_URL", f"sqlite:///{data_dir / 'hearth.db'}"),
            secret_key=os.environ.get("HEARTH_SECRET_KEY") or None,
            cookie_secure=_bool("HEARTH_COOKIE_SECURE", False),
            session_hours=int(os.environ.get("HEARTH_SESSION_HOURS", "168")),
            curseforge_api_key=os.environ.get("CURSEFORGE_API_KEY") or None,
            update_interval_minutes=int(os.environ.get("HEARTH_UPDATE_INTERVAL_MINUTES", "360")),
            web_dir=Path(os.environ.get("HEARTH_WEB_DIR", Path(__file__).resolve().parent.parent / "web")),
            user_agent=os.environ.get(
                "HEARTH_USER_AGENT", "hearth-panel/0.1 (self-hosted game server panel)"
            ),
            http_timeout=float(os.environ.get("HEARTH_HTTP_TIMEOUT", "10")),
            local_node_token=os.environ.get("HEARTH_AGENT_TOKEN") or None,
            local_node_url=os.environ.get("HEARTH_LOCAL_NODE_URL", "http://agent:8080"),
            local_public_host=os.environ.get("HEARTH_PUBLIC_HOST", "localhost"),
        )
