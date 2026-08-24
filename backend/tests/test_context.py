from pathlib import Path


def test_context_validation_and_immutable_versions(client, event, valid_documents):
    validation = client.post(
        f"/api/v1/events/{event['id']}/contexts/validate",
        json={"documents": valid_documents},
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is True

    first = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": valid_documents},
    )
    assert first.status_code == 201
    assert first.json()["version"] == 1

    same = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": valid_documents},
    )
    assert same.json()["id"] == first.json()["id"]

    changed = dict(valid_documents)
    changed["faq.md"] += "\nA newly approved answer."
    second = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": changed},
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert second.json()["id"] != first.json()["id"]


def test_context_rejects_price_floor_that_breaks_discount_cap(client, event, valid_documents):
    documents = dict(valid_documents)
    documents["packages.md"] = documents["packages.md"].replace("min_price: 9000", "min_price: 8000")
    response = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": documents},
    )
    assert response.status_code == 422
    assert "larger discount" in response.json()["detail"]


def test_repository_templates_activate_with_timestamp_front_matter(client, event):
    """The documented pilot walkthrough activates the shipped templates verbatim.

    Their front matter carries `starts_at`/`outreach_cutoff_at` timestamps, which YAML parses
    into datetime objects that must still serialize into the compiled JSON column.
    """
    root = Path(__file__).resolve().parents[2]
    documents = {
        path.name: path.read_text()
        for path in [
            *(root / "contexts" / "organization").glob("*.md"),
            *(root / "contexts" / "events" / "example-event").glob("*.md"),
        ]
    }
    assert "starts_at" in documents["event.md"]

    validation = client.post(
        f"/api/v1/events/{event['id']}/contexts/validate",
        json={"documents": documents},
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True

    activated = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": documents},
    )
    assert activated.status_code == 201, activated.text
    assert activated.json()["compiled"]["event"]["starts_at"] == "2027-01-15T09:00:00+00:00"


def test_context_read_returns_exact_documents_for_editor_restore(
    client, event, valid_documents
):
    activated = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": valid_documents},
    )
    assert activated.status_code == 201
    assert activated.json()["documents"] == valid_documents
    listed = client.get(f"/api/v1/events/{event['id']}/contexts").json()
    assert listed[0]["documents"] == valid_documents
    assert "sales-deck.md" in listed[0]["documents"]


def test_event_metadata_can_be_updated_without_mutating_context_versions(
    client, event, valid_documents
):
    context = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": valid_documents},
    ).json()
    updated = client.patch(
        f"/api/v1/events/{event['id']}",
        json={
            "name": "Updated Web3 Summit",
            "timezone": "America/New_York",
            "starts_at": "2027-01-15T14:00:00Z",
            "outreach_cutoff_at": "2027-01-10T22:00:00Z",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Updated Web3 Summit"
    assert updated.json()["timezone"] == "America/New_York"
    fetched = client.get(f"/api/v1/events/{event['id']}").json()
    assert fetched["outreach_cutoff_at"].startswith("2027-01-10T22:00:00")
    contexts = client.get(f"/api/v1/events/{event['id']}/contexts").json()
    assert contexts[0]["id"] == context["id"]
    assert contexts[0]["documents"] == valid_documents

    invalid_zone = client.patch(
        f"/api/v1/events/{event['id']}", json={"timezone": "Mars/Olympus"}
    )
    assert invalid_zone.status_code == 422
    invalid_cutoff = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"outreach_cutoff_at": "2027-01-20T00:00:00Z"},
    )
    assert invalid_cutoff.status_code == 422


def test_two_events_keep_decks_audiences_packages_and_prices_isolated(
    client, event, valid_documents
):
    event_b = client.post(
        "/api/v1/events",
        json={"slug": "protocol-expo", "name": "Protocol Expo", "timezone": "Europe/Paris"},
    ).json()
    documents_a = dict(valid_documents)
    documents_a["sales-deck.md"] += "\nEvent A validator infrastructure positioning."
    documents_b = dict(valid_documents)
    documents_b["event.md"] = documents_b["event.md"].replace("Test Summit", "Protocol Expo")
    documents_b["audience.md"] = documents_b["audience.md"].replace(
        "Technology leaders", "DAO governance and protocol treasury leaders"
    )
    documents_b["sales-deck.md"] = (
        "---\nowner: protocol partnerships\n---\nEvent B protocol treasury positioning."
    )
    documents_b["packages.md"] = (
        documents_b["packages.md"]
        .replace("list_price: 10000", "list_price: 30000")
        .replace("min_price: 9000", "min_price: 27000")
        .replace("list_price: 5000", "list_price: 15000")
        .replace("min_price: 4500", "min_price: 13500")
    )
    context_a = client.post(
        f"/api/v1/events/{event['id']}/contexts/activate",
        json={"documents": documents_a},
    )
    context_b = client.post(
        f"/api/v1/events/{event_b['id']}/contexts/activate",
        json={"documents": documents_b},
    )
    assert context_a.status_code == 201, context_a.text
    assert context_b.status_code == 201, context_b.text
    kit_a = client.get(f"/api/v1/events/{event['id']}/contexts").json()[0]
    kit_b = client.get(f"/api/v1/events/{event_b['id']}/contexts").json()[0]
    assert "Event A validator" in kit_a["documents"]["sales-deck.md"]
    assert "Event B protocol" not in kit_a["documents"]["sales-deck.md"]
    assert "Event B protocol" in kit_b["documents"]["sales-deck.md"]
    assert "DAO governance" in kit_b["documents"]["audience.md"]
    assert kit_a["compiled"]["packages"][0]["list_price"] == "10000"
    assert kit_b["compiled"]["packages"][0]["list_price"] == "30000"
