"""Single mods and plugins from Modrinth, matched to what a server runs."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Server, ServerMod
from . import events
from .http import UpstreamError
from .node_client import NodeError

log = logging.getLogger("hearth.mods")

MODRINTH = "https://api.modrinth.com/v2"

# Server software -> (Modrinth project type, loaders accepted, folder)
SOFTWARE = {
    "PAPER": ("plugin", ["paper", "spigot", "bukkit"], "/plugins"),
    "PURPUR": ("plugin", ["purpur", "paper", "spigot", "bukkit"], "/plugins"),
    "FABRIC": ("mod", ["fabric"], "/mods"),
    "QUILT": ("mod", ["quilt", "fabric"], "/mods"),
    "FORGE": ("mod", ["forge"], "/mods"),
    "NEOFORGE": ("mod", ["neoforge"], "/mods"),
}


@dataclass
class Target:
    kind: str  # "mod" | "plugin"
    loaders: list[str]
    folder: str
    game_version: str


class NotModdable(Exception):
    pass


async def target_for(ctx, server: Server) -> Target:
    if server.template != "minecraft-java":
        raise NotModdable("Mods and plugins are for Minecraft: Java servers.")
    if server.modpack_provider:
        loaders: list[str] = []
        if server.modpack_project_id:
            versions = await ctx.modpacks.versions(server.modpack_provider, server.modpack_project_id)
            current = next((v for v in versions if v["id"] == server.modpack_version_id), None)
            loaders = [l for l in (current or {}).get("loaders", []) if l in ("fabric", "forge", "neoforge", "quilt")]
        if not loaders or not server.modpack_mc_version:
            raise NotModdable("Couldn't tell which mod loader this modpack uses.")
        return Target("mod", loaders, "/mods", server.modpack_mc_version)
    software = str((server.variables or {}).get("TYPE", "VANILLA")).upper()
    if software not in SOFTWARE:
        raise NotModdable("Vanilla servers can't load mods or plugins. Switch to Paper (plugins) or Fabric/"
                          "Forge/NeoForge (mods) under Software.")
    kind, loaders, folder = SOFTWARE[software]
    version = str((server.variables or {}).get("VERSION") or "LATEST")
    if version.upper() == "LATEST":
        listed = await ctx.versions.list(software)
        if not listed:
            raise NotModdable("Couldn't work out which Minecraft version “Latest” means right now.")
        version = listed[0]["id"]
    return Target(kind, loaders, folder, version)


async def search(ctx, target: Target, query: str, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    facets = [[f"project_type:{target.kind}"], [f"categories:{l}" for l in target.loaders],
              [f"versions:{target.game_version}"], ["server_side:required", "server_side:optional"]]
    data = await ctx.http.get_json("Modrinth", f"{MODRINTH}/search", params={
        "query": query, "facets": json.dumps(facets), "index": "relevance" if query else "downloads",
        "offset": max(0, offset), "limit": max(1, min(limit, 50))})
    return {
        "total": data.get("total_hits", 0),
        "target": {"kind": target.kind, "loaders": target.loaders, "game_version": target.game_version,
                   "folder": target.folder},
        "hits": [{"id": h["project_id"], "slug": h.get("slug"), "name": h.get("title"), "summary": h.get("description"),
                  "author": h.get("author"), "icon_url": h.get("icon_url") or None, "downloads": h.get("downloads", 0),
                  "url": f"https://modrinth.com/{target.kind}/{h.get('slug') or h['project_id']}"}
                 for h in data.get("hits", [])],
    }


async def best_version(ctx, project_id: str, target: Target) -> dict[str, Any] | None:
    versions = await ctx.http.get_json("Modrinth", f"{MODRINTH}/project/{project_id}/version", params={
        "loaders": json.dumps(target.loaders), "game_versions": json.dumps([target.game_version])})
    releases = [v for v in versions if v.get("version_type") == "release"] or versions
    return releases[0] if releases else None


async def installed_projects(ctx, server: Server, target: Target) -> dict[str, dict[str, Any]]:
    """Which Modrinth projects are already in the mods/plugins folder, identified by file hash
    (so it works whatever the files are called): {project_id: {"filename", "version_id"}}."""
    try:
        hashes = await ctx.nodes.file_hashes(server.node, server.uuid, target.folder)
    except NodeError as exc:
        if exc.status == 404:
            return {}  # the folder appears on first start
        raise
    if not hashes:
        return {}
    data = await ctx.http.post_json("Modrinth", f"{MODRINTH}/version_files",
                                    {"hashes": list(hashes.values()), "algorithm": "sha512"})
    by_hash = {digest: name for name, digest in hashes.items()}
    found: dict[str, dict[str, Any]] = {}
    for digest, version in (data or {}).items():
        if isinstance(version, dict) and version.get("project_id") and digest in by_hash:
            found[version["project_id"]] = {"filename": by_hash[digest], "version_id": version.get("id")}
    return found


async def install(ctx, server: Server, target: Target, project_id: str, *,
                  installed: dict[str, dict[str, Any]] | None = None, dependency: bool = False,
                  _seen: set[str] | None = None) -> list[dict[str, Any]]:
    """Install a project and its required dependencies.

    A dependency that's already present (in any version) is left alone, so a second copy of e.g.
    Fabric API never appears. Installing a project that's already there replaces the old file.
    Returns one entry per project: {project_id, filename, version_id, dependency, action}, where
    action is "installed", "updated", "current" (already this version) or "present".
    """
    seen = _seen if _seen is not None else set()
    if project_id in seen:
        return []
    seen.add(project_id)
    if installed is None:
        installed = await installed_projects(ctx, server, target)
    existing = installed.get(project_id)
    if existing and dependency:
        return [{"project_id": project_id, "filename": existing["filename"], "version_id": existing["version_id"],
                 "dependency": True, "action": "present"}]
    version = await best_version(ctx, project_id, target)
    if version is None:
        raise UpstreamError("Modrinth", f"no version for Minecraft {target.game_version} "
                                        f"({'/'.join(target.loaders)})", 404)
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else None)
    if file is None:
        raise UpstreamError("Modrinth", "that version has no downloadable file", 404)
    name = file["filename"].replace("/", "_")
    if existing and existing["version_id"] == version.get("id"):
        action = "current"
        name = existing["filename"]
    else:
        await ctx.nodes.fetch(server.node, server.uuid, file["url"], f"{target.folder}/{name}",
                              (file.get("hashes") or {}).get("sha512"))
        action = "updated" if existing else "installed"
        if existing and existing["filename"] != name:  # the old version's file, under its old name
            try:
                await ctx.nodes.delete_file(server.node, server.uuid, f"{target.folder}/{existing['filename']}")
            except NodeError as exc:
                if exc.status != 404:
                    raise
        installed[project_id] = {"filename": name, "version_id": version.get("id")}
    results = [{"project_id": project_id, "filename": name, "version_id": version.get("id"),
                "dependency": dependency, "action": action}]
    for dep in version.get("dependencies") or []:
        if dep.get("dependency_type") == "required" and dep.get("project_id"):
            results += await install(ctx, server, target, dep["project_id"], installed=installed,
                                     dependency=True, _seen=seen)
    return results


def record(db: Session, server: Server, results: list[dict[str, Any]]) -> None:
    """Remember what was installed through Hearth (see ServerMod)."""
    for r in results:
        if r["action"] == "present":
            continue
        row = db.scalar(select(ServerMod).where(ServerMod.server_id == server.id,
                                                ServerMod.project_id == r["project_id"]))
        if row is None:
            row = ServerMod(server_id=server.id, project_id=r["project_id"], dependency=r["dependency"])
            db.add(row)
        elif not r["dependency"]:
            row.dependency = False  # installed on purpose now, even if it first came as a dependency
        row.filename, row.version_id = r["filename"], r["version_id"]
    db.commit()


# ----- after a modpack update -----------------------------------------------------------------------

RESTORE_WAIT = 900  # seconds to wait for the updated server to come up
RESTORE_POLL = 10.0
_tasks: set[asyncio.Task] = set()


def restore_after_update(ctx, server_id: int) -> None:
    """In the background: once the updated server is running, put back any mods the user added
    that the modpack update removed (installing versions that fit the new Minecraft version)."""
    task = asyncio.get_running_loop().create_task(_restore(ctx, server_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _restore(ctx, server_id: int) -> list[str]:
    from .status import live_statuses

    waited = 0.0
    while waited < RESTORE_WAIT:
        with ctx.db.sessionmaker() as db:
            server = db.get(Server, server_id)
            if server is None:
                return []
            state = (await live_statuses(ctx, [server]))[server.uuid].get("state")
        if state == "running":
            break
        await asyncio.sleep(RESTORE_POLL)
        waited += RESTORE_POLL
    else:
        log.warning("not restoring mods on server %s: it didn't come back up", server_id)
        return []
    with ctx.db.sessionmaker() as db:
        server = db.get(Server, server_id)
        rows = list(db.scalars(select(ServerMod).where(ServerMod.server_id == server_id,
                                                       ServerMod.dependency.is_(False))))
        if server is None or not rows:
            return []
        try:
            target = await target_for(ctx, server)
            installed = await installed_projects(ctx, server, target)
            missing = [r for r in rows if r.project_id not in installed]
            if not missing:
                return []
            restored: list[str] = []
            for row in missing:
                try:
                    results = await install(ctx, server, target, row.project_id, installed=installed)
                except UpstreamError as exc:
                    events.emit(ctx, db, "mods.restore_failed", server=server,
                                detail=f"{row.filename}: {exc}")
                    continue
                record(db, server, results)
                restored += [r["filename"] for r in results if r["action"] in ("installed", "updated")]
        except (NodeError, UpstreamError, NotModdable) as exc:
            log.warning("restoring mods on %s failed: %s", server.name, exc)
            return []
        if restored:
            events.emit(ctx, db, "mods.restored", server=server,
                        detail=f"Put back after the modpack update: {', '.join(restored)}")
            try:
                await ctx.nodes.power(server.node, server.uuid, "restart")  # load them
            except NodeError as exc:
                log.warning("restart after restoring mods on %s failed: %s", server.name, exc)
        return restored
