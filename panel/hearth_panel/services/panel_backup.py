"""Nightly copies of the panel's own database (users, servers, schedules, settings, activity).

SQLite only: a consistent snapshot is taken with SQLite's online backup API, so the panel keeps
running. The last KEEP snapshots stay next to the database; with off-site storage set up, each is
also uploaded there. PostgreSQL users should back up with pg_dump instead.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import offsite, s3

log = logging.getLogger("hearth.panel_backup")

KEEP = 14
EVERY = 24 * 3600


def database_path(ctx) -> Path | None:
    url = ctx.settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    path = url.split("///", 1)[1]
    return Path(path) if path and path != ":memory:" else None


def folder(ctx) -> Path | None:
    db = database_path(ctx)
    return db.parent / "panel-backups" if db else None


def list_snapshots(ctx) -> list[dict]:
    where = folder(ctx)
    if where is None or not where.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size,
             "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in sorted(where.glob("hearth-*.db"), reverse=True)]


def snapshot_path(ctx, name: str) -> Path | None:
    where = folder(ctx)
    if where is None or "/" in name or not name.startswith("hearth-") or not name.endswith(".db"):
        return None
    path = where / name
    return path if path.is_file() else None


def _snapshot(source: Path, target: Path) -> None:
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)


async def take(ctx) -> dict:
    db_path = database_path(ctx)
    if db_path is None:
        raise RuntimeError("Panel backups need the built-in SQLite database; back up PostgreSQL with pg_dump.")
    where = folder(ctx)
    where.mkdir(parents=True, exist_ok=True)
    name = f"hearth-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    await asyncio.to_thread(_snapshot, db_path, where / name)
    for old in sorted(where.glob("hearth-*.db"), reverse=True)[KEEP:]:
        old.unlink(missing_ok=True)
    uploaded = False
    with ctx.db.sessionmaker() as session:
        t = offsite.target(session)
    if t is not None:
        try:
            data = await asyncio.to_thread((where / name).read_bytes)
            await s3.put_bytes(ctx.http.client, t, t.key("panel", name), data)
            uploaded = True
        except s3.S3Error as exc:
            log.warning("couldn't copy panel backup off-site: %s", exc)
    log.info("panel database backed up to %s%s", name, " (and off-site)" if uploaded else "")
    return {"name": name, "offsite": uploaded}


async def run_forever(ctx) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            latest = list_snapshots(ctx)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(latest[0]["created_at"])).total_seconds() \
                if latest else None
            if database_path(ctx) is not None and (age is None or age >= EVERY):
                await take(ctx)
        except Exception:
            log.exception("panel database backup failed")
        await asyncio.sleep(3600)
