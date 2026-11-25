from pathlib import Path

import pytest

from hearth_agent.files import FileAccessError, delete, list_dir, read_text, rename, write_text
from hearth_agent.models import ServerSpec
from hearth_agent.runtime import ServerManager, build_create_kwargs, cpu_percent_from_stats, memory_bytes_from_stats

UUID = "0b9f6a2e-3c1d-4e5f-8a7b-6c5d4e3f2a1b"


def test_spec_rejects_bad_input():
    with pytest.raises(ValueError):
        ServerSpec(uuid="../../etc", image="x", memory_mb=1024)
    with pytest.raises(ValueError):
        ServerSpec(uuid=UUID, image="x", memory_mb=1024, env={"BAD KEY": "1"})
    with pytest.raises(ValueError):
        ServerSpec(uuid=UUID, image="x", memory_mb=1024, data_mount="/data/../etc")


def test_create_kwargs(tmp_path):
    spec = ServerSpec(uuid=UUID, image="itzg/minecraft-server:java21", memory_mb=4096, swap_mb=512,
                      cpu_percent=200, env={"EULA": "TRUE"},
                      ports=[{"host_port": 25565, "container_port": 25565},
                             {"host_port": 24454, "container_port": 24454, "protocol": "udp"}])
    kw = build_create_kwargs(spec, tmp_path)
    assert kw["name"] == f"hearth-{UUID}"
    assert kw["ports"] == {"25565/tcp": [("0.0.0.0", 25565)], "24454/udp": [("0.0.0.0", 24454)]}
    assert kw["mem_limit"] == "4096m" and kw["memswap_limit"] == "4608m"
    assert kw["nano_cpus"] == 2_000_000_000
    assert kw["volumes"] == {str(tmp_path): {"bind": "/data", "mode": "rw"}}


def test_stats_math():
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 2_000}, "system_cpu_usage": 20_000, "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 10_000},
        "memory_stats": {"usage": 1000, "stats": {"inactive_file": 300}},
    }
    assert cpu_percent_from_stats(stats) == 40.0
    assert memory_bytes_from_stats(stats) == 700
    assert cpu_percent_from_stats({}) == 0.0


def test_file_sandbox(tmp_path: Path):
    root = tmp_path / "srv"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    (root / "link").symlink_to(outside)

    write_text(root, "/config/server.properties", "motd=hi\n")
    assert read_text(root, "config/server.properties") == "motd=hi\n"
    assert [e["name"] for e in list_dir(root, "/")] == ["config", "link"]
    for bad in ("../secret.txt", "/../../etc/passwd", "link"):
        with pytest.raises(FileAccessError):
            read_text(root, bad)
    with pytest.raises(FileAccessError):
        delete(root, "/")
    rename(root, "config/server.properties", "config/old.properties")
    assert (root / "config/old.properties").exists()
    delete(root, "config")
    assert not (root / "config").exists()


def test_delete_symlink_removes_link_not_target(tmp_path: Path):
    root = tmp_path / "srv"
    (root / "real").mkdir(parents=True)
    (root / "real" / "keep.txt").write_text("x")
    (root / "alias").symlink_to(root / "real")
    delete(root, "alias")
    assert not (root / "alias").exists() and (root / "real" / "keep.txt").exists()
    with pytest.raises(FileAccessError):
        rename(root, "real", "../escaped")


def test_host_path_mapping():
    from hearth_agent.runtime import host_path_for

    mounts = [
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
        {"Type": "bind", "Source": "/run/desktop/mnt/host/d/Hearth", "Destination": "/var/lib/hearth"},
    ]
    root = Path("/var/lib/hearth")
    assert host_path_for(mounts, root) == Path("/run/desktop/mnt/host/d/Hearth")
    assert host_path_for(mounts, root / "servers") == Path("/run/desktop/mnt/host/d/Hearth/servers")
    assert host_path_for(mounts, Path("/elsewhere")) is None
    # The most specific mount wins.
    mounts.append({"Type": "bind", "Source": "/mnt/fast", "Destination": "/var/lib/hearth/servers"})
    assert host_path_for(mounts, root / "servers" / "x") == Path("/mnt/fast/x")


class _Box:
    def __init__(self, health=None, boom=False):
        self.attrs = {"State": {"Health": {"Status": health}} if health else {"State": {}}}
        self.boom = boom

    def reload(self):
        if self.boom:
            raise RuntimeError("docker unavailable")


def test_stop_wait_is_short_only_while_starting():
    spec = ServerSpec(uuid=UUID, image="x", memory_mb=1024, stop_timeout=90)
    assert ServerManager._stop_wait(_Box("starting"), spec) == 10
    assert ServerManager._stop_wait(_Box("healthy"), spec) == 90
    assert ServerManager._stop_wait(_Box(), spec) == 90  # no healthcheck: keep the clean shutdown
    assert ServerManager._stop_wait(_Box(boom=True), spec) == 90
    short = ServerSpec(uuid=UUID, image="x", memory_mb=1024, stop_timeout=5)
    assert ServerManager._stop_wait(_Box("starting"), short) == 5  # never longer than the configured timeout
