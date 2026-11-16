"""Minecraft server versions per server software, fetched live and cached."""

from __future__ import annotations

from typing import Any

from ..templates import parse_mc_version
from .cache import TTLCache
from .http import PublicHTTP, UpstreamError

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_V3 = "https://fill.papermc.io/v3/projects/paper"
PAPER_V2 = "https://api.papermc.io/v2/projects/paper"
PURPUR = "https://api.purpurmc.org/v2/purpur"
FABRIC = "https://meta.fabricmc.net/v2/versions/game"
QUILT = "https://meta.quiltmc.org/v3/versions/game"
FORGE = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"

TTL = 30 * 60


def neoforge_to_minecraft(version: str) -> str | None:
    """Map a NeoForge version to its Minecraft version.

    20.4.237 -> 1.20.4, 21.0.10-beta -> 1.21, 21.1.77 -> 1.21.1, 26.1.0.3 -> 26.1, 26.1.1.0 -> 26.1.1
    """
    parts = version.split("-", 1)[0].split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:3]):
        return None
    major, minor = int(parts[0]), int(parts[1])
    if major >= 26:  # NeoForge adopted Minecraft's year-based numbering
        patch = int(parts[2]) if len(parts) > 2 else 0
        return f"{major}.{minor}" + (f".{patch}" if patch else "")
    return f"1.{major}" + (f".{minor}" if minor else "")


def _sort_key(version: str) -> tuple[int, int, int]:
    return parse_mc_version(version) or (0, 0, 0)


class VersionService:
    def __init__(self, http: PublicHTTP, cache: TTLCache) -> None:
        self.http = http
        self.cache = cache

    async def _manifest(self) -> dict[str, Any]:
        return await self.cache.get(
            "mc:manifest", TTL, lambda: self.http.get_json("Mojang", MOJANG_MANIFEST)
        )

    async def list(self, server_type: str, include_snapshots: bool = False) -> list[dict[str, Any]]:
        server_type = server_type.upper()
        if server_type == "VANILLA":
            manifest = await self._manifest()
            return [
                {"id": v["id"], "type": v["type"], "released": v.get("releaseTime")}
                for v in manifest.get("versions", [])
                if v["type"] == "release" or (include_snapshots and v["type"] == "snapshot")
            ]
        loader = {
            "PAPER": self._paper,
            "PURPUR": self._purpur,
            "FABRIC": lambda: self._meta_game(FABRIC, "Fabric", include_snapshots),
            "QUILT": lambda: self._meta_game(QUILT, "Quilt", include_snapshots),
            "FORGE": self._forge,
            "NEOFORGE": self._neoforge,
        }.get(server_type)
        if loader is None:
            raise UpstreamError("versions", f"unknown server type {server_type}", 400)
        ids: list[str] = await self.cache.get(f"mc:{server_type}:{include_snapshots}", TTL, loader)
        return await self._decorate(ids)

    async def _decorate(self, ids: list[str]) -> list[dict[str, Any]]:
        """Attach release dates from Mojang's manifest and order newest first."""
        try:
            manifest = await self._manifest()
            meta = {v["id"]: v for v in manifest.get("versions", [])}
            order = {v["id"]: i for i, v in enumerate(manifest.get("versions", []))}
        except UpstreamError:
            meta, order = {}, {}
        unique = list(dict.fromkeys(ids))
        unique.sort(key=lambda v: (order.get(v, 10**6), tuple(-x for x in _sort_key(v))))
        return [
            {"id": v, "type": meta.get(v, {}).get("type", "release"), "released": meta.get(v, {}).get("releaseTime")}
            for v in unique
        ]

    async def _paper(self) -> list[str]:
        try:
            data = await self.http.get_json("PaperMC", PAPER_V3)
            groups = data.get("versions", {})
            return [v for group in groups.values() for v in group]
        except UpstreamError:
            data = await self.http.get_json("PaperMC", PAPER_V2)
            return list(reversed(data.get("versions", [])))

    async def _purpur(self) -> list[str]:
        data = await self.http.get_json("Purpur", PURPUR)
        return list(reversed(data.get("versions", [])))

    async def _meta_game(self, url: str, service: str, include_snapshots: bool) -> list[str]:
        data = await self.http.get_json(service, url)
        return [v["version"] for v in data if v.get("stable") or include_snapshots]

    async def _forge(self) -> list[str]:
        data = await self.http.get_json("Forge", FORGE)
        versions = {key.rsplit("-", 1)[0] for key in data.get("promos", {})}
        return sorted(versions, key=_sort_key, reverse=True)

    async def _neoforge(self) -> list[str]:
        data = await self.http.get_json("NeoForge", NEOFORGE)
        mapped = {neoforge_to_minecraft(v) for v in data.get("versions", [])}
        mapped.discard(None)
        return sorted(mapped, key=_sort_key, reverse=True)  # type: ignore[arg-type]
