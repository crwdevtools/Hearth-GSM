"""Background watcher: turns status changes into events, enforces suspensions, and puts empty
servers to sleep."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from ..models import Server
from . import events, mcping, metrics, playtime
from urllib.parse import urlsplit
from ..templates import get_template
from .node_client import NodeError
from .status import live_statuses, parse_player_list

log = logging.getLogger("hearth.monitor")

INTERVAL = 15  # seconds between status sweeps
PLAYER_CHECK = 60  # seconds between player counts for auto-sleep
LIST_EVERY = 300  # seconds between full `list` reads while players are online and the ping count is steady
LOW_TPS = 15.0  # TPS under this for LOW_TPS_MINUTES recorded minutes in a row raises an alert
LOW_TPS_MINUTES = 5
FILL_WARN_DAYS = 3
TICKS_IDLE = 300  # seconds between tick-speed reads while nobody is online (every PLAYER_CHECK when someone is)
READING_FRESH = 400  # seconds a player count or tick reading counts as current
TICK_TIMEOUT = 12  # seconds for the whole tick-speed read of one server
TICK_GIVE_UP = 3  # unreadable replies in a row before a server is left alone (an old version, say)
NODE_CHECK = 60  # seconds between node health checks
OFFLINE_AFTER = 2  # failed node checks in a row before "node offline" is announced
CRASH_LOOP = (3, 600)  # this many crashes within this many seconds: stop it and tell someone
DISK_WARN, DISK_RESET = 0.90, 0.85  # share of a server's disk limit
NODE_DISK_LOW = (0.10, 5 * 1024**3)  # node free space below 10 % or 5 GB (whichever is larger)


@dataclass
class Seen:
    state: str
    restarts: int = 0
    oom_kills: int = 0
    empty_since: float | None = None
    last_player_check: float = -1e9
    players: list[str] = field(default_factory=list)
    quiet_stop: bool = False  # the monitor stopped it and already announced why
    crashes: list[float] = field(default_factory=list)
    disk_level: int = 0  # 0 fine, 1 warned, 2 full (announce each step once)
    last_count: int | None = None  # players online at the last `list`, not yet acted on by auto-sleep
    # What the console charts show, carried from one sweep to the next.
    online: int | None = None
    online_at: float | None = None
    list_at: float = -1e9
    max_players: int | None = None
    tps: float | None = None
    mspt: float | None = None
    ticks_at: float = -1e9
    tick_cmd: str | None = None  # the command that worked on this server
    tick_miss: int = 0
    tick_off: bool = False
    mspt_off: bool = False
    metric_minute: int = 0
    low_tps_run: int = 0
    ping_miss: int = 0
    ping_skip_until: float = -1e9
    low_tps_sent: bool = False
    fill_checked: float = -1e9
    fill_sent: bool = False


CARRIED = ("online", "online_at", "list_at", "max_players", "tps", "mspt", "ticks_at", "tick_cmd", "tick_miss", "tick_off",
           "mspt_off", "metric_minute", "ping_miss", "ping_skip_until", "low_tps_run", "low_tps_sent", "fill_checked", "fill_sent")


@dataclass
class NodeSeen:
    failures: int = 0
    announced_offline: bool = False
    disk_low: bool = False
    checked: float = -1e9


class Monitor:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.seen: dict[int, Seen] = {}
        self.nodes: dict[int, NodeSeen] = {}
        self.pruned = -1e9

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
            await self._check_nodes(db, now)
            if now - self.pruned > 86400:
                self.pruned = now
                playtime.prune(db)
                metrics.prune(db)

    async def _check_nodes(self, db, now: float) -> None:
        from ..models import Node

        nodes = [n for n in db.scalars(select(Node))
                 if now - self.nodes.setdefault(n.id, NodeSeen()).checked >= NODE_CHECK]

        async def probe(node):
            try:
                return node, await self.ctx.nodes.system(node), None
            except NodeError as exc:
                return node, None, exc

        for node, system, error in await asyncio.gather(*(probe(n) for n in nodes)):
            seen = self.nodes[node.id]
            seen.checked = now
            if system is None:
                seen.failures += 1
                if seen.failures >= OFFLINE_AFTER and not seen.announced_offline:
                    seen.announced_offline = True
                    events.emit(self.ctx, db, "node.offline", node=node, detail=str(error))
                continue
            if seen.announced_offline:
                events.emit(self.ctx, db, "node.online", node=node)
            seen.failures, seen.announced_offline = 0, False
            total, free = int(system.get("disk_total") or 0), int(system.get("disk_free") or 0)
            if total:
                low = free < max(total * NODE_DISK_LOW[0], NODE_DISK_LOW[1])
                if low and not seen.disk_low:
                    events.emit(self.ctx, db, "node.disk_low", node=node,
                                detail=f"{free / 1024**3:.1f} GB free of {total / 1024**3:.0f} GB.")
                seen.disk_low = low if low else free < max(total * NODE_DISK_LOW[0] * 1.5, NODE_DISK_LOW[1])

    async def _observe(self, db, server: Server, st: dict, now: float) -> None:
        state = st.get("state", "unknown")
        if state in ("node_offline", "unknown"):
            return  # can't tell what happened; don't invent events
        restarts, ooms = int(st.get("restarts") or 0), int(st.get("oom_kills") or 0)
        prev = self.seen.get(server.id)
        self.seen[server.id] = cur = Seen(state, restarts, ooms,
                                          prev.empty_since if prev else None,
                                          prev.last_player_check if prev else -1e9,
                                          prev.players if prev else [],
                                          prev.quiet_stop if prev else False,
                                          prev.crashes if prev else [],
                                          prev.disk_level if prev else 0,
                                          prev.last_count if prev else None)
        if prev is not None:
            for name in CARRIED:
                setattr(cur, name, getattr(prev, name))
        if state != "running":  # a restart may bring a different version: read the ticks afresh
            cur.online = cur.tps = cur.mspt = None
            cur.tick_miss, cur.tick_off, cur.mspt_off, cur.tick_cmd = 0, False, False, None
            cur.ping_miss, cur.ping_skip_until = 0, -1e9
        self._check_disk(db, server, st, cur)
        if prev is None:
            # First sight after a panel restart: no history to compare. But a server that stopped while
            # the panel was down must not keep sessions open (they would count playtime forever).
            if state in ("offline", "sleeping", "missing") and "players" in get_template(server.template).features:
                playtime.observe(db, server, None)
            return

        emit = lambda action, detail="": events.emit(self.ctx, db, action, server=server, detail=detail)  # noqa: E731
        if ooms > prev.oom_kills:
            emit("server.out_of_memory", st.get("error") or "")
        elif restarts > prev.restarts:
            emit("server.crashed", f"Docker restarted it (crash #{restarts}).")
        if restarts > prev.restarts or ooms > prev.oom_kills:
            cur.crashes = [t for t in cur.crashes if now - t < CRASH_LOOP[1]] + [now]
            if len(cur.crashes) >= CRASH_LOOP[0] and not server.in_maintenance():
                cur.crashes = []
                await self._safe(server, lambda: self.ctx.nodes.power(server.node, server.uuid, "stop"))
                cur.quiet_stop = True
                emit("server.crash_loop", f"It crashed {CRASH_LOOP[0]} times in {CRASH_LOOP[1] // 60} minutes, so "
                                          "Hearth stopped it. Check the console and crash reports before starting it again.")
        if prev.state == "sleeping" and state in ("starting", "running"):
            emit("server.woke", "Someone tried to join.")
        elif state == "running" and prev.state in ("starting", "offline", "installing", "restoring"):
            emit("server.started")
        elif state == "offline" and prev.state in ("running", "stopping", "starting") and ooms == prev.oom_kills:
            if not prev.quiet_stop:
                emit("server.stopped")
            cur.quiet_stop = False
        elif state == "sleeping" and prev.state != "sleeping":
            emit("server.sleeping", "Nobody was online. It wakes up when someone joins.")

        if server.suspended and state in ("running", "starting"):
            await self._safe(server, lambda: self.ctx.nodes.power(server.node, server.uuid, "stop"))
            return
        await self._track_players(db, server, cur, prev, state, now)
        self._record_metric(db, server, st, cur, state, now)
        if server.in_maintenance():
            cur.empty_since = None
            return
        await self._auto_sleep(db, server, cur, state, now)

    async def _track_players(self, db, server: Server, cur: Seen, prev: Seen, state: str, now: float) -> None:
        """Playtime: who's online, once a minute (the same `list` reply also feeds auto-sleep)."""
        if "players" not in get_template(server.template).features:
            return
        if state != "running":
            if prev.state == "running":
                playtime.observe(db, server, None)
                cur.players = []
            return
        if now - cur.last_player_check < PLAYER_CHECK:
            return
        cur.last_player_check = now
        pinged = await self._ping(server, cur, now)
        if pinged is not None and pinged[0] == cur.online == 0 and cur.players == [] and now - cur.online_at < READING_FRESH:
            # Still empty: no console command needed (keeps the server log quiet).
            cur.online_at, cur.max_players = now, pinged[1]
            cur.last_count = 0
            playtime.observe(db, server, [])
            await self._track_ticks(server, cur, now)
            return
        if pinged is not None and pinged[0] == cur.online and cur.players and now - cur.list_at < LIST_EVERY:
            cur.online_at, cur.max_players = now, pinged[1]  # same head-count: names haven't changed much
            await self._track_ticks(server, cur, now)
            return
        try:
            parsed = parse_player_list(await self.ctx.nodes.command(server.node, server.uuid, "list"))
        except NodeError:
            return
        if parsed is None:
            if pinged is not None:
                # A plugin that rewrites `list` (EssentialsX, say) leaves nothing to parse; the head-count
                # still drives auto-sleep and the charts, only the names (and so playtime) are unknown.
                cur.online, cur.online_at, cur.max_players, cur.last_count = pinged[0], now, pinged[1], pinged[0]
                await self._track_ticks(server, cur, now)
            return
        cur.list_at = now
        cur.players = parsed[2]
        cur.last_count = parsed[0]
        cur.online, cur.online_at, cur.max_players = parsed[0], now, parsed[1]
        playtime.observe(db, server, parsed[2])
        await self._track_ticks(server, cur, now)

    async def _ping(self, server: Server, cur: Seen, now: float) -> tuple[int, int] | None:
        """Head-count from the status ping. A game port the panel can't reach (a firewall, say) is retried
        only every 10 minutes after 3 misses, so it doesn't slow every sweep by the connect timeout."""
        if server.template != "minecraft-java" or now < cur.ping_skip_until:
            return None
        port = next((a.port for a in server.allocations if a.is_primary), None) or (server.allocations[0].port if server.allocations else None)
        host = urlsplit(server.node.url).hostname
        got = await mcping.ping(host, port) if host and port else None
        cur.ping_miss = 0 if got else cur.ping_miss + 1
        if cur.ping_miss >= 3:
            cur.ping_skip_until, cur.ping_miss = now + 600, 2
        return got

    async def _track_ticks(self, server: Server, cur: Seen, now: float) -> None:
        """TPS and tick time through the console: each minute with someone on, every few minutes without."""
        if server.template != "minecraft-java" or cur.tick_off:
            return
        if now - cur.ticks_at < (PLAYER_CHECK if cur.online else TICKS_IDLE):
            return
        cur.ticks_at = now
        try:  # a console that hangs must not hold up the sweep for every other server
            await asyncio.wait_for(self._read_ticks(server, cur), TICK_TIMEOUT)
        except asyncio.TimeoutError:
            cur.tick_miss += 1
            cur.tick_off = cur.tick_miss >= TICK_GIVE_UP

    async def _read_ticks(self, server: Server, cur: Seen) -> None:
        for command in ([cur.tick_cmd] if cur.tick_cmd else metrics.tick_commands(server)):
            try:
                output = await self.ctx.nodes.command(server.node, server.uuid, command)
            except NodeError:
                return
            got = metrics.parse_ticks(command, output)
            if got is None:
                continue
            cur.tick_cmd, cur.tick_miss = command, 0
            tps, mspt = got
            if command == "tps" and mspt is None and not cur.mspt_off:
                try:
                    mspt = (metrics.parse_ticks("mspt", await self.ctx.nodes.command(server.node, server.uuid, "mspt")) or (None, None))[1]
                except NodeError:
                    mspt = None
                cur.mspt_off = mspt is None
            cur.tps, cur.mspt = tps, mspt
            return
        cur.tick_miss += 1
        cur.tick_off = cur.tick_miss >= TICK_GIVE_UP

    def _record_metric(self, db, server: Server, st: dict, cur: Seen, state: str, now: float) -> None:
        """One row a minute while it runs: what the console charts are drawn from."""
        stamp = int(time.time())
        minute = stamp // 60 * 60
        if state != "running" or minute == cur.metric_minute:
            return
        cur.metric_minute = minute
        fresh = lambda at: at is not None and now - at < READING_FRESH  # noqa: E731
        net = st.get("network") or {}
        metrics.record(db, server.id, stamp, cpu=st.get("cpu"), mem=st.get("memory"), disk=st.get("disk"),
                       rx=net.get("rx"), tx=net.get("tx"),
                       players=cur.online if fresh(cur.online_at) else None,
                       tps=cur.tps if fresh(cur.ticks_at) else None, mspt=cur.mspt if fresh(cur.ticks_at) else None)
        self._metric_alerts(db, server, cur, now, fresh(cur.ticks_at))

    def _metric_alerts(self, db, server: Server, cur: Seen, now: float, have_tps: bool) -> None:
        """Tell the admin about lag that lasts, and about a disk that will fill within days."""
        if have_tps and cur.tps is not None:
            cur.low_tps_run = cur.low_tps_run + 1 if cur.tps < LOW_TPS else 0
            if cur.tps >= LOW_TPS + 3:
                cur.low_tps_sent = False
            if cur.low_tps_run >= LOW_TPS_MINUTES and not cur.low_tps_sent:
                cur.low_tps_sent = True
                events.emit(self.ctx, db, "server.low_tps", server=server,
                            detail=f"TPS has stayed under {LOW_TPS:g} for {LOW_TPS_MINUTES} minutes (now {cur.tps:g}).")
        if now - cur.fill_checked >= 3600:
            cur.fill_checked = now
            days = metrics.disk_days_left(db, server)
            if days is not None and days < FILL_WARN_DAYS and not cur.fill_sent:
                cur.fill_sent = True
                events.emit(self.ctx, db, "server.disk_filling", server=server,
                            detail=f"At this rate the disk limit is reached in about {max(1, round(days))} day(s).")
            elif days is None or days > FILL_WARN_DAYS * 2:
                cur.fill_sent = False

    def _check_disk(self, db, server: Server, st: dict, cur: Seen) -> None:
        if not server.disk_mb or "disk" not in st:
            return
        share = int(st.get("disk") or 0) / (server.disk_mb * 1048576)
        level = 2 if share >= 1 else 1 if share >= DISK_WARN else 0 if share < DISK_RESET else min(cur.disk_level, 1)
        if level > cur.disk_level:
            used = f"{int(st.get('disk') or 0) / 1048576:,.0f} MB of {server.disk_mb:,} MB used."
            if level == 2:
                events.emit(self.ctx, db, "server.disk_full", server=server,
                            detail=used + " Uploads and mod installs are blocked until files are deleted or the limit is raised.")
            else:
                events.emit(self.ctx, db, "server.disk_warning", server=server, detail=used)
        cur.disk_level = level

    async def _auto_sleep(self, db, server: Server, cur: Seen, state: str, now: float) -> None:
        if not server.auto_stop_minutes or server.template != "minecraft-java" or state != "running":
            cur.empty_since = None
            return
        if cur.last_count is None:
            return  # no player count yet
        online, cur.last_count = cur.last_count, None  # act on each count once
        if online:
            cur.empty_since = None
            return
        cur.empty_since = cur.empty_since or now
        if now - cur.empty_since < server.auto_stop_minutes * 60:
            return
        cur.empty_since = None
        if server.wake_on_join:
            motd = str((server.variables or {}).get("MOTD") or "").strip() or "Sleeping - join to wake me up"
            await self._safe(server, lambda: self.ctx.nodes.sleep(server.node, server.uuid, motd))
        else:
            await self._safe(server, lambda: self.ctx.nodes.power(server.node, server.uuid, "stop"))
            cur.quiet_stop = True  # the running → offline change that follows is this stop
            events.emit(self.ctx, db, "server.stopped",
                        server=server, detail=f"Nobody was online for {server.auto_stop_minutes} minutes.")

    async def _safe(self, server: Server, call) -> None:
        try:
            await call()
        except NodeError as exc:
            log.warning("monitor action on %s failed: %s", server.name, exc)
