"""FastAPI dependencies: context, DB session, current user, access checks."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from .context import PanelContext
from .models import PERMISSIONS, ApiToken, Server, ServerShare, User
from .security import SESSION_COOKIE, api_token_matches, api_token_prefix, read_session


def ctx(request: Request) -> PanelContext:
    return request.app.state.ctx


def db_session(request: Request) -> Iterator[Session]:
    yield from request.app.state.ctx.db.session()


def user_from_token(context: PanelContext, db: Session, token: str | None) -> User | None:
    if not token:
        return None
    parsed = read_session(context.secret, token)
    if parsed is None:
        return None
    user = db.get(User, parsed[0])
    if user is None or user.session_version != parsed[1]:
        return None
    return user


def user_from_api_token(db: Session, token: str) -> User | None:
    prefix = api_token_prefix(token)
    if prefix is None:
        return None
    for row in db.scalars(select(ApiToken).where(ApiToken.prefix == prefix)):
        if api_token_matches(token, row.token_hash):
            row.last_used = datetime.now(timezone.utc)
            db.commit()
            return row.user
    return None


def current_user(request: Request, db: Session = Depends(db_session)) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        user = user_from_api_token(db, auth[7:].strip())
        if user is None:
            raise HTTPException(401, "Invalid API token.")
        return user
    user = user_from_token(request.app.state.ctx, db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Only admins can do that.")
    return user


def permissions_for(server: Server, user: User) -> set[str]:
    """Everything for admins and the owner; a shared user gets what they were granted."""
    if user.is_admin or server.owner_id == user.id:
        return set(PERMISSIONS) | {"owner"}
    share = next((s for s in server.shares if s.user_id == user.id), None)
    return set(share.permissions) & set(PERMISSIONS) if share else set()


def server_for(server_id: int, user: User, db: Session, permission: str | None = None) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(404, "Server not found.")
    perms = permissions_for(server, user)
    if not perms and not any(s.user_id == user.id for s in server.shares):
        raise HTTPException(404, "Server not found.")
    if permission and permission not in perms:
        raise HTTPException(403, "You don't have permission to do that on this server.")
    return server


def accessible_server(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)) -> Server:
    return server_for(server_id, user, db)


def server_with(permission: str) -> Callable[..., Server]:
    """Dependency: the server, if the caller holds `permission` on it ("owner" = owner/admin only)."""

    def dependency(server_id: int, user: User = Depends(current_user), db: Session = Depends(db_session)) -> Server:
        return server_for(server_id, user, db, permission)

    return dependency


def visible_servers_query(user: User):
    query = select(Server).order_by(Server.name)
    if not user.is_admin:
        shared = select(ServerShare.server_id).where(ServerShare.user_id == user.id)
        query = query.where((Server.owner_id == user.id) | (Server.id.in_(shared)))
    return query


def websocket_user(ws: WebSocket, db: Session) -> User | None:
    return user_from_token(ws.app.state.ctx, db, ws.cookies.get(SESSION_COOKIE))
