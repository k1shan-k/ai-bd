def test_operations_expose_audit_actions_and_admin_suppression_controls(
    client, event, campaign, imported_lead
):
    started = client.post(
        f"/api/v1/leads/{imported_lead['id']}/workflow/start",
        json={"campaign_id": campaign["id"], "now": "2026-08-17T10:00:00Z"},
    )
    assert started.status_code == 200
    actions = client.get("/api/v1/operations/actions").json()
    assert len(actions) == 2
    assert all(action["lead_id"] == imported_lead["id"] for action in actions)

    suppressed = client.post(
        f"/api/v1/leads/{imported_lead['id']}/suppress",
        json={"reason": "manual_block"},
    )
    assert suppressed.status_code == 200
    entries = client.get("/api/v1/operations/suppressions").json()
    assert {entry["identity_type"] for entry in entries} == {
        "email",
        "telegram",
        "whatsapp",
    }
    contact_id = entries[0]["contact_id"]

    viewer = client.delete(
        f"/api/v1/operations/suppressions/contact/{contact_id}",
        headers={"X-Actor": "viewer", "X-Role": "viewer"},
    )
    assert viewer.status_code == 403
    removed = client.delete(f"/api/v1/operations/suppressions/contact/{contact_id}")
    assert removed.status_code == 200
    assert removed.json()["removed_identities"] == 3
    assert client.get("/api/v1/operations/suppressions").json() == []
    audits = client.get("/api/v1/operations/audit").json()
    assert "contact.unsuppress" in {entry["action"] for entry in audits}


def test_campaign_simulation_runs_integrated_fake_sequence(client, event, campaign, imported_lead):
    response = client.post(
        f"/api/v1/campaigns/{campaign['id']}/simulate",
        json={"now": "2026-08-17T10:00:00Z", "lead_ids": [imported_lead["id"]]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["launched"] == [imported_lead["id"]]
    assert len(result["cycles"]) == 4
    detail = client.get(f"/api/v1/leads/{imported_lead['id']}").json()
    assert [message["channel"] for message in detail["messages"]].count("email") >= 1
    assert [message["channel"] for message in detail["messages"]].count("telegram") >= 1
    assert [message["channel"] for message in detail["messages"]].count("whatsapp") == 1
