"""ORM models."""

from __future__ import annotations

import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every database.

    SQLite stores no timezone, so plain UTCDateTime() hands back naive values that
    code (and browsers, via isoformat) would misread as local time. Store UTC, always return
    aware UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bumped on password change / forced logout; embedded in the session token.
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # Two-factor login (TOTP). The secret is only trusted once totp_enabled is set.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    totp_last_step: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))  # no code reuse
    # Self-service quota: non-admins may create servers up to these totals (0 = not allowed).
    max_servers: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_memory_mb: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    servers: Mapped[list["Server"]] = relationship(back_populates="owner")


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    location: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(String(255))  # agent base URL, e.g. https://vps.example.com:8443
    token: Mapped[str] = mapped_column(String(128))
    public_host: Mapped[str] = mapped_column(String(255))  # address players connect to
    memory_mb: Mapped[int] = mapped_column(Integer, default=0)  # 0 == no limit enforced
    disk_mb: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    playit_secret: Mapped[str | None] = mapped_column(String(255))  # playit.gg tunnel agent key

    allocations: Mapped[list["Allocation"]] = relationship(
        back_populates="node", cascade="all, delete-orphan", order_by="Allocation.port"
    )
    servers: Mapped[list["Server"]] = relationship(back_populates="node")


class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (UniqueConstraint("node_id", "ip", "port", name="uq_allocation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    ip: Mapped[str] = mapped_column(String(64), default="0.0.0.0")
    port: Mapped[int] = mapped_column(Integer)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    # True when the template claimed this port (e.g. Valheim's +1 query port); not user-removable.
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(64), default="")

    node: Mapped[Node] = relationship(back_populates="allocations")
    server: Mapped["Server | None"] = relationship(back_populates="allocations")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuidlib.uuid4()))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), index=True)
    template: Mapped[str] = mapped_column(String(64))

    memory_mb: Mapped[int] = mapped_column(Integer)
    swap_mb: Mapped[int] = mapped_column(Integer, default=0)
    cpu_percent: Mapped[int] = mapped_column(Integer, default=0)
    disk_mb: Mapped[int] = mapped_column(Integer, default=0)

    # Template variable values (env-backed), e.g. {"VERSION": "1.21.1", "MOTD": "hi"}.
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Minecraft modpack source. provider is "modrinth" | "curseforge" | None.
    modpack_provider: Mapped[str | None] = mapped_column(String(16))
    modpack_project_id: Mapped[str | None] = mapped_column(String(64))
    modpack_slug: Mapped[str | None] = mapped_column(String(128))
    modpack_name: Mapped[str | None] = mapped_column(String(255))
    modpack_icon: Mapped[str | None] = mapped_column(String(512))
    modpack_version_id: Mapped[str | None] = mapped_column(String(64))
    modpack_version_name: Mapped[str | None] = mapped_column(String(255))
    modpack_mc_version: Mapped[str | None] = mapped_column(String(32))
    auto_update: Mapped[bool] = mapped_column(Boolean, default=False)
    update_version_id: Mapped[str | None] = mapped_column(String(64))
    update_version_name: Mapped[str | None] = mapped_column(String(255))
    last_update_check: Mapped[datetime | None] = mapped_column(UTCDateTime())

    # Address players use instead of node:port, e.g. a playit.gg tunnel (abc.joinmc.link).
    public_address: Mapped[str | None] = mapped_column(String(255))
    discord_webhook: Mapped[str | None] = mapped_column(String(512))
    # Stop after this many minutes with nobody online (0 = never); wake when someone joins.
    auto_stop_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    wake_on_join: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    suspended_reason: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    owner: Mapped[User] = relationship(back_populates="servers")
    node: Mapped[Node] = relationship(back_populates="servers")
    allocations: Mapped[list[Allocation]] = relationship(
        back_populates="server", order_by="Allocation.port"
    )
    shares: Mapped[list["ServerShare"]] = relationship(back_populates="server", cascade="all, delete-orphan")
    backups: Mapped[list["Backup"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", order_by="Backup.created_at.desc()"
    )
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="server", cascade="all, delete-orphan")

    @property
    def primary_allocation(self) -> Allocation | None:
        return next((a for a in self.allocations if a.is_primary), None)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


PERMISSIONS = ("console", "power", "files", "backups", "schedules", "settings", "players")


class ServerShare(Base):
    """A friend invited to a server with limited rights (see PERMISSIONS)."""

    __tablename__ = "server_shares"
    __table_args__ = (UniqueConstraint("server_id", "user_id", name="uq_share"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)

    server: Mapped[Server] = relationship(back_populates="shares")
    user: Mapped[User] = relationship()


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(primary_key=True)
    uuid: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuidlib.uuid4()))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="manual")  # manual | scheduled | pre-update | pre-restore
    state: Mapped[str] = mapped_column(String(16), default="creating")  # creating | ready | failed
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)  # never pruned automatically
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    server: Mapped[Server] = relationship(back_populates="backups")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    cron: Mapped[str] = mapped_column(String(64))  # 5-field cron in the panel's timezone
    action: Mapped[str] = mapped_column(String(16))  # restart | start | stop | backup | command
    payload: Mapped[str] = mapped_column(Text, default="")  # command text for action=command
    warn_minutes: Mapped[int] = mapped_column(Integer, default=0)  # in-game warning before restart/stop
    keep_backups: Mapped[int] = mapped_column(Integer, default=7)  # for action=backup
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_result: Mapped[str | None] = mapped_column(Text)

    server: Mapped[Server] = relationship(back_populates="schedules")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")

    user: Mapped[User | None] = relationship()


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(UTCDateTime())

    user: Mapped[User] = relationship()


class Webhook(Base):
    """Outgoing JSON webhooks for events (billing systems, bots, monitoring)."""

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(512))
    events: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = all events
    secret: Mapped[str] = mapped_column(String(128))  # HMAC-SHA256 signing key
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
