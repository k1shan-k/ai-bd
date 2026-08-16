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
