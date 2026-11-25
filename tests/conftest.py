import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "panel"), str(ROOT / "agent")]

from fastapi.testclient import TestClient  # noqa: E402

from hearth_panel import VERSION  # noqa: E402
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
        self.jars: dict[str, dict[str, str]] = {}  # server uuid -> {file name: sha512} in mods/plugins
        self.files: dict[str, str] = {}
        self.copies: list[tuple[str, str]] = []
        self.applies: list[tuple[str, str | None]] = []  # (server uuid, ?start= of each spec push)
        self.slept: list[tuple[str, str | None]] = []
        self.command_output = "ok"
        self.fail_backups = False
        self.pushed: list[str] = []
        self.received: dict[str, bytes] = {}
        self.storage = lambda request: httpx.Response(404)  # wired to FakePublic by the client fixture
        self.online = True
        self.state = "offline"  # what every server reports in the status list
        self.status_extra: dict = {}  # merged into every status (restarts, disk, ...)
        self.disk_free = 50 << 30

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            raise httpx.ConnectError("down", request=request)
        if request.headers.get("authorization", "").count(" ") != 1:
            return httpx.Response(401)
        path, method = request.url.path, request.method
        if path == "/api/system":
            return httpx.Response(200, json={"version": VERSION, "cpus": 4, "memory_total": 8 << 30,
                                             "disk_total": 100 << 30, "disk_free": self.disk_free,
                                             "docker_version": "27", "servers": len(self.specs)})
        if path == "/api/servers":
            return httpx.Response(200, json=[{"uuid": u, "state": self.state, "history": [], **self.status_extra}
                                             for u in self.specs])
        parts = path.split("/")
        uuid = parts[3]
        tail = "/".join(parts[4:])
        if method == "PUT" and not tail:
            self.specs[uuid] = json.loads(request.content)
            self.applies.append((uuid, request.url.params.get("start")))
            return httpx.Response(202, json={"accepted": True})
        if method == "DELETE" and not tail:
            self.specs.pop(uuid, None)
            return httpx.Response(204)
        if method == "GET" and not tail:
            if uuid not in self.specs:
                return httpx.Response(404, json={"detail": "Server not found."})
            return httpx.Response(200, json={"uuid": uuid, "state": self.state, **self.status_extra})
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
            if tail.endswith("/archive") and method == "PUT":
                if uuid not in self.specs:
                    return httpx.Response(404, json={"detail": "Server not found."})
                self.received[bid] = request.read()
                self.backups[bid] = {"state": "ready", "size": len(self.received[bid])}
                return httpx.Response(204)
            if method == "DELETE":
                self.backups.pop(bid, None)
                return httpx.Response(204)
            if tail.endswith("/download"):
                return httpx.Response(200, content=b"tar-bytes")
            if tail.endswith("/push"):  # "upload" straight into the fake storage
                body = json.loads(request.content)
                etags, push = [], {"state": "done"}
                for n, url in enumerate(body["urls"], 1):
                    put = httpx.Request("PUT", url, content=b"tar-bytes" if len(body["urls"]) == 1 else b"part%d" % n)
                    got = self.storage(put)
                    if got.status_code >= 300:
                        push = {"state": "failed", "error": f"upload failed: HTTP {got.status_code}"}
                        break
                    etags.append(got.headers.get("ETag", ""))
                push.setdefault("etags", etags) if push["state"] == "done" else None
                self.backups[bid] = {**self.backups.get(bid, {}), "push": push}
                self.pushed.append(bid)
                return httpx.Response(202, json={"accepted": True})
            if tail.endswith("/pull"):
                got = self.storage(httpx.Request("GET", json.loads(request.content)["url"]))
                self.backups[bid] = ({"state": "ready", "size": len(got.content)} if got.status_code == 200
                                     else {"state": "failed", "error": "Download failed: 404"})
                return httpx.Response(202, json={"accepted": True})
            return httpx.Response(200, json=self.backups.get(bid, {"state": "missing"}))
        if tail == "files/hashes":
            return httpx.Response(200, json=self.jars.get(uuid, {}))
        if tail == "files/fetch":
            body = json.loads(request.content)
            self.fetched.append(body)
            self.jars.setdefault(uuid, {})[body["path"].rsplit("/", 1)[-1]] = body.get("sha512") or "f" * 128
            return httpx.Response(200, json={"size": 10})
        if tail == "files/content" and method == "GET":
            path = request.url.params["path"]
            if path in self.files:
                return httpx.Response(200, json={"content": self.files[path]})
            return httpx.Response(404, json={"detail": "File not found."})
        if tail == "files/content" and method == "PUT":
            body = json.loads(request.content)
            self.files[body["path"]] = body["content"]
            return httpx.Response(204)
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
            self.slept.append((uuid, json.loads(request.content or b"{}").get("motd")))
            return httpx.Response(202, json={"accepted": True})
        if tail == "power":
            self.power.append((uuid, json.loads(request.content)["action"]))
            return httpx.Response(202, json={"accepted": True})
        if tail == "command":
            self.commands.append((uuid, json.loads(request.content)["command"]))
            return httpx.Response(200, json={"output": self.command_output})
        if tail == "files" and method == "DELETE":
            if self.files.pop(request.url.params.get("path", ""), None) is not None:
                return httpx.Response(204)
            name = request.url.params.get("path", "").rsplit("/", 1)[-1]
            if self.jars.get(uuid, {}).pop(name, None) is None:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(204)
        if tail == "files":
            folder = request.url.params.get("path", "/")
            if folder in ("/plugins", "/mods"):
                return httpx.Response(200, json=[{"name": n, "is_dir": False, "size": 100, "modified": 0,
                                                  "is_symlink": False} for n in self.jars.get(uuid, {})])
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
        self.redirect_posts_to: str | None = None  # answer webhook POSTs with a redirect
        self.breached = {"password1234": 2_000_000}  # passwords the fake breach service knows
        self.pwned_down = False
        self.mod_versions = {"PLUGIN01": 1, "DEP00001": 1}
        self.slugs = {"geyser": "GEYSER01", "floodgate": "FLOOD001", "lithium": "LITH0001", "ferrite-core": "FERR0001",
                      "krypton": "KRYP0001", "modernfix": "MODF0001"}
        self.latest_release: dict | None = None  # what GitHub says the newest Hearth release is
        self.pwned_prefixes: list[str] = []  # what was sent to it
        self.s3: dict[str, bytes] = {}  # fake S3-compatible storage at https://s3.test/<bucket>/<key>
        self.s3_parts: dict[str, dict[int, bytes]] = {}
        self.s3_down = False

    def _s3(self, request: httpx.Request) -> httpx.Response:
        import hashlib

        if self.s3_down:
            return httpx.Response(503, text="<Error><Code>SlowDown</Code><Message>try later</Message></Error>")
        q = request.url.params
        if "X-Amz-Signature" not in q or q.get("X-Amz-Credential", "").split("/")[0] != "AKTEST":
            return httpx.Response(403, text="<Error><Code>InvalidAccessKeyId</Code><Message>no</Message></Error>")
        path, method = request.url.path, request.method
        if method == "POST" and "uploads" in q:
            upload = f"up{len(self.s3_parts) + 1}"
            self.s3_parts[upload] = {}
            return httpx.Response(200, text=f"<InitiateMultipartUploadResult><UploadId>{upload}</UploadId></InitiateMultipartUploadResult>")
        if method == "POST" and "uploadId" in q:
            parts = self.s3_parts.pop(q["uploadId"])
            self.s3[path] = b"".join(parts[n] for n in sorted(parts))
            return httpx.Response(200, text="<CompleteMultipartUploadResult/>")
        if method == "PUT":
            if "uploadId" in q:
                self.s3_parts[q["uploadId"]][int(q["partNumber"])] = request.content
            else:
                self.s3[path] = request.content
            return httpx.Response(200, headers={"ETag": f'"{hashlib.md5(request.content).hexdigest()}"'})
        if method == "GET":
            return httpx.Response(200, content=self.s3[path]) if path in self.s3 else httpx.Response(404)
        if method == "DELETE":
            if "uploadId" in q:
                self.s3_parts.pop(q["uploadId"], None)
            else:
                self.s3.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def _pwned(self, request: httpx.Request) -> httpx.Response:
        import hashlib

        if self.pwned_down:
            return httpx.Response(503)
        prefix = request.url.path.rsplit("/", 1)[-1]
        self.pwned_prefixes.append(prefix)
        lines = ["0" * 35 + ":0"]  # padding, like the real service
        for password, count in self.breached.items():
            digest = hashlib.sha1(password.encode()).hexdigest().upper()
            if digest.startswith(prefix):
                lines.append(f"{digest[5:]}:{count}")
        return httpx.Response(200, text="\r\n".join(lines))

    # Plugins on the fake Modrinth: project -> current version number (bump to "release" a new one).
    def mod_file(self, pid: str, n: int) -> dict:
        import hashlib

        return {"id": f"{pid}-{n}", "project_id": pid, "filename": f"{pid.lower()}-{n}.jar",
                "sha512": hashlib.sha512(f"{pid}-{n}".encode()).hexdigest()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.modrinth.com" and request.url.path == "/v2/version_files":
            wanted = set(json.loads(request.content)["hashes"])
            found = {}
            for pid, latest in self.mod_versions.items():
                for n in range(1, latest + 1):
                    f = self.mod_file(pid, n)
                    if f["sha512"] in wanted:
                        found[f["sha512"]] = {"id": f["id"], "project_id": pid}
            return httpx.Response(200, json=found)
        if request.url.host == "api.pwnedpasswords.com":
            return self._pwned(request)
        if request.url.host == "s3.test":
            return self._s3(request)
        if request.url.host == "api.github.com":
            if self.latest_release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self.latest_release)
        url = str(request.url)
        if request.method == "POST":  # Discord + outgoing webhooks
            self.posts.append((url, json.loads(request.content or b"{}")))
            if self.redirect_posts_to:
                return httpx.Response(307, headers={"Location": self.redirect_posts_to})
            return httpx.Response(204)
        if "api.mcsrvstat.us" in url:
            return httpx.Response(200, json={"online": self.reachable, "players": {"online": 1, "max": 20}})
        if "modrinth.com/v2/search" in url and "project_type%3Aplugin" in url or "project_type:plugin" in url:
            return httpx.Response(200, json={"total_hits": 1, "hits": [{
                "project_id": "PLUGIN01", "slug": "essentials-demo", "title": "Essentials Demo",
                "description": "d", "author": "a", "icon_url": "", "downloads": 9}]})
        preset_slug = next((slug for slug, pid in self.slugs.items()
                            if f"/project/{slug}/version" in url or f"/project/{pid}/version" in url), None)
        if preset_slug:
            pid = self.slugs[preset_slug]
            f = self.mod_file(pid, 1)
            return httpx.Response(200, json=[{"id": f["id"], "project_id": pid, "version_type": "release",
                                              "dependencies": [], "files": [{
                                                  "primary": True, "filename": f["filename"],
                                                  "url": f"https://cdn.modrinth.com/data/{pid}/x.jar",
                                                  "hashes": {"sha512": f["sha512"]}}]}])
        if "/project/PLUGIN01/version" in url or "/project/DEP00001/version" in url:
            pid = "PLUGIN01" if "PLUGIN01" in url else "DEP00001"
            deps = [{"project_id": "DEP00001", "dependency_type": "required"}] if pid == "PLUGIN01" else []
            f = self.mod_file(pid, self.mod_versions[pid])
            return httpx.Response(200, json=[{"id": f["id"], "version_type": "release", "dependencies": deps,
                                              "files": [{"primary": True, "filename": f["filename"],
                                                         "url": f"https://cdn.modrinth.com/data/{pid}/x.jar",
                                                         "hashes": {"sha512": f["sha512"]}}]}])
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


# No DNS in tests: names resolve to a public documentation-free address unless listed here.
FAKE_DNS = {"inside.example.com": ["10.0.0.5"], "metadata.example.com": ["169.254.169.254"]}


async def fake_resolver(host: str) -> list[str]:
    import ipaddress

    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        return FAKE_DNS.get(host, ["8.8.4.4"])


@pytest.fixture
def public():
    return FakePublic()


@pytest.fixture(autouse=True)
def fast_backups(monkeypatch):
    from hearth_panel.services import backups

    monkeypatch.setattr(backups, "POLL", 0.01)


@pytest.fixture
def client(tmp_path, agent, public):
    agent.storage = public
    settings = Settings.from_env()
    settings = Settings(**{**settings.__dict__, "database_url": f"sqlite:///{tmp_path / 'test.db'}",
                           "secret_key": "test-secret-" + "x" * 32, "curseforge_api_key": None,
                           "data_dir": tmp_path})
    context = PanelContext.build(settings, node_transport=httpx.MockTransport(agent),
                                 public_transport=httpx.MockTransport(public))
    context.resolver = fake_resolver
    with TestClient(create_app(context, run_updater=False), client=("127.0.0.1", 50000)) as c:
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
