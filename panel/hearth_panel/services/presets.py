"""One-click starting points: a Minecraft server with a useful set of plugins or mods.

Presets install from Modrinth by slug when the server is created, before its first start.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Server
from . import mods
from .http import UpstreamError
from .node_client import NodeError

BEDROCK_PORT = 19132


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    description: str
    software: tuple[str, ...]  # server TYPEs it works with
    projects: tuple[str, ...]  # Modrinth slugs
    bedrock_port: bool = False


PRESETS: dict[str, Preset] = {p.key: p for p in (
    Preset("geyser", "Java + Bedrock (Geyser)",
           "Geyser and Floodgate let Bedrock players (phones, consoles, Windows) join without a Java account.",
           ("PAPER", "PURPUR"), ("geyser", "floodgate"), bedrock_port=True),
    Preset("fabric-performance", "Fabric performance pack",
           "Lithium, FerriteCore, Krypton and ModernFix: faster ticks and less memory, no gameplay changes.",
           ("FABRIC",), ("lithium", "ferrite-core", "krypton", "modernfix")),
)}


class PresetError(ValueError):
    pass


def check(preset_key: str, template: str, variables: dict, modpack: bool) -> Preset:
    preset = PRESETS.get(preset_key)
    if preset is None:
        raise PresetError("Unknown preset.")
    software = str(variables.get("TYPE") or "").upper()
    if template != "minecraft-java" or modpack or software not in preset.software:
        raise PresetError(f"{preset.name} needs a Minecraft: Java server running {' or '.join(s.title() for s in preset.software)}.")
    return preset


def claim_bedrock_port(db: Session, server: Server) -> str | None:
    """Geyser listens on UDP 19132. Give the server that port too, if it's free on the node."""
    primary = server.primary_allocation
    alloc = next((a for a in server.node.allocations
                  if a.port == BEDROCK_PORT and a.ip == primary.ip and a.server_id is None), None)
    if alloc is None:
        return (f"Bedrock players need port {BEDROCK_PORT} (UDP), which isn't free on {server.node.name}. "
                f"Add it under Admin → Nodes, then add it to this server under Network.")
    alloc.server_id, alloc.is_primary, alloc.is_template = server.id, False, False
    alloc.note = "Bedrock (Geyser)"
    db.flush()
    return None


async def install(ctx, db: Session, server: Server, preset: Preset) -> str | None:
    """Install the preset's projects. Returns a warning instead of raising."""
    try:
        target = await mods.target_for(ctx, server)
        installed = await mods.installed_projects(ctx, server, target)
        results = []
        for slug in preset.projects:
            version = await mods.best_version(ctx, slug, target)
            if version is None:
                return f"{slug} has no version for Minecraft {target.game_version} yet; the rest were installed."
            results += await mods.install(ctx, server, target, version.get("project_id") or slug, installed=installed)
        mods.record(db, server, results)
        db.commit()
    except (mods.NotModdable, UpstreamError, NodeError) as exc:
        return f"The server was created, but the preset's {'plugins' if 'PAPER' in preset.software else 'mods'} weren't all installed: {exc}"
    return None


def public() -> list[dict]:
    return [{"key": p.key, "name": p.name, "description": p.description, "software": list(p.software)}
            for p in PRESETS.values()]
