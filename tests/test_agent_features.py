"""Agent features: backups, imports, downloads, cloning, metrics, wake-on-join, tunnel."""

import asyncio
import gzip
import io
import json
import os
import socket
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from fake_docker import FakeDocker  # noqa: E402
from hearth_agent import files  # noqa: E402
from hearth_agent.app import create_app  # noqa: E402
from hearth_agent.config import AgentConfig  # noqa: E402
from hearth_agent.sleeper import Sleeper, encode_varint, mc_string, packet, serve  # noqa: E402

TOKEN = "a" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UUID = "0b9f6a2e-3c1d-4e5f-8a7b-6c5d4e3f2a1b"
OTHER = "11111111-2222-4333-8444-555555555555"
BACKUP = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SPEC = {"uuid": UUID, "image": "itzg/minecraft-server:java21", "memory_mb": 2048,
        "ports": [{"host_port": 25565, "container_port": 25565}], "console": "exec",
        "console_exec": ["rcon-cli"], "stop_timeout": 5,
        "pre_backup": ["save-off", "save-all flush"], "post_backup": ["save-on"]}


@pytest.fixture
def env(tmp_path):
    docker = FakeDocker()
    config = AgentConfig(token=TOKEN, data_dir=tmp_path / "servers", state_dir=tmp_path / "state",
                         sample_interval=1, history_size=10)
    with TestClient(create_app(config, docker_factory=lambda: docker)) as client:
        yield client, docker, tmp_path


def wait(fn, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition never became true")


def install(client, spec=SPEC, start=False):
    client.put(f"/api/servers/{spec['uuid']}", json=spec, headers=AUTH, params={"start": str(start).lower()})
    wait(lambda: client.get(f"/api/servers/{spec['uuid']}", headers=AUTH).json()["state"] in ("offline", "running")
         if not start else client.get(f"/api/servers/{spec['uuid']}", headers=AUTH).json()["state"] == "running")


def test_backup_create_restore_download(env):
    client, docker, tmp = env
    install(client, start=True)
    world = tmp / "servers" / UUID / "world"
    world.mkdir(parents=True)
    (world / "level.dat").write_text("v1")
    (tmp / "servers" / UUID / "crash-reports").mkdir()
    (tmp / "servers" / UUID / "crash-reports" / "boom.txt").write_text("ignored")

    assert client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH).status_code == 202
    meta = wait(lambda: (m := client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json())["state"] == "ready" and m)
    assert meta["size"] > 0
    log = b"".join(line for _, line in docker.containers.get(f"hearth-{UUID}").lines)
    assert log.count(b"RCON Client") == 3  # save-off, save-all flush, save-on ran around the archive

    r = client.get(f"/api/servers/{UUID}/backups/{BACKUP}/download", headers=AUTH)
    names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
    assert "world/level.dat" in names and not any(n.startswith("crash-reports") for n in names)

    (world / "level.dat").write_text("v2-griefed")
    assert client.post(f"/api/servers/{UUID}/backups/{BACKUP}/restore", headers=AUTH).status_code == 202
    wait(lambda: client.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"] == "running", 15)
    assert (world / "level.dat").read_text() == "v1"

    assert client.delete(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).status_code == 204
    assert client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "missing"


def test_world_upload_and_import(env):
    client, _, tmp = env
    install(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:  # worlds are usually zipped inside a folder
        zf.writestr("My Survival World/level.dat", "imported")
        zf.writestr("My Survival World/region/r.0.0.mca", "chunks")
    r = client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/upload.zip"}, content=buf.getvalue(), headers=AUTH)
    assert r.status_code == 204
    r = client.post(f"/api/servers/{UUID}/files/extract", params={"world": "true"}, headers=AUTH,
                    json={"archive": "/upload.zip", "target": "/world", "replace": True})
    assert r.status_code == 200, r.text
    assert (tmp / "servers" / UUID / "world" / "level.dat").read_text() == "imported"
    assert (tmp / "servers" / UUID / "world" / "region" / "r.0.0.mca").exists()
    assert not (tmp / "servers" / UUID / "upload.zip").exists()

    r = client.get(f"/api/servers/{UUID}/files/archive", params={"path": "/world"}, headers=AUTH)
    assert "world/level.dat" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_zip_slip_is_rejected(env):
    client, _, tmp = env
    install(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../escaped.txt", "nope")
    client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/evil.zip"}, content=buf.getvalue(), headers=AUTH)
    r = client.post(f"/api/servers/{UUID}/files/extract", headers=AUTH,
                    json={"archive": "/evil.zip", "target": "/plugins"})
    assert r.status_code == 400 and not (tmp / "escaped.txt").exists()


def test_fetch_only_from_mod_hosts(env):
    client, _, _ = env
    install(client)
    r = client.post(f"/api/servers/{UUID}/files/fetch", headers=AUTH,
                    json={"url": "https://evil.example/x.jar", "path": "/mods/x.jar"})
    assert r.status_code == 400 and "Modrinth" in r.json()["detail"]


def test_clone_copies_files(env):
    client, _, tmp = env
    install(client)
    install(client, {**SPEC, "uuid": OTHER, "ports": [{"host_port": 25566, "container_port": 25565}]})
    (tmp / "servers" / UUID / "server.properties").write_text("motd=original")
    assert client.post(f"/api/servers/{OTHER}/copy-from/{UUID}", headers=AUTH).status_code == 204
    assert (tmp / "servers" / OTHER / "server.properties").read_text() == "motd=original"


def test_metrics_minutes_aggregate():
    from hearth_agent.runtime import Sample, ServerManager, ServerRuntimeState

    st = ServerRuntimeState()
    for i in range(130):  # ~2 minutes of 1-second samples
        ServerManager._aggregate(st, Sample(1_000_020 + i, 10.0 + (i % 2), 1000 + i))
    assert len(st.minutes) == 2
    assert st.minutes[0][1] == pytest.approx(10.5, abs=0.1)


def test_sleeper_status_and_wake():
    woke = threading.Event()

    class Wake(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            woke.set()
            self.send_response(202)
            self.end_headers()

        def log_message(self, *a):
            pass

    http = HTTPServer(("127.0.0.1", 0), Wake)
    threading.Thread(target=http.serve_forever, daemon=True).start()

    async def scenario():
        sleeper = Sleeper("Sleeping - join to wake", f"http://127.0.0.1:{http.server_port}/wake")
        server = await serve(0, sleeper, "127.0.0.1")
        port = server.sockets[0].getsockname()[1]

        def handshake(state):
            return packet(0x00, encode_varint(767) + mc_string("localhost") + port.to_bytes(2, "big") + encode_varint(state))

        # Server list ping
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(handshake(1) + packet(0x00, b""))
        await w.drain()
        data = await r.read(4096)
        status = json.loads(data[data.index(b"{"):].decode())
        assert status["description"]["text"] == "Sleeping - join to wake"
        assert status["version"]["protocol"] == 767
        w.close()

        # Join attempt: disconnect message + wake call
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(handshake(2) + packet(0x00, mc_string("alex")))
        await w.drain()
        reply = await r.read(4096)
        assert b"waking up" in reply
        w.close()
        server.close()

    asyncio.run(scenario())
    assert woke.wait(5)
    http.shutdown()


def test_sleep_and_wake_through_agent(env):
    client, docker, tmp = env

    class Me:
        status = "running"
        attrs = {"Config": {"Image": "hearth-agent:latest"},
                 "NetworkSettings": {"Networks": {"hearth_default": {"IPAddress": "172.18.0.5"}}}}

    docker._containers[socket.gethostname()] = Me()
    install(client, start=True)
    assert client.post(f"/api/servers/{UUID}/sleep", json={}, headers=AUTH).status_code == 202
    assert client.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"] == "sleeping"
    sleeper = docker.containers.get(f"hearth-sleep-{UUID}")
    assert sleeper.kwargs["ports"] == {"25565/tcp": ("0.0.0.0", 25565)}
    wake_url = sleeper.kwargs["environment"]["WAKE_URL"]
    assert wake_url.startswith(f"http://172.18.0.5:8080/api/wake/{UUID}?key=")

    assert client.post(f"/api/wake/{UUID}", params={"key": "wrong"}).status_code == 409
    key = wake_url.split("key=")[1]
    assert client.post(f"/api/wake/{UUID}", params={"key": key}).status_code == 202
    wait(lambda: client.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"] == "running")
    with pytest.raises(Exception):
        docker.containers.get(f"hearth-sleep-{UUID}")


def test_tunnel_lifecycle(env):
    client, docker, _ = env
    assert client.get("/api/tunnel", headers=AUTH).json()["configured"] is False
    assert client.put("/api/tunnel", json={"secret": "s" * 32}, headers=AUTH).status_code == 202
    c = docker.containers.get("hearth-playit")
    assert c.kwargs["network_mode"] == "host" and c.kwargs["environment"]["SECRET_KEY"] == "s" * 32
    assert client.get("/api/tunnel", headers=AUTH).json()["configured"] is True
    assert client.delete("/api/tunnel", headers=AUTH).status_code == 204


def test_upload_rejects_escape(tmp_path):
    with pytest.raises(files.FileAccessError):
        files.open_upload(tmp_path, "../outside.jar")


# ----- bug-check regressions ---------------------------------------------------------------------


def _me(docker, ip="172.18.0.5"):
    class Me:
        status = "running"
        attrs = {"Config": {"Image": "hearth-agent:latest"},
                 "NetworkSettings": {"Networks": {"hearth_default": {"IPAddress": ip}}}}

    docker._containers[socket.gethostname()] = Me()


def test_power_and_file_writes_wait_for_a_restore(env):
    client, _, _ = env
    install(client)
    manager = client.app.state.manager
    manager.begin_restore(UUID)
    try:
        r = client.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH)
        assert r.status_code == 409 and "restore" in r.json()["detail"]
        r = client.put(f"/api/servers/{UUID}/files/content", json={"path": "/a.txt", "content": "x"}, headers=AUTH)
        assert r.status_code == 409
        assert client.put(f"/api/servers/{UUID}", json=SPEC, headers=AUTH).status_code == 409
        assert client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH).status_code == 409
    finally:
        manager.end_restore(UUID, error=None, start=False)
    assert client.post(f"/api/servers/{UUID}/power", json={"action": "start"}, headers=AUTH).status_code == 202


def test_backup_interrupted_by_agent_restart_reports_failed(env):
    client, _, _ = env
    install(client)
    store = client.app.state.backups
    store._write_meta(UUID, BACKUP, state="creating", size=0, error=None)  # left behind by a dead thread
    meta = client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()
    assert meta["state"] == "failed" and "restarted" in meta["error"]


def test_world_import_stops_the_server_and_swaps_dimensions(env):
    client, docker, tmp = env
    install(client, start=True)
    root = tmp / "servers" / UUID
    for name in ("world", "world_nether", "world_the_end"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "level.dat").write_text(f"old {name}")
    (root / "world_nether" / "stale.mca").write_text("old chunks")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:  # a Paper server's worlds, zipped together
        for name in ("aaa_nether", "aaa", "aaa_the_end"):
            zf.writestr(f"{name}/level.dat", f"new {name}")
    client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/up.zip"}, content=buf.getvalue(), headers=AUTH)
    r = client.post(f"/api/servers/{UUID}/files/extract", params={"world": "true"}, headers=AUTH,
                    json={"archive": "/up.zip", "target": "/world", "replace": True})
    assert r.status_code == 200, r.text
    assert (root / "world" / "level.dat").read_text() == "new aaa"
    assert (root / "world_nether" / "level.dat").read_text() == "new aaa_nether"
    assert not (root / "world_nether" / "stale.mca").exists()
    assert (root / "world_the_end" / "level.dat").read_text() == "new aaa_the_end"
    container = docker.containers.get(f"hearth-{UUID}")
    assert container.status == "running"  # brought back after the swap


def test_world_import_replaces_stale_dimensions_for_a_vanilla_zip(env):
    client, _, tmp = env
    install(client)
    root = tmp / "servers" / UUID
    (root / "world_nether").mkdir(parents=True)
    (root / "world_nether" / "level.dat").write_text("old")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("level.dat", "vanilla")
        zf.writestr("DIM-1/region/r.0.0.mca", "nether chunks")
    client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/v.zip"}, content=buf.getvalue(), headers=AUTH)
    r = client.post(f"/api/servers/{UUID}/files/extract", params={"world": "true"}, headers=AUTH,
                    json={"archive": "/v.zip", "target": "/world", "replace": True})
    assert r.status_code == 200, r.text
    assert (root / "world" / "DIM-1" / "region" / "r.0.0.mca").exists()
    assert not (root / "world_nether").exists()  # Paper rebuilds it from DIM-1


def test_archive_too_big_for_the_disk_is_refused(env, monkeypatch):
    client, _, tmp = env
    install(client)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"\0" * (2 << 20))
    client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/b.zip"}, content=buf.getvalue(), headers=AUTH)
    monkeypatch.setattr(files, "free_space", lambda _fd: 256 * 1024 * 1024 + (1 << 20))
    r = client.post(f"/api/servers/{UUID}/files/extract", headers=AUTH, json={"archive": "/b.zip", "target": "/x"})
    assert r.status_code == 507 and not (tmp / "servers" / UUID / "x" / "big.bin").exists()


def test_sleepers_are_recreated_when_the_agent_restarts(env):
    client, docker, _ = env
    _me(docker, "172.18.0.5")
    install(client, start=True)
    client.post(f"/api/servers/{UUID}/sleep", json={"motd": "zzz"}, headers=AUTH)
    _me(docker, "172.18.0.9")  # the agent came back with a new address
    client.app.state.manager.resume()
    sleeper = docker.containers.get(f"hearth-sleep-{UUID}")
    env_vars = sleeper.kwargs["environment"]
    assert env_vars["WAKE_URL"].startswith("http://172.18.0.9:8080/") and env_vars["MOTD"] == "zzz"
    key = env_vars["WAKE_URL"].split("key=")[1]
    assert client.post(f"/api/wake/{UUID}", params={"key": key}).status_code == 202


def test_wake_url_uses_https_when_the_agent_does(env, monkeypatch):
    client, docker, _ = env
    _me(docker)
    install(client, start=True)
    monkeypatch.setenv("HEARTH_TLS_CERT", "/certs/agent.pem")
    client.post(f"/api/servers/{UUID}/sleep", json={}, headers=AUTH)
    assert docker.containers.get(f"hearth-sleep-{UUID}").kwargs["environment"]["WAKE_URL"].startswith("https://")


def test_mod_download_redirects_must_stay_on_allowed_hosts():
    import urllib.request

    from hearth_agent.files import FileAccessError, _AllowlistRedirects

    handler = _AllowlistRedirects()
    req = urllib.request.Request("https://cdn.modrinth.com/data/abc/versions/1/mod.jar")
    ok = handler.redirect_request(req, None, 302, "Found", {}, "https://edge.forgecdn.net/files/1/mod.jar")
    assert ok.full_url == "https://edge.forgecdn.net/files/1/mod.jar"
    for bad in ("http://169.254.169.254/latest/meta-data", "http://agent:8080/api/servers", "https://evil.example/x",
                "http://cdn.modrinth.com/plain-http.jar"):
        with pytest.raises(FileAccessError):
            handler.redirect_request(req, None, 302, "Found", {}, bad)


# ----- symlink planting: a mod in the container controls the server folder ----------------------


def _planted(tmp):
    """A server folder where game code has planted links to the host's files."""
    root = tmp / "servers" / UUID
    secret_dir = tmp / "host-secrets"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "shadow").write_text("root:$6$hash")
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    (root / "evil-dir").symlink_to(secret_dir)  # a folder swapped for a link
    (root / "evil-file").symlink_to(secret_dir / "shadow")
    return root, secret_dir


def test_planted_links_are_never_followed_by_file_operations(env):
    client, _, tmp = env
    install(client)
    root, secret_dir = _planted(tmp)
    for path in ("/evil-dir/shadow", "/evil-file"):
        assert client.get(f"/api/servers/{UUID}/files/content", params={"path": path}, headers=AUTH).status_code in (403, 404)
        assert client.get(f"/api/servers/{UUID}/files/download", params={"path": path}, headers=AUTH).status_code in (403, 404)
    assert client.get(f"/api/servers/{UUID}/files", params={"path": "/evil-dir"}, headers=AUTH).status_code == 403
    r = client.put(f"/api/servers/{UUID}/files/content", json={"path": "/evil-dir/authorized_keys", "content": "x"}, headers=AUTH)
    assert r.status_code == 403 and not (secret_dir / "authorized_keys").exists()
    r = client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/evil-dir/x.jar"}, content=b"jar", headers=AUTH)
    assert r.status_code == 403 and not (secret_dir / "x.jar").exists()
    # Writing over the link itself replaces the link, never the file it points to.
    client.put(f"/api/servers/{UUID}/files/content", json={"path": "/evil-file", "content": "mine"}, headers=AUTH)
    assert (secret_dir / "shadow").read_text() == "root:$6$hash" and (root / "evil-file").read_text() == "mine"
    assert client.delete(f"/api/servers/{UUID}/files", params={"path": "/evil-dir"}, headers=AUTH).status_code == 204
    assert (secret_dir / "shadow").exists() and not (root / "evil-dir").is_symlink()


def test_world_import_replaces_a_planted_link_instead_of_writing_through_it(env):
    client, _, tmp = env
    install(client)
    root, secret_dir = _planted(tmp)
    (root / "world").symlink_to(secret_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("w/level.dat", "new world")
    client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/w.zip"}, content=buf.getvalue(), headers=AUTH)
    r = client.post(f"/api/servers/{UUID}/files/extract", params={"world": "true"}, headers=AUTH,
                    json={"archive": "/w.zip", "target": "/world", "replace": True})
    assert r.status_code == 200, r.text
    assert not (root / "world").is_symlink() and (root / "world" / "level.dat").read_text() == "new world"
    assert sorted(p.name for p in secret_dir.iterdir()) == ["shadow"]
    assert not any(p.name.startswith((".hearth-extract", "extract-")) for p in root.iterdir())  # unpacked elsewhere


def test_backups_zips_and_copies_store_links_not_host_files(env):
    client, docker, tmp = env
    install(client)
    root, _ = _planted(tmp)
    (root / "plugins" / "ok.jar").write_text("jar")
    client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH)
    wait(lambda: client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready")
    raw = client.get(f"/api/servers/{UUID}/backups/{BACKUP}/download", headers=AUTH).content
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert members["evil-file"].issym() and members["evil-dir"].issym()
        assert not any(name.startswith("evil-dir/") for name in members)
    assert b"root:$6$hash" not in gzip.decompress(raw)

    zipped = client.get(f"/api/servers/{UUID}/files/archive", params={"path": "/"}, headers=AUTH).content
    names = zipfile.ZipFile(io.BytesIO(zipped)).namelist()
    assert any(n.endswith("plugins/ok.jar") for n in names) and not any("evil" in n for n in names)

    from hearth_agent.files import copy_tree

    copy = tmp / "servers" / "copy"
    copy_tree(root, copy)
    assert (copy / "evil-file").is_symlink() and (copy / "plugins" / "ok.jar").read_text() == "jar"
    assert not (copy / "evil-dir" / "shadow").is_file() or (copy / "evil-dir").is_symlink()


def test_chunked_upload_without_length_is_still_capped_by_free_space(env, monkeypatch):
    client, _, tmp = env
    install(client)
    monkeypatch.setattr(files, "free_space", lambda _fd: files.FREE_SPACE_MARGIN + 1000)

    def body():
        for _ in range(10):
            yield b"x" * 500

    r = client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/big.bin"}, content=body(), headers=AUTH)
    assert r.status_code == 507
    root = tmp / "servers" / UUID
    assert not (root / "big.bin").exists() and not any(p.name.endswith(".hearth-tmp") for p in root.iterdir())


def test_sleeper_hangs_up_on_oversized_handshakes():
    from hearth_agent.sleeper import Sleeper, encode_varint

    async def scenario():
        sleeper = Sleeper("zzz", None)
        reader = asyncio.StreamReader()
        reader.feed_data(encode_varint(50_000_000))  # claims a 50 MB handshake
        closed = []

        class Writer:
            def write(self, _data): pass
            async def drain(self): pass
            def close(self): closed.append(True)

        await asyncio.wait_for(sleeper.handle(reader, Writer()), 2)
        return closed

    assert asyncio.run(scenario()) == [True]


def test_ids_with_trailing_newline_are_rejected():
    from hearth_agent.models import UUID_RE

    assert UUID_RE.match(UUID) and not UUID_RE.match(UUID + "\n")


def test_power_action_finishing_during_restore_keeps_the_restore_lock(env):
    client, _, _ = env
    install(client, start=True)
    manager = client.app.state.manager
    st = manager._state(UUID)
    st.transition = "stopping"  # a stop is in flight...
    manager.begin_restore(UUID)  # ...when a restore begins
    from hearth_agent.runtime import _end_transition

    _end_transition(st, "stopping")  # the stop finishes
    assert manager.busy(UUID)  # still locked for the restore
    assert client.delete(f"/api/servers/{UUID}", headers=AUTH).status_code == 409
    manager.end_restore(UUID, error=None, start=False)


def test_failed_restore_leaves_the_server_as_it_was(env):
    client, _, tmp = env
    install(client, start=True)
    root = tmp / "servers" / UUID
    (root / "world").mkdir(parents=True)
    (root / "world" / "level.dat").write_text("current")
    client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH)
    wait(lambda: client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready")
    archive = client.app.state.backups.archive_path(UUID, BACKUP)
    archive.write_bytes(archive.read_bytes()[: len(archive.read_bytes()) // 2])  # truncated: fails mid-unpack
    assert client.post(f"/api/servers/{UUID}/backups/{BACKUP}/restore", headers=AUTH).status_code == 202
    status = wait(lambda: (s := client.get(f"/api/servers/{UUID}", headers=AUTH).json())["state"] != "restoring" and s, 15)
    assert "Restore failed" in (status["error"] or "")
    assert (root / "world" / "level.dat").read_text() == "current"
    assert not any(p.name.startswith(f".{UUID}") for p in root.parent.iterdir())  # no leftovers


def test_restore_failing_after_the_swap_puts_the_old_files_back(env, monkeypatch):
    from pathlib import Path

    client, _, tmp = env
    install(client)
    root = tmp / "servers" / UUID
    (root / "world").mkdir(parents=True)
    (root / "world" / "level.dat").write_text("current")
    client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH)
    wait(lambda: client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready")
    real_rename = Path.rename

    def flaky(self, target):
        if self.name.endswith(".restoring"):
            raise OSError(28, "No space left on device")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    client.post(f"/api/servers/{UUID}/backups/{BACKUP}/restore", headers=AUTH)
    wait(lambda: client.get(f"/api/servers/{UUID}", headers=AUTH).json()["state"] != "restoring", 15)
    assert (root / "world" / "level.dat").read_text() == "current"


def test_jar_hashes_for_modrinth_lookup(env):
    import hashlib

    client, _, tmp = env
    install(client)
    root, _ = _planted(tmp)
    (root / "plugins" / "ok.jar").write_bytes(b"jar bytes")
    (root / "plugins" / "notes.txt").write_text("ignored")
    (root / "plugins" / "evil.jar").symlink_to(tmp / "host-secrets" / "shadow")
    r = client.get(f"/api/servers/{UUID}/files/hashes", params={"path": "/plugins"}, headers=AUTH)
    assert r.json() == {"ok.jar": hashlib.sha512(b"jar bytes").hexdigest()}
    assert client.get(f"/api/servers/{UUID}/files/hashes", params={"path": "/evil-dir"}, headers=AUTH).status_code == 403


class _FakeStorage(BaseHTTPRequestHandler):
    """Just enough S3: PUT stores the body (per part), GET returns it, ETag is the body's md5."""

    objects: dict[str, bytes] = {}
    fail_first: set[str] = set()

    def do_PUT(self):  # noqa: N802
        import hashlib

        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path in self.fail_first:
            self.fail_first.discard(self.path)
            self.send_response(503)
            self.end_headers()
            return
        self.objects[self.path] = body
        self.send_response(200)
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()

    def do_GET(self):  # noqa: N802
        body = self.objects.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args):
        pass


def test_backups_go_off_site_in_parts_and_come_back(env, monkeypatch):
    import hashlib

    from hearth_agent import backups as backups_mod

    import time as real_time
    from types import SimpleNamespace

    # Only the backup code skips its retry pauses; patching time.sleep itself would also turn this
    # test's own polling into a busy loop that starves the worker threads on a loaded machine.
    monkeypatch.setattr(backups_mod, "time", SimpleNamespace(sleep=lambda _s: None, time=real_time.time,
                                                             monotonic=real_time.monotonic))
    storage = HTTPServer(("127.0.0.1", 0), _FakeStorage)
    threading.Thread(target=storage.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{storage.server_port}"
    client, _, tmp = env
    install(client)
    root = tmp / "servers" / UUID
    (root / "world").mkdir(parents=True)
    (root / "world" / "level.dat").write_bytes(os.urandom(300_000))  # incompressible, ~300 KB archive
    client.post(f"/api/servers/{UUID}/backups", json={"backup_id": BACKUP}, headers=AUTH)
    wait(lambda: client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready")
    archive = client.app.state.backups.archive_path(UUID, BACKUP).read_bytes()

    part = 5 * 1024 * 1024  # the minimum; force a 1-part multipart and a retry on it
    _FakeStorage.fail_first = {"/part1"}
    r = client.post(f"/api/servers/{UUID}/backups/{BACKUP}/push", headers=AUTH,
                    json={"urls": [f"{base}/part1"], "part_size": part})
    assert r.status_code == 202
    meta = wait(lambda: (m := client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json())["push"]["state"] != "uploading" and m)
    assert meta["push"] == {"state": "done", "etags": [f'"{hashlib.md5(archive).hexdigest()}"']}
    assert _FakeStorage.objects["/part1"] == archive

    # The node lost its copy (new disk): pull it back down.
    client.delete(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH)
    r = client.post(f"/api/servers/{UUID}/backups/{BACKUP}/pull", headers=AUTH, json={"url": f"{base}/part1"})
    assert r.status_code == 202
    wait(lambda: client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready")
    assert client.app.state.backups.archive_path(UUID, BACKUP).read_bytes() == archive
    storage.shutdown()


def test_multipart_upload_splits_the_file(tmp_path):
    from hearth_agent.backups import _Slice

    path = tmp_path / "f"
    path.write_bytes(bytes(range(256)) * 100)  # 25,600 bytes
    with open(path, "rb") as f:
        parts = [b"".join(iter(lambda s=_Slice(f, n * 10_000, min(10_000, 25_600 - n * 10_000)): s.read(4096), b""))
                 for n in range(3)]
    assert [len(p) for p in parts] == [10_000, 10_000, 5_600] and b"".join(parts) == path.read_bytes()


def test_disk_limit_blocks_uploads_downloads_and_unpacking(env):
    client, _, tmp = env
    install(client, {**SPEC, "disk_mb": 1})
    root = tmp / "servers" / UUID
    r = client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/big.bin"}, content=b"x" * 2_000_000, headers=AUTH)
    assert r.status_code == 507 and "disk limit" in r.json()["detail"]
    assert not (root / "big.bin").exists()

    def chunks():
        for _ in range(20):
            yield b"x" * 100_000

    r = client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/big.bin"}, content=chunks(), headers=AUTH)
    assert r.status_code == 507 and not any(p.name.endswith(".hearth-tmp") for p in root.iterdir())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.dat", b"\0" * 3_000_000)
    assert client.put(f"/api/servers/{UUID}/files/upload", params={"path": "/a.zip"}, content=buf.getvalue(),
                      headers=AUTH).status_code == 204
    r = client.post(f"/api/servers/{UUID}/files/extract", json={"archive": "/a.zip", "target": "/"}, headers=AUTH)
    assert r.status_code == 507 and "disk limit" in r.json()["detail"]

    (root / "filler").write_bytes(b"x" * 1_100_000)  # the game itself filled it up
    client.app.state.manager._state(UUID).disk_checked = 0
    r = client.put(f"/api/servers/{UUID}/files/content", json={"path": "/note.txt", "content": "hi"}, headers=AUTH)
    assert r.status_code == 507 and "at its disk limit" in r.json()["detail"]
    (root / "filler").unlink()
    client.app.state.manager._state(UUID).disk_checked = 0
    assert client.put(f"/api/servers/{UUID}/files/content", json={"path": "/note.txt", "content": "hi"}, headers=AUTH).status_code == 204


def test_receiving_a_backup_from_another_node(env):
    client, _, tmp = env
    install(client)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"moved world"
        info = tarfile.TarInfo("world/level.dat")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    r = client.put(f"/api/servers/{UUID}/backups/{BACKUP}/archive", content=buf.getvalue(), headers=AUTH)
    assert r.status_code == 204
    assert client.get(f"/api/servers/{UUID}/backups/{BACKUP}", headers=AUTH).json()["state"] == "ready"
    client.post(f"/api/servers/{UUID}/backups/{BACKUP}/restore", headers=AUTH)
    wait(lambda: (tmp / "servers" / UUID / "world" / "level.dat").exists())
    assert (tmp / "servers" / UUID / "world" / "level.dat").read_bytes() == b"moved world"

    r = client.put(f"/api/servers/{UUID}/backups/{OTHER}/archive", content=buf.getvalue()[:20], headers=AUTH)
    assert r.status_code == 409 and "corrupt" in r.json()["detail"]
    assert client.get(f"/api/servers/{UUID}/backups/{OTHER}", headers=AUTH).json()["state"] == "failed"
