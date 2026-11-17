"""Off-site copies of backups in S3-compatible storage, and getting them back.

Credentials live in the panel's settings (Admin → Settings). After a backup finishes, the panel
presigns upload URLs (a single PUT, or one per part for big archives), the node uploads, and the
panel completes the multipart upload. Deleting or pruning a backup deletes its off-site copy.
"""

from __future__ import annotations

import asyncio
import logging
import math

from sqlalchemy.orm import Session

from ..models import Backup
from . import events, s3, settings_store
from .node_client import NodeError

log = logging.getLogger("hearth.offsite")

KEYS = ("endpoint", "region", "bucket", "access_key", "secret_key", "prefix", "path_style")
POLL = 3.0
TIMEOUT = 12 * 3600
_tasks: set[asyncio.Task] = set()


def _key(name: str) -> str:
    return f"offsite.{name}"


def target(db: Session) -> s3.Target | None:
    values = {k: settings_store.get(db, _key(k)) for k in KEYS}
    if not (values["endpoint"] and values["bucket"] and values["access_key"] and values["secret_key"]):
        return None
    return s3.Target(endpoint=values["endpoint"], region=values["region"] or "us-east-1", bucket=values["bucket"],
                     access_key=values["access_key"], secret_key=values["secret_key"],
                     prefix=values["prefix"] if values["prefix"] is not None else "hearth/",
                     path_style=values["path_style"] != "false")


def settings_out(db: Session) -> dict:
    t = target(db)
    return {
        "configured": t is not None,
        "endpoint": settings_store.get(db, _key("endpoint")) or "",
        "region": settings_store.get(db, _key("region")) or "us-east-1",
        "bucket": settings_store.get(db, _key("bucket")) or "",
        "access_key": settings_store.get(db, _key("access_key")) or "",
        "secret_set": bool(settings_store.get(db, _key("secret_key"))),
        "prefix": settings_store.get(db, _key("prefix")) if settings_store.get(db, _key("prefix")) is not None else "hearth/",
        "path_style": settings_store.get(db, _key("path_style")) != "false",
    }


def save(db: Session, values: dict) -> None:
    """None leaves a value alone; "" clears it. Clearing the endpoint turns off-site copies off."""
    for name in KEYS:
        if values.get(name) is None:
            continue
        value = values[name]
        if name == "path_style":
            value = "true" if value else "false"
        settings_store.put(db, _key(name), str(value).strip() or None)
    db.commit()


def start(ctx, backup_id: int) -> None:
    """Copy a finished backup off-site in the background (if off-site storage is set up)."""
    task = asyncio.get_running_loop().create_task(_push(ctx, backup_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _push(ctx, backup_id: int) -> None:
    with ctx.db.sessionmaker() as db:
        t = target(db)
        row = db.get(Backup, backup_id)
        if t is None or row is None or row.state != "ready":
            return
        server = row.server
        key = t.key(server.uuid, f"{row.uuid}.tar.gz")
        upload_id = None
        try:
            if row.size_bytes > s3.SINGLE_PUT_LIMIT:
                parts = math.ceil(row.size_bytes / s3.PART_SIZE)
                if parts > 10_000:
                    raise s3.S3Error("this backup is too large for the storage's upload limits")
                upload_id = await s3.start_multipart(ctx.http.client, t, key)
                urls, part_size = s3.part_urls(t, key, upload_id, parts), s3.PART_SIZE
            else:
                urls, part_size = [s3.presign(t, "PUT", key, expires=12 * 3600)], max(row.size_bytes, 5 * 1024 * 1024)
            row.offsite_state, row.offsite_key, row.offsite_error = "uploading", key, None
            db.commit()
            await ctx.nodes.backup_push(server.node, server.uuid, row.uuid, urls, part_size)
            push = await _wait_for_push(ctx, server, row)
            if push.get("state") != "done":
                raise s3.S3Error(push.get("error") or "the node couldn't upload it")
            if upload_id:
                await s3.complete_multipart(ctx.http.client, t, key, upload_id, push.get("etags") or [])
            row.offsite_state = "done"
            db.commit()
        except (s3.S3Error, NodeError) as exc:
            if upload_id:
                await s3.abort_multipart(ctx.http.client, t, key, upload_id)
            row.offsite_state, row.offsite_error = "failed", str(exc)[:500]
            db.commit()
            events.emit(ctx, db, "backup.offsite_failed", server=server, detail=f"{row.name}: {exc}")


async def _wait_for_push(ctx, server, row: Backup) -> dict:
    waited = 0.0
    while waited < TIMEOUT:
        await asyncio.sleep(POLL)
        waited += POLL
        try:
            meta = await ctx.nodes.backup_status(server.node, server.uuid, row.uuid)
        except NodeError:
            continue
        push = meta.get("push") or {}
        if push.get("state") in ("done", "failed"):
            return push
    return {"state": "failed", "error": "timed out waiting for the node to upload"}


async def remove(ctx, db: Session, row: Backup) -> None:
    """Delete a backup's off-site copy, if it has one (best effort: storage may be unreachable)."""
    t = target(db)
    if t is None or not row.offsite_key or row.offsite_state not in ("done", "uploading", "failed"):
        return
    try:
        await s3.delete(ctx.http.client, t, row.offsite_key)
    except s3.S3Error as exc:
        log.warning("couldn't delete off-site copy %s: %s", row.offsite_key, exc)


async def fetch(ctx, db: Session, row: Backup) -> None:
    """Have the node download the off-site copy (when its own copy is gone)."""
    t = target(db)
    if t is None:
        raise s3.S3Error("Off-site storage isn't set up.")
    if row.offsite_state != "done" or not row.offsite_key:
        raise s3.S3Error("This backup has no off-site copy.")
    server = row.server
    await ctx.nodes.backup_pull(server.node, server.uuid, row.uuid, s3.presign(t, "GET", row.offsite_key, expires=12 * 3600))
    row.state = "creating"
    db.commit()
    from .backups import watch  # finishes the row like a new backup (no new off-site copy)

    watch(ctx, row.id, offsite=False)


async def test(ctx, t: s3.Target) -> None:
    """Write, read back and delete a small object, so a typo shows up now rather than at 3 AM."""
    s3.validate(t)
    key = t.key(".hearth-test")
    await s3.put_bytes(ctx.http.client, t, key, b"hearth")
    if await s3.get_bytes(ctx.http.client, t, key) != b"hearth":
        raise s3.S3Error("wrote a test file but read back something else")
    await s3.delete(ctx.http.client, t, key)
