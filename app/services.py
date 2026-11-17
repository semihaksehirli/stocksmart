"""Business logic shared by the REST API, the Gradio UI and the agent tools.

Every stock change goes through `record_movement`, which updates the
product quantity and writes a StockMovement row in the same transaction.
"""
from typing import Optional, Union
import csv
import io
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MovementType, Product, StockMovement, Supplier


class InventoryError(ValueError):
    """Raised for invalid inventory operations (shown to the user)."""


# ---------- Suppliers ----------

def create_supplier(db: Session, name: str, email: Optional[str] = None, phone: Optional[str] = None) -> Supplier:
    name = name.strip()
    if not name:
        raise InventoryError("Supplier name is required.")
    if db.scalar(select(Supplier).where(Supplier.name == name)):
        raise InventoryError(f"Supplier '{name}' already exists.")
    supplier = Supplier(name=name, email=email or None, phone=phone or None)
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


def list_suppliers(db: Session) -> list[Supplier]:
    return list(db.scalars(select(Supplier).order_by(Supplier.name)))


# ---------- Products ----------

def create_product(
    db: Session,
    sku: str,
    name: str,
    price: float,
    cost: float,
    quantity: int = 0,
    reorder_level: int = 5,
    category: str = "General",
    supplier_id: Optional[int] = None,
) -> Product:
    sku, name = sku.strip().upper(), name.strip()
    if not sku or not name:
        raise InventoryError("SKU and name are required.")
    if price < 0 or cost < 0:
        raise InventoryError("Price and cost cannot be negative.")
    if quantity < 0 or reorder_level < 0:
        raise InventoryError("Quantity and reorder level cannot be negative.")
    if db.scalar(select(Product).where(Product.sku == sku)):
        raise InventoryError(f"A product with SKU '{sku}' already exists.")
    if supplier_id is not None and db.get(Supplier, supplier_id) is None:
        raise InventoryError(f"Supplier {supplier_id} not found.")

    product = Product(
        sku=sku, name=name, category=category.strip() or "General",
        price=Decimal(str(price)), cost=Decimal(str(cost)),
        quantity=0, reorder_level=reorder_level, supplier_id=supplier_id,
    )
    db.add(product)
    db.flush()
    if quantity:
        _apply_movement(db, product, MovementType.ADJUSTMENT, quantity, "Opening stock")
    db.commit()
    db.refresh(product)
    return product


def update_product(db: Session, product_id: int, **fields) -> Product:
    product = get_product(db, product_id)
    allowed = {"name", "category", "price", "cost", "reorder_level", "supplier_id"}
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key in {"price", "cost"}:
            if value < 0:
                raise InventoryError(f"{key.title()} cannot be negative.")
            value = Decimal(str(value))
        if key == "reorder_level" and value < 0:
            raise InventoryError("Reorder level cannot be negative.")
        setattr(product, key, value)
    db.commit()
    db.refresh(product)
    return product


def delete_product(db: Session, product_id: int) -> None:
    db.delete(get_product(db, product_id))
    db.commit()


def get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise InventoryError(f"Product {product_id} not found.")
    return product


def find_product(db: Session, sku_or_name: str) -> Product:
    """Look a product up by exact SKU, then by case-insensitive name."""
    term = sku_or_name.strip()
    product = db.scalar(select(Product).where(Product.sku == term.upper()))
    if product is None:
        product = db.scalar(select(Product).where(func.lower(Product.name) == term.lower()))
    if product is None:
        raise InventoryError(f"No product matches '{sku_or_name}'.")
    return product


def search_products(db: Session, query: str = "", category: Optional[str] = None) -> list[Product]:
    stmt = select(Product).order_by(Product.name)
    if query:
        like = f"%{query.strip().lower()}%"
        stmt = stmt.where(func.lower(Product.name).like(like) | func.lower(Product.sku).like(like))
    if category:
        stmt = stmt.where(Product.category == category)
    return list(db.scalars(stmt))


# ---------- Stock movements ----------

def _apply_movement(db: Session, product: Product, mtype: MovementType, change: int, note: Optional[str]):
    new_qty = product.quantity + change
    if new_qty < 0:
        raise InventoryError(
            f"Not enough stock for {product.name}: {product.quantity} on hand, {abs(change)} requested."
        )
    product.quantity = new_qty
    movement = StockMovement(
        product_id=product.id, type=mtype.value, change=change, quantity_after=new_qty, note=note
    )
    db.add(movement)
    return movement


def record_movement(db: Session, product_id: int, mtype: Union[MovementType, str], amount: int, note: Optional[str] = None) -> StockMovement:
    """Record a sale, delivery or adjustment in a single transaction.

    For sales and deliveries `amount` must be positive; the sign is applied here.
    Adjustments take a signed amount.
    """
    mtype = MovementType(mtype)
    if mtype in (MovementType.SALE, MovementType.DELIVERY) and amount <= 0:
        raise InventoryError("Quantity must be greater than zero.")
    if mtype is MovementType.ADJUSTMENT and amount == 0:
        raise InventoryError("Adjustment cannot be zero.")
    change = -amount if mtype is MovementType.SALE else amount

    try:
        # Lock the row on databases that support it (PostgreSQL); SQLite serializes writes.
        product = db.scalar(select(Product).where(Product.id == product_id).with_for_update())
        if product is None:
            raise InventoryError(f"Product {product_id} not found.")
        movement = _apply_movement(db, product, mtype, change, note)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(movement)
    return movement


def list_movements(db: Session, product_id: Optional[int] = None, limit: int = 100) -> list[StockMovement]:
    stmt = select(StockMovement).order_by(StockMovement.created_at.desc(), StockMovement.id.desc()).limit(limit)
    if product_id is not None:
        stmt = stmt.where(StockMovement.product_id == product_id)
    return list(db.scalars(stmt))


# ---------- Alerts and reports ----------

def low_stock(db: Session) -> list[Product]:
    return list(
        db.scalars(select(Product).where(Product.quantity <= Product.reorder_level).order_by(Product.quantity))
    )


def top_sellers(db: Session, limit: int = 5) -> list[tuple[Product, int]]:
    units = func.sum(-StockMovement.change).label("units")
    rows = db.execute(
        select(Product, units)
        .join(StockMovement)
        .where(StockMovement.type == MovementType.SALE.value)
        .group_by(Product.id)
        .order_by(units.desc())
        .limit(limit)
    ).all()
    return [(p, int(u)) for p, u in rows]


def dashboard_summary(db: Session) -> dict:
    products = list(db.scalars(select(Product)))
    sales = db.execute(
        select(Product.price, StockMovement.change)
        .join(Product)
        .where(StockMovement.type == MovementType.SALE.value)
    ).all()
    return {
        "product_count": len(products),
        "units_on_hand": sum(p.quantity for p in products),
        "inventory_value_cost": round(float(sum(Decimal(p.cost) * p.quantity for p in products)), 2),
        "inventory_value_retail": round(float(sum(Decimal(p.price) * p.quantity for p in products)), 2),
        "low_stock_count": sum(1 for p in products if p.is_low_stock),
        "units_sold": sum(-c for _, c in sales),
        "sales_revenue": round(float(sum(Decimal(price) * -c for price, c in sales)), 2),
    }


def inventory_csv(db: Session) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["SKU", "Name", "Category", "Supplier", "Price", "Cost", "Quantity", "Reorder level", "Low stock"])
    for p in search_products(db):
        writer.writerow([
            p.sku, p.name, p.category, p.supplier.name if p.supplier else "",
            f"{Decimal(p.price):.2f}", f"{Decimal(p.cost):.2f}", p.quantity, p.reorder_level,
            "yes" if p.is_low_stock else "no",
        ])
    return buf.getvalue()
