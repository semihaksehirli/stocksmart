"""Load realistic sample data for a small convenience/coffee shop.

Run:  python -m app.seed
Demo logins:  owner / owner1234   and   employee / employee1234
"""
import random

from sqlalchemy import select

from app import auth, services
from app.db import SessionLocal, init_db
from app.models import MovementType, Product, Role, User

SUPPLIERS = [
    ("Garden State Coffee Co.", "orders@gscoffee.example", "973-555-0110"),
    ("Passaic Valley Dairy", "sales@pvdairy.example", "973-555-0142"),
    ("North Jersey Snacks", "hello@njsnacks.example", "201-555-0177"),
    ("Paper & Pack Supply", "support@paperpack.example", "862-555-0191"),
]

# sku, name, category, price, cost, qty, reorder level, supplier index
PRODUCTS = [
    ("COF-001", "House Blend Beans 12oz", "Coffee", 14.99, 7.50, 24, 8, 0),
    ("COF-002", "Espresso Roast 12oz", "Coffee", 15.99, 8.10, 6, 8, 0),
    ("COF-003", "Decaf Colombian 12oz", "Coffee", 15.49, 7.90, 11, 5, 0),
    ("DAI-001", "Whole Milk 1gal", "Dairy", 4.79, 3.10, 18, 10, 1),
    ("DAI-002", "Oat Milk 64oz", "Dairy", 5.49, 3.40, 4, 6, 1),
    ("DAI-003", "Greek Yogurt Cup", "Dairy", 1.99, 0.95, 30, 12, 1),
    ("SNK-001", "Sea Salt Chips", "Snacks", 2.49, 1.05, 40, 15, 2),
    ("SNK-002", "Granola Bar", "Snacks", 1.79, 0.70, 9, 20, 2),
    ("SNK-003", "Dark Chocolate Bar", "Snacks", 3.29, 1.40, 22, 10, 2),
    ("SNK-004", "Trail Mix Pouch", "Snacks", 4.49, 2.00, 16, 8, 2),
    ("SUP-001", "To-Go Cups 12oz (50)", "Supplies", 8.99, 4.20, 3, 5, 3),
    ("SUP-002", "Paper Bags (100)", "Supplies", 6.49, 2.80, 12, 4, 3),
]


def seed(reset_sales: bool = True) -> None:
    init_db()
    random.seed(42)
    with SessionLocal() as db:
        if db.scalar(select(User).limit(1)) is None:
            auth.create_user(db, "owner", "owner1234", Role.OWNER)
            auth.create_user(db, "employee", "employee1234", Role.EMPLOYEE)
        if db.scalar(select(Product).limit(1)) is not None:
            print("Database already has products; skipping product seed.")
            return
        suppliers = [services.create_supplier(db, *s) for s in SUPPLIERS]
        products = []
        for sku, name, cat, price, cost, qty, reorder, sup in PRODUCTS:
            products.append(services.create_product(
                db, sku=sku, name=name, category=cat, price=price, cost=cost,
                quantity=qty + 20, reorder_level=reorder, supplier_id=suppliers[sup].id,
            ))
        # Simulate a couple of weeks of sales so reports have data.
        for p, (_, _, _, _, _, target_qty, _, _) in zip(products, PRODUCTS):
            to_sell = 20
            while to_sell > 0:
                n = min(to_sell, random.randint(1, 4))
                services.record_movement(db, p.id, MovementType.SALE, n, "Sample sale")
                to_sell -= n
        services.record_movement(db, products[0].id, MovementType.DELIVERY, 12, "Weekly coffee delivery")
        services.record_movement(db, products[0].id, MovementType.SALE, 12, "Sample sale")
    print("Seeded demo data. Logins: owner/owner1234, employee/employee1234")


if __name__ == "__main__":
    seed()
