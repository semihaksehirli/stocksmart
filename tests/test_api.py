from tests.conftest import login

PRODUCT = {"sku": "COF-001", "name": "House Blend", "price": 14.99, "cost": 7.5, "quantity": 10, "reorder_level": 4}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_login_rejects_bad_password(client):
    assert client.post("/auth/login", json={"username": "owner", "password": "nope"}).status_code == 401


def test_requires_token(client):
    assert client.get("/products").status_code == 401
    assert client.get("/products", headers={"Authorization": "Bearer junk.token.x"}).status_code == 401


def test_owner_creates_product_employee_cannot(client):
    owner, staff = login(client, "owner", "owner1234"), login(client, "staff", "staff1234")
    assert client.post("/products", json=PRODUCT, headers=staff).status_code == 403
    r = client.post("/products", json=PRODUCT, headers=owner)
    assert r.status_code == 201 and r.json()["quantity"] == 10
    assert len(client.get("/products", headers=staff).json()) == 1


def test_validation_errors(client):
    owner = login(client, "owner", "owner1234")
    assert client.post("/products", json={**PRODUCT, "price": -1}, headers=owner).status_code == 422
    client.post("/products", json=PRODUCT, headers=owner)
    assert client.post("/products", json=PRODUCT, headers=owner).status_code == 400  # duplicate SKU


def test_full_stock_flow(client):
    owner, staff = login(client, "owner", "owner1234"), login(client, "staff", "staff1234")
    pid = client.post("/products", json=PRODUCT, headers=owner).json()["id"]

    r = client.post("/movements", json={"product_id": pid, "type": "sale", "quantity": 7}, headers=staff)
    assert r.status_code == 201 and r.json()["quantity_after"] == 3

    low = client.get("/alerts/low-stock", headers=staff).json()
    assert [p["sku"] for p in low] == ["COF-001"]

    over = client.post("/movements", json={"product_id": pid, "type": "sale", "quantity": 99}, headers=staff)
    assert over.status_code == 400 and "Not enough stock" in over.json()["detail"]

    adj = client.post("/movements", json={"product_id": pid, "type": "adjustment", "quantity": -1}, headers=staff)
    assert adj.status_code == 403

    client.post("/movements", json={"product_id": pid, "type": "delivery", "quantity": 12}, headers=staff)
    dash = client.get("/reports/dashboard", headers=owner).json()
    assert dash["units_on_hand"] == 15 and dash["units_sold"] == 7
    assert dash["top_sellers"][0]["sku"] == "COF-001"

    history = client.get(f"/movements?product_id={pid}", headers=owner).json()
    assert [m["type"] for m in history] == ["delivery", "sale", "adjustment"]


def test_update_and_delete(client):
    owner = login(client, "owner", "owner1234")
    pid = client.post("/products", json=PRODUCT, headers=owner).json()["id"]
    r = client.patch(f"/products/{pid}", json={"price": 15.49}, headers=owner)
    assert r.json()["price"] == 15.49
    assert client.delete(f"/products/{pid}", headers=owner).status_code == 204
    assert client.get(f"/products/{pid}", headers=owner).status_code == 404


def test_csv_owner_only(client):
    owner, staff = login(client, "owner", "owner1234"), login(client, "staff", "staff1234")
    client.post("/products", json=PRODUCT, headers=owner)
    assert client.get("/reports/inventory.csv", headers=staff).status_code == 403
    r = client.get("/reports/inventory.csv", headers=owner)
    assert r.status_code == 200 and "COF-001" in r.text


def test_me_returns_current_user(client):
    staff = login(client, "staff", "staff1234")
    r = client.get("/auth/me", headers=staff)
    assert r.status_code == 200 and r.json()["username"] == "staff" and r.json()["role"] == "employee"


def test_assistant_built_in_mode(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    owner = login(client, "owner", "owner1234")
    client.post("/products", json=PRODUCT, headers=owner)
    assert client.post("/assistant", json={"message": "hi"}).status_code == 401
    r = client.post("/assistant", json={"message": "check COF-001"}, headers=owner)
    assert r.status_code == 200 and r.json()["mode"] == "built-in"
    assert "10 on hand" in r.json()["reply"]
