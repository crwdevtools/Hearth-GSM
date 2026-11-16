"""Panel application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .context import PanelContext
from .routers import account, admin, auth, backups, catalog, files, mods, nodes, players, schedules, servers, sharing, users
from .services import settings_store, updater
from .services import backups as backup_jobs
from .services.monitor import Monitor
from .services.scheduler import Scheduler
from .services.http import UpstreamError
from .services.node_client import NodeError
from .services.provisioning import ProvisionError
from .templates import TemplateError

log = logging.getLogger("hearth.panel")

CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def register_local_node(db, settings: Settings) -> None:
    """With the bundled docker-compose, the panel and an agent share HEARTH_AGENT_TOKEN; register that
    agent as the "local" node on first start so a fresh install works without manual node setup."""
    from sqlalchemy import select

    from .models import Allocation, Node

    if not settings.local_node_token:
        return
    existing = db.scalar(select(Node).where(Node.url == settings.local_node_url))
    if existing is not None:
        # .env is the source of truth for the bundled agent: re-running setup issues a new token.
        if existing.token != settings.local_node_token:
            existing.token = settings.local_node_token
            db.commit()
            log.info("updated the local node's token from HEARTH_AGENT_TOKEN")
        return
    node = Node(name="local", location="This machine", url=settings.local_node_url,
                token=settings.local_node_token, public_host=settings.local_public_host)
    db.add(node)
    db.flush()
    db.add_all(Allocation(node_id=node.id, port=p) for p in [*range(25565, 25576), 19132, 2456, 2457])
    db.commit()
    log.info("registered the local agent at %s as node 'local'", settings.local_node_url)


def create_app(context: PanelContext | None = None, *, run_updater: bool = True) -> FastAPI:
    context = context or PanelContext.build(Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        context.db.create_all()
        with context.db.sessionmaker() as db:
            context.secret = settings_store.secret_key(db, context.settings)
            context.modpacks.curseforge_key = settings_store.curseforge_key(db, context.settings)
            register_local_node(db, context.settings)
        tasks = []
        if run_updater:  # background work: modpack updates, status events + auto-sleep, schedules
            backup_jobs.resume_pending(context)
            tasks = [asyncio.create_task(updater.run_forever(context)),
                     asyncio.create_task(app.state.monitor.run_forever()),
                     asyncio.create_task(app.state.scheduler.run_forever())]
        yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await context.close()

    app = FastAPI(title="Hearth", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.ctx = context
    app.state.monitor = Monitor(context)
    app.state.scheduler = Scheduler(context)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")  # frame-ancestors, for older browsers
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(NodeError)
    async def node_error(_, exc: NodeError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(UpstreamError)
    async def upstream_error(_, exc: UpstreamError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(ProvisionError)
    @app.exception_handler(TemplateError)
    async def bad_input(_, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        message = str(first.get("msg", "Invalid input")).removeprefix("Value error, ")
        # errors() can hold the raw ValueError from custom validators; encode it safely.
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        return JSONResponse({"detail": f"{field}: {message}", "errors": errors}, status_code=422)

    for module in (auth, account, users, nodes, servers, files, backups, schedules, players, mods, sharing,
                   catalog, admin):
        app.include_router(module.router)

    web = context.settings.web_dir
    if web.exists():
        app.mount("/assets", StaticFiles(directory=web), name="assets")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def main() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=os.environ.get("HEARTH_LOG_LEVEL", "INFO"))
    uvicorn.run(create_app(), host=os.environ.get("HEARTH_HOST", "0.0.0.0"),
                port=int(os.environ.get("HEARTH_PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("HEARTH_TRUSTED_PROXIES", "127.0.0.1"))


if __name__ == "__main__":
    main()
