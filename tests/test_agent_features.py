"""Agent features: backups, imports, downloads, cloning, metrics, wake-on-join, tunnel."""

import asyncio
import io
import json
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


def wait(fn, timeout=10.0):
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
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(files.shutil, "disk_usage", lambda _: usage(1 << 30, 0, 256 * 1024 * 1024 + (1 << 20)))
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
