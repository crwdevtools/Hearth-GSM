import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "panel"), str(ROOT / "agent")]

from fastapi.testclient import TestClient  # noqa: E402

from hearth_panel.config import Settings  # noqa: E402
from hearth_panel.context import PanelContext  # noqa: E402
from hearth_panel.main import create_app  # noqa: E402


class FakeAgent:
    """Stands in for a node agent at http://agent:8080."""

    def __init__(self) -> None:
        self.specs: dict[str, dict] = {}
        self.power: list[tuple[str, str]] = []
        self.commands: list[tuple[str, str]] = []
        self.backups: dict[str, dict] = {}
        self.restored: list[str] = []
        self.fetched: list[dict] = []
        self.files: dict[str, str] = {}
        self.copies: list[tuple[str, str]] = []
        self.slept: list[str] = []
        self.command_output = "ok"
        self.fail_backups = False
        self.online = True
        self.state = "offline"  # what every server reports in the status list

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("down", request=request)
        if request.headers.get("authorization", "").count(" ") != 1:
            return httpx.Response(401)
        path, method = request.url.path, request.method
        if path == "/api/system":
            return httpx.Response(200, json={"version": "test", "cpus": 4, "memory_total": 8 << 30,
                                             "disk_total": 100 << 30, "disk_free": 50 << 30,
                                             "docker_version": "27", "servers": len(self.specs)})
        if path == "/api/servers":
            return httpx.Response(200, json=[{"uuid": u, "state": self.state, "history": []} for u in self.specs])
        parts = path.split("/")
        uuid = parts[3]
        tail = "/".join(parts[4:])
        if method == "PUT" and not tail:
            self.specs[uuid] = json.loads(request.content)
            return httpx.Response(202, json={"accepted": True})
        if method == "DELETE" and not tail:
            self.specs.pop(uuid, None)
            return httpx.Response(204)
        if tail == "backups" and method == "POST":
            body = json.loads(request.content)
            state = {"state": "failed", "error": "disk full"} if self.fail_backups else {"state": "ready", "size": 4096}
            self.backups[body["backup_id"]] = state
            return httpx.Response(202, json={"accepted": True})
        if tail.startswith("backups/"):
            bid = tail.split("/")[1]
            if tail.endswith("/restore"):
                self.restored.append(bid)
                return httpx.Response(202, json={"accepted": True})
            if method == "DELETE":
                self.backups.pop(bid, None)
                return httpx.Response(204)
            if tail.endswith("/download"):
                return httpx.Response(200, content=b"tar-bytes")
            return httpx.Response(200, json=self.backups.get(bid, {"state": "missing"}))
        if tail == "files/fetch":
            self.fetched.append(json.loads(request.content))
            return httpx.Response(200, json={"size": 10})
        if tail == "files/content" and method == "GET":
            path = request.url.params["path"]
            if path in self.files:
                return httpx.Response(200, json={"content": self.files[path]})
            return httpx.Response(404, json={"detail": "File not found."})
        if tail == "files/upload":
            self.files[request.url.params["path"]] = request.content.decode(errors="replace")
            return httpx.Response(204)
        if tail == "files/extract":
            return httpx.Response(200, json={"path": json.loads(request.content)["target"]})
        if tail.startswith("copy-from/"):
            self.copies.append((uuid, tail.split("/")[1]))
            return httpx.Response(204)
        if tail == "metrics":
            return httpx.Response(200, json={"minutes": [{"t": 0, "cpu": 5.0, "mem": 1}], "recent": []})
        if tail == "sleep":
            self.slept.append(uuid)
            return httpx.Response(202, json={"accepted": True})
        if tail == "power":
            self.power.append((uuid, json.loads(request.content)["action"]))
            return httpx.Response(202, json={"accepted": True})
        if tail == "command":
            self.commands.append((uuid, json.loads(request.content)["command"]))
            return httpx.Response(200, json={"output": self.command_output})
        if tail == "files":
            return httpx.Response(200, json=[{"name": "server.properties", "is_dir": False, "size": 10,
                                              "modified": 0, "is_symlink": False}])
        return httpx.Response(404, json={"detail": "nope"})


MODRINTH_VERSIONS = [
    {"id": "v3", "name": "Pack 3.0 beta", "version_number": "3.0b", "game_versions": ["1.21.1"],
     "loaders": ["neoforge"], "version_type": "beta", "date_published": "2026-11-01T00:00:00Z"},
    {"id": "v2", "name": "Pack 2.0", "version_number": "2.0", "game_versions": ["1.21.1"],
     "loaders": ["neoforge"], "version_type": "release", "date_published": "2026-10-01T00:00:00Z"},
    {"id": "v1", "name": "Pack 1.0", "version_number": "1.0", "game_versions": ["1.20.1"],
     "loaders": ["forge"], "version_type": "release", "date_published": "2026-01-01T00:00:00Z"},
]


class FakePublic:
    def __init__(self) -> None:
        self.versions = list(MODRINTH_VERSIONS)
        self.posts: list[tuple[str, dict]] = []
        self.reachable = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":  # Discord + outgoing webhooks
            self.posts.append((url, json.loads(request.content or b"{}")))
            return httpx.Response(204)
        if "api.mcsrvstat.us" in url:
            return httpx.Response(200, json={"online": self.reachable, "players": {"online": 1, "max": 20}})
        if "modrinth.com/v2/search" in url and "project_type%3Aplugin" in url or "project_type:plugin" in url:
            return httpx.Response(200, json={"total_hits": 1, "hits": [{
                "project_id": "PLUGIN01", "slug": "essentials-demo", "title": "Essentials Demo",
                "description": "d", "author": "a", "icon_url": "", "downloads": 9}]})
        if "/project/PLUGIN01/version" in url or "/project/DEP00001/version" in url:
            pid = "PLUGIN01" if "PLUGIN01" in url else "DEP00001"
            deps = [{"project_id": "DEP00001", "dependency_type": "required"}] if pid == "PLUGIN01" else []
            return httpx.Response(200, json=[{"id": f"{pid}v", "version_type": "release", "dependencies": deps,
                                              "files": [{"primary": True, "filename": f"{pid.lower()}.jar",
                                                         "url": f"https://cdn.modrinth.com/data/{pid}/x.jar",
                                                         "hashes": {"sha512": "0" * 128}}]}])
        if "api.modrinth.com/v2/search" in url:
            return httpx.Response(200, json={"total_hits": 1, "hits": [{
                "project_id": "AAAA1111", "slug": "cool-pack", "title": "Cool Pack", "description": "d",
                "author": "me", "icon_url": "", "downloads": 1234, "date_modified": "2026-11-01",
                "categories": ["neoforge", "tech"], "versions": ["1.21.1", "1.20.1"]}]})
        if url.endswith("/project/cool-pack") or url.endswith("/project/AAAA1111"):
            return httpx.Response(200, json={"id": "AAAA1111", "slug": "cool-pack", "title": "Cool Pack",
                                             "project_type": "modpack", "description": "d", "icon_url": None,
                                             "downloads": 1, "game_versions": ["1.21.1"], "loaders": ["neoforge"]})
        if url.endswith("/version"):
            return httpx.Response(200, json=self.versions)
        if "piston-meta" in url:
            return httpx.Response(200, json={"versions": [
                {"id": "1.21.4", "type": "release", "releaseTime": "2024-12-03"},
                {"id": "24w14a", "type": "snapshot", "releaseTime": "2024-04-01"},
                {"id": "1.20.1", "type": "release", "releaseTime": "2023-06-12"}]})
        if "fill.papermc.io" in url:
            return httpx.Response(200, json={"versions": {"1.21": ["1.21.4"], "1.20": ["1.20.1"]}})
        return httpx.Response(404)


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def public():
    return FakePublic()


@pytest.fixture(autouse=True)
def fast_backups(monkeypatch):
    from hearth_panel.services import backups

    monkeypatch.setattr(backups, "POLL", 0.01)


@pytest.fixture
def client(tmp_path, agent, public):
    settings = Settings.from_env()
    settings = Settings(**{**settings.__dict__, "database_url": f"sqlite:///{tmp_path / 'test.db'}",
                           "secret_key": "test-secret-" + "x" * 32, "curseforge_api_key": None})
    context = PanelContext.build(settings, node_transport=httpx.MockTransport(agent),
                                 public_transport=httpx.MockTransport(public))
    with TestClient(create_app(context, run_updater=False)) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def node(admin):
    r = admin.post("/api/nodes", json={"name": "Home", "location": "Basement", "url": "http://agent:8080",
                                       "public_host": "play.example.net"})
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    r = admin.post(f"/api/nodes/{nid}/allocations", json={"ports": "25565-25570"})
    assert r.json()["added"] == 6
    return r.json() | {"id": nid}
