from conftest import csv_bytes


def test_import_selects_only_yes_maybe_and_is_idempotent(client, event):
    content = csv_bytes(
        [
            ("Yes Person", "yes@example.com", "@yesperson", "", "YES"),
            ("Maybe Person", "maybe@example.com", "@maybeperson", "+15550000002", "Maybe"),
            ("No Person", "no@example.com", "@noperson", "", "No"),
            ("Broken", "not-an-email", "@broken", "", "yes"),
        ]
    )
    first = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("registrants.csv", content, "text/csv")},
    )
    assert first.status_code == 200
    assert first.json() | {"import_job_id": "ignored"} == {
        "import_job_id": "ignored",
        "eligible": 2,
        "ineligible": 1,
        "duplicate": 0,
        "suppressed": 0,
        "invalid": 1,
        "quarantined": 0,
    }
    second = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("registrants.csv", content, "text/csv")},
    )
    assert second.json() == first.json()
    assert len(client.get(f"/api/v1/leads?event_id={event['id']}").json()) == 2


def test_global_suppression_blocks_future_event_import(client, event, imported_lead):
    suppressed = client.post(
        f"/api/v1/leads/{imported_lead['id']}/suppress", json={"reason": "manual_block"}
    )
    assert suppressed.status_code == 200
    other = client.post(
        "/api/v1/events", json={"slug": "other-event", "name": "Other Event", "timezone": "UTC"}
    ).json()
    content = csv_bytes([("Ava Sponsor", "ava@example.com", "avasponsor", "+15550000001", "maybe")])
    result = client.post(
        f"/api/v1/events/{other['id']}/imports",
        files={"file": ("other.csv", content, "text/csv")},
    )
    assert result.json()["suppressed"] == 1
    assert client.get(f"/api/v1/leads?event_id={other['id']}").json() == []



def test_sponsor_answer_requires_exact_yes_or_maybe(client, event):
    content = csv_bytes(
        [
            ("Prefix", "prefix@example.com", "prefix", "", "yes please"),
            ("Synonym", "synonym@example.com", "synonym", "", "interested"),
            ("Maybe Exact", "maybeexact@example.com", "maybeexact", "", " MAYBE "),
        ]
    )
    result = client.post(
        f"/api/v1/events/{event['id']}/imports",
        files={"file": ("strict.csv", content, "text/csv")},
    )
    assert result.status_code == 200
    assert result.json()["eligible"] == 1
    assert result.json()["ineligible"] == 2



def test_global_suppression_stops_every_existing_event_lead(client, event, imported_lead):
    other = client.post(
        "/api/v1/events",
        json={"slug": "existing-other", "name": "Existing Other", "timezone": "UTC"},
    ).json()
    content = csv_bytes(
        [("Ava Sponsor", "ava@example.com", "avasponsor", "+15550000001", "yes")]
    )
    imported = client.post(
        f"/api/v1/events/{other['id']}/imports",
        files={"file": ("existing.csv", content, "text/csv")},
    )
    assert imported.json()["eligible"] == 1
    response = client.post(
        f"/api/v1/leads/{imported_lead['id']}/suppress",
        json={"reason": "prospect_rejection"},
    )
    assert response.status_code == 200
    all_leads = client.get("/api/v1/leads").json()
    ava_leads = [lead for lead in all_leads if lead["email"] == "ava@example.com"]
    assert len(ava_leads) == 2
    assert all(lead["state"] == "suppressed" for lead in ava_leads)
    assert all(lead["automation_status"] == "stopped" for lead in ava_leads)
