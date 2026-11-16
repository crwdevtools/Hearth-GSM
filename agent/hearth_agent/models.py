"""Wire models shared between the panel and the agent."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PortBinding(BaseModel):
    host_ip: str = "0.0.0.0"
    host_port: int = Field(ge=1, le=65535)
    container_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"


class ServerSpec(BaseModel):
    """Everything the agent needs to build a container. The panel is the source of truth."""

    uuid: str
    image: str = Field(min_length=1, max_length=255)
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[PortBinding] = Field(default_factory=list)
    memory_mb: int = Field(ge=128, le=1_048_576)
    swap_mb: int = Field(default=0, ge=0, le=1_048_576)
    cpu_percent: int = Field(default=0, ge=0, le=10_000)  # 100 == one core, 0 == unlimited
    data_mount: str = "/data"
    command: list[str] | None = None
    console: Literal["stdin", "exec"] = "stdin"
    console_exec: list[str] | None = None  # e.g. ["rcon-cli"]; the command is appended
    # Console commands run around a backup so the archive is consistent (Minecraft: save-off/save-on).
    pre_backup: list[str] = Field(default_factory=list)
    post_backup: list[str] = Field(default_factory=list)
    stop_timeout: int = Field(default=60, ge=1, le=600)

    @field_validator("uuid")
    @classmethod
    def _uuid(cls, v: str) -> str:
        if not UUID_RE.match(v):
            raise ValueError("uuid must be a lowercase UUID")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not ENV_KEY_RE.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
        return v

    @field_validator("data_mount")
    @classmethod
    def _mount(cls, v: str) -> str:
        if not v.startswith("/") or ".." in v.split("/"):
            raise ValueError("data_mount must be an absolute container path")
        return v


class PowerAction(BaseModel):
    action: Literal["start", "stop", "restart", "kill"]


class Command(BaseModel):
    command: str = Field(min_length=1, max_length=2000)


class FileWrite(BaseModel):
    path: str
    content: str = Field(max_length=5_000_000)


class PathBody(BaseModel):
    path: str


class RenameBody(BaseModel):
    source: str
    target: str


class BackupRequest(BaseModel):
    backup_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    ignore: list[str] = Field(default_factory=list)  # glob patterns relative to the server folder


class FetchRequest(BaseModel):
    url: str = Field(max_length=2048)
    path: str = Field(max_length=1024)
    sha512: str | None = Field(default=None, pattern=r"^[0-9a-f]{128}$")


class ExtractRequest(BaseModel):
    archive: str
    target: str
    replace: bool = False


class SleepRequest(BaseModel):
    motd: str = Field(default="Sleeping - join to wake me up", max_length=120)


class TunnelRequest(BaseModel):
    secret: str = Field(min_length=8, max_length=255)
