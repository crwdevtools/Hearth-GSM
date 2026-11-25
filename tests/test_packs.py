import io
import json
import zipfile

import pytest

from hearth_panel.services import packzip
from tests.test_panel import _create


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


MRPACK = _zip({"modrinth.index.json": json.dumps({
    "formatVersion": 1, "game": "minecraft", "versionId": "2.1.0", "name": "Cool Pack",
    "summary": "A pack", "files": [{"path": "mods/a.jar"}, {"path": "mods/b.jar"}],
    "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.7"}}), "overrides/config/a.toml": "x=1"})
CFPACK = _zip({"manifest.json": json.dumps({
    "manifestType": "minecraftModpack", "manifestVersion": 1, "name": "All The Mods 9", "version": "0.3.1",
    "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.2.0", "primary": True}]},
    "files": [{"projectID": 1, "fileID": 2, "required": True}], "overrides": "overrides"}), "overrides/a.cfg": "x"})


def _upload(client, data: bytes, name="pack.mrpack"):
    return client.put("/api/modpacks/upload", params={"filename": name}, content=data)


def test_inspect_modrinth_and_curseforge(tmp_path):
    (tmp_path / "m").write_bytes(MRPACK)
    (tmp_path / "c").write_bytes(CFPACK)
    m, c = packzip.inspect(tmp_path / "m"), packzip.inspect(tmp_path / "c")
    assert (m["format"], m["name"], m["mc_version"], m["loader"], m["loader_version"], m["mods"]) == (
        "modrinth", "Cool Pack", "1.20.1", "fabric", "0.15.7", 2)
    assert (c["format"], c["loader"], c["loader_version"], c["slug"], c["version"]) == (
        "curseforge", "forge", "47.2.0", "all-the-mods-9", "0.3.1")


@pytest.mark.parametrize("data,message", [
    (b"not a zip", "valid zip"),
    (_zip({"readme.txt": "hi"}), "No modpack manifest"),
    (_zip({"Pack/manifest.json": "{}"}), "inside a folder"),
    (_zip({"mods/a.jar": "x", "start.sh": "#!/bin/sh"}), "server pack"),
    (_zip({"manifest.json": "{nope"}), "valid JSON"),
    (_zip({"manifest.json": json.dumps({"manifestType": "other"})}), "isn't a CurseForge"),
    (_zip({"manifest.json": json.dumps({"manifestType": "minecraftModpack", "minecraft": {
        "version": "24w10a", "modLoaders": [{"id": "forge-1"}]}})}), "release versions"),
    (_zip({"modrinth.index.json": json.dumps({"dependencies": {"minecraft": "1.20.1"}})}), "Forge, NeoForge"),
])
def test_inspect_rejects(tmp_path, data, message):
    (tmp_path / "p").write_bytes(data)
    with pytest.raises(packzip.PackError, match=message):
        packzip.inspect(tmp_path / "p")


def test_clean_slug():
    assert packzip.clean_slug("https://www.curseforge.com/minecraft/modpacks/all-the-mods-9/files") == "all-the-mods-9"
    assert packzip.clean_slug("atm9") == "atm9" and packzip.clean_slug("  ") is None
    with pytest.raises(packzip.PackError):
        packzip.clean_slug("../etc/passwd")


def test_upload_rejects_wrong_type_and_garbage(admin):
    assert _upload(admin, MRPACK, "pack.exe").status_code == 400
    r = _upload(admin, b"junk", "pack.zip")
    assert r.status_code == 400 and "valid zip" in r.json()["detail"]


def test_modrinth_upload_creates_server(admin, node, agent):
    up = _upload(admin, MRPACK)
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["name"] == "Cool Pack" and body["loader"] == "fabric" and body["mc_version"] == "1.20.1"
    r = _create(admin, node, modpack_upload={"token": body["token"]})
    assert r.status_code == 201, r.text
    mp = r.json()["modpack"]
    assert mp["uploaded"] and mp["name"] == "Cool Pack" and mp["loader"] == "fabric" and mp["version_name"] == "2.1.0"
    spec = agent.specs[r.json()["uuid"]]
    assert spec["env"]["TYPE"] == "MODRINTH" and spec["env"]["MODRINTH_MODPACK"] == "/data/modpack/pack.mrpack"
    assert "VERSION" not in spec["env"] and spec["image"] == "itzg/minecraft-server:java17"  # Minecraft 1.20.1
    assert "modpack/pack.mrpack" in agent.files  # delivered to the node before the first start
    assert [s for _, s in agent.applies] == ["false", "true"]  # create the folder, then start on the pack
    # the staged copy is gone, and a second create with the same token fails
    assert _create(admin, node, name="again", modpack_upload={"token": body["token"]}).status_code == 400


def test_curseforge_upload_needs_slug_and_key_only_for_old_versions(admin, node, agent):
    token = _upload(admin, CFPACK, "atm9.zip").json()["token"]
    r = _create(admin, node, modpack_upload={"token": token, "curseforge_page":
                "https://www.curseforge.com/minecraft/modpacks/all-the-mods-9"})
    assert r.status_code == 201, r.text
    env = agent.specs[r.json()["uuid"]]["env"]
    assert env["TYPE"] == "AUTO_CURSEFORGE" and env["CF_MODPACK_ZIP"] == "/data/modpack/pack.zip"
    assert env["CF_SLUG"] == "all-the-mods-9" and "CF_API_KEY" not in env  # Java 17+ images carry their own key


def test_old_curseforge_pack_needs_key(admin, node):
    old = _zip({"manifest.json": json.dumps({"manifestType": "minecraftModpack", "name": "Old",
                "minecraft": {"version": "1.12.2", "modLoaders": [{"id": "forge-14.23.5.2860"}]}})})
    token = _upload(admin, old, "old.zip").json()["token"]
    r = _create(admin, node, modpack_upload={"token": token})
    assert r.status_code == 201 and "API key" in r.json()["warning"]


def test_upload_belongs_to_one_user(admin, node):
    token = _upload(admin, MRPACK).json()["token"]
    assert _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"},
                   modpack_upload={"token": token}).status_code == 400
    assert _create(admin, node, modpack_upload={"token": "x" * 20}).status_code == 400


def test_uploaded_pack_has_no_updates_and_switches_to_software(admin, node, agent):
    token = _upload(admin, MRPACK).json()["token"]
    sid = _create(admin, node, modpack_upload={"token": token}).json()["id"]
    assert admin.post(f"/api/servers/{sid}/updates/apply").status_code == 400
    r = admin.put(f"/api/servers/{sid}/source", json={"kind": "software", "server_type": "PAPER", "version": "1.21.1"})
    assert r.status_code == 200 and r.json()["modpack"] is None
    assert "modpack/pack.mrpack" not in agent.files  # the uploaded zip is cleaned up


def test_staging_sweep_keeps_three(tmp_path):
    stage = packzip.stage_dir(tmp_path)
    for i in range(5):
        token = f"token{i}".ljust(20, "x")
        packzip.packed(stage, token).write_bytes(b"z")
        packzip.meta_of(stage, token).write_text(json.dumps({"user_id": 1, "created": 1000 + i + 9e9}))
    packzip.sweep(stage, 1)
    assert len(list(stage.glob("*.pack"))) == packzip.MAX_STAGED_PER_USER - 1
