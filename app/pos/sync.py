"""Applies POS data to StockSmart: sales → stock movements, StockSmart counts → POS.

Rules:
  * Each POS sale line is stored as a PosEvent with a unique (connection, external_id),
    so a sale that arrives by webhook *and* by polling is only counted once.
  * Lines are matched to products through PosItemLink (set by "Import items" or by hand),
    falling back to an exact SKU match.
  * Unmatched lines and lines that would push stock below zero are kept (status
    "unmatched" / "failed") so the owner can fix them and press Retry.
  * After stock changes, the new counts are pushed to every connection that has
    "push stock" turned on — except the POS the sale came from, which already knows.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import services
from app.models import MovementType, PosConnection, PosEvent, PosItemLink, Product
from app.pos import get_adapter_class
from app.pos.base import HttpClient, PosAdapter, PosError, SaleLine

log = logging.getLogger("stocksmart.pos")
OVERLAP = timedelta(minutes=10)  # re-read a little history each sync; duplicates are skipped


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    # SQLite returns naive datetimes; treat them as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def get_settings(conn: PosConnection) -> Dict[str, str]:
    try:
        return json.loads(conn.settings_json or "{}")
    except ValueError:
        return {}


def adapter_for(conn: PosConnection, http: Optional[HttpClient] = None) -> PosAdapter:
    return get_adapter_class(conn.provider)(get_settings(conn), http)


def set_status(conn: PosConnection, ok: bool, message: str) -> None:
    conn.last_status_ok = ok
    conn.last_status = f"{utcnow():%Y-%m-%d %H:%M} UTC · {message}"[:500]


# ---------- matching ----------

def _find_link(db: Session, conn_id: int, external_item_id: Optional[str]) -> Optional[PosItemLink]:
    if not external_item_id:
        return None
    return db.scalar(select(PosItemLink).where(PosItemLink.connection_id == conn_id,
                                               PosItemLink.external_item_id == external_item_id))


def _product_by_sku(db: Session, sku: Optional[str]) -> Optional[Product]:
    if not sku:
        return None
    return db.scalar(select(Product).where(Product.sku == sku.strip().upper()))


def match_product(db: Session, conn: PosConnection, line: SaleLine) -> Optional[Product]:
    link = _find_link(db, conn.id, line.external_item_id)
    if link and link.product_id:
        product = db.get(Product, link.product_id)
        if product:
            return product
    product = _product_by_sku(db, line.sku)
    if line.external_item_id:
        if link is None:
            link = PosItemLink(connection_id=conn.id, external_item_id=line.external_item_id)
            db.add(link)
        link.external_name = link.external_name or (line.name or None)
        link.external_sku = link.external_sku or (line.sku or None)
        if product and not link.product_id:
            link.product_id = product.id
    return product


# ---------- sales in ----------

def apply_lines(db: Session, conn: PosConnection, lines: Iterable[SaleLine]) -> Dict[str, object]:
    """Record POS sale lines as stock movements. Returns counts and the product ids that changed."""
    result = {"applied": 0, "unmatched": 0, "failed": 0, "duplicates": 0, "product_ids": set()}
    for line in lines:
        if db.scalar(select(PosEvent.id).where(PosEvent.connection_id == conn.id, PosEvent.external_id == line.external_id)):
            result["duplicates"] += 1
            continue
        event = PosEvent(connection_id=conn.id, external_id=line.external_id[:200], order_id=(line.order_id or "")[:120],
                         external_item_id=line.external_item_id, sku=line.sku, name=(line.name or "")[:200] or None,
                         quantity=line.quantity)
        try:
            product = match_product(db, conn, line)
            if product is None:
                event.status = "unmatched"
                event.message = "No product matches this item. Match it under Items, then press Retry."
            else:
                event.product_id = product.id
                if line.quantity > 0:
                    mtype, change, note = MovementType.SALE, -line.quantity, f"{conn.name} sale, order {line.order_id}"
                else:
                    mtype, change, note = MovementType.ADJUSTMENT, -line.quantity, f"{conn.name} refund, order {line.order_id}"
                try:
                    movement = services._apply_movement(db, product, mtype, change, note[:200])
                    db.flush()
                    event.movement_id = movement.id
                    event.status = "applied"
                    result["product_ids"].add(product.id)
                except services.InventoryError as exc:
                    event.status = "failed"
                    event.message = f"{exc} Correct the count, then press Retry."[:300]
            db.add(event)
            db.commit()
            result[event.status] += 1
        except IntegrityError:  # the same line arrived twice at the same moment
            db.rollback()
            result["duplicates"] += 1
    return result


def summarize(result: Dict[str, object]) -> str:
    parts = [f"{result['applied']} sale line{'s' if result['applied'] != 1 else ''} recorded"]
    if result["unmatched"]:
        parts.append(f"{result['unmatched']} unmatched")
    if result["failed"]:
        parts.append(f"{result['failed']} need attention")
    if result["duplicates"]:
        parts.append(f"{result['duplicates']} already counted")
    return ", ".join(parts) + "."


def sync_connection(db: Session, conn: PosConnection, http: Optional[HttpClient] = None) -> Dict[str, object]:
    """Pull recent sales from a POS and apply them."""
    adapter = adapter_for(conn, http)
    if not adapter.supports_polling:
        raise PosError(f"{adapter.label} sends sales by webhook or CSV import; there's nothing to pull.")
    since = (_aware(conn.last_synced_at) or utcnow()) - OVERLAP
    try:
        lines, newest = adapter.fetch_sales(since)
    except PosError as exc:
        set_status(conn, False, str(exc))
        db.commit()
        raise
    result = apply_lines(db, conn, lines)
    if newest and newest > (_aware(conn.last_synced_at) or datetime.min.replace(tzinfo=timezone.utc)):
        conn.last_synced_at = newest
    set_status(conn, not result["failed"], "Synced: " + summarize(result))
    db.commit()
    push_products(db, result["product_ids"], exclude_connection_id=conn.id, http=http)
    return result


def retry_events(db: Session, conn: PosConnection) -> Dict[str, object]:
    events = list(db.scalars(select(PosEvent).where(PosEvent.connection_id == conn.id,
                                                    PosEvent.status.in_(["unmatched", "failed"]))
                             .order_by(PosEvent.id)))
    lines = [SaleLine(e.external_id, e.quantity, e.order_id or "", e.external_item_id, e.sku, e.name) for e in events]
    for e in events:
        db.delete(e)
    db.commit()
    result = apply_lines(db, conn, lines)
    db.commit()
    push_products(db, result["product_ids"], exclude_connection_id=None)
    return result


# ---------- catalog ----------

def import_catalog(db: Session, conn: PosConnection, http: Optional[HttpClient] = None) -> Dict[str, int]:
    items = adapter_for(conn, http).list_catalog()
    existing = {l.external_item_id: l for l in db.scalars(select(PosItemLink).where(PosItemLink.connection_id == conn.id))}
    matched = 0
    for item in items:
        link = existing.get(item.external_item_id)
        if link is None:
            link = PosItemLink(connection_id=conn.id, external_item_id=item.external_item_id)
            db.add(link)
            existing[item.external_item_id] = link
        link.external_name = item.name[:200] if item.name else None
        link.external_sku = item.sku[:80] if item.sku else None
        if not link.product_id:
            product = _product_by_sku(db, item.sku)
            if product:
                link.product_id = product.id
        if link.product_id:
            matched += 1
    set_status(conn, True, f"Imported {len(items)} items; {matched} matched to products.")
    db.commit()
    return {"items": len(items), "matched": matched, "unmatched": len(items) - matched}


# ---------- stock out ----------

def push_connection(db: Session, conn: PosConnection, product_ids: Optional[Set[int]] = None,
                    http: Optional[HttpClient] = None) -> int:
    adapter = adapter_for(conn, http)
    if not adapter.supports_push:
        raise PosError(f"{adapter.label} can't receive stock counts.")
    stmt = select(PosItemLink).where(PosItemLink.connection_id == conn.id, PosItemLink.product_id.is_not(None))
    if product_ids is not None:
        stmt = stmt.where(PosItemLink.product_id.in_(product_ids))
    counts: List = []
    for link in db.scalars(stmt):
        product = db.get(Product, link.product_id)
        if product:
            counts.append((link.external_item_id, product.quantity))
    if not counts:
        return 0
    try:
        sent = adapter.push_stock(counts)
    except PosError as exc:
        set_status(conn, False, f"Couldn't update stock: {exc}")
        db.commit()
        raise
    set_status(conn, True, f"Sent stock counts for {sent} item{'s' if sent != 1 else ''}.")
    db.commit()
    return sent


def push_products(db: Session, product_ids: Iterable[int], exclude_connection_id: Optional[int] = None,
                  http: Optional[HttpClient] = None) -> None:
    """Best effort: send new counts for these products to every POS with push turned on."""
    ids = set(product_ids)
    if not ids:
        return
    conns = db.scalars(select(PosConnection).where(PosConnection.enabled.is_(True), PosConnection.push_stock.is_(True)))
    for conn in list(conns):
        if conn.id == exclude_connection_id:
            continue
        try:
            push_connection(db, conn, ids, http)
        except Exception as exc:  # one POS being down must not break the others
            log.warning("Stock push to %s failed: %s", conn.name, exc)


def push_products_background(product_ids: Iterable[int]) -> None:
    """For FastAPI BackgroundTasks: uses its own database session."""
    from app import db as db_module

    with db_module.SessionLocal() as db:
        push_products(db, product_ids)


def sync_all(http: Optional[HttpClient] = None) -> None:
    """Called on a timer by main.py: pull sales from every connection with auto sync on."""
    from app import db as db_module

    with db_module.SessionLocal() as db:
        conns = list(db.scalars(select(PosConnection).where(PosConnection.enabled.is_(True), PosConnection.auto_sync.is_(True))))
        for conn in conns:
            if not get_adapter_class(conn.provider).supports_polling:
                continue
            try:
                sync_connection(db, conn, http)
            except Exception as exc:
                db.rollback()
                log.warning("Auto sync of %s failed: %s", conn.name, exc)
