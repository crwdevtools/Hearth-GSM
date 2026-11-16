"""Admin user management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..deps import admin_user, db_session
from ..models import ApiToken, Server, User
from ..security import hash_password
from ..serializers import user_out
from .auth import USERNAME

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    username: str = Field(pattern=USERNAME)
    email: str | None = Field(default=None, max_length=255)
    password: str = Field(min_length=10, max_length=256)
    is_admin: bool = False
    max_servers: int = Field(default=0, ge=0, le=1000)
    max_memory_mb: int = Field(default=0, ge=0)


class UserPatch(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=10, max_length=256)
    is_admin: bool | None = None
    max_servers: int | None = Field(default=None, ge=0, le=1000)
    max_memory_mb: int | None = Field(default=None, ge=0)
    reset_2fa: bool = False


@router.get("")
def list_users(_: User = Depends(admin_user), db: Session = Depends(db_session)):
    rows = db.execute(select(Server.owner_id, func.count(Server.id), func.coalesce(func.sum(Server.memory_mb), 0))
                      .group_by(Server.owner_id)).all()
    usage = {owner: (count, memory) for owner, count, memory in rows}
    return [{**user_out(u), "servers": usage.get(u.id, (0, 0))[0], "memory_used_mb": usage.get(u.id, (0, 0))[1]}
            for u in db.scalars(select(User).order_by(User.id))]


@router.post("", status_code=201)
def create_user(body: UserCreate, _: User = Depends(admin_user), db: Session = Depends(db_session)):
    if db.scalar(select(User).where(func.lower(User.username) == body.username.lower())):
        raise HTTPException(409, "That username is taken.")
    user = User(username=body.username, email=body.email, password_hash=hash_password(body.password),
                is_admin=body.is_admin, max_servers=body.max_servers, max_memory_mb=body.max_memory_mb)
    db.add(user)
    db.commit()
    return user_out(user)


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: User = Depends(admin_user), db: Session = Depends(db_session)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if body.is_admin is False and user.id == admin.id:
        raise HTTPException(400, "You can't remove your own admin access.")
    if body.email is not None:
        user.email = body.email or None
    revoke = False
    if body.password:
        user.password_hash = hash_password(body.password)
        user.session_version += 1
        revoke = True
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.max_servers is not None:
        user.max_servers = body.max_servers
    if body.max_memory_mb is not None:
        user.max_memory_mb = body.max_memory_mb
    if body.reset_2fa:  # for a friend who lost their phone
        user.totp_enabled, user.totp_secret = False, None
        user.session_version += 1
        revoke = True
    if revoke:  # an admin reset usually means the account was lost or exposed: API tokens go too
        db.execute(delete(ApiToken).where(ApiToken.user_id == user.id))
    db.commit()
    return user_out(user)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(db_session)) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if user.id == admin.id:
        raise HTTPException(400, "You can't delete your own account.")
    if user.servers:
        raise HTTPException(409, f"{user.username} still owns {len(user.servers)} server(s). Reassign or delete them first.")
    db.delete(user)
    db.commit()
