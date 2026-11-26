"""Input checks: odd values are refused with a message, never stored or answered with a 500."""

from tests.test_features import _create


def test_names_made_of_spaces_are_refused_everywhere(admin, node, agent):
    sid = _create(admin, node)["id"]
    blank = {"name": "   "}
    assert admin.patch(f"/api/servers/{sid}", json=blank).status_code == 422
    assert admin.post(f"/api/servers/{sid}/clone", json=blank).status_code == 422
    assert admin.post(f"/api/servers/{sid}/schedules", json={**blank, "cron": "* * * * *", "action": "restart"}).status_code == 422
    assert admin.post("/api/account/tokens", json=blank).status_code == 422
    assert admin.post("/api/nodes", json={**blank, "url": "http://x:1", "public_host": "h"}).status_code == 422
    assert admin.post("/api/servers", json={**blank, "node_id": node["id"], "template": "minecraft-java",
                                             "memory_mb": 2048, "accept_eula": True}).status_code == 422
    assert admin.get(f"/api/servers/{sid}").json()["name"] == "Survival"


def test_names_are_trimmed(admin, node, agent):
    sid = _create(admin, node)["id"]
    assert admin.patch(f"/api/servers/{sid}", json={"name": "  Creative  "}).json()["name"] == "Creative"
