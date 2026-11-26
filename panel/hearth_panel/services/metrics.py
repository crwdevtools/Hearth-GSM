"""Console charts: a minute-by-minute record of each running server, and the Minecraft tick numbers.

The monitor writes one row a minute from the node's live status (CPU, memory, disk, network), the
player count it already checks for playtime, and the server's tick rate (TPS) and tick time (MSPT)
read through the console. The console page reads it back in 1 hour, 24 hour and 7 day views.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, PlayerSession, Server, ServerMetric

KEEP_DAYS = 8
RANGES = {"1h": (3600, 60), "24h": (86400, 300), "7d": (7 * 86400, 1800)}  # seconds shown, seconds per point
GAP = 3  # a hole of this many steps between points means the server was off
FRESH = 400  # seconds a TPS or player reading stays valid for the minute rows
EVENTS = ("server.started", "server.stopped", "server.crashed", "server.out_of_memory", "server.crash_loop")

_CODES = re.compile(r"§.|\x1b\[[0-9;]*m")
_NUM = r"(\d+(?:\.\d+)?)"

PAPER_FAMILY = {"PAPER", "PURPUR", "SPIGOT", "BUKKIT", "FOLIA", "PUFFERFISH", "LEAF", "CANVAS", "AIRPLANE", "CRAFTBUKKIT"}


# ----- reading the tick numbers --------------------------------------------------------------------

def tick_commands(server: Server) -> list[str]:
    """Console commands that report tick speed on this server, most likely first."""
    kind = str((server.variables or {}).get("TYPE") or "PAPER").upper()
    loader = (server.modpack_loader or "").lower()
    if server.modpack_provider and not server.modpack_file:
        return ["tick query", "forge tps"]  # a catalog pack: the loader isn't recorded
    if loader == "forge" or kind == "FORGE":
        return ["forge tps", "tick query"]
    if kind in PAPER_FAMILY and not server.modpack_provider:
        return ["tps"]
    return ["tick query"]


def parse_ticks(command: str, output: str | None) -> tuple[float | None, float | None] | None:
    """(tps, mspt) from a console reply, or None when the reply isn't tick information."""
    if not output:
        return None
    text = _CODES.sub("", output).replace("*", "")
    tps = mspt = None
    if command == "tps":
        header = re.search(r"TPS from last ([^:\n]+):\s*([^\n]+)", text)
        if header:
            labels = [x.strip() for x in header.group(1).split(",")]
            values = [float(v) for v in re.findall(_NUM, header.group(2))]
            if values:
                tps = values[labels.index("1m")] if "1m" in labels and labels.index("1m") < len(values) else values[0]
        m = re.search(r"MSPT[^\d\n]*" + _NUM, text)
        if m:
            mspt = float(m.group(1))
    elif command == "forge tps":
        m = re.search(r"Overall:\s*Mean tick time:\s*" + _NUM + r"\s*ms\.\s*Mean TPS:\s*" + _NUM, text)
        if m:
            mspt, tps = float(m.group(1)), float(m.group(2))
    elif command == "tick query":
        avg = re.search(r"Average time per tick:\s*" + _NUM + r"\s*ms", text)
        if avg:
            mspt = float(avg.group(1))
            rate = re.search(r"Target tick rate:\s*" + _NUM, text)
            target = float(rate.group(1)) if rate else 20.0
            tps = min(target, 1000 / mspt) if mspt > 0 else target
    elif command == "mspt":
        triples = re.findall(_NUM + r"/" + _NUM + r"/" + _NUM, text)
        if triples:
            mspt = float(triples[-1][0])  # the longest window (a minute) is the steadiest
    if tps is None and mspt is None:
        return None
    return (round(tps, 2) if tps is not None else None, round(mspt, 2) if mspt is not None else None)


# ----- storing and reading the series --------------------------------------------------------------

def record(db: Session, server_id: int, ts: int, *, cpu=None, mem=None, disk=None, rx=None, tx=None,
           players=None, tps=None, mspt=None) -> None:
    db.add(ServerMetric(server_id=server_id, ts=ts // 60 * 60, cpu=cpu, mem=mem, disk=disk, rx=rx, tx=tx,
                        players=players, tps=tps, mspt=mspt))
    try:
        db.commit()
    except IntegrityError:  # the panel restarted within the same minute
        db.rollback()


def prune(db: Session, now: float | None = None) -> None:
    db.execute(delete(ServerMetric).where(ServerMetric.ts < int(now or time.time()) - KEEP_DAYS * 86400))
    db.commit()


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def series(db: Session, server: Server, span: str = "24h", now: float | None = None) -> dict[str, Any]:
    """Points for the console charts: {t, cpu, mem, disk, rx, tx, players, tps, mspt}, None where unknown.

    Network is bytes per second, worked out from the counters. A hole where the server was off is
    marked with a point of all None, so the charts break their lines there.
    """
    seconds, step = RANGES.get(span, RANGES["24h"])
    now = int(now if now is not None else time.time())
    since = now - seconds
    rows = list(db.scalars(select(ServerMetric).where(ServerMetric.server_id == server.id, ServerMetric.ts >= since - 60)
                           .order_by(ServerMetric.ts)))
    buckets: dict[int, dict[str, list[float]]] = {}
    prev: ServerMetric | None = None
    for row in rows:
        rate = {}
        if prev is not None and 0 < row.ts - prev.ts <= 180 and row.rx is not None and prev.rx is not None:
            dt = row.ts - prev.ts
            rate = {"rx": max(0, row.rx - prev.rx) / dt, "tx": max(0, (row.tx or 0) - (prev.tx or 0)) / dt}
        prev = row
        if row.ts < since:
            continue
        slot = buckets.setdefault(row.ts // step * step, {})
        for key, value in [("cpu", row.cpu), ("mem", row.mem), ("disk", row.disk), ("players", row.players),
                           ("tps", row.tps), ("mspt", row.mspt), *rate.items()]:
            if value is not None:
                slot.setdefault(key, []).append(value)
    points: list[dict[str, Any]] = []
    last_t = None
    for t in sorted(buckets):
        b = buckets[t]
        if last_t is not None and t - last_t > step * GAP:
            points.append({"t": last_t + step, "gap": True})
        points.append({
            "t": t, "cpu": _avg(b.get("cpu", [])), "mem": max(b["mem"]) if b.get("mem") else None,
            "disk": b["disk"][-1] if b.get("disk") else None, "rx": _avg(b.get("rx", [])), "tx": _avg(b.get("tx", [])),
            "players": max(b["players"]) if b.get("players") else None, "tps": _avg(b.get("tps", [])),
            "mspt": max(b["mspt"]) if b.get("mspt") else None})
        last_t = t
    return {"range": span, "step": step, "points": points}


def peak_players(db: Session, server: Server, hours: int = 24, now: float | None = None) -> int | None:
    since = int(now if now is not None else time.time()) - hours * 3600
    return db.scalar(select(func.max(ServerMetric.players)).where(ServerMetric.server_id == server.id, ServerMetric.ts >= since))


def events_since(db: Session, server: Server, seconds: int, now: datetime | None = None) -> list[dict[str, Any]]:
    since = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    rows = db.scalars(select(AuditLog).where(AuditLog.server_id == server.id, AuditLog.at >= since,
                                              AuditLog.action.in_(EVENTS)).order_by(AuditLog.at.desc()).limit(30))
    out = []
    for r in rows:
        at = r.at if r.at.tzinfo else r.at.replace(tzinfo=timezone.utc)
        out.append({"t": int(at.timestamp()), "action": r.action})
    return out


def recent_players(db: Session, server: Server, limit: int = 8, now: datetime | None = None) -> dict[str, Any]:
    """The last joins and leaves, and the longest stretch anyone has been on in the past day."""
    now = now or datetime.now(timezone.utc)
    day = now - timedelta(hours=24)
    aware = lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc)  # noqa: E731
    sessions = list(db.scalars(select(PlayerSession).where(
        PlayerSession.server_id == server.id, (PlayerSession.left_at.is_(None)) | (PlayerSession.left_at >= day))))
    moves = []
    longest = 0
    for s in sessions:
        joined = aware(s.joined_at)
        moves.append({"name": s.name, "event": "joined", "t": int(joined.timestamp())})
        if s.left_at:
            moves.append({"name": s.name, "event": "left", "t": int(aware(s.left_at).timestamp())})
        longest = max(longest, int(((aware(s.left_at) if s.left_at else now) - max(joined, day)).total_seconds()))
    moves.sort(key=lambda m: (-m["t"], m["event"] != "left"))
    return {"recent": moves[:limit], "longest_session": longest or None}
