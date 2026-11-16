"""Modpack catalogs: Modrinth and CurseForge, normalised to one shape."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from ..templates import parse_mc_version
from .cache import TTLCache
from .http import PublicHTTP, UpstreamError

Provider = Literal["modrinth", "curseforge"]
Sort = Literal["relevance", "downloads", "updated", "newest"]

MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
CF_MINECRAFT = 432
CF_MODPACKS = 4471
CF_SORT = {"relevance": 2, "downloads": 6, "updated": 3, "newest": 11}
CF_LOADERS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
CF_RELEASE = {1: "release", 2: "beta", 3: "alpha"}
LOADER_NAMES = {"forge", "neoforge", "fabric", "quilt"}
_MC_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

SEARCH_TTL = 10 * 60
VERSIONS_TTL = 15 * 60


class CurseForgeKeyMissing(UpstreamError):
    def __init__(self) -> None:
        super().__init__("CurseForge", "no API key configured; an admin can add one in Admin → Settings", 412)


def _pack(**kw: Any) -> dict[str, Any]:
    keys = ("provider", "id", "slug", "name", "summary", "author", "icon_url", "downloads",
            "updated", "categories", "game_versions", "loaders", "url")
    return {k: kw.get(k) for k in keys}


class ModpackService:
    def __init__(self, http: PublicHTTP, cache: TTLCache) -> None:
        self.http = http
        self.cache = cache
        self.curseforge_key: str | None = None

    # ----- public API ---------------------------------------------------------------------

    async def search(self, provider: Provider, query: str = "", sort: Sort = "downloads",
                     game_version: str | None = None, loader: str | None = None,
                     offset: int = 0, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        offset = max(0, min(offset, 9_950))
        key = f"mp:search:{provider}:{json.dumps([query, sort, game_version, loader, offset, limit])}"
        if provider == "modrinth":
            loader_fn = lambda: self._modrinth_search(query, sort, game_version, loader, offset, limit)  # noqa: E731
        else:
            self._require_cf()
            loader_fn = lambda: self._cf_search(query, sort, game_version, loader, offset, limit)  # noqa: E731
        return await self.cache.get(key, SEARCH_TTL, loader_fn)

    async def project(self, provider: Provider, project_id: str) -> dict[str, Any]:
        key = f"mp:project:{provider}:{project_id}"
        if provider == "modrinth":
            return await self.cache.get(key, VERSIONS_TTL, lambda: self._modrinth_project(project_id))
        self._require_cf()
        return await self.cache.get(key, VERSIONS_TTL, lambda: self._cf_project(project_id))

    async def versions(self, provider: Provider, project_id: str, fresh: bool = False) -> list[dict[str, Any]]:
        """All versions of a pack, newest first."""
        key = f"mp:versions:{provider}:{project_id}"
        if fresh:
            self.cache.invalidate(key)
        if provider == "modrinth":
            return await self.cache.get(key, VERSIONS_TTL, lambda: self._modrinth_versions(project_id))
        self._require_cf()
        return await self.cache.get(key, VERSIONS_TTL, lambda: self._cf_versions(project_id))

    # ----- Modrinth -----------------------------------------------------------------------

    async def _modrinth_search(self, query, sort, game_version, loader, offset, limit) -> dict[str, Any]:
        facets = [["project_type:modpack"]]
        if game_version:
            facets.append([f"versions:{game_version}"])
        if loader:
            facets.append([f"categories:{loader.lower()}"])
        data = await self.http.get_json("Modrinth", f"{MODRINTH}/search", params={
            "query": query, "facets": json.dumps(facets), "index": sort,
            "offset": offset, "limit": limit,
        })
        hits = [
            _pack(provider="modrinth", id=h["project_id"], slug=h.get("slug"), name=h.get("title"),
                  summary=h.get("description"), author=h.get("author"), icon_url=h.get("icon_url") or None,
                  downloads=h.get("downloads", 0), updated=h.get("date_modified"),
                  categories=[c for c in h.get("categories", []) if c not in LOADER_NAMES],
                  game_versions=_newest_mc(h.get("versions", [])),
                  loaders=[c for c in h.get("categories", []) if c in LOADER_NAMES],
                  url=f"https://modrinth.com/modpack/{h.get('slug') or h['project_id']}")
            for h in data.get("hits", [])
        ]
        return {"total": data.get("total_hits", len(hits)), "offset": offset, "limit": limit, "hits": hits}

    async def _modrinth_project(self, project_id: str) -> dict[str, Any]:
        p = await self.http.get_json("Modrinth", f"{MODRINTH}/project/{_safe_id(project_id)}")
        if p.get("project_type") != "modpack":
            raise UpstreamError("Modrinth", "that project is not a modpack", 400)
        return _pack(provider="modrinth", id=p["id"], slug=p.get("slug"), name=p.get("title"),
                     summary=p.get("description"), author=None, icon_url=p.get("icon_url") or None,
                     downloads=p.get("downloads", 0), updated=p.get("updated"),
                     categories=p.get("categories", []), game_versions=_newest_mc(p.get("game_versions", [])),
                     loaders=p.get("loaders", []), url=f"https://modrinth.com/modpack/{p.get('slug')}")

    async def _modrinth_versions(self, project_id: str) -> list[dict[str, Any]]:
        data = await self.http.get_json("Modrinth", f"{MODRINTH}/project/{_safe_id(project_id)}/version")
        versions = [
            {
                "id": v["id"],
                "name": v.get("name") or v.get("version_number"),
                "version_number": v.get("version_number"),
                "game_versions": v.get("game_versions", []),
                "loaders": v.get("loaders", []),
                "type": v.get("version_type", "release"),
                "published": v.get("date_published"),
                "downloads": v.get("downloads", 0),
            }
            for v in data
        ]
        versions.sort(key=lambda v: v["published"] or "", reverse=True)
        return versions

    # ----- CurseForge ---------------------------------------------------------------------

    def _require_cf(self) -> None:
        if not self.curseforge_key:
            raise CurseForgeKeyMissing()

    def _cf_headers(self) -> dict[str, str]:
        return {"x-api-key": self.curseforge_key or ""}

    async def _cf_search(self, query, sort, game_version, loader, offset, limit) -> dict[str, Any]:
        params: dict[str, Any] = {
            "gameId": CF_MINECRAFT, "classId": CF_MODPACKS, "searchFilter": query,
            "sortField": CF_SORT.get(sort, 2), "sortOrder": "desc", "index": offset, "pageSize": limit,
        }
        if game_version:
            params["gameVersion"] = game_version
        if loader and loader.lower() in CF_LOADERS:
            params["modLoaderType"] = CF_LOADERS[loader.lower()]
        data = await self.http.get_json("CurseForge", f"{CURSEFORGE}/mods/search", params=params,
                                        headers=self._cf_headers())
        hits = [self._cf_pack(m) for m in data.get("data", [])]
        page = data.get("pagination", {})
        return {"total": min(page.get("totalCount", len(hits)), 10_000), "offset": offset, "limit": limit, "hits": hits}

    def _cf_pack(self, m: dict[str, Any]) -> dict[str, Any]:
        files = m.get("latestFilesIndexes", [])
        game_versions = _newest_mc([f.get("gameVersion", "") for f in files])
        loaders = sorted({
            name for f in files
            for name, num in CF_LOADERS.items() if f.get("modLoader") == num
        })
        return _pack(provider="curseforge", id=str(m["id"]), slug=m.get("slug"), name=m.get("name"),
                     summary=m.get("summary"), author=(m.get("authors") or [{}])[0].get("name"),
                     icon_url=(m.get("logo") or {}).get("thumbnailUrl") or None,
                     downloads=int(m.get("downloadCount") or 0), updated=m.get("dateModified"),
                     categories=[c.get("name") for c in m.get("categories", [])][:4],
                     game_versions=game_versions, loaders=loaders,
                     url=(m.get("links") or {}).get("websiteUrl"))

    async def _cf_project(self, project_id: str) -> dict[str, Any]:
        data = await self.http.get_json("CurseForge", f"{CURSEFORGE}/mods/{_safe_id(project_id)}",
                                        headers=self._cf_headers())
        mod = data.get("data", {})
        if mod.get("classId") != CF_MODPACKS:
            raise UpstreamError("CurseForge", "that project is not a modpack", 400)
        return self._cf_pack(mod)

    async def _cf_versions(self, project_id: str) -> list[dict[str, Any]]:
        versions: list[dict[str, Any]] = []
        index = 0
        while index < 1000:  # 20 pages of 50 is plenty of history for a picker
            data = await self.http.get_json(
                "CurseForge", f"{CURSEFORGE}/mods/{_safe_id(project_id)}/files",
                params={"index": index, "pageSize": 50}, headers=self._cf_headers())
            batch = data.get("data", [])
            for f in batch:
                if f.get("isServerPack"):
                    continue
                gv = f.get("gameVersions", [])
                versions.append({
                    "id": str(f["id"]),
                    "name": f.get("displayName") or f.get("fileName"),
                    "version_number": f.get("fileName"),
                    "game_versions": [g for g in gv if _MC_RE.match(g)],
                    "loaders": [g.lower() for g in gv if g.lower() in LOADER_NAMES],
                    "type": CF_RELEASE.get(f.get("releaseType"), "release"),
                    "published": f.get("fileDate"),
                    "downloads": f.get("downloadCount", 0),
                    "has_server_pack": bool(f.get("serverPackFileId")),
                })
            total = (data.get("pagination") or {}).get("totalCount", 0)
            index += 50
            if not batch or index >= total:
                break
        versions.sort(key=lambda v: v["published"] or "", reverse=True)
        return versions


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", value or ""):
        raise UpstreamError("modpacks", "invalid project id", 400)
    return value


def _newest_mc(versions: list[str]) -> list[str]:
    releases = {v for v in versions if _MC_RE.match(v or "")}
    return sorted(releases, key=lambda v: parse_mc_version(v) or (0, 0, 0), reverse=True)[:6]
