"""Long-lived services shared by every request (attached to app.state.ctx)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from .config import Settings
from .db import Database
from .security import LoginThrottle
from .services import netguard
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
    # Failed sign-ins per address across all usernames: stops one address trying a common
    # password against every account, which the per-username limit alone doesn't.
    address_throttle: LoginThrottle = field(default_factory=lambda: LoginThrottle(limit=30, window=900.0))
    # Failed "share with <username>" lookups per user: the answer reveals whether an account
    # exists, so don't let anyone run through a list of names.
    share_throttle: LoginThrottle = field(default_factory=lambda: LoginThrottle(limit=10, window=600.0))
    secret: str = ""
    resolver: Callable[[str], Awaitable[list[str]]] = netguard.resolve  # swapped out in tests

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
