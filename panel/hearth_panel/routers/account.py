"""Your own account: two-factor sign-in and API tokens."""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session
from ..models import ApiToken, User
from ..security import new_api_token, match_totp, new_totp_secret, totp_uri, verify_password, verify_totp
from ..services import events

router = APIRouter(prefix="/api/account", tags=["account"])


class CodeBody(BaseModel):
    code: str = Field(min_length=6, max_length=12)


class DisableBody(BaseModel):
    password: str = Field(max_length=256)
    code: str = Field(min_length=6, max_length=12)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def qr_svg(data: str) -> str | None:
    try:
        import segno
    except ImportError:
        return None
    out = io.BytesIO()
    segno.make(data, error="m").save(out, kind="svg", scale=5, border=2, dark="#ededed", light="#0a0a0a",
                                     xmldecl=False, svgns=True)
    return out.getvalue().decode()


@router.post("/2fa/setup")
def setup_2fa(user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Start enrolment: a fresh secret that only takes effect once a code from it is confirmed."""
    if user.totp_enabled:
        raise HTTPException(409, "Two-factor sign-in is already on.")
    user.totp_secret = new_totp_secret()
    db.commit()
    uri = totp_uri(user.totp_secret, user.username)
    return {"secret": user.totp_secret, "uri": uri, "qr_svg": qr_svg(uri)}


@router.post("/2fa/enable")
def enable_2fa(body: CodeBody, user: User = Depends(current_user), db: Session = Depends(db_session),
               context: PanelContext = Depends(ctx)):
    if user.totp_enabled:
        raise HTTPException(409, "Two-factor sign-in is already on.")
    step = match_totp(user.totp_secret or "", body.code)
    if step is None:
        raise HTTPException(400, "That code didn't match. Check your phone's clock and try the current code.")
    user.totp_enabled, user.totp_last_step = True, step
    db.commit()
    events.emit(context, db, "account.2fa_enabled", user=user)
    return {"enabled": True}


@router.post("/2fa/disable")
def disable_2fa(body: DisableBody, user: User = Depends(current_user), db: Session = Depends(db_session),
                context: PanelContext = Depends(ctx)):
    if not user.totp_enabled:
        return {"enabled": False}
    key = f"password:{user.id}"
    if context.throttle.blocked(key):
        raise HTTPException(429, "Too many wrong attempts. Try again in a few minutes.")
    if not verify_password(body.password, user.password_hash) or not verify_totp(user.totp_secret or "", body.code):
        context.throttle.fail(key)
        raise HTTPException(400, "Password or code is wrong.")
    user.totp_enabled, user.totp_secret = False, None
    db.commit()
    events.emit(context, db, "account.2fa_disabled", user=user)
    return {"enabled": False}


@router.get("/tokens")
def list_tokens(user: User = Depends(current_user), db: Session = Depends(db_session)):
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()))
    return [{"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at.isoformat(),
             "last_used": t.last_used.isoformat() if t.last_used else None} for t in rows]


@router.post("/tokens", status_code=201)
def create_token(body: TokenBody, user: User = Depends(current_user), db: Session = Depends(db_session),
                 context: PanelContext = Depends(ctx)):
    token, prefix, digest = new_api_token()
    row = ApiToken(user_id=user.id, name=body.name.strip(), prefix=prefix, token_hash=digest)
    db.add(row)
    db.commit()
    events.emit(context, db, "account.token_created", user=user, detail=row.name)
    return {"id": row.id, "name": row.name, "token": token}  # shown once


@router.delete("/tokens/{token_id}", status_code=204)
def delete_token(token_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Token not found.")
    db.delete(row)
    db.commit()
