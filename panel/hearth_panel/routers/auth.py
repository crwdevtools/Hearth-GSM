"""Sign-in, first-run setup, sign-out, password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, user_from_token
from ..models import User
from ..security import DUMMY_HASH, SESSION_COOKIE, hash_password, issue_session, verify_password, match_totp
from ..serializers import user_out

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME = r"^[A-Za-z0-9_.\-]{3,32}$"


class Credentials(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, max_length=12)


class SetupBody(BaseModel):
    username: str = Field(pattern=USERNAME)
    password: str = Field(min_length=10, max_length=256)
    email: str | None = Field(default=None, max_length=255)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


def _set_cookie(response: Response, context: PanelContext, user: User) -> None:
    token = issue_session(context.secret, user.id, user.session_version, context.settings.session_hours)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=context.settings.session_hours * 3600, httponly=True,
        secure=context.settings.cookie_secure, samesite="strict", path="/",
    )


@router.get("/state")
def state(request: Request, db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    needs_setup = db.scalar(select(func.count(User.id))) == 0
    user = user_from_token(context, db, request.cookies.get(SESSION_COOKIE))
    return {"needs_setup": needs_setup, "user": user_out(user) if user else None}


@router.post("/setup")
def setup(body: SetupBody, response: Response, db: Session = Depends(db_session),
          context: PanelContext = Depends(ctx)):
    if db.scalar(select(func.count(User.id))) > 0:
        raise HTTPException(409, "Setup is already complete. Sign in instead.")
    user = User(username=body.username, email=body.email, password_hash=hash_password(body.password), is_admin=True)
    db.add(user)
    db.commit()
    _set_cookie(response, context, user)
    return {"user": user_out(user)}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(db_session),
          context: PanelContext = Depends(ctx)):
    client = request.client.host if request.client else "?"
    key = f"{client}:{body.username.lower()}"
    wait = context.throttle.blocked(key)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {int(wait // 60) + 1} min.")
    user = db.scalar(select(User).where(func.lower(User.username) == body.username.lower()))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or not ok:
        context.throttle.fail(key)
        raise HTTPException(401, "Wrong username or password.")
    if user.totp_enabled:
        if not body.totp_code:
            # Password was right; ask for the second factor without counting a failure.
            return JSONResponse({"detail": "Enter the 6-digit code from your authenticator app.",
                                 "totp_required": True}, status_code=401)
        step = match_totp(user.totp_secret or "", body.totp_code)
        if step is None or step <= (user.totp_last_step or 0):  # wrong, or already used once
            context.throttle.fail(key)
            return JSONResponse({"detail": "That code didn't match. Codes change every 30 seconds.",
                                 "totp_required": True}, status_code=401)
        user.totp_last_step = step
        db.commit()
    context.throttle.reset(key)
    _set_cookie(response, context, user)
    return {"user": user_out(user)}


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/password")
def change_password(body: PasswordChange, response: Response, user: User = Depends(current_user),
                    db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    key = f"password:{user.id}"
    if context.throttle.blocked(key):
        raise HTTPException(429, "Too many wrong passwords. Try again in a few minutes.")
    if not verify_password(body.current_password, user.password_hash):
        context.throttle.fail(key)
        raise HTTPException(400, "Current password is wrong.")
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1  # signs out every other session
    db.commit()
    _set_cookie(response, context, user)
    return {"user": user_out(user)}
