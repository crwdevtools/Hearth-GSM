"""Background watcher: turns status changes into events, enforces suspensions, and puts empty
servers to sleep."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from ..models import Server
from . import events
from .node_client import NodeError
from .status import live_statuses, parse_player_list

log = logging.getLogger("hearth.monitor")

INTERVAL = 15  # seconds between status sweeps
PLAYER_CHECK = 60  # seconds between player counts for auto-sleep


@dataclass
class Seen:
    state: str
    restarts: int = 0
    oom_kills: int = 0
    empty_since: float | None = None
    last_player_check: float = 0.0
    players: list[str] = field(default_factory=list)


class Monitor:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.seen: dict[int, Seen] = {}

    async def run_forever(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("monitor sweep failed")
            await asyncio.sleep(INTERVAL)

    async def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self.ctx.db.sessionmaker() as db:
            servers = list(db.scalars(select(Server)))
            statuses = await live_statuses(self.ctx, servers)
            for server in servers:
                st = statuses.get(server.uuid, {})
                await self._observe(db, server, st, now)

    async def _observe(self, db, server: Server, st: dict, now: float) -> None:
        state = st.get("state", "unknown")
        if state in ("node_offline", "unknown"):
            return  # can't tell what happened; don't invent events
        restarts, ooms = int(st.get("restarts") or 0), int(st.get("oom_kills") or 0)
        prev = self.seen.get(server.id)
        self.seen[server.id] = cur = Seen(state, restarts, ooms,
                                          prev.empty_since if prev else None,
                                          prev.last_player_check if prev else 0.0,
                                          prev.players if prev else [])
        if prev is None:
            return  # first sight after a panel restart: no history to compare

        emit = lambda action, detail="": events.emit(self.ctx, db, action, server=server, detail=detail)  # noqa: E731
        if ooms > prev.oom_kills:
            emit("server.out_of_memory", st.get("error") or "")
        elif restarts > prev.restarts:
            emit("server.crashed", f"Docker restarted it (crash #{restarts}).")
        if prev.state == "sleeping" and state in ("starting", "running"):
            emit("server.woke", "Someone tried to join.")
        elif state == "running" and prev.state in ("starting", "offline", "installing", "restoring"):
            emit("server.started")
        elif state == "offline" and prev.state in ("running", "stopping", "starting") and ooms == prev.oom_kills:
            emit("server.stopped")
        elif state == "sleeping" and prev.state != "sleeping":
            emit("server.sleeping", "Nobody was online. It wakes up when someone joins.")

        if server.suspended and state in ("running", "starting"):
            await self._safe(server, lambda: self.ctx.nodes.power(server.node, server.uuid, "stop"))
            return
        await self._auto_sleep(db, server, cur, state, now)

    async def _auto_sleep(self, db, server: Server, cur: Seen, state: str, now: float) -> None:
        if not server.auto_stop_minutes or server.template != "minecraft-java" or state != "running":
            cur.empty_since = None
            return
        if now - cur.last_player_check < PLAYER_CHECK:
            return
        cur.last_player_check = now
        try:
            parsed = parse_player_list(await self.ctx.nodes.command(server.node, server.uuid, "list"))
        except NodeError:
            return
        if parsed is None:
            return
        online, _, names = parsed
        cur.players = names
        if online:
            cur.empty_since = None
            return
        cur.empty_since = cur.empty_since or now
        if now - cur.empty_since < server.auto_stop_minutes * 60:
            return
        cur.empty_since = None
        if server.wake_on_join:
            await self._safe(server, lambda: self.ctx.nodes.sleep(server.node, server.uuid))
        else:
            await self._safe(server, lambda: self.ctx.nodes.power(server.node, server.uuid, "stop"))
            events.emit(self.ctx, db, "server.stopped",
                        server=server, detail=f"Nobody was online for {server.auto_stop_minutes} minutes.")

    async def _safe(self, server: Server, call) -> None:
        try:
            await call()
        except NodeError as exc:
            log.warning("monitor action on %s failed: %s", server.name, exc)
