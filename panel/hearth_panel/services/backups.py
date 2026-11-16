"""Panel-side backup bookkeeping: records, waiting for the node, retention, events."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Backup, Server, User
from . import events
from .node_client import NodeError

log = logging.getLogger("hearth.backups")

POLL = 2.0
TIMEOUT = 3 * 3600  # a very large world on a slow disk can take a long time

_tasks: set[asyncio.Task] = set()


class BackupFailed(Exception):
    pass


async def create_backup(ctx, db: Session, server: Server, *, name: str | None = None, kind: str = "manual",
                        user: User | None = None, wait: bool = False, keep: int | None = None) -> Backup:
    """Start a backup on the node. With wait=True, return once it finished (raises BackupFailed)."""
    running = db.scalar(select(Backup).where(Backup.server_id == server.id, Backup.state == "creating"))
    if running is not None:
        raise BackupFailed("A backup of this server is already in progress.")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    row = Backup(server_id=server.id, name=(name or f"{kind.replace('-', ' ').capitalize()} {stamp}")[:128], kind=kind)
    db.add(row)
    db.commit()
    try:
        await ctx.nodes.backup(server.node, server.uuid, row.uuid)
    except NodeError as exc:
        row.state, row.error = "failed", str(exc)
        db.commit()
        raise BackupFailed(str(exc)) from exc
    events.emit(ctx, db, "backup.started", server=server, user=user, detail=row.name)
    job = _finish(ctx, row.id, keep)
    if wait:
        await job
        db.refresh(row)
        if row.state != "ready":
            raise BackupFailed(row.error or "Backup failed.")
    else:
        task = asyncio.get_running_loop().create_task(job)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return row


async def _finish(ctx, backup_id: int, keep: int | None) -> None:
    waited = 0.0
    while waited < TIMEOUT:
        await asyncio.sleep(POLL)
        waited += POLL
        with ctx.db.sessionmaker() as db:
            row = db.get(Backup, backup_id)
            if row is None:
                return
            server = row.server
            try:
                meta = await ctx.nodes.backup_status(server.node, server.uuid, row.uuid)
            except NodeError as exc:
                if exc.status == 404:  # the node has no record of it (e.g. its files were wiped)
                    meta = {"state": "failed", "error": "The node has no record of this backup."}
                else:
                    continue  # node blip; keep waiting
            if meta.get("state") == "creating":
                continue
            if meta.get("state") == "ready":
                row.state, row.size_bytes, row.error = "ready", int(meta.get("size") or 0), None
                db.commit()
                events.emit(ctx, db, "backup.completed", server=server,
                            detail=f"{row.name} ({row.size_bytes / 1048576:.0f} MB)")
                if keep:
                    await prune(ctx, db, server, keep, kind=row.kind)
            else:
                row.state, row.error = "failed", meta.get("error") or "The node lost track of this backup."
                db.commit()
                events.emit(ctx, db, "backup.failed", server=server, detail=row.error)
            return
    with ctx.db.sessionmaker() as db:
        row = db.get(Backup, backup_id)
        if row is not None and row.state == "creating":
            row.state, row.error = "failed", "Timed out waiting for the node."
            db.commit()


def resume_pending(ctx) -> int:
    """After a panel restart, keep watching backups that were still being made; otherwise they
    would stay "creating" forever and block every new backup of that server."""
    with ctx.db.sessionmaker() as db:
        ids = list(db.scalars(select(Backup.id).where(Backup.state == "creating")))
    loop = asyncio.get_running_loop()
    for backup_id in ids:
        task = loop.create_task(_finish(ctx, backup_id, None))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return len(ids)


async def prune(ctx, db: Session, server: Server, keep: int, kind: str) -> int:
    """Delete the oldest unlocked backups of one kind beyond `keep`."""
    rows = list(db.scalars(select(Backup).where(Backup.server_id == server.id, Backup.kind == kind,
                                                Backup.state == "ready", Backup.locked.is_(False))
                           .order_by(Backup.created_at.desc())))
    removed = 0
    for row in rows[keep:]:
        try:
            await ctx.nodes.backup_delete(server.node, server.uuid, row.uuid)
        except NodeError as exc:
            log.warning("couldn't prune backup %s: %s", row.uuid, exc)
            continue
        db.delete(row)
        removed += 1
    db.commit()
    return removed
