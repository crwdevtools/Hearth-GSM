"""Shared outbound HTTP client for public APIs (Mojang, PaperMC, Modrinth, CurseForge...)."""

from __future__ import annotations

from typing import Any

import httpx


class UpstreamError(Exception):
    """A third-party API could not be reached or returned an error."""

    def __init__(self, service: str, message: str, status: int = 502) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status


class PublicHTTP:
    def __init__(self, user_agent: str, timeout: float) -> None:
        self.client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )

    async def get_json(self, service: str, url: str, *, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> Any:
        try:
            resp = await self.client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(service, f"could not connect ({exc.__class__.__name__})") from exc
        return self._decode(service, resp)

    async def post_json(self, service: str, url: str, body: Any, *, headers: dict[str, str] | None = None) -> Any:
        try:
            resp = await self.client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(service, f"could not connect ({exc.__class__.__name__})") from exc
        return self._decode(service, resp)

    @staticmethod
    def _decode(service: str, resp: httpx.Response) -> Any:
        if resp.status_code in (401, 403):
            raise UpstreamError(service, "the API key was rejected", 502)
        if resp.status_code == 404:
            raise UpstreamError(service, "not found", 404)
        if resp.status_code == 429:
            raise UpstreamError(service, "rate limited; try again in a minute", 503)
        if resp.status_code >= 400:
            raise UpstreamError(service, f"HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(service, "returned invalid JSON") from exc

    async def close(self) -> None:
        await self.client.aclose()
