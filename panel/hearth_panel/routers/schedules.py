"""Scheduled tasks per server."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, permissions_for, server_with
from ..models import Schedule, Server, User
from ..services import events
from ..services.scheduler import Cron, CronError, describe_next, panel_timezone

router = APIRouter(prefix="/api/servers/{server_id}/schedules", tags=["schedules"])
access = server_with("schedules")


class ScheduleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cron: str = Field(min_length=1, max_length=64)
    action: Literal["restart", "start", "stop", "backup", "command"]
    payload: str = Field(default="", max_length=500)
    warn_minutes: int = Field(default=0, ge=0, le=60)
    keep_backups: int = Field(default=7, ge=1, le=100)
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        try:
            Cron.parse(v)
        except CronError as exc:
            raise ValueError(str(exc)) from exc
        return v.strip()


def schedule_out(context, s: Schedule) -> dict:
    try:
        upcoming = describe_next(context, s.cron, 1) if s.enabled else []
    except CronError:
        upcoming = []
    return {"id": s.id, "name": s.name, "cron": s.cron, "action": s.action, "payload": s.payload,
            "warn_minutes": s.warn_minutes, "keep_backups": s.keep_backups, "enabled": s.enabled,
            "last_run": s.last_run.isoformat() if s.last_run else None, "last_result": s.last_result,
            "next_run": upcoming[0] if upcoming else None}


def _get(server: Server, schedule_id: int) -> Schedule:
    row = next((s for s in server.schedules if s.id == schedule_id), None)
    if row is None:
        raise HTTPException(404, "Schedule not found.")
    return row


# A schedule acts with its creator's rights later, so creating one needs the right it uses.
NEEDS = {"restart": "power", "start": "power", "stop": "power", "backup": "backups", "command": "console"}


def _check(body: ScheduleBody, server: Server, user: User) -> None:
    if body.action == "command" and not body.payload.strip():
        raise HTTPException(400, "Enter the console command to run.")
    if NEEDS[body.action] not in permissions_for(server, user):
        raise HTTPException(403, f"Scheduling “{body.action}” needs the {NEEDS[body.action]} permission.")


@router.get("")
def list_schedules(server: Server = Depends(access), context: PanelContext = Depends(ctx)):
    return {"timezone": str(panel_timezone(context)),
            "schedules": [schedule_out(context, s) for s in sorted(server.schedules, key=lambda s: s.id)]}


@router.get("/preview")
def preview(cron: str = Query(..., max_length=64), server: Server = Depends(access),
            context: PanelContext = Depends(ctx)):
    try:
        return {"next": describe_next(context, cron, 3), "timezone": str(panel_timezone(context))}
    except CronError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("", status_code=201)
def create(body: ScheduleBody, server: Server = Depends(access), user: User = Depends(current_user),
           db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    _check(body, server, user)
    row = Schedule(server_id=server.id, **body.model_dump())
    db.add(row)
    db.commit()
    events.emit(context, db, "schedule.created", server=server, user=user, detail=f"{row.name} ({row.cron})")
    return schedule_out(context, row)


@router.put("/{schedule_id}")
def update(schedule_id: int, body: ScheduleBody, server: Server = Depends(access), user: User = Depends(current_user),
           db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    _check(body, server, user)
    row = _get(server, schedule_id)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return schedule_out(context, row)


@router.post("/{schedule_id}/run", status_code=202)
async def run_now(schedule_id: int, request: Request, server: Server = Depends(access),
                  user: User = Depends(current_user)):
    row = _get(server, schedule_id)
    if NEEDS[row.action] not in permissions_for(server, user):
        raise HTTPException(403, f"Running “{row.action}” needs the {NEEDS[row.action]} permission.")
    result = await request.app.state.scheduler.execute(schedule_id)
    return {"result": result}


@router.delete("/{schedule_id}", status_code=204)
def delete(schedule_id: int, server: Server = Depends(access), db: Session = Depends(db_session)) -> None:
    db.delete(_get(server, schedule_id))
    db.commit()
