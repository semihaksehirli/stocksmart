"""SQLAlchemy models: users, suppliers, products and stock movements."""
from typing import Optional
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    OWNER = "owner"
    EMPLOYEE = "employee"


class MovementType(str, Enum):
    SALE = "sale"            # stock goes out
    DELIVERY = "delivery"    # stock comes in from a supplier
    ADJUSTMENT = "adjustment"  # manual correction (+ or -), e.g. damaged items


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default=Role.EMPLOYEE.value)


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    products: Mapped[list["Product"]] = relationship(back_populates="supplier")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_quantity_non_negative"),
        CheckConstraint("reorder_level >= 0", name="ck_reorder_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    category: Mapped[str] = mapped_column(String(60), default="General")
    price: Mapped[float] = mapped_column(Numeric(10, 2))  # selling price
    cost: Mapped[float] = mapped_column(Numeric(10, 2))   # what the shop pays
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=5)
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"), nullable=True)

    supplier: Mapped[Optional[Supplier]] = relationship(back_populates="products")
    movements: Mapped[list["StockMovement"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    @property
    def is_low_stock(self) -> bool:
        return self.quantity <= self.reorder_level


class StockMovement(Base):
    """Every change to a product's quantity is logged here (audit trail)."""

    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    change: Mapped[int] = mapped_column(Integer)  # signed: negative for sales
    quantity_after: Mapped[int] = mapped_column(Integer)
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="movements")


# ---------- POS integrations ----------

class PosConnection(Base):
    """A link to one POS account (e.g. a Square location or a Clover merchant)."""

    __tablename__ = "pos_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    settings_json: Mapped[str] = mapped_column(Text, default="{}")  # credentials and options
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=True)    # pull sales on a timer
    push_stock: Mapped[bool] = mapped_column(Boolean, default=False)  # send stock counts to this POS
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_status_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    links: Mapped[list["PosItemLink"]] = relationship(back_populates="connection", cascade="all, delete-orphan")
    events: Mapped[list["PosEvent"]] = relationship(back_populates="connection", cascade="all, delete-orphan")


class PosItemLink(Base):
    """Matches an item in a POS catalog to a StockSmart product."""

    __tablename__ = "pos_item_links"
    __table_args__ = (UniqueConstraint("connection_id", "external_item_id", name="uq_pos_link"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("pos_connections.id", ondelete="CASCADE"), index=True)
    external_item_id: Mapped[str] = mapped_column(String(120))
    external_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    external_sku: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)

    connection: Mapped[PosConnection] = relationship(back_populates="links")
    product: Mapped[Optional[Product]] = relationship()


class PosEvent(Base):
    """One sale line received from a POS. The unique key stops the same sale being counted twice."""

    __tablename__ = "pos_events"
    __table_args__ = (UniqueConstraint("connection_id", "external_id", name="uq_pos_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("pos_connections.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    order_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    external_item_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    sku: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # applied | unmatched | failed
    message: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    movement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stock_movements.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    connection: Mapped[PosConnection] = relationship(back_populates="events")


# ---------- AI assistant providers ----------

class AiProvider(Base):
    """An AI service the assistant may use, tried in `priority` order."""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    preset: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(300))
    api_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
