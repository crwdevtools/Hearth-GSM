"""Backups: list, create, download, lock, restore, delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, server_with
from ..models import Backup, Server, User
from ..services import events, offsite, s3
from ..services.backups import BackupFailed, create_backup
from ..services.node_client import NodeError
from .files import _stream

router = APIRouter(prefix="/api/servers/{server_id}/backups", tags=["backups"])
access = server_with("backups")


class BackupCreate(BaseModel):
    name: str | None = Field(default=None, max_length=128)


class BackupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    locked: bool | None = None


class RestoreBody(BaseModel):
    backup_first: bool = True


def backup_out(b: Backup) -> dict:
    return {"id": b.id, "name": b.name, "kind": b.kind, "state": b.state, "size": b.size_bytes,
            "error": b.error, "locked": b.locked, "created_at": b.created_at.isoformat(),
            "offsite": b.offsite_state, "offsite_error": b.offsite_error}


def _get(server: Server, backup_id: int) -> Backup:
    row = next((b for b in server.backups if b.id == backup_id), None)
    if row is None:
        raise HTTPException(404, "Backup not found.")
    return row


@router.get("")
def list_backups(server: Server = Depends(access)):
    return [backup_out(b) for b in server.backups]


@router.post("", status_code=202)
async def create(body: BackupCreate, server: Server = Depends(access), user: User = Depends(current_user),
                 db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    try:
        row = await create_backup(context, db, server, name=body.name, kind="manual", user=user)
    except BackupFailed as exc:
        raise HTTPException(409, str(exc)) from exc
    return backup_out(row)


@router.patch("/{backup_id}")
def update(backup_id: int, body: BackupPatch, server: Server = Depends(access), db: Session = Depends(db_session)):
    row = _get(server, backup_id)
    if body.name is not None:
        row.name = body.name
    if body.locked is not None:
        row.locked = body.locked
    db.commit()
    return backup_out(row)


@router.get("/{backup_id}/download")
async def download(backup_id: int, server: Server = Depends(access), context: PanelContext = Depends(ctx)):
    row = _get(server, backup_id)
    if row.state != "ready":
        raise HTTPException(409, "That backup isn't finished.")
    resp = await context.nodes.backup_download(server.node, server.uuid, row.uuid)
    return _stream(resp, f"{server.name}-{row.created_at:%Y%m%d-%H%M}.tar.gz", "application/gzip")


@router.post("/{backup_id}/restore", status_code=202)
async def restore(backup_id: int, body: RestoreBody, server: Server = Depends(access),
                  user: User = Depends(current_user), db: Session = Depends(db_session),
                  context: PanelContext = Depends(ctx)):
    row = _get(server, backup_id)
    if row.state != "ready":
        raise HTTPException(409, "Only finished backups can be restored.")
    if body.backup_first:
        try:
            await create_backup(context, db, server, kind="pre-restore", wait=True, keep=3, user=user,
                                name=f"Before restoring “{row.name}”")
        except BackupFailed as exc:
            raise HTTPException(409, f"Not restored: the safety backup failed ({exc}).") from exc
    await context.nodes.backup_restore(server.node, server.uuid, row.uuid)
    events.emit(context, db, "backup.restored", server=server, user=user, detail=row.name)
    return {"accepted": True}


@router.post("/{backup_id}/fetch", status_code=202)
async def fetch(backup_id: int, server: Server = Depends(access), db: Session = Depends(db_session),
                context: PanelContext = Depends(ctx)):
    """Download the off-site copy back onto the node, e.g. after the node's disk was replaced."""
    row = _get(server, backup_id)
    if row.state == "creating":
        raise HTTPException(409, "That backup is busy.")
    try:
        await offsite.fetch(context, db, row)
    except s3.S3Error as exc:
        raise HTTPException(409, str(exc)) from exc
    return backup_out(row)


@router.post("/{backup_id}/offsite", status_code=202)
async def copy_offsite(backup_id: int, server: Server = Depends(access), db: Session = Depends(db_session),
                       context: PanelContext = Depends(ctx)):
    """Copy (or retry copying) one backup off-site."""
    row = _get(server, backup_id)
    if row.state != "ready":
        raise HTTPException(409, "Only finished backups can be copied off-site.")
    if offsite.target(db) is None:
        raise HTTPException(409, "Set up off-site storage in Admin → Settings first.")
    row.offsite_state = None
    db.commit()
    offsite.start(context, row.id)
    return backup_out(row)


@router.delete("/{backup_id}", status_code=204)
async def delete(backup_id: int, server: Server = Depends(access), user: User = Depends(current_user),
                 db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    row = _get(server, backup_id)
    if row.locked:
        raise HTTPException(409, "Unlock this backup before deleting it.")
    if row.state == "creating":
        raise HTTPException(409, "That backup is still being made.")
    try:
        await context.nodes.backup_delete(server.node, server.uuid, row.uuid)
    except NodeError as exc:
        if exc.status != 404:
            raise
    await offsite.remove(context, db, row)
    events.emit(context, db, "backup.deleted", server=server, user=user, detail=row.name, commit=False)
    db.delete(row)
    db.commit()
