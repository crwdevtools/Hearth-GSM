from hearth_panel.services.mc_versions import neoforge_to_minecraft
from hearth_panel.services.provisioning import jvm_heap_mb
from hearth_panel.templates import TemplateError, coerce_variables, get_template, java_tag_for, parse_extra_env

import pytest


# ----- pure helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize("version,tag", [
    ("1.12.2", "java8"), ("1.16.5", "java8"), ("1.17.1", "java17"), ("1.20.4", "java17"),
    ("1.20.5", "java21"), ("1.21.4", "java21"), ("26.1", "latest"), ("LATEST", "latest"), (None, "latest"),
])
def test_java_tag(version, tag):
    assert java_tag_for(version) == tag


@pytest.mark.parametrize("neo,mc", [
    ("20.4.237", "1.20.4"), ("21.0.10-beta", "1.21"), ("21.1.77", "1.21.1"), ("26.1.0.3", "26.1"),
    ("26.1.1.0", "26.1.1"), ("garbage", None),
])
def test_neoforge_mapping(neo, mc):
    assert neoforge_to_minecraft(neo) == mc


def test_heap_leaves_headroom():
    assert jvm_heap_mb(1024) == 512    # floor
    assert jvm_heap_mb(2048) == 1280   # 768 MB headroom minimum
    assert jvm_heap_mb(4096) == 3072   # 25%
    assert jvm_heap_mb(16384) == 14336  # 2 GB headroom cap


def test_variable_validation():
    t = get_template("minecraft-java")
    assert coerce_variables(t, {"MAX_PLAYERS": "12", "PVP": "false"}) == {"MAX_PLAYERS": 12, "PVP": False}
    with pytest.raises(TemplateError):
        coerce_variables(t, {"MAX_PLAYERS": 0})
    with pytest.raises(TemplateError):
        coerce_variables(t, {"DIFFICULTY": "nightmare"})
    with pytest.raises(TemplateError):
        coerce_variables(t, {"MOTD": "two\nlines"})
    with pytest.raises(TemplateError):
        coerce_variables(t, {"NOT_A_VAR": 1})


def test_extra_env():
    assert parse_extra_env("# c\nA=1\nB = two=2\n") == {"A": "1", "B": " two=2"}
    with pytest.raises(TemplateError):
        parse_extra_env("not valid")


# ----- auth ------------------------------------------------------------------------------------


def test_setup_only_once(admin):
    r = admin.post("/api/auth/setup", json={"username": "other", "password": "x" * 12})
    assert r.status_code == 409


def test_login_and_throttle(client):
    client.post("/api/auth/setup", json={"username": "admin", "password": "correct-horse-battery"})
    client.post("/api/auth/logout")
    assert client.get("/api/servers").status_code == 401
    for _ in range(8):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 429


def test_password_change_revokes_old_sessions(admin):
    old_cookie = admin.cookies.get("hearth_session")
    r = admin.post("/api/auth/password", json={"current_password": "correct-horse-battery",
                                               "new_password": "another-long-password"})
    assert r.status_code == 200
    admin.cookies.set("hearth_session", old_cookie)
    assert admin.get("/api/servers").status_code == 401


# ----- servers ---------------------------------------------------------------------------------


def _create(admin, node, **extra):
    body = {"name": "Survival", "node_id": node["id"], "template": "minecraft-java", "memory_mb": 4096,
            "accept_eula": True, "variables": {"TYPE": "PAPER", "VERSION": "1.20.4", "MOTD": "hi"}}
    body.update(extra)
    return admin.post("/api/servers", json=body)


def test_create_minecraft_server_builds_spec(admin, node, agent):
    r = _create(admin, node)
    assert r.status_code == 201, r.text
    server = r.json()
    assert server["address"] == "play.example.net:25565"
    spec = agent.specs[server["uuid"]]
    assert spec["image"] == "itzg/minecraft-server:java17"
    assert spec["env"]["TYPE"] == "PAPER" and spec["env"]["VERSION"] == "1.20.4"
    assert spec["env"]["MEMORY"] == "3072M" and spec["env"]["EULA"] == "TRUE"
    assert spec["env"]["PVP"] == "true"
    assert "SEED" not in spec["env"]  # empty values aren't forced onto the server
    assert spec["ports"] == [{"host_ip": "0.0.0.0", "host_port": 25565, "container_port": 25565, "protocol": "tcp"}]
    assert spec["console"] == "exec" and spec["console_exec"] == ["rcon-cli"]


def test_eula_required(admin, node):
    assert _create(admin, node, accept_eula=False).status_code == 400


def test_second_server_gets_next_port(admin, node):
    _create(admin, node)
    assert _create(admin, node, name="Creative").json()["address"].endswith(":25566")


def test_valheim_claims_consecutive_ports(admin, node, agent):
    _create(admin, node)  # takes 25565
    r = admin.post("/api/servers", json={"name": "Vikings", "node_id": node["id"], "template": "valheim",
                                         "memory_mb": 4096, "variables": {"SERVER_PASS": "secret123"}})
    assert r.status_code == 201, r.text
    spec = agent.specs[r.json()["uuid"]]
    assert {(p["host_port"], p["container_port"]) for p in spec["ports"]} == {(25566, 25566), (25567, 25567)}
    assert spec["env"]["SERVER_PORT"] == "25566"
    assert r.json()["variables"]["SERVER_PASS"] == ""  # secrets never come back


def test_valheim_requires_password(admin, node):
    r = admin.post("/api/servers", json={"name": "V", "node_id": node["id"], "template": "valheim",
                                         "memory_mb": 4096})
    assert r.status_code == 400 and "Password" in r.json()["detail"]


def test_runs_out_of_ports(admin, node):
    for i in range(6):
        assert _create(admin, node, name=f"s{i}").status_code == 201
    r = _create(admin, node, name="one-too-many")
    assert r.status_code == 400 and "No free ports" in r.json()["detail"]


def test_node_memory_cap(admin, node):
    admin.patch(f"/api/nodes/{node['id']}", json={"memory_mb": 6000})
    assert _create(admin, node).status_code == 201
    r = _create(admin, node, name="b")
    assert r.status_code == 400 and "memory left" in r.json()["detail"]


def test_modrinth_modpack_server(admin, node, agent):
    r = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"}, auto_update=True)
    assert r.status_code == 201, r.text
    mp = r.json()["modpack"]
    assert mp["version_id"] == "v2"  # newest *release*, skipping the beta
    spec = agent.specs[r.json()["uuid"]]
    assert spec["env"]["TYPE"] == "MODRINTH" and spec["env"]["MODRINTH_VERSION"] == "v2"
    assert "VERSION" not in spec["env"]
    assert spec["image"] == "itzg/minecraft-server:java21"


def test_curseforge_needs_key(admin, node):
    r = _create(admin, node, modpack={"provider": "curseforge", "project_id": "123"})
    assert r.status_code == 412


def test_auto_update_applies_new_release(admin, node, agent, public):
    sid = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"},
                  auto_update=True).json()["id"]
    public.versions.insert(0, {"id": "v4", "name": "Pack 4.0", "version_number": "4.0",
                               "game_versions": ["1.21.4"], "loaders": ["neoforge"],
                               "version_type": "release", "date_published": "2026-11-10T00:00:00Z"})
    r = admin.post("/api/admin/updates/run")
    assert r.json()["checked"] == 1
    server = admin.get(f"/api/servers/{sid}").json()
    assert server["modpack"]["version_id"] == "v4" and server["modpack"]["update"] is None
    spec = agent.specs[server["uuid"]]
    assert spec["env"]["MODRINTH_VERSION"] == "v4"
    assert any("Updating modpack" in c for _, c in agent.commands)


def test_update_check_without_auto_update_only_flags(admin, node, agent, public):
    sid = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"}).json()["id"]
    public.versions.insert(0, {"id": "v4", "name": "Pack 4.0", "game_versions": ["1.21.4"],
                               "version_type": "release", "date_published": "2026-11-10T00:00:00Z"})
    r = admin.post(f"/api/servers/{sid}/updates/check").json()
    assert r["update"]["id"] == "v4"
    assert r["server"]["modpack"]["version_id"] == "v2"
    r = admin.post(f"/api/servers/{sid}/updates/apply").json()
    assert r["server"]["modpack"]["version_id"] == "v4"


def test_switch_from_modpack_back_to_software(admin, node, agent):
    sid = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"}).json()["id"]
    r = admin.put(f"/api/servers/{sid}/source", json={"kind": "software", "server_type": "FABRIC",
                                                       "version": "1.21.4"})
    assert r.status_code == 200, r.text
    assert r.json()["modpack"] is None
    spec = agent.specs[r.json()["uuid"]]
    assert spec["env"]["TYPE"] == "FABRIC" and "MODRINTH_MODPACK" not in spec["env"]


def test_patch_properties_and_resources(admin, node, agent):
    sid = _create(admin, node).json()["id"]
    r = admin.patch(f"/api/servers/{sid}", json={"memory_mb": 8192, "variables": {"DIFFICULTY": "hard"}})
    assert r.status_code == 200, r.text
    spec = agent.specs[r.json()["uuid"]]
    assert spec["memory_mb"] == 8192 and spec["env"]["DIFFICULTY"] == "hard" and spec["env"]["MOTD"] == "hi"


def test_non_owner_cannot_see_server(admin, node):
    sid = _create(admin, node).json()["id"]
    admin.post("/api/users", json={"username": "friend", "password": "friend-password-1"})
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"username": "friend", "password": "friend-password-1"})
    assert admin.get("/api/servers").json() == []
    assert admin.get(f"/api/servers/{sid}").status_code == 404
    assert admin.post(f"/api/servers/{sid}/power", json={"action": "start"}).status_code == 404


def test_owner_cannot_raise_memory(admin, node):
    friend = admin.post("/api/users", json={"username": "friend", "password": "friend-password-1"}).json()
    sid = _create(admin, node, owner_id=friend["id"]).json()["id"]
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"username": "friend", "password": "friend-password-1"})
    assert admin.patch(f"/api/servers/{sid}", json={"memory_mb": 65536}).status_code == 403
    assert admin.patch(f"/api/servers/{sid}", json={"variables": {"MOTD": "mine"}}).status_code == 200
    assert admin.post(f"/api/servers/{sid}/power", json={"action": "restart"}).status_code == 202


def test_node_offline_is_reported_not_fatal(admin, node, agent):
    _create(admin, node)
    agent.online = False
    servers = admin.get("/api/servers").json()
    assert servers[0]["status"]["state"] == "node_offline"
    r = admin.patch(f"/api/servers/{servers[0]['id']}", json={"variables": {"MOTD": "x"}})
    assert r.status_code == 200 and "Reinstall" in r.json()["warning"]


def test_extra_allocation_maps_both_protocols(admin, node, agent):
    sid = _create(admin, node).json()["id"]
    r = admin.post(f"/api/servers/{sid}/allocations", json={})
    assert r.status_code == 201
    spec = agent.specs[r.json()["uuid"]]
    extra = [p for p in spec["ports"] if p["host_port"] == 25566]
    assert {p["protocol"] for p in extra} == {"tcp", "udp"}
    alloc_id = next(a["id"] for a in r.json()["allocations"] if a["port"] == 25566)
    primary_id = next(a["id"] for a in r.json()["allocations"] if a["is_primary"])
    assert admin.delete(f"/api/servers/{sid}/allocations/{primary_id}").status_code == 400
    assert admin.delete(f"/api/servers/{sid}/allocations/{alloc_id}").status_code == 200


def test_delete_server_frees_ports(admin, node, agent):
    server = _create(admin, node).json()
    assert admin.delete(f"/api/servers/{server['id']}").status_code == 204
    assert server["uuid"] not in agent.specs
    assert _create(admin, node, name="again").json()["address"].endswith(":25565")


def test_catalog_endpoints(admin):
    hits = admin.get("/api/modpacks/search?provider=modrinth&query=cool").json()["hits"]
    assert hits[0]["name"] == "Cool Pack" and hits[0]["loaders"] == ["neoforge"]
    versions = admin.get("/api/minecraft/versions?type=PAPER").json()
    assert [v["id"] for v in versions] == ["1.21.4", "1.20.1"]
    vanilla = admin.get("/api/minecraft/versions?type=VANILLA&snapshots=true").json()
    assert "24w14a" in [v["id"] for v in vanilla]
    assert admin.get("/api/modpacks/providers").json() == {"modrinth": True, "curseforge": False}


def test_settings_enable_curseforge(admin):
    r = admin.put("/api/admin/settings", json={"curseforge_api_key": "$2a$10$abcdefghijklmnop"})
    assert r.json()["curseforge_api_key_set"] and r.json()["curseforge_api_key_hint"] == "…mnop"
    assert admin.get("/api/modpacks/providers").json()["curseforge"] is True


def test_each_game_prefers_its_usual_port(admin, node, agent):
    admin.post(f"/api/nodes/{node['id']}/allocations", json={"ports": "2456-2460"})
    assert _create(admin, node).json()["address"].endswith(":25565")
    r = admin.post("/api/servers", json={"name": "V", "node_id": node["id"], "template": "valheim",
                                         "memory_mb": 4096, "variables": {"SERVER_PASS": "secret123"}})
    assert r.json()["address"].endswith(":2456")


def test_update_check_never_offers_an_older_release():
    from hearth_panel.services.updater import newer_release

    versions = [
        {"id": "b3", "type": "beta", "published": "2026-11-01"},
        {"id": "r2", "type": "release", "published": "2026-10-01"},
    ]
    assert newer_release(versions, "b3") is None  # pinned to a newer beta
    assert newer_release(versions, "r2") is None  # already latest
    assert newer_release(versions, "r1")["id"] == "r2"  # unknown/older version


def test_owner_cannot_change_custom_image(admin, node):
    friend = admin.post("/api/users", json={"username": "friend", "password": "friend-password-1"}).json()
    r = admin.post("/api/servers", json={"name": "C", "node_id": node["id"], "template": "custom", "memory_mb": 512,
                                         "owner_id": friend["id"], "variables": {"IMAGE": "nginx:alpine"}})
    sid = r.json()["id"]
    admin.post("/api/auth/logout")
    admin.post("/api/auth/login", json={"username": "friend", "password": "friend-password-1"})
    assert admin.patch(f"/api/servers/{sid}", json={"variables": {"IMAGE": "evil/miner"}}).status_code == 403
    # Re-sending the unchanged value (as the form does) is fine.
    assert admin.patch(f"/api/servers/{sid}", json={"variables": {"IMAGE": "nginx:alpine"}}).status_code == 200


def test_local_node_registered_from_env(client):
    from hearth_panel.main import register_local_node
    from hearth_panel.models import Node

    ctx = client.app.state.ctx
    settings = type(ctx.settings)(**{**ctx.settings.__dict__, "local_node_token": "t" * 40})
    with ctx.db.sessionmaker() as db:
        register_local_node(db, settings)
        register_local_node(db, settings)  # idempotent
        nodes = db.query(Node).all()
        assert len(nodes) == 1 and nodes[0].name == "local" and len(nodes[0].allocations) == 14
        rerun = type(settings)(**{**settings.__dict__, "local_node_token": "n" * 40})  # setup re-run
        register_local_node(db, rerun)
        db.refresh(nodes[0])
        assert nodes[0].token == "n" * 40 and db.query(Node).count() == 1


def test_custom_validator_errors_are_422_not_500(admin):
    r = admin.post("/api/nodes", json={"name": "x", "url": "ftp://bad", "public_host": "h"})
    assert r.status_code == 422 and "Agent URL" in r.json()["detail"]
    r = admin.post("/api/nodes/1/allocations", json={"ip": "not-an-ip", "ports": "25565"})
    assert r.status_code in (404, 422)


def test_apply_update_when_nothing_newer(admin, node):
    sid = _create(admin, node, modpack={"provider": "modrinth", "project_id": "cool-pack"}).json()["id"]
    assert admin.post(f"/api/servers/{sid}/updates/apply").status_code == 409


def test_claim_refuses_taken_port(admin, node):
    from hearth_panel.models import Allocation
    from hearth_panel.services.provisioning import ProvisionError, claim

    sid = _create(admin, node).json()["id"]
    with admin.app.state.ctx.db.sessionmaker() as db:
        taken = db.query(Allocation).filter_by(server_id=sid).first()
        with pytest.raises(ProvisionError):
            claim(db, taken, sid + 1, is_primary=True, is_template=False)
