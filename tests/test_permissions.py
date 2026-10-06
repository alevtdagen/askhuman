import pytest

from askhuman.channels import reply_token, reply_url


async def test_agent_cannot_configure_or_answer(
    http, human, agent_headers, admin_headers, settings
):
    request = await human.create("Who owns this dataset?")
    assert (await http.get("/api/admin/config", headers=agent_headers)).status_code == 401
    assert (await http.put("/api/admin/config", headers=agent_headers, json={})).status_code == 401
    assert (
        await http.post(
            f"/api/admin/requests/{request.id}/answer",
            headers=agent_headers,
            json={"answer": "Me", "respondent": "Agent"},
        )
    ).status_code == 401
    assert (await http.get(f"/api/replies/{request.id}", headers=agent_headers)).status_code == 401
    assert (await http.get(f"/v1/requests/{request.id}")).status_code == 401
    body = (await http.get(f"/v1/requests/{request.id}", headers=agent_headers)).text
    assert settings.signing_key not in body and settings.admin_key not in body
    assert reply_token(settings, request.id) not in body


async def test_reply_capability_scoped_to_one_request(http, human, settings):
    first, second = await human.create("Question one"), await human.create("Question two")
    token = reply_token(settings, first.id)
    headers = {"Authorization": f"Bearer {token}"}
    assert "#" + token in reply_url(settings, first)
    assert (await http.get(f"/api/replies/{first.id}", headers=headers)).status_code == 200
    assert (await http.get(f"/api/replies/{second.id}", headers=headers)).status_code == 401
    assert (await http.get("/api/admin/config", headers=headers)).status_code == 401
    reply = await http.post(
        f"/api/replies/{first.id}",
        headers=headers,
        json={"answer": "The finance team", "respondent": "Sam"},
    )
    assert reply.status_code == 200
    assert reply.json()["response"]["source"] == "reply_link"
    assert reply.json()["deliveries"] == []


async def test_routes_are_human_owned_and_unknown_recipient_fails(http, human, admin_headers, app):
    config = (await http.get("/api/admin/config", headers=admin_headers)).json()
    config["routes"] = {"data-owner": ["inbox"]}
    assert (
        await http.put("/api/admin/config", headers=admin_headers, json=config)
    ).status_code == 200
    routed = await human.create("Which definition?", recipient="data-owner")
    assert routed.deliveries[0].channel == "inbox"
    with pytest.raises(Exception, match="No human-configured route"):
        await human.create("Private question", recipient="data-owenr")
    assert len(app.state.store.list()) == 1


async def test_invalid_routing_does_not_replace_current_config(http, admin_headers):
    original = (await http.get("/api/admin/config", headers=admin_headers)).json()
    result = await http.put(
        "/api/admin/config",
        headers=admin_headers,
        json=original | {"default_channels": ["missing"]},
    )
    assert result.status_code == 422
    assert (await http.get("/api/admin/config", headers=admin_headers)).json() == original


async def test_browser_security_headers_and_no_inline_scripts(http):
    page = await http.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert page.headers["Cache-Control"] == "no-store"
    assert "<script>" not in page.text
    for filename in ("app.js", "shared.js", "reply.js", "style.css"):
        assert (await http.get("/assets/" + filename)).status_code == 200
