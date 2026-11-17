"""Player sessions and stats, from the `list` command the monitor runs once a minute."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PlayerSession, Server

KEEP_DAYS = 365


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def observe(db: Session, server: Server, online: list[str] | None, now: datetime | None = None) -> None:
    """Open sessions for players who appeared, close them for players who left. `online=None`
    means the server is down: everyone's session ends."""
    now = now or datetime.now(timezone.utc)
    open_rows = {r.name: r for r in db.scalars(select(PlayerSession).where(
        PlayerSession.server_id == server.id, PlayerSession.left_at.is_(None)))}
    names = set(online or [])
    for name, row in open_rows.items():
        if name not in names:
            row.left_at = now
    for name in names - open_rows.keys():
        db.add(PlayerSession(server_id=server.id, name=name[:16], joined_at=now))
    db.commit()


def prune(db: Session, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)
    for row in db.scalars(select(PlayerSession).where(PlayerSession.left_at < cutoff)):
        db.delete(row)
    db.commit()


def stats(db: Session, server: Server, days: int = 30, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = list(db.scalars(select(PlayerSession).where(
        PlayerSession.server_id == server.id,
        (PlayerSession.left_at.is_(None)) | (PlayerSession.left_at >= since))))
    per: dict[str, dict] = defaultdict(lambda: {"seconds": 0, "sessions": 0, "last_seen": None, "online": False})
    events: list[tuple[datetime, int]] = []
    daily_peak: dict[str, int] = defaultdict(int)
    for r in rows:
        start = max(_aware(r.joined_at), since)
        end = _aware(r.left_at) if r.left_at else now
        p = per[r.name]
        p["seconds"] += max(0, int((end - start).total_seconds()))
        p["sessions"] += 1
        p["online"] = p["online"] or r.left_at is None
        p["last_seen"] = max(p["last_seen"] or end, end)
        events += [(start, 1), (end, -1)]
    # Peak players online at once, overall and per day (sweep over joins and leaves).
    current = peak = 0
    peak_at = None
    for at, delta in sorted(events, key=lambda e: (e[0], e[1])):
        current += delta
        if current > peak:
            peak, peak_at = current, at
        day = at.date().isoformat()
        daily_peak[day] = max(daily_peak[day], current)
    players = sorted(({"name": n, **v, "last_seen": v["last_seen"].isoformat() if v["last_seen"] else None}
                      for n, v in per.items()), key=lambda p: -p["seconds"])
    return {
        "days": days,
        "unique_players": len(players),
        "total_seconds": sum(p["seconds"] for p in players),
        "peak": peak,
        "peak_at": peak_at.isoformat() if peak_at else None,
        "daily_peak": [{"day": d, "peak": daily_peak[d]} for d in sorted(daily_peak)],
        "players": players,
    }
