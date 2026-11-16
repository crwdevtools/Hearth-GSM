"""Server files: browse, edit, upload, download, and import/export worlds."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, server_with
from ..models import Server, User
from ..services import events
from ..services.backups import BackupFailed, create_backup
from ..templates import get_template

router = APIRouter(prefix="/api/servers/{server_id}/files", tags=["files"])
files_access = server_with("files")


class FileBody(BaseModel):
    path: str = Field(max_length=1024)
    content: str = Field(default="", max_length=5_000_000)


class RenameBody(BaseModel):
    source: str = Field(max_length=1024)
    target: str = Field(max_length=1024)


class WorldImport(BaseModel):
    upload: str = Field(max_length=1024)  # path of an uploaded .zip/.tar.gz in the server folder
    backup_first: bool = True


def _stream(resp, filename: str, media_type: str) -> StreamingResponse:
    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", filename) or "download"
    headers = {"Content-Disposition": f'attachment; filename="{safe}"'}
    # aiter_bytes() undoes any Content-Encoding, so only pass the length through when there is none.
    if resp.headers.get("content-length") and not resp.headers.get("content-encoding"):
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(body(), media_type=media_type, headers=headers)


def world_folder(server: Server) -> str:
    template = get_template(server.template)
    if not template.world_folder_var:
        raise HTTPException(400, "This game doesn't have a single world folder to import or export.")
    name = (server.variables or {}).get(template.world_folder_var) or "world"
    return f"/{name}"


@router.get("")
async def list_files(path: str = Query("/", max_length=1024), server: Server = Depends(files_access),
                     context: PanelContext = Depends(ctx)):
    return await context.nodes.files(server.node, server.uuid, path)


@router.get("/content")
async def read_file(path: str = Query(..., max_length=1024), server: Server = Depends(files_access),
                    context: PanelContext = Depends(ctx)):
    return {"content": await context.nodes.read_file(server.node, server.uuid, path)}


@router.put("/content", status_code=204)
async def write_file(body: FileBody, server: Server = Depends(files_access), user: User = Depends(current_user),
                     db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    await context.nodes.write_file(server.node, server.uuid, body.path, body.content)
    events.emit(context, db, "file.saved", server=server, user=user, detail=body.path)


@router.post("/mkdir", status_code=204)
async def mkdir(body: FileBody, server: Server = Depends(files_access), context: PanelContext = Depends(ctx)) -> None:
    await context.nodes.mkdir(server.node, server.uuid, body.path)


@router.post("/rename", status_code=204)
async def rename(body: RenameBody, server: Server = Depends(files_access), user: User = Depends(current_user),
                 db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    await context.nodes.rename(server.node, server.uuid, body.source, body.target)
    events.emit(context, db, "file.renamed", server=server, user=user, detail=f"{body.source} → {body.target}")


@router.delete("", status_code=204)
async def delete_file(path: str = Query(..., max_length=1024), server: Server = Depends(files_access),
                      user: User = Depends(current_user), db: Session = Depends(db_session),
                      context: PanelContext = Depends(ctx)) -> None:
    await context.nodes.delete_file(server.node, server.uuid, path)
    events.emit(context, db, "file.deleted", server=server, user=user, detail=path)


@router.put("/upload", status_code=204)
async def upload(request: Request, path: str = Query(..., max_length=1024), server: Server = Depends(files_access),
                 user: User = Depends(current_user), db: Session = Depends(db_session),
                 context: PanelContext = Depends(ctx)) -> None:
    """Stream the request body straight to the node (no size limit beyond the node's free disk)."""
    length = int(request.headers.get("content-length") or 0) or None
    await context.nodes.upload(server.node, server.uuid, path, request.stream(), length)
    events.emit(context, db, "file.uploaded", server=server, user=user, detail=path)


@router.get("/download")
async def download(path: str = Query(..., max_length=1024), server: Server = Depends(files_access),
                   context: PanelContext = Depends(ctx)):
    resp = await context.nodes.download(server.node, server.uuid, path)
    return _stream(resp, path.rstrip("/").split("/")[-1], "application/octet-stream")


@router.get("/archive")
async def archive(path: str = Query("/", max_length=1024), server: Server = Depends(files_access),
                  context: PanelContext = Depends(ctx)):
    resp = await context.nodes.archive(server.node, server.uuid, path)
    name = path.strip("/").split("/")[-1] or server.name
    return _stream(resp, f"{name}.zip", "application/zip")


@router.get("/world")
async def export_world(server: Server = Depends(files_access), context: PanelContext = Depends(ctx)):
    resp = await context.nodes.archive(server.node, server.uuid, world_folder(server))
    return _stream(resp, f"{server.name}-world.zip", "application/zip")


@router.post("/world")
async def import_world(body: WorldImport, server: Server = Depends(files_access), user: User = Depends(current_user),
                       db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    """Replace the world with an uploaded archive (the archive may hold the world in a subfolder)."""
    target = world_folder(server)
    if body.backup_first:
        try:
            await create_backup(context, db, server, kind="pre-restore", wait=True, keep=3, user=user,
                                name="Before importing a world")
        except BackupFailed as exc:
            raise HTTPException(409, f"Not imported: the safety backup failed ({exc}).") from exc
    result = await context.nodes.extract(server.node, server.uuid, body.upload, target, replace=True, world=True)
    events.emit(context, db, "world.imported", server=server, user=user, detail=target)
    return result


@router.post("/upload-name")
def upload_name(filename: str = Query(..., max_length=255), server: Server = Depends(files_access)):
    """A safe temporary name for staging an import upload."""
    ext = ".tar.gz" if filename.lower().endswith(".tar.gz") else "." + filename.rsplit(".", 1)[-1].lower()
    if ext not in (".zip", ".tar.gz", ".tgz"):
        raise HTTPException(415, "Upload a .zip, .tar.gz or .tgz file.")
    return {"path": f"/.hearth-upload-{secrets.token_hex(6)}{ext}"}
