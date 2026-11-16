"""HTTP + WebSocket API exposed by the agent to the panel."""

from __future__ import annotations

import asyncio
import secrets
import contextlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from . import VERSION, files
from .config import AgentConfig
from .backups import BackupStore, free_bytes
from .models import (BackupRequest, Command, ExtractRequest, FetchRequest, FileWrite, PathBody, PowerAction,
                     RenameBody, ServerSpec, SleepRequest, TunnelRequest, UUID_RE)
from .runtime import RuntimeFailure, ServerManager, ServerNotFound

log = logging.getLogger("hearth.agent")


def create_app(config: AgentConfig | None = None, docker_factory: Callable[[], Any] | None = None) -> FastAPI:
    config = config or AgentConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if docker_factory is None:
            import docker

            client = docker.from_env()
        else:
            client = docker_factory()
        app.state.manager = ServerManager(config, client)
        app.state.backups = BackupStore(app.state.manager)
        await asyncio.to_thread(app.state.manager.resume)
        app.state.manager.start_sampler()
        yield
        app.state.manager.stop_sampler()

    app = FastAPI(title="Hearth Agent", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)

    def authorize(token: str) -> bool:
        return hmac.compare_digest(token.encode(), config.token.encode())

    async def require_token(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not authorize(token):
            raise HTTPException(401, "Invalid agent token.")

    def manager() -> ServerManager:
        return app.state.manager

    def check_uuid(uuid: str) -> str:
        if not UUID_RE.match(uuid):
            raise HTTPException(400, "Invalid server id.")
        return uuid

    @app.exception_handler(ServerNotFound)
    async def _not_found(_, exc: ServerNotFound):
        return JSONResponse({"detail": "Server is not installed on this node."}, status_code=404)

    @app.exception_handler(RuntimeFailure)
    async def _runtime(_, exc: RuntimeFailure):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(files.FileAccessError)
    async def _files(_, exc: files.FileAccessError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    auth = [Depends(require_token)]

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @app.get("/api/system", dependencies=auth)
    async def system() -> dict[str, Any]:
        info = await asyncio.to_thread(manager().system_info)
        return {"version": VERSION, **info}

    @app.get("/api/servers", dependencies=auth)
    async def list_servers() -> list[dict[str, Any]]:
        return await asyncio.to_thread(manager().all_statuses)

    @app.put("/api/servers/{uuid}", status_code=202, dependencies=auth)
    async def put_server(uuid: str, spec: ServerSpec, start: bool = False) -> dict[str, Any]:
        if check_uuid(uuid) != spec.uuid:
            raise HTTPException(400, "Spec uuid does not match the URL.")
        await asyncio.to_thread(manager().apply_spec, spec, start)
        return {"accepted": True}

    @app.get("/api/servers/{uuid}", dependencies=auth)
    async def get_server(uuid: str) -> dict[str, Any]:
        manager().load_spec(check_uuid(uuid))
        return await asyncio.to_thread(manager().status, uuid)

    @app.delete("/api/servers/{uuid}", status_code=204, dependencies=auth)
    async def delete_server(uuid: str, purge: bool = True) -> None:
        await asyncio.to_thread(manager().delete, check_uuid(uuid), purge)
        if purge:
            await asyncio.to_thread(app.state.backups.delete_all, uuid)

    @app.post("/api/servers/{uuid}/power", status_code=202, dependencies=auth)
    async def power(uuid: str, body: PowerAction) -> dict[str, bool]:
        await asyncio.to_thread(manager().power, check_uuid(uuid), body.action)
        return {"accepted": True}

    @app.post("/api/servers/{uuid}/command", dependencies=auth)
    async def command(uuid: str, body: Command) -> dict[str, str | None]:
        output = await asyncio.to_thread(manager().send_command, check_uuid(uuid), body.command)
        return {"output": output}

    # ----- files -------------------------------------------------------------------------

    def root(uuid: str):
        mgr = manager()
        mgr.load_spec(check_uuid(uuid))
        return mgr.data_path(uuid)

    def writable_root(uuid: str):
        """The server folder, unless a restore or world import is rewriting it right now."""
        path = root(uuid)
        reason = manager().busy(uuid)
        if reason:
            raise HTTPException(409, reason)
        return path

    @app.get("/api/servers/{uuid}/files", dependencies=auth)
    async def file_list(uuid: str, path: str = "/") -> list[dict[str, Any]]:
        return await asyncio.to_thread(files.list_dir, root(uuid), path)

    @app.get("/api/servers/{uuid}/files/content", dependencies=auth)
    async def file_read(uuid: str, path: str) -> dict[str, str]:
        return {"content": await asyncio.to_thread(files.read_text, root(uuid), path)}

    @app.put("/api/servers/{uuid}/files/content", status_code=204, dependencies=auth)
    async def file_write(uuid: str, body: FileWrite) -> None:
        await asyncio.to_thread(files.write_text, writable_root(uuid), body.path, body.content)

    @app.post("/api/servers/{uuid}/files/mkdir", status_code=204, dependencies=auth)
    async def file_mkdir(uuid: str, body: PathBody) -> None:
        await asyncio.to_thread(files.make_dir, writable_root(uuid), body.path)

    @app.post("/api/servers/{uuid}/files/rename", status_code=204, dependencies=auth)
    async def file_rename(uuid: str, body: RenameBody) -> None:
        await asyncio.to_thread(files.rename, writable_root(uuid), body.source, body.target)

    @app.delete("/api/servers/{uuid}/files", status_code=204, dependencies=auth)
    async def file_delete(uuid: str, path: str) -> None:
        await asyncio.to_thread(files.delete, writable_root(uuid), path)

    # ----- backups -----------------------------------------------------------------------

    def backup_id_ok(backup_id: str) -> str:
        if not UUID_RE.match(backup_id):
            raise HTTPException(400, "Invalid backup id.")
        return backup_id

    @app.post("/api/servers/{uuid}/backups", status_code=202, dependencies=auth)
    async def create_backup(uuid: str, body: BackupRequest) -> dict[str, Any]:
        store: BackupStore = app.state.backups
        await asyncio.to_thread(store.create, check_uuid(uuid), body.backup_id, body.ignore)
        return {"accepted": True}

    @app.get("/api/servers/{uuid}/backups/{backup_id}", dependencies=auth)
    async def backup_status(uuid: str, backup_id: str) -> dict[str, Any]:
        return app.state.backups.status(check_uuid(uuid), backup_id_ok(backup_id))

    @app.delete("/api/servers/{uuid}/backups/{backup_id}", status_code=204, dependencies=auth)
    async def delete_backup(uuid: str, backup_id: str) -> None:
        await asyncio.to_thread(app.state.backups.delete, check_uuid(uuid), backup_id_ok(backup_id))

    @app.post("/api/servers/{uuid}/backups/{backup_id}/restore", status_code=202, dependencies=auth)
    async def restore_backup(uuid: str, backup_id: str) -> dict[str, bool]:
        await asyncio.to_thread(app.state.backups.restore, check_uuid(uuid), backup_id_ok(backup_id))
        return {"accepted": True}

    @app.get("/api/servers/{uuid}/backups/{backup_id}/download", dependencies=auth)
    async def download_backup(uuid: str, backup_id: str):
        path = app.state.backups.archive_path(check_uuid(uuid), backup_id_ok(backup_id))
        if not path.exists():
            raise HTTPException(404, "Backup file not found.")
        return FileResponse(path, media_type="application/gzip", filename=f"{uuid[:8]}-{backup_id[:8]}.tar.gz")

    # ----- uploads, archives, mod downloads ----------------------------------------------

    @app.put("/api/servers/{uuid}/files/upload", status_code=204, dependencies=auth)
    async def upload(uuid: str, request: Request, path: str = Query(..., max_length=1024)) -> None:
        target, tmp = files.open_upload(writable_root(uuid), path)
        need = int(request.headers.get("content-length") or 0)
        if need and need > free_bytes(target.parent) - 256 * 1024 * 1024:
            raise HTTPException(507, "Not enough free disk space on the node for this upload.")
        try:
            with open(tmp, "wb") as out:
                async for chunk in request.stream():
                    await asyncio.to_thread(out.write, chunk)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/servers/{uuid}/files/extract", dependencies=auth)
    async def extract(uuid: str, body: ExtractRequest, world: bool = False) -> dict[str, str]:
        mgr = manager()
        base = writable_root(uuid)
        if not world:  # plain unzip in the file manager
            dest = await asyncio.to_thread(files.extract_archive, base, body.archive, body.target,
                                           replace=body.replace, world=False)
            return {"path": dest}
        # A running server keeps the old world in memory and would save it over the new one, so
        # stop it for the swap and bring it back afterwards.
        mgr.begin_restore(uuid)
        error = None
        was_running = False
        try:
            was_running = await asyncio.to_thread(mgr.stop_for_maintenance, uuid)
            dest = await asyncio.to_thread(files.extract_archive, base, body.archive, body.target,
                                           replace=body.replace, world=True)
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            await asyncio.to_thread(mgr.end_restore, uuid, error and f"World import failed: {error}"[:500],
                                    was_running)
        return {"path": dest}

    @app.get("/api/servers/{uuid}/files/archive", dependencies=auth)
    async def archive(uuid: str, path: str = Query(..., max_length=1024)):
        base = root(uuid)
        tmp_dir = app.state.backups.root / ".exports"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out = tmp_dir / f"{uuid}-{secrets.token_hex(8)}.zip"

        def build() -> None:
            with manager().quiesced(uuid):
                files.archive_folder(base, path, out)

        try:
            await asyncio.to_thread(build)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        name = (path.strip("/").split("/")[-1] or "server") + ".zip"
        return FileResponse(out, media_type="application/zip", filename=name,
                            background=BackgroundTask(lambda: out.unlink(missing_ok=True)))

    @app.get("/api/servers/{uuid}/files/download", dependencies=auth)
    async def download_file(uuid: str, path: str = Query(..., max_length=1024)):
        target = files.resolve(root(uuid), path)
        if not target.is_file():
            raise HTTPException(404, "File not found.")
        return FileResponse(target, filename=target.name)

    @app.post("/api/servers/{uuid}/files/fetch", dependencies=auth)
    async def fetch(uuid: str, body: FetchRequest) -> dict[str, int]:
        size = await asyncio.to_thread(files.fetch, writable_root(uuid), body.url, body.path, body.sha512)
        return {"size": size}

    # ----- cloning, metrics, sleep -------------------------------------------------------

    @app.post("/api/servers/{uuid}/copy-from/{source}", status_code=204, dependencies=auth)
    async def copy_from(uuid: str, source: str) -> None:
        await asyncio.to_thread(manager().copy_from, check_uuid(uuid), check_uuid(source))

    @app.get("/api/servers/{uuid}/metrics", dependencies=auth)
    async def metrics(uuid: str) -> dict[str, Any]:
        return manager().metrics(check_uuid(uuid))

    @app.post("/api/servers/{uuid}/sleep", status_code=202, dependencies=auth)
    async def sleep(uuid: str, body: SleepRequest) -> dict[str, bool]:
        await asyncio.to_thread(manager().sleep, check_uuid(uuid), body.motd)
        return {"accepted": True}

    @app.post("/api/wake/{uuid}", status_code=202)
    async def wake(uuid: str, key: str = Query(..., max_length=128)) -> dict[str, bool]:
        """Called by a sleeping server's stand-in with its one-off key (no agent token)."""
        await asyncio.to_thread(manager().wake, check_uuid(uuid), key)
        return {"accepted": True}

    # ----- playit.gg tunnel --------------------------------------------------------------

    @app.get("/api/tunnel", dependencies=auth)
    async def tunnel_status() -> dict[str, Any]:
        return await asyncio.to_thread(manager().tunnel_status)

    @app.put("/api/tunnel", status_code=202, dependencies=auth)
    async def tunnel_set(body: TunnelRequest) -> dict[str, bool]:
        await asyncio.to_thread(manager().tunnel_set, body.secret)
        return {"accepted": True}

    @app.delete("/api/tunnel", status_code=204, dependencies=auth)
    async def tunnel_clear() -> None:
        await asyncio.to_thread(manager().tunnel_clear)

    # ----- console websocket -------------------------------------------------------------

    @app.websocket("/api/servers/{uuid}/console")
    async def console(ws: WebSocket, uuid: str, token: str = Query(default="")) -> None:
        header = ws.headers.get("authorization", "")
        supplied = header.partition(" ")[2] if header.lower().startswith("bearer ") else token
        if not authorize(supplied) or not UUID_RE.match(uuid):
            await ws.close(code=4401)
            return
        mgr = manager()
        try:
            mgr.load_spec(uuid)
        except ServerNotFound:
            await ws.close(code=4404)
            return
        await ws.accept()
        await ConsoleSession(ws, mgr, uuid).run()

    return app


class ConsoleSession:
    """Streams container output and status to one socket; accepts commands back."""

    def __init__(self, ws: WebSocket, manager: ServerManager, uuid: str) -> None:
        self.ws = ws
        self.manager = manager
        self.uuid = uuid
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self.closed = False
        self._stream: Any = None

    def _push(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(message)

    def _follow_logs(self, since: float | None) -> None:
        """Blocking: read container output until the stream ends (container stopped)."""
        stream = self.manager.log_stream(self.uuid, tail=200, since=since)
        if stream is None:
            return
        self._stream = stream
        try:
            for chunk in stream:
                if self.closed:
                    break
                text = chunk.decode("utf-8", "replace")
                try:
                    self.loop.call_soon_threadsafe(self._push, {"type": "log", "data": text})
                except RuntimeError:  # event loop closed while we were blocked on the stream
                    break
        finally:
            self._stream = None
            with contextlib.suppress(Exception):
                stream.close()

    async def _logs_forever(self) -> None:
        since: float | None = None  # first attach: last 200 lines
        while not self.closed:
            await asyncio.to_thread(self._follow_logs, since)
            # Re-attach from where this stream ended, so lines printed while the container was
            # restarting (between the stream closing and re-attaching) are not lost.
            since = time.time()
            await asyncio.sleep(2)

    async def _status_forever(self) -> None:
        while not self.closed:
            status = await asyncio.to_thread(self.manager.status, self.uuid)
            status.pop("history", None)
            self._push({"type": "status", "data": status})
            await asyncio.sleep(2)

    async def _sender(self) -> None:
        while True:
            message = await self.queue.get()
            await self.ws.send_json(message)

    async def _receiver(self) -> None:
        while True:
            try:
                message = json.loads(await self.ws.receive_text())
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("type") != "command":
                continue
            text = str(message.get("data", ""))[:2000]
            if not text.strip():
                continue
            try:
                output = await asyncio.to_thread(self.manager.send_command, self.uuid, text)
            except RuntimeFailure as exc:
                self._push({"type": "error", "data": str(exc)})
                continue
            if output:
                self._push({"type": "log", "data": output if output.endswith("\n") else output + "\n"})

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self._logs_forever()),
            asyncio.create_task(self._status_forever()),
            asyncio.create_task(self._sender()),
            asyncio.create_task(self._receiver()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:  # disconnects end the session; retrieve so asyncio doesn't warn
                task.exception()
        finally:
            self.closed = True
            if self._stream is not None:
                # Closing the HTTP response unblocks the log-reader thread immediately.
                with contextlib.suppress(Exception):
                    self._stream.close()
            for task in tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                await self.ws.close()
