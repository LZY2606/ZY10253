def test_index_title(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "移液执行证言台".encode() in response.content


def test_api_flow_and_resume(client):
    run = client.post("/api/runs", json={}).json()
    run_id = run["id"]
    event = client.post(f"/api/runs/{run_id}/step", json={}).json()
    assert event["op_name"] == "move_head"
    again = client.post(
        f"/api/runs/{run_id}/step", json={"replay_key": event["replay_key"]}
    ).json()
    assert again["id"] == event["id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["conservation"]["conserved"] is True

    faulty = client.post(
        "/api/runs",
        json={"faults": [{"fault": "motion_conflict", "op_name": "move_head",
                          "op_index": 4, "step_id": "X3"}]},
    ).json()
    fid = faulty["id"]
    while client.get(f"/api/runs/{fid}").json()["run"]["status"] == "ready":
        client.post(f"/api/runs/{fid}/step", json={})
    parent = client.get(f"/api/runs/{fid}").json()
    assert parent["recovery_pending"] is True
    child = client.post(f"/api/runs/{fid}/resume", json={}).json()
    while client.get(f"/api/runs/{child['id']}").json()["run"]["status"] == "ready":
        client.post(f"/api/runs/{child['id']}/step", json={})
    done = client.get(f"/api/runs/{child['id']}").json()
    assert done["run"]["status"] == "done"
    assert done["conservation"]["conserved"] is True


def test_export_reset_import(client):
    run = client.post("/api/runs", json={}).json()
    while client.get(f"/api/runs/{run['id']}").json()["run"]["status"] == "ready":
        client.post(f"/api/runs/{run['id']}/step", json={})
    bundle = client.get("/api/export").json()
    client.post("/api/reset", json={})
    assert client.get("/api/runs").json()["runs"] == []
    result = client.post("/api/import", json={"bundle": bundle, "clear": True}).json()
    assert result["verification"]["verified"] is True
    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["run"]["status"] == "done"
