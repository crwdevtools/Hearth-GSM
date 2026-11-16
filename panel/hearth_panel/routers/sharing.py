"""Share a server with friends, each with chosen permissions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, server_with
from ..models import PERMISSIONS, Server, ServerShare, User
from ..services import events

router = APIRouter(prefix="/api/servers/{server_id}/shares", tags=["sharing"])
owner_access = server_with("owner")


class ShareBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    permissions: list[str] = Field(default_factory=lambda: ["console", "power"])

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(PERMISSIONS)
        if unknown:
            raise ValueError(f"Unknown permission: {', '.join(sorted(unknown))}")
        return sorted(set(v))


class SharePatch(BaseModel):
    permissions: list[str]
    _perms = field_validator("permissions")(ShareBody._perms.__func__)


def share_out(s: ServerShare) -> dict:
    return {"id": s.id, "user": {"id": s.user.id, "username": s.user.username}, "permissions": s.permissions}


@router.get("")
def list_shares(server: Server = Depends(owner_access)):
    return {"permissions": list(PERMISSIONS), "shares": [share_out(s) for s in server.shares]}


@router.post("", status_code=201)
def add(body: ShareBody, server: Server = Depends(owner_access), user: User = Depends(current_user),
        db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    friend = db.scalar(select(User).where(func.lower(User.username) == body.username.strip().lower()))
    if friend is None:
        raise HTTPException(404, "No user with that name. An admin can add them under Admin → Users.")
    if friend.id == server.owner_id:
        raise HTTPException(400, "They already own this server.")
    if any(s.user_id == friend.id for s in server.shares):
        raise HTTPException(409, f"{friend.username} already has access. Edit their permissions instead.")
    share = ServerShare(server_id=server.id, user_id=friend.id, permissions=body.permissions)
    db.add(share)
    db.commit()
    db.refresh(share)
    events.emit(context, db, "share.added", server=server, user=user,
                detail=f"{friend.username}: {', '.join(body.permissions) or 'view only'}")
    return share_out(share)


@router.put("/{share_id}")
def update(share_id: int, body: SharePatch, server: Server = Depends(owner_access), db: Session = Depends(db_session)):
    share = next((s for s in server.shares if s.id == share_id), None)
    if share is None:
        raise HTTPException(404, "Share not found.")
    share.permissions = body.permissions
    db.commit()
    return share_out(share)


@router.delete("/{share_id}", status_code=204)
def remove(share_id: int, server: Server = Depends(owner_access), user: User = Depends(current_user),
           db: Session = Depends(db_session), context: PanelContext = Depends(ctx)) -> None:
    share = next((s for s in server.shares if s.id == share_id), None)
    if share is None:
        raise HTTPException(404, "Share not found.")
    name = share.user.username
    db.delete(share)
    db.commit()
    events.emit(context, db, "share.removed", server=server, user=user, detail=name)
