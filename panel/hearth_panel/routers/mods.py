"""Install, list and remove single mods/plugins."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, server_with
from ..models import Server, User
from ..services import events, mods
from ..services.node_client import NodeError

router = APIRouter(prefix="/api/servers/{server_id}/mods", tags=["mods"])
access = server_with("settings")


class InstallBody(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9]{1,32}$")


async def _target(context, server):
    try:
        return await mods.target_for(context, server)
    except mods.NotModdable as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def installed(server: Server = Depends(access), context: PanelContext = Depends(ctx)):
    target = await _target(context, server)
    try:
        entries = await context.nodes.files(server.node, server.uuid, target.folder)
    except NodeError as exc:
        if exc.status != 404:
            raise
        entries = []  # folder appears after the first start
    jars = [{"name": e["name"], "size": e["size"], "modified": e["modified"]}
            for e in entries if not e["is_dir"] and e["name"].endswith((".jar", ".jar.disabled"))]
    return {"target": {"kind": target.kind, "loaders": target.loaders, "game_version": target.game_version,
                       "folder": target.folder}, "installed": jars}


@router.get("/search")
async def search(query: str = Query("", max_length=100), offset: int = Query(0, ge=0),
                 server: Server = Depends(access), context: PanelContext = Depends(ctx)):
    return await mods.search(context, await _target(context, server), query.strip(), offset)


@router.post("", status_code=201)
async def install(body: InstallBody, server: Server = Depends(access), user: User = Depends(current_user),
                  db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    target = await _target(context, server)
    names = await mods.install(context, server, target, body.project_id)
    events.emit(context, db, "mods.installed", server=server, user=user, detail=", ".join(names))
    return {"installed": names, "restart_needed": True}


@router.delete("/{name}", status_code=204)
async def remove(name: str, server: Server = Depends(access), user: User = Depends(current_user),
                 db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    if "/" in name or not name.endswith((".jar", ".jar.disabled")):
        raise HTTPException(400, "Not a mod file.")
    target = await _target(context, server)
    await context.nodes.delete_file(server.node, server.uuid, f"{target.folder}/{name}")
    events.emit(context, db, "mods.removed", server=server, user=user, detail=name)
