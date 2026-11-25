"""Run Hearth locally with a simulated Docker daemon and a synthetic modpack catalog.

    python tools/demo.py        # then open http://127.0.0.1:8000  (admin / demo-password-123)

The agent is the real agent code; only Docker and the Modrinth/Mojang APIs are faked, so the
UI can be explored on a machine without Docker or internet access. All data is made up.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "panel"), str(ROOT / "agent"), str(ROOT / "tools")]

from fake_docker import FakeDocker  # noqa: E402
from hearth_agent.app import create_app as create_agent  # noqa: E402
from hearth_agent.config import AgentConfig  # noqa: E402
from hearth_panel.config import Settings  # noqa: E402
from hearth_panel.context import PanelContext  # noqa: E402
from hearth_panel.main import create_app as create_panel  # noqa: E402

PACKS = [
    ("DEMOPK01", "example-tech-pack", "Example Tech Pack", "Factories, pipes and power grids. (demo data)", "neoforge", "1.21.1", 4_412_000),
    ("DEMOPK02", "cozy-farmlands", "Cozy Farmlands", "Slow-paced farming and cooking. (demo data)", "fabric", "1.20.1", 2_310_500),
    ("DEMOPK03", "skyblock-sample", "Skyblock Sample", "Start on one block in the void. (demo data)", "forge", "1.20.1", 1_874_200),
    ("DEMOPK04", "magic-sampler", "Magic Sampler", "Spells, rituals and mana. (demo data)", "neoforge", "1.21.1", 988_000),
    ("DEMOPK05", "vanilla-plus-demo", "Vanilla Plus Demo", "Small quality-of-life additions. (demo data)", "fabric", "1.21.4", 612_300),
    ("DEMOPK06", "adventure-quest-demo", "Adventure Quest Demo", "Dungeons, bosses and quest lines. (demo data)", "forge", "1.19.2", 403_900),
]


def catalog(request: httpx.Request) -> httpx.Response:
    url = request.url
    path = url.path
    if "piston-meta" in url.host:
        versions = [{"id": v, "type": "release", "releaseTime": f"2025-01-{i + 1:02d}"} for i, v in
                    enumerate(["1.21.4", "1.21.3", "1.21.1", "1.21", "1.20.6", "1.20.4", "1.20.1", "1.19.4", "1.19.2", "1.18.2", "1.16.5", "1.12.2"])]
        return httpx.Response(200, json={"versions": versions})
    if "papermc" in url.host:
        return httpx.Response(200, json={"versions": {"1.21": ["1.21.4", "1.21.3", "1.21.1"], "1.20": ["1.20.6", "1.20.4", "1.20.1"]}})
    if "fabricmc" in url.host or "quiltmc" in url.host:
        return httpx.Response(200, json=[{"version": v, "stable": True} for v in ["1.21.4", "1.21.1", "1.20.4", "1.20.1"]])
    facets = json.loads(url.params.get("facets", "[]") or "[]") if "modrinth" in url.host else []
    project_type = next((f[0].split(":", 1)[1] for f in facets if f and f[0].startswith("project_type:")), None)
    if "modrinth" in url.host and path.endswith("/search") and project_type in ("plugin", "mod"):
        kind = project_type
        names = [("Essentials Demo", "Homes, warps and kits. (demo data)", 2_100_000),
                 ("Chunky Demo", "Pre-generate the world to stop lag. (demo data)", 980_000),
                 ("Map Viewer Demo", "A live web map of your world. (demo data)", 640_000)]
        q = url.params.get("query", "").lower()
        hits = [{"project_id": f"DEMO{kind[0].upper()}{i}", "slug": n.lower().replace(" ", "-"), "title": n, "description": d,
                 "author": "demo", "icon_url": "", "downloads": dl} for i, (n, d, dl) in enumerate(names) if q in n.lower()]
        return httpx.Response(200, json={"total_hits": len(hits), "hits": hits})
    if "modrinth" in url.host and path.endswith("/search"):
        q = url.params.get("query", "").lower()
        hits = [{"project_id": pid, "slug": slug, "title": title, "description": desc, "author": "demo",
                 "icon_url": "", "downloads": dl, "date_modified": "2026-11-0%dT10:00:00Z" % (i + 1),
                 "categories": [loader, "technology"], "versions": [mc]}
                for i, (pid, slug, title, desc, loader, mc, dl) in enumerate(PACKS) if q in title.lower()]
        return httpx.Response(200, json={"total_hits": len(hits), "hits": hits})
    if "modrinth" in url.host and "/project/" in path:
        pid = path.split("/project/")[1].split("/")[0]
        pack = next((p for p in PACKS if pid in (p[0], p[1])), PACKS[0])
        if path.endswith("/version"):
            return httpx.Response(200, json=[
                {"id": f"{pack[0]}V{n}", "name": f"{pack[2]} {n}.0", "version_number": f"{n}.0",
                 "game_versions": [pack[5]], "loaders": [pack[4]], "version_type": "beta" if n == 6 else "release",
                 "date_published": f"2026-{n + 4:02d}-01T00:00:00Z", "downloads": 1000 * n}
                for n in range(6, 0, -1)])
        return httpx.Response(200, json={"id": pack[0], "slug": pack[1], "title": pack[2], "project_type": "modpack",
                                         "description": pack[3], "icon_url": None, "downloads": pack[6],
                                         "game_versions": [pack[5]], "loaders": [pack[4]]})
    return httpx.Response(404)


def serve(app, port: int) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hearth-demo-"))
    settings = Settings.from_env()
    settings = Settings(**{**settings.__dict__, "database_url": f"sqlite:///{tmp / 'panel.db'}", "secret_key": None,
                           "data_dir": tmp})
    context = PanelContext.build(settings, public_transport=httpx.MockTransport(catalog))
    serve(create_panel(context, run_updater=False), 8000)
    time.sleep(1.5)

    c = httpx.Client(base_url="http://127.0.0.1:8000")
    c.post("/api/auth/setup", json={"username": "admin", "password": "demo-password-123"}).raise_for_status()
    node = c.post("/api/nodes", json={"name": "home-box", "location": "Basement", "url": "http://127.0.0.1:8081",
                                      "public_host": "play.example.net", "memory_mb": 32768}).json()
    remote = c.post("/api/nodes", json={"name": "fsn-vps", "location": "Rented VPS · Falkenstein",
                                        "url": "http://127.0.0.1:8099", "public_host": "eu.example.net"}).json()
    c.post(f"/api/nodes/{node['id']}/allocations", json={"ports": "25565-25580, 2456-2460"})
    c.post(f"/api/nodes/{remote['id']}/allocations", json={"ports": "25565-25570"})

    agent_config = AgentConfig(token=node["token"], data_dir=tmp / "servers", state_dir=tmp / "agent",
                               sample_interval=2, history_size=60)
    serve(create_agent(agent_config, docker_factory=FakeDocker), 8081)
    time.sleep(1)

    friend = c.post("/api/users", json={"username": "sam", "password": "friend-password-1"}).json()
    base = {"node_id": node["id"], "accept_eula": True}
    servers = [
        {"name": "Survival", "template": "minecraft-java", "memory_mb": 6144, "variables": {"TYPE": "PAPER", "VERSION": "1.21.4", "MOTD": "Welcome home"}},
        {"name": "Tech Weekend", "template": "minecraft-java", "memory_mb": 10240, "owner_id": friend["id"],
         "modpack": {"provider": "modrinth", "project_id": "example-tech-pack", "version_id": "DEMOPK01V4"}},
        {"name": "Creative Build", "template": "minecraft-java", "memory_mb": 4096, "start": False, "variables": {"TYPE": "FABRIC", "VERSION": "1.21.1", "MODE": "creative"}},
        {"name": "Vikings", "template": "valheim", "memory_mb": 4096, "allocation_id": None, "start": False, "variables": {"SERVER_PASS": "skol-2026"}},
    ]
    for body in servers:
        r = c.post("/api/servers", json={**base, **body})
        print(r.status_code, r.json().get("name"), r.json().get("warning") or "")
    c.post("/api/servers", json={**base, "node_id": remote["id"], "name": "EU Lobby", "template": "minecraft-java",
                                 "memory_mb": 2048, "variables": {"TYPE": "PURPUR", "VERSION": "1.21.4"}})
    c.post("/api/servers/2/updates/check")
    print("Hearth demo running at http://127.0.0.1:8000  (admin / demo-password-123)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
