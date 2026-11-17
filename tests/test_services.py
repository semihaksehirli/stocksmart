import pytest

from app import services
from app.models import MovementType


def make_product(db, sku="TST-001", qty=10, reorder=3, **kw):
    return services.create_product(db, sku=sku, name=kw.pop("name", f"Item {sku}"), price=5.0, cost=2.0,
                                   quantity=qty, reorder_level=reorder, **kw)


def test_create_product_logs_opening_stock(db):
    p = make_product(db, qty=10)
    assert p.quantity == 10
    moves = services.list_movements(db, p.id)
    assert len(moves) == 1 and moves[0].note == "Opening stock" and moves[0].quantity_after == 10


def test_duplicate_sku_rejected(db):
    make_product(db, sku="abc-1")
    with pytest.raises(services.InventoryError):
        make_product(db, sku="ABC-1")  # SKUs are normalized to upper case


def test_negative_price_rejected(db):
    with pytest.raises(services.InventoryError):
        services.create_product(db, sku="X-1", name="X", price=-1, cost=1)


def test_sale_and_delivery_update_quantity(db):
    p = make_product(db, qty=10)
    services.record_movement(db, p.id, MovementType.SALE, 4)
    services.record_movement(db, p.id, MovementType.DELIVERY, 6)
    assert services.get_product(db, p.id).quantity == 12
    assert [m.change for m in services.list_movements(db, p.id)] == [6, -4, 10]


def test_cannot_sell_more_than_on_hand(db):
    p = make_product(db, qty=2)
    with pytest.raises(services.InventoryError, match="Not enough stock"):
        services.record_movement(db, p.id, MovementType.SALE, 3)
    assert services.get_product(db, p.id).quantity == 2  # rolled back
    assert len(services.list_movements(db, p.id)) == 1


@pytest.mark.parametrize("mtype,amount", [(MovementType.SALE, 0), (MovementType.DELIVERY, -5), (MovementType.ADJUSTMENT, 0)])
def test_invalid_amounts(db, mtype, amount):
    p = make_product(db)
    with pytest.raises(services.InventoryError):
        services.record_movement(db, p.id, mtype, amount)


def test_adjustment_can_be_negative(db):
    p = make_product(db, qty=5)
    services.record_movement(db, p.id, MovementType.ADJUSTMENT, -2, "Damaged")
    assert services.get_product(db, p.id).quantity == 3


def test_low_stock_includes_items_at_reorder_level(db):
    make_product(db, sku="A-1", qty=3, reorder=3)
    make_product(db, sku="B-1", qty=4, reorder=3)
    make_product(db, sku="C-1", qty=0, reorder=1)
    assert [p.sku for p in services.low_stock(db)] == ["C-1", "A-1"]


def test_dashboard_and_top_sellers(db):
    a = make_product(db, sku="A-1", qty=10)
    b = make_product(db, sku="B-1", qty=10)
    services.record_movement(db, a.id, MovementType.SALE, 2)
    services.record_movement(db, b.id, MovementType.SALE, 5)
    s = services.dashboard_summary(db)
    assert s["units_on_hand"] == 13
    assert s["inventory_value_cost"] == 26.0
    assert s["units_sold"] == 7 and s["sales_revenue"] == 35.0
    assert [(p.sku, u) for p, u in services.top_sellers(db)] == [("B-1", 5), ("A-1", 2)]


def test_search_and_find(db):
    make_product(db, sku="COF-1", name="House Blend")
    make_product(db, sku="SNK-1", name="Chips")
    assert [p.sku for p in services.search_products(db, "blend")] == ["COF-1"]
    assert services.find_product(db, "chips").sku == "SNK-1"
    assert services.find_product(db, "cof-1").name == "House Blend"


def test_csv_export(db):
    sup = services.create_supplier(db, "Acme")
    make_product(db, sku="A-1", qty=1, reorder=2, supplier_id=sup.id)
    lines = services.inventory_csv(db).strip().splitlines()
    assert lines[0].startswith("SKU,Name")
    assert lines[1] == "A-1,Item A-1,General,Acme,5.00,2.00,1,2,yes"
