"""POS integration through the REST API, with POS servers faked (no network)."""
import hashlib
import hmac
import json

import pytest

from app.pos import base
from tests.conftest import login

PRODUCTS = [
    {"sku": "COF-001", "name": "House Blend", "price": 14.99, "cost": 7.5, "quantity": 10, "reorder_level": 4},
    {"sku": "SNK-001", "name": "Chips", "price": 2.49, "cost": 1.05, "quantity": 5, "reorder_level": 2},
]


@pytest.fixture()
def owner(client):
    h = login(client, "owner", "owner1234")
    for p in PRODUCTS:
        assert client.post("/products", json=p, headers=h).status_code == 201
    return h


@pytest.fixture()
def fake_pos(monkeypatch):
    """Routes adapter HTTP calls to a dict of handlers keyed by (method, url fragment)."""
    routes, calls = {}, []

    def request(self, method, url, headers=None, params=None, json_body=None):
        calls.append((method, url, json_body))
        for (m, frag), reply in routes.items():
            if m == method and frag in url:
                return reply(json_body) if callable(reply) else reply
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(base.HttpClient, "request", request)
    return routes, calls


def qty(client, h, sku):
    return next(p["quantity"] for p in client.get("/products", headers=h).json() if p["sku"] == sku)


def signed(secret, payload):
    body = json.dumps(payload).encode()
    return body, {"X-StockSmart-Signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
                  "Content-Type": "application/json"}


def test_owner_only(client):
    staff = login(client, "staff", "staff1234")
    assert client.get("/pos/connections", headers=staff).status_code == 403
    assert client.get("/pos/providers", headers=staff).status_code == 403


def test_providers_listed(client, owner):
    names = {p["provider"] for p in client.get("/pos/providers", headers=owner).json()}
    assert {"square", "clover", "generic"} <= names


def test_required_fields_and_secret_masking(client, owner):
    r = client.post("/pos/connections", json={"provider": "square", "name": "Front", "settings": {"location_id": "L1"}}, headers=owner)
    assert r.status_code == 400 and "Access token" in r.json()["detail"]
    r = client.post("/pos/connections", json={"provider": "square", "name": "Front",
                                              "settings": {"access_token": "EAAAsecret1234", "location_id": "L1"}}, headers=owner)
    assert r.status_code == 201
    c = r.json()
    assert c["settings"]["access_token"] == "•••• 1234" and c["settings"]["environment"] == "production"
    assert c["webhook_url"].endswith(f"/pos/webhooks/{c['id']}")
    # editing other settings keeps the saved token
    r = client.patch(f"/pos/connections/{c['id']}", json={"settings": {"access_token": "", "location_id": "L2"}}, headers=owner)
    assert r.json()["settings"]["access_token"] == "•••• 1234" and r.json()["settings"]["location_id"] == "L2"


def test_generic_webhook_counts_each_sale_once(client, owner):
    c = client.post("/pos/connections", json={"provider": "generic", "name": "Toast", "settings": {"webhook_secret": "s3cret"}},
                    headers=owner).json()
    payload = {"order_id": "T-1", "lines": [{"sku": "COF-001", "quantity": 3}, {"sku": "NOPE-1", "quantity": 1}]}
    body, headers = signed("s3cret", payload)
    url = f"/pos/webhooks/{c['id']}"
    assert client.post(url, content=body, headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post(url, content=body, headers=headers).status_code == 200
    assert client.post(url, content=body, headers=headers).status_code == 200  # POS retried
    assert qty(client, owner, "COF-001") == 7
    events = client.get(f"/pos/events?connection_id={c['id']}", headers=owner).json()
    assert sorted(e["status"] for e in events) == ["applied", "unmatched"]
    # the unknown SKU can be fixed by creating the product, then retried
    client.post("/products", json={"sku": "NOPE-1", "name": "New", "price": 1, "cost": 1, "quantity": 4}, headers=owner)
    assert "1 sale line recorded" in client.post(f"/pos/connections/{c['id']}/retry", headers=owner).json()["message"]
    assert qty(client, owner, "NOPE-1") == 3


def test_sale_larger_than_stock_is_held_for_review(client, owner):
    c = client.post("/pos/connections", json={"provider": "generic", "name": "Other", "settings": {"webhook_secret": "k"}}, headers=owner).json()
    body, headers = signed("k", {"order_id": "9", "lines": [{"sku": "SNK-001", "quantity": 50}]})
    client.post(f"/pos/webhooks/{c['id']}", content=body, headers=headers)
    assert qty(client, owner, "SNK-001") == 5
    assert client.get(f"/pos/events?status_filter=failed", headers=owner).json()[0]["quantity"] == 50
    assert client.get("/pos/connections", headers=owner).json()[0]["events"]["failed"] == 1


def test_csv_import_and_refund(client, owner):
    c = client.post("/pos/connections", json={"provider": "generic", "name": "Export"}, headers=owner).json()
    csv_text = "order_id,sku,quantity\nA,COF-001,2\nA,SNK-001,1\nB,COF-001,-1\n"
    r = client.post(f"/pos/connections/{c['id']}/import-csv", json={"csv": csv_text}, headers=owner)
    assert r.status_code == 200 and r.json()["applied"] == 3
    assert qty(client, owner, "COF-001") == 9 and qty(client, owner, "SNK-001") == 4
    r = client.post(f"/pos/connections/{c['id']}/import-csv", json={"csv": csv_text}, headers=owner)
    assert r.json()["duplicates"] == 3 and qty(client, owner, "COF-001") == 9


def test_square_sync_pushes_new_count_to_clover(client, owner, fake_pos):
    routes, calls = fake_pos
    routes[("POST", "/v2/orders/search")] = (200, {"orders": [{
        "id": "SQ1", "state": "COMPLETED", "closed_at": "2030-01-01T00:00:00Z",
        "line_items": [{"uid": "a", "catalog_object_id": "VAR1", "quantity": "2", "name": "House Blend"}]}]})
    routes[("POST", "/v2/catalog/batch-retrieve")] = (200, {"objects": [{"id": "VAR1", "item_variation_data": {"sku": "COF-001"}}]})
    routes[("GET", "/v3/merchants/M1/items")] = (200, {"elements": [{"id": "CLV1", "name": "House Blend", "sku": "COF-001"},
                                                                    {"id": "CLV2", "name": "Gift card"}]})
    routes[("POST", "/item_stocks/")] = (200, {})

    square = client.post("/pos/connections", json={"provider": "square", "name": "Square",
                                                   "settings": {"access_token": "t", "location_id": "L1"}}, headers=owner).json()
    clover = client.post("/pos/connections", json={"provider": "clover", "name": "Clover", "push_stock": True,
                                                   "settings": {"access_token": "t", "merchant_id": "M1"}}, headers=owner).json()
    r = client.post(f"/pos/connections/{clover['id']}/catalog", headers=owner)
    assert r.json()["matched"] == 1 and r.json()["unmatched"] == 1
    items = client.get(f"/pos/connections/{clover['id']}/items", headers=owner).json()
    assert items[0]["name"] == "Gift card" and items[0]["product_id"] is None  # unmatched listed first

    r = client.post(f"/pos/connections/{square['id']}/sync", headers=owner)
    assert r.status_code == 200 and r.json()["applied"] == 1
    assert qty(client, owner, "COF-001") == 8
    assert ("POST", "https://api.clover.com/v3/merchants/M1/item_stocks/CLV1", {"quantity": 8}) in calls
    # syncing again doesn't double count
    assert client.post(f"/pos/connections/{square['id']}/sync", headers=owner).json()["duplicates"] == 1
    # a sale entered in StockSmart is pushed too
    pid = next(p["id"] for p in client.get("/products", headers=owner).json() if p["sku"] == "COF-001")
    client.post("/movements", json={"product_id": pid, "type": "sale", "quantity": 1}, headers=owner)
    assert calls[-1] == ("POST", "https://api.clover.com/v3/merchants/M1/item_stocks/CLV1", {"quantity": 7})


def test_manual_matching(client, owner, fake_pos):
    routes, _ = fake_pos
    routes[("GET", "/v3/merchants/M1/items")] = (200, {"elements": [{"id": "X9", "name": "Kettle chips"}]})
    c = client.post("/pos/connections", json={"provider": "clover", "name": "C", "settings": {"access_token": "t", "merchant_id": "M1"}}, headers=owner).json()
    client.post(f"/pos/connections/{c['id']}/catalog", headers=owner)
    link = client.get(f"/pos/connections/{c['id']}/items", headers=owner).json()[0]
    pid = next(p["id"] for p in client.get("/products", headers=owner).json() if p["sku"] == "SNK-001")
    assert client.patch(f"/pos/items/{link['id']}", json={"product_id": pid}, headers=owner).json()["product_id"] == pid
    assert client.get("/pos/connections", headers=owner).json()[0]["items"] == {"linked": 1, "unmatched": 0}


def test_clover_verification_code_is_shown(client, owner):
    c = client.post("/pos/connections", json={"provider": "clover", "name": "C", "settings": {"access_token": "t", "merchant_id": "M1"}}, headers=owner).json()
    assert client.post(f"/pos/webhooks/{c['id']}", json={"verificationCode": "abc-123"}).status_code == 200
    assert "abc-123" in client.get("/pos/connections", headers=owner).json()[0]["last_status"]
