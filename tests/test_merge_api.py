import json

import pytest


@pytest.fixture()
def auth(client):
    _req, resp = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    return {"Cookie": f"flow_session={cookie}"}


def post(client, headers, path, body=None):
    _req, resp = client.post(path, headers=headers, json=body)
    return resp.status, resp.json


def get(client, headers, path):
    _req, resp = client.get(path, headers=headers)
    return resp.status, resp.json


def test_flow_detail_and_land_routes_resolve(client, auth):
    status, flow = get(client, auth, "/api/flows/1")
    assert status == 200 and flow["id"] == 1
    status, _ = post(client, auth, "/api/flows/1/extract")
    assert status == 200


def test_land_blocked_then_allowed_over_http(client, auth):
    status, body = post(client, auth, "/api/flows/1/land")
    assert status == 409 and body["ok"] is False
    assert body["merge"]["finished"] is False

    post(client, auth, "/api/flows/1/extract")
    status, body = post(
        client, auth, "/api/merge/action", {"flow_id": 1, "left": "A", "right": "B"}
    )
    assert status == 200 and body["box"] is True

    status, body = post(client, auth, "/api/flows/1/land")
    assert status == 200 and body["status"] == "已落库"


def test_panel_filters_by_flow_and_exposes_box(client, auth):
    post(client, auth, "/api/merge/action", {"flow_id": 1, "left": "A", "right": "B"})
    post(client, auth, "/api/merge/action", {"flow_id": 2, "left": "C", "right": "D"})

    status, one = get(client, auth, "/api/merge/panel?flow_id=1")
    assert status == 200
    assert [j["flow_id"] for j in one["jobs"]] == [1]
    assert one["box_count"] == 1 and one["conflict_count"] == 1
    assert json.loads(one["box"][0]["body"])["discarded"] == "次源"

    status, allp = get(client, auth, "/api/merge/panel")
    assert status == 200 and len(allp["jobs"]) == 2


def test_swap_sources_keeps_single_job_over_http(client, auth):
    _s, a = post(
        client, auth, "/api/merge/action", {"flow_id": 1, "left": "A", "right": "B"}
    )
    _s, b = post(
        client, auth, "/api/merge/action", {"flow_id": 1, "left": "B", "right": "A"}
    )
    assert a["key"] == b["key"]
    assert b["deduplicated"] is True
    _s, panel = get(client, auth, "/api/merge/panel?flow_id=1")
    assert len(panel["jobs"]) == 1


def test_merge_does_not_unlock_landing_by_itself(client, auth):
    post(client, auth, "/api/flows/1/land")
    _s, pending = post(
        client,
        auth,
        "/api/merge/action",
        {"flow_id": 1, "left": "A", "right": "B", "complete": False},
    )
    assert pending["status"] == "待确认"
    status, body = post(client, auth, "/api/flows/1/land")
    assert status == 409 and body["merge"]["pending"] == 1
