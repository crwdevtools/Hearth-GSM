"""Admin settings and overview."""

from __future__ import annotations

import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import admin_user, ctx, db_session
from ..models import Allocation, AuditLog, Node, Server, User, Webhook
from ..services import events, netguard, settings_store, updater

router = APIRouter(prefix="/api/admin", tags=["admin"])


class SettingsBody(BaseModel):
    # None leaves the key unchanged; "" removes it.
    curseforge_api_key: str | None = Field(default=None, max_length=200)
    update_interval_minutes: int | None = Field(default=None, ge=15, le=10_080)
    timezone: str | None = Field(default=None, max_length=64)


class WebhookBody(BaseModel):
    url: str = Field(max_length=512, pattern=r"^https?://\S+$")
    events: list[str] = Field(default_factory=list)
    enabled: bool = True


def _settings_out(db: Session, context: PanelContext) -> dict:
    key = settings_store.curseforge_key(db, context.settings)
    return {
        "curseforge_api_key_set": bool(key),
        "curseforge_api_key_hint": f"…{key[-4:]}" if key else None,
        "curseforge_from_env": bool(context.settings.curseforge_api_key) and not settings_store.get(
            db, settings_store.CURSEFORGE_KEY),
        "update_interval_minutes": settings_store.update_interval(db, context.settings),
        "timezone": settings_store.timezone_name(db, context.settings),
    }


@router.get("/settings")
def get_settings(_: User = Depends(admin_user), db: Session = Depends(db_session),
                 context: PanelContext = Depends(ctx)):
    return _settings_out(db, context)


@router.put("/settings")
def put_settings(body: SettingsBody, _: User = Depends(admin_user), db: Session = Depends(db_session),
                 context: PanelContext = Depends(ctx)):
    if body.curseforge_api_key is not None:
        settings_store.put(db, settings_store.CURSEFORGE_KEY, body.curseforge_api_key.strip() or None)
    if body.update_interval_minutes is not None:
        settings_store.put(db, settings_store.UPDATE_INTERVAL, str(body.update_interval_minutes))
    if body.timezone is not None:
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(400, "Unknown time zone. Use a name like America/Toronto.") from exc
        settings_store.put(db, settings_store.TIMEZONE, body.timezone)
    db.commit()
    context.modpacks.curseforge_key = settings_store.curseforge_key(db, context.settings)
    context.cache.invalidate("mp:")
    return _settings_out(db, context)


@router.get("/overview")
def overview(_: User = Depends(admin_user), db: Session = Depends(db_session)):
    return {
        "users": db.scalar(select(func.count(User.id))),
        "nodes": db.scalar(select(func.count(Node.id))),
        "servers": db.scalar(select(func.count(Server.id))),
        "allocations_free": db.scalar(select(func.count(Allocation.id)).where(Allocation.server_id.is_(None))),
        "modpack_servers": db.scalar(select(func.count(Server.id)).where(Server.modpack_provider.is_not(None))),
        "updates_available": db.scalar(select(func.count(Server.id)).where(Server.update_version_id.is_not(None))),
    }


@router.post("/updates/run")
async def run_updates(_: User = Depends(admin_user), context: PanelContext = Depends(ctx)):
    return {"checked": await updater.check_all(context)}


@router.get("/activity")
def activity(server_id: int | None = None, user_id: int | None = None, limit: int = 200,
             _: User = Depends(admin_user), db: Session = Depends(db_session)):
    query = select(AuditLog).order_by(AuditLog.at.desc()).limit(min(max(limit, 1), 1000))
    if server_id is not None:
        query = query.where(AuditLog.server_id == server_id)
    if user_id is not None:
        query = query.where(AuditLog.user_id == user_id)
    names = dict(db.execute(select(Server.id, Server.name)).all())
    return [{"at": r.at.isoformat(), "action": r.action, "detail": r.detail,
             "user": r.user.username if r.user else None, "server_id": r.server_id,
             "server": names.get(r.server_id)} for r in db.scalars(query)]


def webhook_out(w: Webhook) -> dict:
    return {"id": w.id, "url": w.url, "events": w.events, "enabled": w.enabled, "secret": w.secret,
            "created_at": w.created_at.isoformat()}


@router.get("/webhooks")
def list_webhooks(_: User = Depends(admin_user), db: Session = Depends(db_session)):
    return {"events": events.EVENT_NAMES, "webhooks": [webhook_out(w) for w in db.scalars(select(Webhook))]}


@router.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookBody, _: User = Depends(admin_user), db: Session = Depends(db_session),
                         context: PanelContext = Depends(ctx)):
    unknown = set(body.events) - set(events.EVENT_NAMES)
    if unknown:
        raise HTTPException(400, f"Unknown event: {', '.join(sorted(unknown))}")
    if not context.settings.allow_private_webhooks:
        reason = await netguard.public_destination(body.url, context.resolver)
        if reason:
            raise HTTPException(400, f"Webhooks can only go to internet addresses: {reason}. "
                                     "To allow local ones, set HEARTH_ALLOW_PRIVATE_WEBHOOKS=true and restart.")
    row = Webhook(url=body.url, events=body.events, enabled=body.enabled, secret=secrets.token_hex(24))
    db.add(row)
    db.commit()
    return webhook_out(row)


@router.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: int, _: User = Depends(admin_user), db: Session = Depends(db_session)) -> None:
    row = db.get(Webhook, webhook_id)
    if row is None:
        raise HTTPException(404, "Webhook not found.")
    db.delete(row)
    db.commit()


@router.post("/webhooks/test")
async def test_webhooks(admin: User = Depends(admin_user), db: Session = Depends(db_session),
                        context: PanelContext = Depends(ctx)):
    # async: notifications are only sent from the event loop, never from a worker thread
    events.emit(context, db, "server.started", user=admin, detail="Test event from Hearth (no server changed).")
    return {"sent": True}
