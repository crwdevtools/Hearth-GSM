"""Templates, Minecraft versions, and modpack catalogs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..context import PanelContext
from ..deps import current_user, ctx
from ..models import User
from ..templates import TEMPLATES

router = APIRouter(prefix="/api", tags=["catalog"])

ProviderParam = Literal["modrinth", "curseforge"]


@router.get("/templates")
def templates(_: User = Depends(current_user)):
    return [t.public() for t in TEMPLATES.values()]


@router.get("/minecraft/versions")
async def minecraft_versions(
    type: str = Query("VANILLA", pattern=r"^[A-Za-z]{3,12}$"),
    snapshots: bool = False,
    _: User = Depends(current_user),
    context: PanelContext = Depends(ctx),
):
    return await context.versions.list(type, include_snapshots=snapshots)


@router.get("/modpacks/providers")
def providers(_: User = Depends(current_user), context: PanelContext = Depends(ctx)):
    return {"modrinth": True, "curseforge": bool(context.modpacks.curseforge_key)}


@router.get("/modpacks/search")
async def search_modpacks(
    provider: ProviderParam = "modrinth",
    query: str = Query("", max_length=100),
    sort: Literal["relevance", "downloads", "updated", "newest"] = "downloads",
    game_version: str | None = Query(None, pattern=r"^\d+\.\d+(\.\d+)?$"),
    loader: Literal["forge", "neoforge", "fabric", "quilt"] | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=50),
    _: User = Depends(current_user),
    context: PanelContext = Depends(ctx),
):
    return await context.modpacks.search(provider, query.strip(), sort, game_version, loader, offset, limit)


@router.get("/modpacks/{provider}/{project_id}")
async def modpack(provider: ProviderParam, project_id: str, _: User = Depends(current_user),
                  context: PanelContext = Depends(ctx)):
    return await context.modpacks.project(provider, project_id)


@router.get("/modpacks/{provider}/{project_id}/versions")
async def modpack_versions(provider: ProviderParam, project_id: str, _: User = Depends(current_user),
                           context: PanelContext = Depends(ctx)):
    return await context.modpacks.versions(provider, project_id)
