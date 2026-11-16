"""HTTP client the panel uses to talk to node agents."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..models import Node


class NodeError(Exception):
    def __init__(self, node: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.node = node
        self.status = status


class NodeClient:
    """One shared connection pool; per-request auth header and base URL."""

    def __init__(self, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0), transport=transport)

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, node: Node, method: str, path: str, *, timeout: float | None = None,
                    **kwargs: Any) -> Any:
        url = node.url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {node.token}"}
        try:
            resp = await self.http.request(method, url, headers=headers,
                                           timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                                           **kwargs)
        except httpx.TimeoutException as exc:
            raise NodeError(node.name, f"Node “{node.name}” did not respond in time.", 504) from exc
        except httpx.HTTPError as exc:
            raise NodeError(node.name, f"Node “{node.name}” is unreachable ({exc.__class__.__name__}).") from exc
        if resp.status_code == 401:
            raise NodeError(node.name, f"Node “{node.name}” rejected the panel's token. Re-copy it from Admin → Nodes.")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            message = detail if isinstance(detail, str) else f"Node returned HTTP {resp.status_code}."
            status = resp.status_code if resp.status_code in (400, 403, 404, 409, 413, 415) else 502
            raise NodeError(node.name, message, status)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise NodeError(node.name, f"Node “{node.name}” sent an unreadable reply. Is the agent URL right?") from exc

    # ----- system / servers ---------------------------------------------------------------

    async def health(self, node: Node) -> dict[str, Any]:
        return await self._call(node, "GET", "/api/health", timeout=4)

    async def system(self, node: Node) -> dict[str, Any]:
        return await self._call(node, "GET", "/api/system", timeout=6)

    async def statuses(self, node: Node) -> list[dict[str, Any]]:
        return await self._call(node, "GET", "/api/servers", timeout=5)

    async def status(self, node: Node, uuid: str) -> dict[str, Any]:
        return await self._call(node, "GET", f"/api/servers/{uuid}", timeout=5)

    async def apply(self, node: Node, spec: dict[str, Any], start: bool = False) -> None:
        await self._call(node, "PUT", f"/api/servers/{spec['uuid']}", params={"start": str(start).lower()}, json=spec)

    async def delete(self, node: Node, uuid: str, purge: bool = True) -> None:
        await self._call(node, "DELETE", f"/api/servers/{uuid}", params={"purge": str(purge).lower()}, timeout=60)

    async def power(self, node: Node, uuid: str, action: str) -> None:
        await self._call(node, "POST", f"/api/servers/{uuid}/power", json={"action": action})

    async def command(self, node: Node, uuid: str, command: str) -> str | None:
        data = await self._call(node, "POST", f"/api/servers/{uuid}/command", json={"command": command}, timeout=20)
        return (data or {}).get("output")

    # ----- files --------------------------------------------------------------------------

    async def files(self, node: Node, uuid: str, path: str) -> list[dict[str, Any]]:
        return await self._call(node, "GET", f"/api/servers/{uuid}/files", params={"path": path})

    async def read_file(self, node: Node, uuid: str, path: str) -> str:
        data = await self._call(node, "GET", f"/api/servers/{uuid}/files/content", params={"path": path})
        return data["content"]

    async def write_file(self, node: Node, uuid: str, path: str, content: str) -> None:
        await self._call(node, "PUT", f"/api/servers/{uuid}/files/content", json={"path": path, "content": content})

    async def mkdir(self, node: Node, uuid: str, path: str) -> None:
        await self._call(node, "POST", f"/api/servers/{uuid}/files/mkdir", json={"path": path})

    async def rename(self, node: Node, uuid: str, source: str, target: str) -> None:
        await self._call(node, "POST", f"/api/servers/{uuid}/files/rename", json={"source": source, "target": target})

    async def delete_file(self, node: Node, uuid: str, path: str) -> None:
        await self._call(node, "DELETE", f"/api/servers/{uuid}/files", params={"path": path})

    # ----- backups, transfers, sleep, tunnel ---------------------------------------------

    async def backup(self, node, uuid, backup_id, ignore=()):
        await self._call(node, "POST", f"/api/servers/{uuid}/backups", json={"backup_id": backup_id, "ignore": list(ignore)})

    async def backup_status(self, node, uuid, backup_id):
        return await self._call(node, "GET", f"/api/servers/{uuid}/backups/{backup_id}", timeout=5)

    async def backup_delete(self, node, uuid, backup_id):
        await self._call(node, "DELETE", f"/api/servers/{uuid}/backups/{backup_id}")

    async def backup_restore(self, node, uuid, backup_id):
        await self._call(node, "POST", f"/api/servers/{uuid}/backups/{backup_id}/restore")

    async def backup_download(self, node, uuid, backup_id):
        return await _stream_call(self, node, "GET", f"/api/servers/{uuid}/backups/{backup_id}/download")

    async def upload(self, node, uuid, path, stream, length=None):
        headers = {"content-length": str(length)} if length else None
        resp = await _stream_call(self, node, "PUT", f"/api/servers/{uuid}/files/upload",
                                  params={"path": path}, content=stream, headers=headers)
        await resp.aclose()

    async def extract(self, node, uuid, archive, target, replace=False, world=False):
        return await self._call(node, "POST", f"/api/servers/{uuid}/files/extract", timeout=600,
                                params={"world": str(world).lower()},
                                json={"archive": archive, "target": target, "replace": replace})

    async def archive(self, node, uuid, path):
        return await _stream_call(self, node, "GET", f"/api/servers/{uuid}/files/archive", params={"path": path})

    async def download(self, node, uuid, path):
        return await _stream_call(self, node, "GET", f"/api/servers/{uuid}/files/download", params={"path": path})

    async def fetch(self, node, uuid, url, path, sha512=None):
        return await self._call(node, "POST", f"/api/servers/{uuid}/files/fetch", timeout=300,
                                json={"url": url, "path": path, "sha512": sha512})

    async def copy_from(self, node, uuid, source):
        await self._call(node, "POST", f"/api/servers/{uuid}/copy-from/{source}", timeout=600)

    async def metrics(self, node, uuid):
        return await self._call(node, "GET", f"/api/servers/{uuid}/metrics", timeout=5)

    async def sleep(self, node, uuid, motd="Sleeping - join to wake me up"):
        await self._call(node, "POST", f"/api/servers/{uuid}/sleep", json={"motd": motd}, timeout=120)

    async def tunnel_status(self, node):
        return await self._call(node, "GET", "/api/tunnel", timeout=5)

    async def tunnel_set(self, node, secret):
        await self._call(node, "PUT", "/api/tunnel", json={"secret": secret}, timeout=180)

    async def tunnel_clear(self, node):
        await self._call(node, "DELETE", "/api/tunnel")


# ----- streaming + extended node API (backups, transfers, sleep, tunnel) ------------------------

async def _stream_call(client: "NodeClient", node: Node, method: str, path: str, **kwargs):
    """Open a streaming request to the agent; caller must close the returned response."""
    url = node.url.rstrip("/") + path
    req = client.http.build_request(method, url, headers={"Authorization": f"Bearer {node.token}"},
                                    timeout=httpx.Timeout(600, connect=5), **kwargs)
    try:
        resp = await client.http.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise NodeError(node.name, f"Node “{node.name}” is unreachable ({exc.__class__.__name__}).") from exc
    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        try:
            detail = httpx.Response(resp.status_code, content=body).json().get("detail")
        except ValueError:
            detail = None
        raise NodeError(node.name, detail or f"Node returned HTTP {resp.status_code}.",
                        resp.status_code if resp.status_code in (400, 404, 409, 413, 415, 507) else 502)
    return resp


def console_ws_url(node: Node, uuid: str) -> str:
    parts = urlsplit(node.url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/api/servers/{uuid}/console", "", ""))
