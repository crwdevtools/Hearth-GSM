"""Background modpack update checks: record available updates and apply them when enabled."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Server
from . import events, settings_store
from .backups import BackupFailed, create_backup
from .http import UpstreamError
from .node_client import NodeError
from .provisioning import ProvisionError, build_spec

log = logging.getLogger("hearth.updater")


async def check_server(ctx, db: Session, server: Server, *, apply: bool | None = None) -> dict[str, Any]:
    """Check one modpack server. `apply=None` follows the server's auto_update flag."""
    if not server.modpack_provider or not server.modpack_project_id:
        return {"checked": False, "reason": "not a modpack server"}
    versions = await ctx.modpacks.versions(server.modpack_provider, server.modpack_project_id, fresh=True)
    latest = newer_release(versions, server.modpack_version_id)
    server.last_update_check = datetime.now(timezone.utc)
    if latest is None:
        server.update_version_id = server.update_version_name = None
        db.commit()
        return {"checked": True, "update": None}

    newly_found = server.update_version_id != latest["id"]
    server.update_version_id, server.update_version_name = latest["id"], latest["name"]
    should_apply = server.auto_update if apply is None else apply
    if newly_found and not should_apply:
        events.emit(ctx, db, "modpack.update_available", server=server,
                    detail=f"{latest['name']} (running {server.modpack_version_name})", commit=False)
    result: dict[str, Any] = {"checked": True, "update": {"id": latest["id"], "name": latest["name"]}, "applied": False}
    if should_apply:
        db.commit()
        try:  # never update without a way back
            await create_backup(ctx, db, server, kind="pre-update", wait=True, keep=5,
                                name=f"Before updating to {latest['name']}")
        except BackupFailed as exc:
            result["error"] = f"Update skipped: the safety backup failed ({exc})."
            events.emit(ctx, db, "schedule.failed", server=server, detail=result["error"])
            return result
        previous = (server.modpack_version_id, server.modpack_version_name, server.modpack_mc_version)
        old_name = server.modpack_version_name
        set_modpack_version(server, latest)
        try:
            await push(ctx, db, server, announce=f"Updating modpack to {latest['name']}; restarting now.")
        except (NodeError, ProvisionError) as exc:
            server.modpack_version_id, server.modpack_version_name, server.modpack_mc_version = previous
            server.update_version_id, server.update_version_name = latest["id"], latest["name"]
            db.commit()
            result["error"] = str(exc)
            return result
        result["applied"] = True
        events.emit(ctx, db, "modpack.updated", server=server, detail=f"{old_name} → {latest['name']}",
                    commit=False)
    db.commit()
    return result


def newer_release(versions: list[dict[str, Any]], current_id: str | None) -> dict[str, Any] | None:
    """The newest *release* published after the installed version, or None.

    A server pinned to a beta newer than the latest release is not offered an older release.
    """
    latest = next((v for v in versions if v["type"] == "release"), None)
    if latest is None or latest["id"] == current_id:
        return None
    current = next((v for v in versions if v["id"] == current_id), None)
    if current and (latest.get("published") or "") <= (current.get("published") or ""):
        return None
    return latest


def set_modpack_version(server: Server, version: dict[str, Any]) -> None:
    server.modpack_version_id = version["id"]
    server.modpack_version_name = version["name"]
    if version.get("game_versions"):
        server.modpack_mc_version = version["game_versions"][0]
    server.update_version_id = server.update_version_name = None


async def push(ctx, db: Session, server: Server, *, start: bool = False, announce: str | None = None) -> None:
    """Send the server's current spec to its node. The agent recreates the container and keeps it
    running if it was running."""
    spec = build_spec(server, settings_store.curseforge_key(db, ctx.settings))
    if announce and server.template == "minecraft-java":
        with contextlib.suppress(NodeError):
            await ctx.nodes.command(server.node, server.uuid, f"say [Hearth] {announce}")
    await ctx.nodes.apply(server.node, spec, start=start)


async def check_all(ctx) -> int:
    with ctx.db.sessionmaker() as db:
        servers = db.scalars(select(Server).where(Server.modpack_provider.is_not(None))).all()
        checked = 0
        for server in servers:
            try:
                await check_server(ctx, db, server)
                checked += 1
            except UpstreamError as exc:
                log.warning("update check for %s failed: %s", server.name, exc)
                db.rollback()
        return checked


async def run_forever(ctx) -> None:
    await asyncio.sleep(30)  # let nodes come up after a panel restart
    while True:
        try:
            count = await check_all(ctx)
            log.info("modpack update check finished for %d servers", count)
        except Exception:
            log.exception("modpack update loop failed")
        with ctx.db.sessionmaker() as db:
            minutes = settings_store.update_interval(db, ctx.settings)
        await asyncio.sleep(minutes * 60)
