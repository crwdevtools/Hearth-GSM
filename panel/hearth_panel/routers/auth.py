"""Sign-in, first-run setup, sign-out, password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..context import PanelContext
from ..deps import ctx, current_user, db_session, user_from_token
from ..models import User
from ..security import DUMMY_HASH, SESSION_COOKIE, hash_password, issue_session, verify_password, match_totp
from ..serializers import user_out
from ..services import pwned
from ..services.netguard import ip_is_local

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


def _setup_allowed(request: Request, context: PanelContext) -> bool:
    """Until an admin exists, anyone who reaches the panel could claim it. Only allow that from
    this machine or the local network, unless HEARTH_ALLOW_REMOTE_SETUP is set."""
    client = request.client.host if request.client else ""
    return context.settings.allow_remote_setup or ip_is_local(client)


@router.get("/state")
def state(request: Request, db: Session = Depends(db_session), context: PanelContext = Depends(ctx)):
    needs_setup = db.scalar(select(func.count(User.id))) == 0
    user = user_from_token(context, db, request.cookies.get(SESSION_COOKIE))
    out = {"needs_setup": needs_setup, "user": user_out(user) if user else None}
    if needs_setup:
        out["setup_allowed"] = _setup_allowed(request, context)
    return out


SETUP_REMOTE = ("Create the admin account from this computer or your local network "
                "(for example http://localhost:8000). To allow it from anywhere, set "
                "HEARTH_ALLOW_REMOTE_SETUP=true and restart.")


@router.post("/setup")
def setup(body: SetupBody, request: Request, response: Response, db: Session = Depends(db_session),
          context: PanelContext = Depends(ctx)):
    if not _setup_allowed(request, context):
        raise HTTPException(403, SETUP_REMOTE)
    if db.scalar(select(func.count(User.id))) > 0:
        raise HTTPException(409, "Setup is already complete. Sign in instead.")
    pwned.reject_breached_sync(context, body.password)
    user = User(username=body.username, email=body.email, password_hash=hash_password(body.password), is_admin=True)
    db.add(user)
    db.flush()
    # Two setups sent at the same moment could both pass the check above; only the first may stay.
    if db.scalar(select(func.count(User.id))) != 1:
        db.rollback()
        raise HTTPException(409, "Setup is already complete. Sign in instead.")
    db.commit()
    _set_cookie(response, context, user)
    return {"user": user_out(user)}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(db_session),
          context: PanelContext = Depends(ctx)):
    client = request.client.host if request.client else "?"
    key = f"{client}:{body.username.lower()}"
    # The across-usernames limit only means something for a real internet address. Behind
    # Docker Desktop or an untrusted proxy every visitor shares one internal address, and one
    # person's typos would lock everybody out.
    per_address = not ip_is_local(client)
    wait = max(context.throttle.blocked(key), context.address_throttle.blocked(client) if per_address else 0)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {int(wait // 60) + 1} min.")

    def failed() -> None:
        context.throttle.fail(key)
        if per_address:
            context.address_throttle.fail(client)  # not cleared by a later success on another account
    user = db.scalar(select(User).where(func.lower(User.username) == body.username.lower()))
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or not ok:
        failed()
        raise HTTPException(401, "Wrong username or password.")
    if user.totp_enabled:
        if not body.totp_code:
            # Password was right; ask for the second factor without counting a failure.
            return JSONResponse({"detail": "Enter the 6-digit code from your authenticator app.",
                                 "totp_required": True}, status_code=401)
        step = match_totp(user.totp_secret or "", body.totp_code)
        # Record the code as used in the same statement that checks it hasn't been, so two
        # sign-ins racing with one code can't both get through.
        used = step is not None and db.execute(
            update(User).where(User.id == user.id, func.coalesce(User.totp_last_step, 0) < step)
            .values(totp_last_step=step)).rowcount == 1
        db.commit()
        if not used:  # wrong, or already used once
            failed()
            return JSONResponse({"detail": "That code didn't match. Codes change every 30 seconds.",
                                 "totp_required": True}, status_code=401)
    context.throttle.reset(key)
    _set_cookie(response, context, user)
    return {"user": user_out(user)}


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/logout-everywhere", status_code=204)
def logout_everywhere(response: Response, user: User = Depends(current_user),
                      db: Session = Depends(db_session)) -> None:
    """Sessions are signed cookies, so signing out in one browser can't recall a copy of the
    cookie. Bumping the session version invalidates every session at once (API tokens stay)."""
    user.session_version += 1
    db.commit()
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
    pwned.reject_breached_sync(context, body.new_password)
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1  # signs out every other session
    db.commit()
    _set_cookie(response, context, user)
    return {"user": user_out(user)}
