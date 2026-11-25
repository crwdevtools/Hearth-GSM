"""Servers: create, configure, power, console, cloning, suspension, joinability, stats."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import (admin_user, ctx, current_user, db_session, permissions_for, server_for, server_with,
                    visible_servers_query, websocket_user)
from ..models import Allocation, AuditLog, Node, Server, User
from ..serializers import server_out
from ..services import events, moves, packzip, presets, settings_store, updater
from ..services import proxy as proxy_links
from ..services.backups import BackupFailed, create_backup
from ..services.events import is_discord_webhook
from ..services.node_client import NodeError, console_ws_url
from ..services.provisioning import UPLOADED_PACK_DIR, ProvisionError, build_spec, check_capacity, choose_allocations, claim
from ..services.status import live_statuses
from ..templates import (MC_SERVER_TYPES, Template, TemplateError, coerce_variables, defaults, get_template,
                         parse_extra_env)

log = logging.getLogger("hearth.servers")
CONSOLE_RECHECK = 30  # seconds between access checks on an open console
router = APIRouter(prefix="/api/servers", tags=["servers"])

Provider = Literal["modrinth", "curseforge"]


class ModpackChoice(BaseModel):
    provider: Provider
    project_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")  # None == latest release


class PackUpload(BaseModel):
    """A modpack zip uploaded beforehand (see routers/packs.py)."""
    token: str = Field(pattern=r"^[A-Za-z0-9_\-]{16,43}$")
    curseforge_page: str | None = Field(default=None, max_length=300)  # page address or slug, CurseForge packs only


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    owner_id: int | None = None
    node_id: int
    template: str
    memory_mb: int = Field(ge=128, le=1_048_576)
    swap_mb: int = Field(default=0, ge=0, le=1_048_576)
    cpu_percent: int = Field(default=0, ge=0, le=10_000)
    disk_mb: int = Field(default=0, ge=0)
    variables: dict[str, Any] = Field(default_factory=dict)
    allocation_id: int | None = None
    modpack: ModpackChoice | None = None
    modpack_upload: PackUpload | None = None
    auto_update: bool = False
    accept_eula: bool = False
    start: bool = True
    preset: str | None = Field(default=None, max_length=32)


def _clean_address(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if v and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}(:\d{1,5})?", v):
        raise ValueError("Use a hostname with an optional :port, like abc.joinmc.link:12345")
    return v


class ServerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    owner_id: int | None = None
    memory_mb: int | None = Field(default=None, ge=128, le=1_048_576)
    swap_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    cpu_percent: int | None = Field(default=None, ge=0, le=10_000)
    disk_mb: int | None = Field(default=None, ge=0)
    variables: dict[str, Any] | None = None
    public_address: str | None = Field(default=None, max_length=260)
    discord_webhook: str | None = Field(default=None, max_length=512)
    auto_stop_minutes: int | None = Field(default=None, ge=0, le=1440)
    wake_on_join: bool | None = None

    _v_addr = field_validator("public_address")(classmethod(lambda cls, v: _clean_address(v)))

    @field_validator("discord_webhook")
    @classmethod
    def _discord(cls, v: str | None) -> str | None:
        if v and not is_discord_webhook(v.strip()):
            raise ValueError("Paste a Discord webhook URL (Server Settings → Integrations → Webhooks)")
        return v.strip() if v is not None else None


class SourceBody(BaseModel):
    kind: Literal["software", "modpack"]
    server_type: str | None = None
    version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._\-]{1,32}$")
    modpack: ModpackChoice | None = None
    auto_update: bool | None = None
    backup_first: bool = True


class PowerBody(BaseModel):
    action: Literal["start", "stop", "restart", "kill"]


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=2000)


class AllocationAdd(BaseModel):
    allocation_id: int | None = None


class SuspendBody(BaseModel):
    reason: str = Field(default="", max_length=255)


class CloneBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    copy_files: bool = True


# ----- helpers ---------------------------------------------------------------------------------


async def sync(context: PanelContext, db: Session, server: Server, *, start: bool = False) -> str | None:
    """Push the spec to the node. Returns a user-facing warning instead of raising, because the
    database change is already saved and can be re-applied with Reinstall."""
    try:
        await updater.push(context, db, server, start=start)
    except ProvisionError as exc:
        return str(exc)
    except NodeError as exc:
        return f"Saved, but the node didn't take the change: {exc} Use Reinstall once it's back."
    return None


async def resolve_modpack(context: PanelContext, server: Server, choice: ModpackChoice) -> None:
    project = await context.modpacks.project(choice.provider, choice.project_id)
    versions = await context.modpacks.versions(choice.provider, choice.project_id)
    if not versions:
        raise HTTPException(400, "That modpack has no downloadable versions.")
    if choice.version_id:
        version = next((v for v in versions if v["id"] == choice.version_id), None)
        if version is None:
            raise HTTPException(400, "That modpack version no longer exists.")
    else:
        version = next((v for v in versions if v["type"] == "release"), versions[0])
    server.modpack_file = server.modpack_loader = None
    server.modpack_provider = choice.provider
    server.modpack_project_id = project["id"]
    server.modpack_slug = project["slug"]
    server.modpack_name = project["name"]
    server.modpack_icon = project["icon_url"]
    updater.set_modpack_version(server, version)


def attach_upload(context: PanelContext, user: User, server: Server, choice: PackUpload) -> tuple[Path, str]:
    """Fill the server's modpack fields from a staged zip. Returns the staged file and its name on the node."""
    stage = packzip.stage_dir(context.settings.data_dir)
    try:
        path, meta = packzip.load(stage, choice.token, user.id)
        slug = packzip.clean_slug(choice.curseforge_page)
    except packzip.PackError as exc:
        raise HTTPException(400, str(exc)) from exc
    info = meta["info"]
    server.modpack_provider = info["format"]
    server.modpack_project_id = server.modpack_icon = server.modpack_version_id = None
    server.modpack_slug = (slug or info.get("slug")) if info["format"] == "curseforge" else None
    server.modpack_name = info["name"]
    server.modpack_version_name = info.get("version") or "uploaded"
    server.modpack_mc_version = info["mc_version"]
    server.modpack_loader = info["loader"]
    server.modpack_file = f"{UPLOADED_PACK_DIR}/pack." + ("mrpack" if info["format"] == "modrinth" else "zip")
    server.auto_update = False
    return path, server.modpack_file


async def _chunks(path: Path):
    with path.open("rb") as fh:
        while chunk := await asyncio.to_thread(fh.read, 1 << 20):
            yield chunk


async def deliver_pack(context: PanelContext, server: Server, path: Path, target: str) -> str | None:
    """Copy the staged zip into the new server's folder (the node makes it moments after the apply)."""
    error: NodeError | None = None
    for attempt in range(20):
        try:
            await context.nodes.upload(server.node, server.uuid, target, _chunks(path), path.stat().st_size)
            return None
        except NodeError as exc:
            error = exc
            if exc.status not in (404, 409):
                break
            await asyncio.sleep(min(1.5, 0.2 * (attempt + 1)))
    return (f"The server was created, but its modpack file didn't reach the node: {error}. "
            "Delete it and create it again.")


def _validate_template_vars(template: Template, values: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    secret = {v.key for v in template.variables if v.secret}
    # Secrets are never sent back to the browser; an empty value means "keep the current one".
    incoming = {k: v for k, v in values.items() if not (k in secret and v in ("", None) and current.get(k))}
    return coerce_variables(template, incoming, base=current)


def check_quota(db: Session, user: User, *, add_servers: int = 0, add_memory: int = 0,
                exclude_server: int | None = None) -> None:
    """Non-admins may create and size their own servers within the quota an admin gave them."""
    if user.is_admin:
        return
    if user.max_servers <= 0:
        raise HTTPException(403, "Your account doesn't have a server quota. Ask an admin for one.")
    # On PostgreSQL this serialises quota checks per user; SQLite already serialises writes, and
    # recheck_quota() below catches anything that slipped past a check made before an await.
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    # Swap is memory the server can use too, so it counts toward the quota.
    query = select(func.count(Server.id), func.coalesce(func.sum(Server.memory_mb + Server.swap_mb), 0)
                   ).where(Server.owner_id == user.id)
    if exclude_server is not None:
        query = query.where(Server.id != exclude_server)
    count, memory = db.execute(query).one()
    if count + add_servers > user.max_servers:
        raise HTTPException(403, f"Your quota allows {user.max_servers} server(s); you have {count}.")
    if user.max_memory_mb and memory + add_memory > user.max_memory_mb:
        left = max(user.max_memory_mb - memory, 0)
        raise HTTPException(403, f"Your quota has {left} MB of memory left; this needs {add_memory} MB.")


def recheck_quota(db: Session, user: User) -> None:
    """Run after the new or resized server is flushed but before commit: if requests raced past
    check_quota(), the totals now include all of them and the late one is rolled back."""
    if user.is_admin:
        return
    try:
        check_quota(db, user)
    except HTTPException:
        db.rollback()
        raise


def _reload_allocations(db: Session, server: Server) -> None:
    db.refresh(server, ["allocations"])


# Admin-only text that may hold credentials (e.g. EXTRA_ENV="RCON_PASSWORD=..."): only admins edit
# it, so only admins see it. Non-admins get "" and sending "" back means "unchanged".
ADMIN_ONLY_SECRET = {"EXTRA_ENV"}


def _out(server: Server, user: User, status: dict | None = None) -> dict[str, Any]:
    out = {**server_out(server, status), "permissions": sorted(permissions_for(server, user))}
    if not user.is_admin:
        out["variables"] = {k: ("" if k in ADMIN_ONLY_SECRET and v else v) for k, v in out["variables"].items()}
    return out


# ----- list / create ---------------------------------------------------------------------------


@router.get("")
async def list_servers(user: User = Depends(current_user), db: Session = Depends(db_session),
                       context: PanelContext = Depends(ctx)):
    servers = list(db.scalars(visible_servers_query(user)))
    statuses = await live_statuses(context, servers)
    return [_out(s, user, statuses.get(s.uuid)) for s in servers]


@router.post("", status_code=201)
async def create_server(body: ServerCreate, user: User = Depends(current_user), db: Session = Depends(db_session),
                        context: PanelContext = Depends(ctx)):
    template = get_template(body.template)
    if not user.is_admin:
        if body.owner_id not in (None, user.id):
            raise HTTPException(403, "You can only create servers for yourself.")
        check_quota(db, user, add_servers=1, add_memory=body.memory_mb + body.swap_mb)
    if "eula" in template.features and not body.accept_eula:
        raise HTTPException(400, "Accept the Minecraft EULA to create this server.")
    if body.memory_mb < template.min_memory_mb:
        raise HTTPException(400, f"{template.name} needs at least {template.min_memory_mb} MB of memory.")
    has_pack = body.modpack is not None or body.modpack_upload is not None
    if body.modpack and body.modpack_upload:
        raise HTTPException(400, "Choose a modpack from the catalog or upload one, not both.")
    if has_pack and "modpacks" not in template.features:
        raise HTTPException(400, f"{template.name} doesn't support modpacks.")
    if not user.is_admin and any(v.admin_only and k in body.variables
                                 for k in body.variables for v in template.variables if v.key == k):
        raise HTTPException(403, "Only admins can create custom-image servers.")
    node = db.get(Node, body.node_id)
    if node is None:
        raise HTTPException(404, "Node not found.")
    owner = db.get(User, body.owner_id) if body.owner_id else user
    if owner is None:
        raise HTTPException(404, "Owner not found.")

    variables = coerce_variables(template, {**defaults(template), **body.variables})
    preset = None
    if body.preset:
        try:
            preset = presets.check(body.preset, template.key, variables, has_pack)
        except presets.PresetError as exc:
            raise HTTPException(400, str(exc)) from exc
    check_capacity(db, node, body.memory_mb, body.disk_mb)

    server = Server(
        name=body.name.strip(), description=body.description, owner_id=owner.id, node_id=node.id,
        template=template.key, memory_mb=body.memory_mb, swap_mb=body.swap_mb, cpu_percent=body.cpu_percent,
        disk_mb=body.disk_mb, variables=variables, auto_update=body.auto_update and body.modpack is not None,
    )
    staged: tuple[Path, str] | None = None
    if body.modpack:
        await resolve_modpack(context, server, body.modpack)
    elif body.modpack_upload:
        staged = attach_upload(context, user, server, body.modpack_upload)
    picks = choose_allocations(node, template, body.allocation_id)
    db.add(server)
    db.flush()
    for alloc, is_primary in picks:
        claim(db, alloc, server.id, is_primary=is_primary, is_template=not is_primary)
    db.flush()
    db.refresh(server)
    warnings = []
    if preset and preset.bedrock_port:
        warnings.append(presets.claim_bedrock_port(db, server))
    if owner.id == user.id:
        recheck_quota(db, user)
    db.commit()
    db.refresh(server)
    events.emit(context, db, "server.created", server=server, user=user,
                detail=f"{template.name} on {node.name}" + (f" ({preset.name})" if preset else ""))
    if staged:  # the pack goes in before the first start, which then installs it
        warnings.append(await sync(context, db, server, start=False))
        if not warnings[-1]:
            warnings.append(await deliver_pack(context, server, *staged))
            if not warnings[-1]:
                packzip.discard(staged[0].parent, body.modpack_upload.token)
                if body.start:
                    warnings.append(await sync(context, db, server, start=True))
    elif preset is None:
        warnings.append(await sync(context, db, server, start=body.start))
    else:  # plugins/mods go in before the first start
        warnings.append(await sync(context, db, server, start=False))
        if not warnings[-1]:
            warnings.append(await presets.install(context, db, server, preset))
            if body.start:
                with contextlib.suppress(NodeError):
                    await context.nodes.power(server.node, server.uuid, "start")
    warning = " ".join(w for w in warnings if w) or None
    return {**_out(server, user, {"state": "installing"}), "warning": warning}


# ----- one server ------------------------------------------------------------------------------


@router.get("/{server_id}")
async def get_server(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session),
                     context: PanelContext = Depends(ctx)):
    server = server_for(server_id, user, db)
    status = (await live_statuses(context, [server]))[server.uuid]
    return {**_out(server, user, status), "template_detail": get_template(server.template).public()}


@router.patch("/{server_id}")
async def update_server(server_id: int, body: ServerPatch, user: User = Depends(current_user),
                        db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    server = server_for(server_id, user, db)
    perms = permissions_for(server, user)
    template = get_template(server.template)
    changes = body.model_dump(exclude_none=True)
    if changes.keys() - {"owner_id", "memory_mb", "swap_mb", "cpu_percent", "disk_mb"} and "settings" not in perms:
        raise HTTPException(403, "You don't have permission to change this server's settings.")
    if "owner_id" in changes and not user.is_admin:
        raise HTTPException(403, "Only admins can change ownership.")

    spec_changed = False
    for key in ("name", "description"):
        if key in changes:
            setattr(server, key, changes[key].strip() if key == "name" else changes[key])
    if "owner_id" in changes:
        if db.get(User, changes["owner_id"]) is None:
            raise HTTPException(404, "Owner not found.")
        server.owner_id = changes["owner_id"]
    if "public_address" in changes:
        server.public_address = changes["public_address"] or None
    if "discord_webhook" in changes:
        server.discord_webhook = changes["discord_webhook"] or None
    if "auto_stop_minutes" in changes:
        if changes["auto_stop_minutes"] and template.key != "minecraft-java":
            raise HTTPException(400, "Auto-sleep is available for Minecraft: Java servers.")
        server.auto_stop_minutes = changes["auto_stop_minutes"]
    if "wake_on_join" in changes:
        server.wake_on_join = changes["wake_on_join"]

    resources = {k: changes[k] for k in ("memory_mb", "swap_mb", "cpu_percent", "disk_mb")
                 if k in changes and changes[k] != getattr(server, k)}
    if resources:
        if not user.is_admin:
            if server.owner_id != user.id:
                raise HTTPException(403, "Only admins and the owner can change resources.")
            check_quota(db, user, exclude_server=server.id,
                        add_memory=resources.get("memory_mb", server.memory_mb) + resources.get("swap_mb", server.swap_mb))
        memory = resources.get("memory_mb", server.memory_mb)
        if memory < template.min_memory_mb:
            raise HTTPException(400, f"{template.name} needs at least {template.min_memory_mb} MB of memory.")
        check_capacity(db, server.node, memory, resources.get("disk_mb", server.disk_mb), exclude_server=server.id)
        for key, value in resources.items():
            setattr(server, key, value)
        if not user.is_admin:
            db.flush()
            recheck_quota(db, user)
        spec_changed = True
    if body.variables is not None:
        current = dict(server.variables or {})
        if not user.is_admin:  # the masked value coming back unchanged
            body.variables = {k: v for k, v in body.variables.items()
                              if not (k in ADMIN_ONLY_SECRET and v in ("", None))}
        locked = {v.key for v in template.variables if v.admin_only}
        if not user.is_admin and any(k in locked and body.variables[k] != current.get(k) for k in body.variables):
            raise HTTPException(403, "Only admins can change the image, ports or environment of a custom server.")
        server.variables = _validate_template_vars(template, body.variables, current)
        spec_changed = True
    db.commit()
    events.emit(context, db, "server.updated", server=server, user=user, detail=", ".join(sorted(changes)))
    warning = await sync(context, db, server) if spec_changed else None
    return {**_out(server, user), "warning": warning}


@router.put("/{server_id}/source")
async def change_source(server_id: int, body: SourceBody, user: User = Depends(current_user),
                        db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    server = server_for(server_id, user, db, "settings")
    template = get_template(server.template)
    if "minecraft_java" not in template.features:
        raise HTTPException(400, "Only Minecraft: Java servers have a selectable source.")
    if body.backup_first:
        try:
            await create_backup(context, db, server, kind="pre-update", wait=True, keep=5, user=user,
                                name="Before changing version")
        except BackupFailed as exc:
            raise HTTPException(409, f"Not changed: the safety backup failed ({exc}). "
                                     "Untick “Back up first” to change anyway.") from exc
    old_pack_file = server.modpack_file
    if body.kind == "modpack":
        if body.modpack is None:
            raise HTTPException(400, "Choose a modpack.")
        await resolve_modpack(context, server, body.modpack)
        if body.auto_update is not None:
            server.auto_update = body.auto_update
    else:
        server_type = (body.server_type or "").upper()
        if server_type not in MC_SERVER_TYPES:
            raise HTTPException(400, "Choose the server software.")
        server.variables = coerce_variables(template, {"TYPE": server_type, "VERSION": body.version or "LATEST"},
                                            base=dict(server.variables or {}))
        for field in ("provider", "project_id", "slug", "name", "icon", "version_id", "version_name", "mc_version",
                  "file", "loader"):
            setattr(server, f"modpack_{field}", None)
        server.update_version_id = server.update_version_name = None
        server.auto_update = False
    db.commit()
    events.emit(context, db, "server.source_changed", server=server, user=user,
                detail=server.modpack_name and f"{server.modpack_name} {server.modpack_version_name}"
                or f"{server.variables.get('TYPE')} {server.variables.get('VERSION')}")
    warning = await sync(context, db, server)
    if old_pack_file and not server.modpack_file:  # the uploaded zip isn't needed any more
        with contextlib.suppress(NodeError):
            await context.nodes.delete_file(server.node, server.uuid, old_pack_file)
    return {**_out(server, user), "warning": warning}


@router.delete("/{server_id}", status_code=204)
async def delete_server(server_id: int, force: bool = False, user: User = Depends(current_user),
                        db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    server = server_for(server_id, user, db, "owner")
    if not user.is_admin and user.max_servers <= 0:
        # A server an admin gave you isn't yours to wipe; self-service users manage their own.
        raise HTTPException(403, "Ask an admin to delete this server.")
    try:
        await context.nodes.delete(server.node, server.uuid, purge=True)
    except NodeError as exc:
        if exc.status != 404 and not force:
            raise HTTPException(502, f"{exc} Delete anyway to remove it from the panel only.") from exc
    name = server.name
    for alloc in server.allocations:
        alloc.server_id, alloc.is_primary, alloc.is_template = None, False, False
    events.emit(context, db, "server.deleted", server=server, user=user, detail=name, commit=False)
    db.delete(server)
    db.commit()


@router.post("/{server_id}/reinstall")
async def reinstall(server: Server = Depends(server_with("settings")), user: User = Depends(current_user),
                    db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    try:
        await updater.push(context, db, server)
    except ProvisionError as exc:
        raise HTTPException(400, str(exc)) from exc
    events.emit(context, db, "server.reinstalled", server=server, user=user)
    return {"accepted": True}


@router.post("/{server_id}/power", status_code=202)
async def power(body: PowerBody, server: Server = Depends(server_with("power")), user: User = Depends(current_user),
                db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    if server.id in moves.moving:
        raise HTTPException(409, "This server is moving to another node. Try again when it's done.")
    if server.suspended and body.action in ("start", "restart") and not user.is_admin:
        raise HTTPException(403, f"This server is suspended{': ' + server.suspended_reason if server.suspended_reason else ''}.")
    await context.nodes.power(server.node, server.uuid, body.action)
    events.emit(context, db, f"server.{body.action}", server=server, user=user)
    return {"accepted": True}


@router.post("/{server_id}/command")
async def command(body: CommandBody, server: Server = Depends(server_with("console")),
                  user: User = Depends(current_user), db: Session = Depends(db_session),
                  context: PanelContext = Depends(ctx)):
    output = await context.nodes.command(server.node, server.uuid, body.command)
    events.emit(context, db, "console.command", server=server, user=user, detail=body.command)
    return {"output": output}


@router.get("/{server_id}/spec")
def spec_preview(server: Server = Depends(server_with("settings")), user: User = Depends(current_user),
                 db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    """The exact container spec the node receives (secrets masked), for troubleshooting."""
    spec = build_spec(server, settings_store.curseforge_key(db, context.settings))
    template = get_template(server.template)
    masked = {v.key for v in template.variables if v.secret} | {"CF_API_KEY"}
    if not user.is_admin and "custom_image" in template.features:
        with contextlib.suppress(TemplateError):
            masked |= set(parse_extra_env(str((server.variables or {}).get("EXTRA_ENV") or "")))
    spec["env"] = {k: ("••••••" if k in masked else v) for k, v in spec["env"].items()}
    return spec


@router.get("/{server_id}/metrics")
async def metrics(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session),
                  context: PanelContext = Depends(ctx)):
    server = server_for(server_id, user, db)
    data = await context.nodes.metrics(server.node, server.uuid)
    return {**data, "memory_limit": server.memory_mb * 1048576, "cpu_limit": server.cpu_percent or None}


@router.get("/{server_id}/activity")
def activity(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session), limit: int = 100):
    server = server_for(server_id, user, db, "owner")
    rows = db.scalars(select(AuditLog).where(AuditLog.server_id == server.id)
                      .order_by(AuditLog.at.desc()).limit(min(limit, 500)))
    return [{"at": r.at.isoformat(), "action": r.action, "detail": r.detail,
             "user": r.user.username if r.user else None} for r in rows]


class MoveBody(BaseModel):
    node_id: int


@router.post("/{server_id}/move", status_code=202)
async def move(body: MoveBody, server_id: int, admin: User = Depends(admin_user),
               db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    """Move a server to another node (admins). Runs in the background; watch `moving`."""
    server = server_for(server_id, admin, db)
    target = db.get(Node, body.node_id)
    if target is None:
        raise HTTPException(404, "Node not found.")
    try:
        moves.check(db, server, target)
    except moves.MoveError as exc:
        raise HTTPException(409, str(exc)) from exc
    moves.start(context, server.id, target.id, admin.id)
    return _out(server, admin)


class ProxyBody(BaseModel):
    backends: list[int] = Field(default_factory=list, max_length=64)  # in try order


def _proxy_candidates(db: Session, proxy: Server, user: User) -> list[Server]:
    from ..deps import permissions_for

    rows = db.scalars(select(Server).where(Server.id != proxy.id).order_by(Server.name))
    return [s for s in rows if proxy_links.eligible(s) and "settings" in permissions_for(s, user)]


@router.get("/{server_id}/proxy")
def proxy_backends(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)):
    proxy = server_for(server_id, user, db, "settings")
    if proxy.template != "velocity":
        raise HTTPException(400, "Only Velocity proxies have backend servers.")
    linked = {s.id: s.proxy_order for s in db.scalars(select(Server).where(Server.proxy_id == proxy.id))}
    candidates = _proxy_candidates(db, proxy, user)
    return {"servers": sorted(
        [{"id": s.id, "name": s.name, "node": s.node.name, "address": proxy_links.backend_address(proxy, s),
          "linked": s.id in linked, "order": linked.get(s.id, 999), "other_proxy": bool(s.proxy_id and s.proxy_id != proxy.id)}
         for s in candidates], key=lambda r: (not r["linked"], r["order"], r["name"].lower()))}


@router.put("/{server_id}/proxy")
async def set_proxy_backends(body: ProxyBody, server_id: int, user: User = Depends(current_user),
                             db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    proxy = server_for(server_id, user, db, "settings")
    if proxy.template != "velocity":
        raise HTTPException(400, "Only Velocity proxies have backend servers.")
    allowed = {s.id: s for s in _proxy_candidates(db, proxy, user)}
    if len(set(body.backends)) != len(body.backends) or any(i not in allowed for i in body.backends):
        raise HTTPException(400, "Only Paper or Purpur servers you can manage can go behind the proxy.")
    chosen = [allowed[i] for i in body.backends]
    if any(s.proxy_id and s.proxy_id != proxy.id for s in chosen):
        raise HTTPException(409, "One of those servers is already behind another proxy.")
    try:
        await proxy_links.link(context, db, proxy, chosen)
    except proxy_links.ProxyError as exc:
        raise HTTPException(409, str(exc)) from exc
    events.emit(context, db, "server.proxy_linked", server=proxy, user=user,
                detail=", ".join(s.name for s in chosen) or "No backend servers")
    return {"restart_needed": True, **proxy_backends(server_id, user, db)}


class MaintenanceBody(BaseModel):
    minutes: int = Field(ge=0, le=7 * 24 * 60)  # 0 ends it now


@router.post("/{server_id}/maintenance")
async def maintenance(body: MaintenanceBody, server_id: int, user: User = Depends(current_user),
                      db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    """Pause schedules, auto-updates, auto-sleep and crash handling while you work on a server."""
    server = server_for(server_id, user, db, "settings")
    if body.minutes:
        server.maintenance_until = datetime.now(timezone.utc) + timedelta(minutes=body.minutes)
        detail = f"Paused for {body.minutes} minutes."
    else:
        server.maintenance_until = None
        detail = "Maintenance pause ended."
    events.emit(context, db, "server.maintenance", server=server, user=user, detail=detail)
    return _out(server, user)


# ----- admin: suspension, allocations ----------------------------------------------------------


@router.post("/{server_id}/suspend")
async def suspend(body: SuspendBody, server_id: int, admin: User = Depends(admin_user),
                  db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    server = server_for(server_id, admin, db)
    server.suspended, server.suspended_reason = True, body.reason or None
    db.commit()
    with contextlib.suppress(NodeError):
        await context.nodes.power(server.node, server.uuid, "stop")
    events.emit(context, db, "server.suspended", server=server, user=admin, detail=body.reason)
    return _out(server, admin)


@router.post("/{server_id}/unsuspend")
async def unsuspend(server_id: int, admin: User = Depends(admin_user), db: Session = Depends(db_session),
                    context: PanelContext = Depends(ctx)):
    server = server_for(server_id, admin, db)
    server.suspended, server.suspended_reason = False, None
    db.commit()
    events.emit(context, db, "server.unsuspended", server=server, user=admin)
    return _out(server, admin)


@router.post("/{server_id}/allocations", status_code=201)
async def add_allocation(server_id: int, body: AllocationAdd, admin: User = Depends(admin_user),
                         db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    server = server_for(server_id, admin, db)
    query = select(Allocation).where(Allocation.node_id == server.node_id, Allocation.server_id.is_(None))
    if body.allocation_id is not None:
        query = query.where(Allocation.id == body.allocation_id)
    alloc = db.scalars(query.order_by(Allocation.port)).first()
    if alloc is None:
        raise HTTPException(409, f"No free ports on node “{server.node.name}”. Add allocations in Admin → Nodes.")
    claim(db, alloc, server.id, is_primary=False, is_template=False)
    db.commit()
    _reload_allocations(db, server)
    warning = await sync(context, db, server)
    return {**_out(server, admin), "warning": warning}


@router.delete("/{server_id}/allocations/{allocation_id}")
async def remove_allocation(server_id: int, allocation_id: int, admin: User = Depends(admin_user),
                            db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    server = server_for(server_id, admin, db)
    alloc = db.get(Allocation, allocation_id)
    if alloc is None or alloc.server_id != server.id:
        raise HTTPException(404, "That port isn't assigned to this server.")
    if alloc.is_primary or alloc.is_template:
        raise HTTPException(400, "The game needs this port; it can't be removed.")
    alloc.server_id = None
    db.commit()
    _reload_allocations(db, server)
    warning = await sync(context, db, server)
    return {**_out(server, admin), "warning": warning}


# ----- modpack updates -------------------------------------------------------------------------


@router.post("/{server_id}/updates/check")
async def check_update(server: Server = Depends(server_with("settings")), user: User = Depends(current_user),
                       db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    result = await updater.check_server(context, db, server, apply=False)
    return {**result, "server": _out(server, user)}


@router.post("/{server_id}/updates/apply")
async def apply_update(server: Server = Depends(server_with("settings")), user: User = Depends(current_user),
                       db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    if not server.modpack_provider:
        raise HTTPException(400, "This server doesn't run a modpack.")
    if server.modpack_file:
        raise HTTPException(400, "This modpack was uploaded, so Hearth can't check it for updates. "
                                 "Create a new server from the newer file, or switch to a catalog modpack under Software.")
    result = await updater.check_server(context, db, server, apply=True)
    if result.get("error"):
        raise HTTPException(502, result["error"])
    if not result.get("applied"):
        raise HTTPException(409, "Already on the newest release.")
    return {**result, "server": _out(server, user)}


# ----- clone -----------------------------------------------------------------------------------


@router.post("/{server_id}/clone", status_code=201)
async def clone(server_id: int, body: CloneBody, user: User = Depends(current_user),
                db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    """A copy on the same node with its own port: for trying updates or mods safely."""
    source = server_for(server_id, user, db, "owner")
    if source.suspended and not user.is_admin:
        raise HTTPException(403, "This server is suspended, so it can't be copied.")
    check_quota(db, user, add_servers=1, add_memory=source.memory_mb + source.swap_mb)
    template = get_template(source.template)
    check_capacity(db, source.node, source.memory_mb, source.disk_mb)
    copy = Server(name=body.name.strip(), description=f"Copy of {source.name}", owner_id=source.owner_id,
                  node_id=source.node_id, template=source.template, memory_mb=source.memory_mb,
                  swap_mb=source.swap_mb, cpu_percent=source.cpu_percent, disk_mb=source.disk_mb,
                  variables=dict(source.variables or {}), auto_update=False)
    for field in ("provider", "project_id", "slug", "name", "icon", "version_id", "version_name", "mc_version",
                  "file", "loader"):
        setattr(copy, f"modpack_{field}", getattr(source, f"modpack_{field}"))
    picks = choose_allocations(source.node, template)
    db.add(copy)
    db.flush()
    for alloc, is_primary in picks:
        claim(db, alloc, copy.id, is_primary=is_primary, is_template=not is_primary)
    recheck_quota(db, user)
    db.commit()
    db.refresh(copy)
    warning = await sync(context, db, copy)
    if (body.copy_files or source.modpack_file) and warning is None:  # an uploaded pack lives in the files
        for _ in range(60):  # wait for the node to finish creating the container
            st = (await live_statuses(context, [copy]))[copy.uuid]
            if st["state"] != "installing":
                break
            await asyncio.sleep(2)
        try:
            await context.nodes.copy_from(copy.node, copy.uuid, source.uuid)
        except NodeError as exc:
            warning = f"Created, but copying files failed: {exc}"
    events.emit(context, db, "server.created", server=copy, user=user, detail=f"Copy of {source.name}")
    return {**_out(copy, user, {"state": "offline"}), "warning": warning}


# ----- can players join? -----------------------------------------------------------------------


def _is_local_host(host: str) -> bool:
    if host.lower() in ("localhost", "host.docker.internal") or host.lower().endswith((".local", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname; can't tell without resolving it
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10")


@router.get("/{server_id}/join-check")
async def join_check(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session),
                     context: PanelContext = Depends(ctx)):
    """Checks, in order, everything between "server created" and "a friend can join"."""
    server = server_for(server_id, user, db)
    template = get_template(server.template)
    status = (await live_statuses(context, [server]))[server.uuid]
    primary = server.primary_allocation
    address = server.public_address or (f"{server.node.public_host}:{primary.port}" if primary else None)
    protocols = template.ports[0].protocols if template.ports else ("tcp",)
    checks: list[dict[str, Any]] = []

    def add(key: str, ok: bool | None, title: str, detail: str = "") -> None:
        checks.append({"key": key, "ok": ok, "title": title, "detail": detail})

    node_ok = status["state"] != "node_offline"
    add("node", node_ok, "Node is reachable", "" if node_ok else status.get("error", ""))
    running = status["state"] in ("running", "sleeping")
    add("running", running, "Server is running" if running else "Server isn't running",
        "" if running else "Start it first; the remaining checks need it online.")
    public_host = server.node.public_host
    looks_local = _is_local_host(public_host)
    add("address", not looks_local if not server.public_address else True,
        f"Players connect to {address}",
        "This is a local address; friends outside your network can't use it. Set the node's public address, "
        "or use a playit.gg tunnel address." if looks_local and not server.public_address else "")

    reachable: bool | None = None
    detail = ""
    if running and "minecraft" in template.game.lower() and address:
        kind = "bedrock/" if "udp" in protocols else ""
        try:
            data = await context.http.get_json("mcsrvstat.us", f"https://api.mcsrvstat.us/{kind}3/{address}")
            reachable = bool(data.get("online"))
            if reachable:
                players = (data.get("players") or {})
                detail = f"Seen from the internet: {players.get('online', 0)}/{players.get('max', '?')} players."
            else:
                detail = ("The internet can't reach it. Usual causes, in order: the router isn't forwarding "
                          f"port {primary.port if primary else '?'}/{protocols[0].upper()} to this machine; a "
                          "firewall (Windows Firewall, ufw, or a cloud provider's security list) blocks it; or "
                          "your internet provider uses CGNAT, in which case use a playit.gg tunnel. "
                          "Results are cached for a few minutes.")
        except Exception as exc:
            reachable, detail = None, f"Couldn't run the outside check ({exc})."
    elif running:
        detail = "An outside check is only available for Minecraft; try joining from another network."
    add("internet", reachable, "Reachable from the internet" if reachable else "Reachability from the internet",
        detail)
    return {"address": address, "checks": checks}


# ----- console ---------------------------------------------------------------------------------


@router.websocket("/{server_id}/console")
async def console(ws: WebSocket, server_id: int) -> None:
    context: PanelContext = ws.app.state.ctx
    origin = ws.headers.get("origin")
    if origin and urlsplit(origin).netloc != ws.headers.get("host"):
        await ws.close(code=4403)
        return
    with context.db.sessionmaker() as db:
        user = websocket_user(ws, db)
        server = db.get(Server, server_id) if user else None
        if user is None or server is None or "console" not in permissions_for(server, user):
            await ws.close(code=4401)
            return
        url, token = console_ws_url(server.node, server.uuid), server.node.token
        user_id = user.id

    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException

    await ws.accept()
    try:
        async with connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                           open_timeout=6, max_size=4 * 1024 * 1024) as upstream:
            async def down() -> None:
                async for message in upstream:
                    await ws.send_text(message if isinstance(message, str) else message.decode())

            def still_allowed() -> tuple[User, Server] | None:
                """Access can be removed (or the password changed) while a console is open."""
                with context.db.sessionmaker() as check:
                    who = websocket_user(ws, check)
                    srv = check.get(Server, server_id)
                    if who is None or srv is None or "console" not in permissions_for(srv, who):
                        return None
                    return who, srv

            async def up() -> None:
                while True:
                    try:
                        message = json.loads(await ws.receive_text())
                    except ValueError:
                        continue
                    if not isinstance(message, dict) or message.get("type") != "command":
                        continue
                    text = str(message.get("data", ""))[:2000]
                    if not text.strip():
                        continue
                    if still_allowed() is None:
                        await ws.send_json({"type": "error", "data": "Your access to this console has ended."})
                        return
                    with context.db.sessionmaker() as log_db:  # same audit trail as the REST command
                        who, srv = log_db.get(User, user_id), log_db.get(Server, server_id)
                        events.emit(context, log_db, "console.command", server=srv, user=who, detail=text)
                    await upstream.send(json.dumps({"type": "command", "data": text}))

            async def watch() -> None:
                while True:
                    await asyncio.sleep(CONSOLE_RECHECK)
                    if still_allowed() is None:
                        with contextlib.suppress(Exception):
                            await ws.send_json({"type": "error", "data": "Your access to this console has ended."})
                        return

            tasks = [asyncio.create_task(down()), asyncio.create_task(up()), asyncio.create_task(watch())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:  # a closed browser or node ends the session; nothing to report
                task.exception()
    except (OSError, asyncio.TimeoutError, WebSocketException) as exc:
        log.info("console proxy for %s ended: %s", server_id, exc)
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "data": "Can't reach this server's node right now."})
    except Exception:  # client went away mid-send, etc.
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
