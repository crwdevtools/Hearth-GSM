"""Single mods and plugins from Modrinth, matched to what a server runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..models import Server
from .http import UpstreamError

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


async def install(ctx, server: Server, target: Target, project_id: str, _seen: set[str] | None = None) -> list[str]:
    """Install a project and its required dependencies. Returns installed file names."""
    seen = _seen if _seen is not None else set()
    if project_id in seen:
        return []
    seen.add(project_id)
    version = await best_version(ctx, project_id, target)
    if version is None:
        raise UpstreamError("Modrinth", f"no version for Minecraft {target.game_version} "
                                        f"({'/'.join(target.loaders)})", 404)
    files = version.get("files") or []
    file = next((f for f in files if f.get("primary")), files[0] if files else None)
    if file is None:
        raise UpstreamError("Modrinth", "that version has no downloadable file", 404)
    name = file["filename"].replace("/", "_")
    await ctx.nodes.fetch(server.node, server.uuid, file["url"], f"{target.folder}/{name}",
                          (file.get("hashes") or {}).get("sha512"))
    installed = [name]
    for dep in version.get("dependencies") or []:
        if dep.get("dependency_type") == "required" and dep.get("project_id"):
            installed += await install(ctx, server, target, dep["project_id"], seen)
    return installed
