"""Moving a server to another node.

1. Stop it and take a backup on the old node.
2. Claim ports on the new node, point the server at it and create it there (empty).
3. Stream the backup from the old node to the new one through the panel, and restore it.
4. Remove it from the old node and start it again if it was running.

Until step 4 the old node keeps everything, so a failure anywhere before that puts the server back
where it was. Other local backups stay on the old node; ones with an off-site copy can be fetched
onto the new node from the Backups tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sqlalchemy import select

from ..models import Allocation, Backup, Node, Server
from . import events, updater
from .backups import BackupFailed, create_backup
from .node_client import NodeError
from .provisioning import ProvisionError, check_capacity, choose_allocations, claim
from ..templates import get_template

log = logging.getLogger("hearth.moves")

POLL = 2.0
RESTORE_TIMEOUT = 6 * 3600
moving: dict[int, str] = {}  # server id -> what's happening right now (shown in the UI)
_tasks: set[asyncio.Task] = set()


class MoveError(Exception):
    pass


def check(db, server: Server, target: Node) -> None:
    """Fail fast, before anything is touched."""
    if server.id in moving:
        raise MoveError("This server is already moving.")
    if target.id == server.node_id:
        raise MoveError("The server is already on that node.")
    if db.scalar(select(Backup).where(Backup.server_id == server.id, Backup.state == "creating")):
        raise MoveError("Wait for the running backup to finish first.")
    try:
        check_capacity(db, target, server.memory_mb, server.disk_mb)
        choose_allocations(target, get_template(server.template))
    except ProvisionError as exc:
        raise MoveError(str(exc)) from exc


def start(ctx, server_id: int, target_id: int, user_id: int | None) -> None:
    moving[server_id] = "Starting"
    task = asyncio.get_running_loop().create_task(_run(ctx, server_id, target_id, user_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(ctx, server_id: int, target_id: int, user_id: int | None) -> None:
    from ..models import User

    try:
        with ctx.db.sessionmaker() as db:
            server, target = db.get(Server, server_id), db.get(Node, target_id)
            user = db.get(User, user_id) if user_id else None
            source_name = server.node.name
            try:
                await _move(ctx, db, server, target)
            except Exception as exc:
                if not isinstance(exc, (MoveError, NodeError, BackupFailed, ProvisionError)):
                    log.exception("moving server %s failed", server_id)
                    exc = "something unexpected went wrong (see the panel log)"
                db.rollback()
                events.emit(ctx, db, "server.move_failed", server=server, user=user,
                            detail=f"Still on {source_name}: {exc}")
                return
            events.emit(ctx, db, "server.moved", server=server, user=user, detail=f"{source_name} → {target.name}")
    except Exception:
        log.exception("moving server %s failed", server_id)
    finally:
        moving.pop(server_id, None)


async def _move(ctx, db, server: Server, target: Node) -> None:
    source = server.node
    status = await ctx.nodes.status(source, server.uuid)
    was_running = status.get("state") in ("running", "starting", "sleeping")

    moving[server.id] = "Stopping and backing up"
    if was_running:
        await ctx.nodes.power(source, server.uuid, "stop")
        await _wait(ctx, source, server.uuid, lambda st: st.get("state") in ("offline", "missing"), 300)
    backup = await create_backup(ctx, db, server, kind="move", wait=True, name=f"Move to {target.name}")

    # Point the server at the new node: release the old ports, claim new ones.
    moving[server.id] = "Setting up on the new node"
    old = [(a.id, a.is_primary, a.is_template) for a in server.allocations]
    template = get_template(server.template)
    picks = choose_allocations(target, template)
    for alloc in list(server.allocations):
        alloc.server_id = None
    db.flush()
    for alloc, is_primary in picks:
        claim(db, alloc, server.id, is_primary=is_primary, is_template=not is_primary)
    server.node_id = target.id
    db.commit()
    db.refresh(server)
    try:
        await updater.push(ctx, db, server, start=False)

        moving[server.id] = "Copying files"
        resp = await ctx.nodes.backup_download(source, server.uuid, backup.uuid)
        try:
            length = int(resp.headers.get("content-length") or 0) or None
            await ctx.nodes.receive_backup(target, server.uuid, backup.uuid, resp.aiter_bytes(), length)
        finally:
            await resp.aclose()

        moving[server.id] = "Restoring on the new node"
        await ctx.nodes.backup_restore(target, server.uuid, backup.uuid)
        final = await _wait(ctx, target, server.uuid, lambda st: st.get("state") not in ("restoring", "installing"),
                            RESTORE_TIMEOUT, first_delay=POLL)
        if final.get("error") and "Restore failed" in str(final.get("error")):
            raise MoveError(final["error"])
    except BaseException:
        # Put it back: old node, old ports. The old node still has everything.
        with contextlib.suppress(NodeError):
            await ctx.nodes.delete(target, server.uuid, purge=True)
        for alloc in list(server.allocations):
            alloc.server_id = None
        db.flush()
        for alloc_id, is_primary, is_template in old:
            row = db.get(Allocation, alloc_id)
            row.server_id, row.is_primary, row.is_template = server.id, is_primary, is_template
        server.node_id = source.id
        db.commit()
        if was_running:
            with contextlib.suppress(NodeError):
                await ctx.nodes.power(source, server.uuid, "start")
        raise

    # It's safely on the new node. Local backups that weren't transferred stay on the old one.
    moving[server.id] = "Cleaning up"
    for row in db.scalars(select(Backup).where(Backup.server_id == server.id, Backup.id != backup.id)):
        if row.state == "ready":
            row.state, row.error = "failed", (f"Left on {source.name} when the server moved."
                                              + (" Use “Get from off-site” to bring it here." if row.offsite_state == "done" else ""))
    db.commit()
    with contextlib.suppress(NodeError):
        await ctx.nodes.delete(source, server.uuid, purge=True)
    if was_running:
        with contextlib.suppress(NodeError):
            await ctx.nodes.power(target, server.uuid, "start")


async def _wait(ctx, node, uuid: str, done, timeout: float, first_delay: float = 0.0) -> dict:
    await asyncio.sleep(first_delay)
    waited = first_delay
    while True:
        try:
            st = await ctx.nodes.status(node, uuid)
            if done(st):
                return st
        except NodeError:
            pass
        if waited >= timeout:
            raise MoveError("Timed out waiting for the node.")
        await asyncio.sleep(POLL)
        waited += POLL
