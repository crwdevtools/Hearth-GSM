"""Scheduled tasks: a small cron engine and the runner that executes server actions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from ..models import Schedule, Server
from . import events, settings_store
from .backups import BackupFailed, create_backup
from .node_client import NodeError
from .status import live_statuses

log = logging.getLogger("hearth.scheduler")

ACTIONS = ("restart", "start", "stop", "backup", "command")
FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 6))
PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


class CronError(ValueError):
    pass


def _field(expr: str, name: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"Bad step in {name}: “{step_s}”")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"Bad range in {name}: “{part}”")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = hi
        else:
            raise CronError(f"Bad {name}: “{part}”")
        if name == "day of week" and end == 7:  # 7 is also Sunday
            values.add(0)
            end = 6 if start <= 6 else 0
            if start == 7:
                continue
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"{name.capitalize()} must be between {lo} and {hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        expr = PRESETS.get(expr.strip().lower(), expr.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("Use 5 fields: minute hour day-of-month month day-of-week (e.g. 0 4 * * *)")
        fields = [_field(p, *spec) for p, spec in zip(parts, FIELDS)]
        # Standard cron: a day field starting with "*" (including "*/2") counts as unrestricted,
        # so day-of-month and day-of-week are only OR-ed when both are really restricted.
        return cls(*fields, day_restricted=not parts[2].startswith("*"),
                   weekday_restricted=not parts[4].startswith("*"))

    def _day_matches(self, dt: datetime) -> bool:
        dom = dt.day in self.days
        dow = (dt.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom or dow  # classic cron: either restriction may match
        return dom and dow

    def matches(self, dt: datetime) -> bool:
        return (dt.minute in self.minutes and dt.hour in self.hours and dt.month in self.months
                and self._day_matches(dt))

    def next_after(self, dt: datetime, limit_days: int = 400) -> datetime | None:
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = t + timedelta(days=limit_days)
        while t < end:
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t
        return None


def panel_timezone(ctx) -> ZoneInfo:
    with ctx.db.sessionmaker() as db:
        name = settings_store.timezone_name(db, ctx.settings)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def describe_next(ctx, expr: str, count: int = 3) -> list[str]:
    tz = panel_timezone(ctx)
    cron = Cron.parse(expr)
    runs, t = [], datetime.now(tz)
    for _ in range(count):
        t = cron.next_after(t)
        if t is None:
            break
        runs.append(t.isoformat())
    return runs


class Scheduler:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self._running: set[asyncio.Task] = set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            now = datetime.now()
            await asyncio.sleep(max(1.0, 60 - now.second - now.microsecond / 1e6 + 0.5))

    async def tick(self, now: datetime | None = None) -> int:
        tz = panel_timezone(self.ctx)
        now = (now or datetime.now(timezone.utc)).astimezone(tz).replace(second=0, microsecond=0)
        started = 0
        with self.ctx.db.sessionmaker() as db:
            for schedule in db.scalars(select(Schedule).where(Schedule.enabled.is_(True))):
                try:
                    cron = Cron.parse(schedule.cron)
                except CronError:
                    continue
                last = schedule.last_run.astimezone(tz).replace(second=0, microsecond=0) if schedule.last_run else None
                if not cron.matches(now) or last == now:
                    continue
                schedule.last_run = now.astimezone(timezone.utc)
                db.commit()
                task = asyncio.get_running_loop().create_task(self.execute(schedule.id))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
                started += 1
        return started

    async def execute(self, schedule_id: int) -> str:
        with self.ctx.db.sessionmaker() as db:
            schedule = db.get(Schedule, schedule_id)
            if schedule is None:
                return "deleted"
            server: Server = schedule.server
            try:
                result = await self._act(db, schedule, server)
            except Exception as exc:  # a schedule that fails must say so, whatever went wrong
                if not isinstance(exc, (NodeError, BackupFailed)):
                    log.exception("schedule %s failed", schedule.name)
                db.rollback()
                result = f"Failed: {exc}"
                events.emit(self.ctx, db, "schedule.failed", server=server, detail=f"{schedule.name}: {exc}")
            schedule.last_result = result[:500]
            db.commit()
            events.emit(self.ctx, db, "schedule.ran", server=server, detail=f"{schedule.name}: {result}")
            return result

    async def _act(self, db, schedule: Schedule, server: Server) -> str:
        nodes = self.ctx.nodes
        if server.suspended:
            return "Skipped: server is suspended."
        if schedule.action == "backup":
            row = await create_backup(self.ctx, db, server, kind="scheduled", wait=True,
                                      keep=max(1, schedule.keep_backups), name=f"{schedule.name}")
            return f"Backup finished ({row.size_bytes / 1048576:.0f} MB)."
        if schedule.action == "command":
            output = await nodes.command(server.node, server.uuid, schedule.payload)
            return f"Sent “{schedule.payload}”" + (f": {output.strip()[:200]}" if output and output.strip() else ".")
        if schedule.action in ("restart", "stop"):
            # A nightly restart must not start a server someone stopped, or wake a sleeping one.
            state = (await live_statuses(self.ctx, [server]))[server.uuid].get("state")
            if state not in ("running", "starting"):
                return f"Skipped: the server is {state.replace('_', ' ') if state else 'not running'}."
        if schedule.action in ("restart", "stop") and schedule.warn_minutes and server.template == "minecraft-java":
            await self._warn(server, schedule.action, schedule.warn_minutes)
            # Someone may have stopped it during the countdown; a restart must not bring it back.
            state = (await live_statuses(self.ctx, [server]))[server.uuid].get("state")
            if state not in ("running", "starting"):
                return f"Skipped: the server was stopped during the countdown ({state or 'not running'})."
        await nodes.power(server.node, server.uuid, schedule.action)
        return {"restart": "Restarted.", "start": "Started.", "stop": "Stopped."}[schedule.action]

    async def _warn(self, server: Server, action: str, minutes: int) -> None:
        verb = "restart" if action == "restart" else "shut down"
        say = lambda text: self.ctx.nodes.command(server.node, server.uuid, f"say {text}")  # noqa: E731
        try:
            await say(f"Server will {verb} in {minutes} minute{'s' if minutes != 1 else ''}.")
            if minutes > 1:
                await asyncio.sleep((minutes - 1) * 60)
                await say(f"Server will {verb} in 1 minute.")
            await asyncio.sleep(50)
            await say(f"Server will {verb} in 10 seconds!")
            await asyncio.sleep(10)
        except NodeError:
            pass  # not running / unreachable: nothing to warn
