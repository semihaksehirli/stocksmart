"""REST endpoints for POS integrations (mounted under /api/pos).

Owner-only: managing connections, matching items, syncing, pushing stock.
Public:     POST /pos/webhooks/{id} — called by the POS, checked by its signature.
"""
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import owner_only
from app.models import PosConnection, PosEvent, PosItemLink, Product, User
from app.pos import PROVIDERS, PosError, get_adapter_class
from app.pos import sync
from app.pos.clover import verification_code
from app.pos.generic import parse_sales_csv

router = APIRouter(prefix="/pos", tags=["POS integrations"])


# ---------- schemas ----------

class ConnectionIn(BaseModel):
    provider: str
    name: str = Field(min_length=1, max_length=120)
    settings: Dict[str, Any] = {}
    auto_sync: bool = True
    push_stock: bool = False


class ConnectionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    settings: Optional[Dict[str, Any]] = None  # blank secret fields keep their saved value
    enabled: Optional[bool] = None
    auto_sync: Optional[bool] = None
    push_stock: Optional[bool] = None


class LinkUpdate(BaseModel):
    product_id: Optional[int] = None


class CsvIn(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)


# ---------- helpers ----------

def _public_base(request: Request) -> str:
    configured = os.getenv("STOCKSMART_PUBLIC_URL", "").rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def webhook_url(request: Request, conn_id: int) -> str:
    # Works whether the API is mounted at /api (main.py) or served on its own.
    prefix = request.url.path.split("/pos/")[0]
    return f"{_public_base(request)}{prefix}/pos/webhooks/{conn_id}"


def _mask(value: str) -> str:
    return "" if not value else "•••• " + value[-4:] if len(value) > 8 else "••••"


def _get(db: Session, conn_id: int) -> PosConnection:
    conn = db.get(PosConnection, conn_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found.")
    return conn


def _check_settings(provider: str, settings: Dict[str, Any]) -> Dict[str, str]:
    cls = get_adapter_class(provider)
    clean = {}
    for spec in cls.fields:
        value = str(settings.get(spec.key) or "").strip() or spec.default
        if spec.choices and value and value not in [c[0] for c in spec.choices]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{spec.label}: choose one of the listed options.")
        if spec.required and not value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{spec.label} is required.")
        clean[spec.key] = value
    return clean


def serialize(db: Session, conn: PosConnection, request: Request) -> dict:
    cls = get_adapter_class(conn.provider)
    settings = sync.get_settings(conn)
    shown = {f.key: (_mask(settings.get(f.key, "")) if f.secret else settings.get(f.key, "")) for f in cls.fields}
    counts = dict(db.execute(select(PosEvent.status, func.count()).where(PosEvent.connection_id == conn.id)
                             .group_by(PosEvent.status)).all())
    unmatched_items = db.scalar(select(func.count()).select_from(PosItemLink).where(
        PosItemLink.connection_id == conn.id, PosItemLink.product_id.is_(None))) or 0
    linked_items = db.scalar(select(func.count()).select_from(PosItemLink).where(
        PosItemLink.connection_id == conn.id, PosItemLink.product_id.is_not(None))) or 0
    return {
        "id": conn.id, "provider": conn.provider, "provider_label": cls.label, "name": conn.name,
        "enabled": conn.enabled, "auto_sync": conn.auto_sync, "push_stock": conn.push_stock,
        "last_synced_at": conn.last_synced_at, "last_status": conn.last_status, "last_status_ok": conn.last_status_ok,
        "settings": shown, "webhook_url": webhook_url(request, conn.id),
        "capabilities": {"polling": cls.supports_polling, "push": cls.supports_push,
                         "catalog": cls.supports_catalog, "webhooks": cls.supports_webhooks},
        "events": {"applied": counts.get("applied", 0), "unmatched": counts.get("unmatched", 0), "failed": counts.get("failed", 0)},
        "items": {"linked": linked_items, "unmatched": unmatched_items},
    }


def _run(fn, *args):
    try:
        return fn(*args)
    except PosError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ---------- providers & connections ----------

@router.get("/providers")
def providers(_: User = Depends(owner_only)):
    return [{
        "provider": cls.provider, "label": cls.label, "description": cls.description, "webhook_help": cls.webhook_help,
        "capabilities": {"polling": cls.supports_polling, "push": cls.supports_push, "catalog": cls.supports_catalog},
        "fields": [{"key": f.key, "label": f.label, "help": f.help, "secret": f.secret, "required": f.required,
                    "choices": [{"value": v, "label": l} for v, l in (f.choices or [])], "default": f.default}
                   for f in cls.fields],
    } for cls in PROVIDERS.values()]


@router.get("/connections")
def list_connections(request: Request, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conns = db.scalars(select(PosConnection).order_by(PosConnection.created_at))
    return [serialize(db, c, request) for c in conns]


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionIn, request: Request, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    if body.provider not in PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown POS '{body.provider}'.")
    settings = _check_settings(body.provider, body.settings)
    cls = PROVIDERS[body.provider]
    conn = PosConnection(provider=body.provider, name=body.name.strip(), settings_json=json.dumps(settings),
                         auto_sync=body.auto_sync and cls.supports_polling, push_stock=body.push_stock and cls.supports_push,
                         last_synced_at=sync.utcnow())  # only sales from now on are imported
    sync.set_status(conn, True, "Connection added.")
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return serialize(db, conn, request)


@router.patch("/connections/{conn_id}")
def update_connection(conn_id: int, body: ConnectionUpdate, request: Request, db: Session = Depends(get_db),
                      _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    cls = get_adapter_class(conn.provider)
    if body.settings is not None:
        current = sync.get_settings(conn)
        merged = dict(current)
        for spec in cls.fields:
            if spec.key in body.settings:
                value = str(body.settings[spec.key] or "").strip()
                if spec.secret and (not value or value.startswith("••••")):
                    continue  # keep the saved secret
                merged[spec.key] = value
        conn.settings_json = json.dumps(_check_settings(conn.provider, merged))
    if body.name is not None:
        conn.name = body.name.strip()
    if body.enabled is not None:
        conn.enabled = body.enabled
    if body.auto_sync is not None:
        conn.auto_sync = body.auto_sync and cls.supports_polling
    if body.push_stock is not None:
        conn.push_stock = body.push_stock and cls.supports_push
    db.commit()
    return serialize(db, conn, request)


@router.delete("/connections/{conn_id}", status_code=204)
def delete_connection(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    db.delete(_get(db, conn_id))
    db.commit()


@router.post("/connections/{conn_id}/test")
def test_connection(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    try:
        message = sync.adapter_for(conn).check()
        sync.set_status(conn, True, message)
    except PosError as exc:
        sync.set_status(conn, False, str(exc))
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return {"message": message}


@router.post("/connections/{conn_id}/sync")
def sync_now(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    result = _run(sync.sync_connection, db, conn)
    return {"message": sync.summarize(result), **{k: v for k, v in result.items() if k != "product_ids"}}


@router.post("/connections/{conn_id}/catalog")
def import_items(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    result = _run(sync.import_catalog, db, conn)
    return {"message": f"Found {result['items']} items; {result['matched']} matched to products by SKU.", **result}


@router.post("/connections/{conn_id}/push")
def push_now(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    sent = _run(sync.push_connection, db, conn)
    return {"message": f"Sent stock counts for {sent} item{'s' if sent != 1 else ''}." if sent
            else "No matched items to send. Import and match items first."}


@router.post("/connections/{conn_id}/retry")
def retry(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    result = sync.retry_events(db, _get(db, conn_id))
    return {"message": "Retried: " + sync.summarize(result)}


@router.post("/connections/{conn_id}/import-csv")
def import_csv(conn_id: int, body: CsvIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    conn = _get(db, conn_id)
    lines = _run(parse_sales_csv, body.csv)
    if not lines:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No rows with a SKU and a quantity were found.")
    result = sync.apply_lines(db, conn, lines)
    sync.set_status(conn, not result["failed"], "CSV import: " + sync.summarize(result))
    db.commit()
    sync.push_products(db, result["product_ids"], exclude_connection_id=conn.id)
    return {"message": sync.summarize(result), **{k: v for k, v in result.items() if k != "product_ids"}}


# ---------- item matching & activity ----------

@router.get("/connections/{conn_id}/items")
def list_items(conn_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    _get(db, conn_id)
    links = db.scalars(select(PosItemLink).where(PosItemLink.connection_id == conn_id)
                       .order_by(PosItemLink.product_id.is_not(None), PosItemLink.external_name))
    return [{"id": l.id, "external_item_id": l.external_item_id, "name": l.external_name, "sku": l.external_sku,
             "product_id": l.product_id} for l in links]


@router.patch("/items/{link_id}")
def match_item(link_id: int, body: LinkUpdate, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    link = db.get(PosItemLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found.")
    if body.product_id is not None and db.get(Product, body.product_id) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Product not found.")
    link.product_id = body.product_id
    db.commit()
    return {"id": link.id, "product_id": link.product_id}


@router.get("/events")
def list_events(connection_id: Optional[int] = None, status_filter: Optional[str] = None, limit: int = 200,
                db: Session = Depends(get_db), _: User = Depends(owner_only)):
    stmt = select(PosEvent).order_by(PosEvent.id.desc()).limit(min(limit, 1000))
    if connection_id is not None:
        stmt = stmt.where(PosEvent.connection_id == connection_id)
    if status_filter:
        stmt = stmt.where(PosEvent.status == status_filter)
    return [{"id": e.id, "connection_id": e.connection_id, "order_id": e.order_id, "name": e.name, "sku": e.sku,
             "quantity": e.quantity, "status": e.status, "message": e.message, "product_id": e.product_id,
             "created_at": e.created_at} for e in db.scalars(stmt)]


# ---------- webhooks (called by the POS) ----------

@router.post("/webhooks/{conn_id}", include_in_schema=False)
async def receive_webhook(conn_id: int, request: Request, background: BackgroundTasks):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    url = webhook_url(request, conn_id)
    # Database work and calls back to the POS are blocking, so run them off the event loop.
    reply, product_ids = await run_in_threadpool(_process_webhook, conn_id, body, headers, url)
    if product_ids:
        background.add_task(_push_after_webhook, product_ids, conn_id)
    return reply


def _process_webhook(conn_id: int, body: bytes, headers: Dict[str, str], url: str):
    from app import db as db_module

    with db_module.SessionLocal() as db:
        conn = db.get(PosConnection, conn_id)
        if conn is None or not conn.enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown webhook.")
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be JSON.") from None

        # Clover's one-time setup request: show the code so the owner can paste it into Clover.
        code = verification_code(payload) if conn.provider == "clover" else None
        if code:
            sync.set_status(conn, True, f"Clover verification code: {code}")
            db.commit()
            return {"ok": True}, []

        adapter = sync.adapter_for(conn)
        if not adapter.verify_webhook(headers, body, url):
            hint = " and STOCKSMART_PUBLIC_URL" if conn.provider == "square" else ""
            sync.set_status(conn, False, f"Rejected a webhook with a missing or wrong signature. Check the webhook secret{hint}.")
            db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature.")

        try:
            incoming = adapter.handle_webhook(payload)
        except PosError as exc:
            sync.set_status(conn, False, f"Webhook: {exc}")
            db.commit()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        result = sync.apply_lines(db, conn, incoming.lines)
        if incoming.lines or incoming.message:
            sync.set_status(conn, not result["failed"], "Webhook: " + (incoming.message or sync.summarize(result)))
            db.commit()
        return incoming.response or {"ok": True}, list(result["product_ids"])


def _push_after_webhook(product_ids: List[int], source_id: int) -> None:
    from app import db as db_module

    with db_module.SessionLocal() as db:
        sync.push_products(db, product_ids, exclude_connection_id=source_id)
