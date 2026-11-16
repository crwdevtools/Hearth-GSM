"""Long-lived services shared by every request (attached to app.state.ctx)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import Settings
from .db import Database
from .security import LoginThrottle
from .services.cache import TTLCache
from .services.http import PublicHTTP
from .services.mc_versions import VersionService
from .services.modpacks import ModpackService
from .services.node_client import NodeClient


@dataclass
class PanelContext:
    settings: Settings
    db: Database
    http: PublicHTTP
    cache: TTLCache
    versions: VersionService
    modpacks: ModpackService
    nodes: NodeClient
    throttle: LoginThrottle
    secret: str = ""

    @classmethod
    def build(cls, settings: Settings, *, node_transport: httpx.AsyncBaseTransport | None = None,
              public_transport: httpx.AsyncBaseTransport | None = None) -> "PanelContext":
        db = Database(settings.database_url)
        http = PublicHTTP(settings.user_agent, settings.http_timeout)
        if public_transport is not None:
            http.client = httpx.AsyncClient(transport=public_transport, headers=http.client.headers)
        cache = TTLCache()
        return cls(
            settings=settings,
            db=db,
            http=http,
            cache=cache,
            versions=VersionService(http, cache),
            modpacks=ModpackService(http, cache),
            nodes=NodeClient(settings.http_timeout, transport=node_transport),
            throttle=LoginThrottle(),
        )

    async def close(self) -> None:
        await self.http.close()
        await self.nodes.close()
