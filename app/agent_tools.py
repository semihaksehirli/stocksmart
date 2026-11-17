"""Inventory assistant: tools plus an optional LLM that decides which tool to call.

Each tool wraps a function in `services`, so the assistant can only do what the
app already allows.

Modes:
  * Built-in (default, no setup): simple keyword matching routes the question
    to a tool. See `answer_offline`.
  * LLM mode: set OPENAI_API_KEY to use OpenAI, or OPENAI_BASE_URL to point at
    any OpenAI-compatible server (e.g. a free local model with Ollama:
    OPENAI_BASE_URL=http://localhost:11434/v1, STOCKSMART_MODEL=llama3.1).
"""
import json
import os

from app import services
from app.db import SessionLocal
from app.models import MovementType


# ---------- tools ----------

def check_stock(product: str) -> str:
    """Return the current quantity, price and reorder level of one product."""
    with SessionLocal() as db:
        try:
            p = services.find_product(db, product)
        except services.InventoryError as exc:
            return str(exc)
        flag = " (LOW STOCK)" if p.is_low_stock else ""
        return f"{p.name} [{p.sku}]: {p.quantity} on hand, reorder at {p.reorder_level}, price ${float(p.price):.2f}{flag}"


def list_low_stock() -> str:
    """List every product at or below its reorder level, with its supplier."""
    with SessionLocal() as db:
        items = services.low_stock(db)
        if not items:
            return "No products are low on stock."
        return "\n".join(
            f"- {p.name} [{p.sku}]: {p.quantity} left (reorder at {p.reorder_level}); supplier: "
            f"{p.supplier.name if p.supplier else 'none'}"
            for p in items
        )


def record_sale(product: str, quantity: int) -> str:
    """Record a sale, which lowers the product's stock."""
    with SessionLocal() as db:
        try:
            p = services.find_product(db, product)
            m = services.record_movement(db, p.id, MovementType.SALE, int(quantity), "Recorded by assistant")
        except services.InventoryError as exc:
            return str(exc)
        return f"Recorded sale of {quantity} x {p.name}. {m.quantity_after} left."


def record_delivery(product: str, quantity: int) -> str:
    """Record a delivery from a supplier, which raises the product's stock."""
    with SessionLocal() as db:
        try:
            p = services.find_product(db, product)
            m = services.record_movement(db, p.id, MovementType.DELIVERY, int(quantity), "Recorded by assistant")
        except services.InventoryError as exc:
            return str(exc)
        return f"Recorded delivery of {quantity} x {p.name}. Now {m.quantity_after} on hand."


def inventory_report() -> str:
    """Summarize the store: inventory value, units on hand, sales and top sellers."""
    with SessionLocal() as db:
        s = services.dashboard_summary(db)
        top = services.top_sellers(db, 3)
    return "\n".join([
        f"Products: {s['product_count']}, units on hand: {s['units_on_hand']}",
        f"Inventory value: ${s['inventory_value_cost']:,.2f} at cost, ${s['inventory_value_retail']:,.2f} at retail",
        f"Low-stock items: {s['low_stock_count']}",
        f"Units sold: {s['units_sold']}, sales revenue: ${s['sales_revenue']:,.2f}",
        "Top sellers: " + ", ".join(f"{p.name} ({u})" for p, u in top),
    ])


TOOLS = {f.__name__: f for f in [check_stock, list_low_stock, record_sale, record_delivery, inventory_report]}

_PRODUCT = {"type": "string", "description": "Product SKU (e.g. COF-001) or exact name"}
_QTY = {"type": "integer", "minimum": 1, "description": "Number of units"}
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": fn.__doc__, "parameters": params}}
    for name, fn, params in [
        ("check_stock", check_stock, {"type": "object", "properties": {"product": _PRODUCT}, "required": ["product"]}),
        ("list_low_stock", list_low_stock, {"type": "object", "properties": {}}),
        ("record_sale", record_sale, {"type": "object", "properties": {"product": _PRODUCT, "quantity": _QTY}, "required": ["product", "quantity"]}),
        ("record_delivery", record_delivery, {"type": "object", "properties": {"product": _PRODUCT, "quantity": _QTY}, "required": ["product", "quantity"]}),
        ("inventory_report", inventory_report, {"type": "object", "properties": {}}),
    ]
]


# ---------- built-in mode ----------

def answer_offline(message: str) -> str:
    text = message.lower()
    words = message.replace(",", " ").replace("?", " ").split()
    sku = next((w.upper() for w in words if "-" in w and w[:3].isalpha()), None)
    qty = next((int(w) for w in words if w.isdigit()), None)
    if "low" in text or "reorder" in text or "running out" in text:
        return list_low_stock()
    if ("sold" in text or "sale" in text) and sku and qty:
        return record_sale(sku, qty)
    if ("deliver" in text or "received" in text or "arrived" in text) and sku and qty:
        return record_delivery(sku, qty)
    if sku:
        return check_stock(sku)
    if "report" in text or "summary" in text or "value" in text or "top" in text:
        return inventory_report()
    return ("I can help with: 'what's running low?', 'check COF-002', "
            "'sold 3 SNK-001', 'received 12 DAI-002', or 'give me a report'.")


# ---------- LLM mode ----------

SYSTEM_PROMPT = (
    "You are StockSmart's inventory assistant for a small shop. Use the tools to answer; "
    "never invent stock numbers. Keep answers short. Before recording a sale or delivery, "
    "make sure the user clearly asked for it."
)


WRITE_TOOLS = {"record_sale", "record_delivery"}


def execute_tool(name: str, args: dict):
    """Run one assistant tool. Returns (result_text, whether_stock_changed)."""
    fn = TOOLS.get(name)
    if fn is None:
        return f"Unknown tool '{name}'.", False
    result = fn(**args)
    return result, name in WRITE_TOOLS and result.startswith("Recorded")


def env_provider():
    """The provider set by environment variables (OPENAI_API_KEY / OPENAI_BASE_URL), if any."""
    from app.ai import ProviderConfig

    if not (os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL")):
        return None
    return ProviderConfig(0, "custom", "Server setting (OPENAI_*)", os.getenv("OPENAI_BASE_URL") or None,
                          os.getenv("OPENAI_API_KEY", ""), os.getenv("STOCKSMART_MODEL", "gpt-4o-mini"))


def provider_chain():
    """Providers to try, in order: env-var provider, then enabled ones from settings not resting."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import db as db_module
    from app.ai import ProviderConfig
    from app.models import AiProvider

    chain = []
    env = env_provider()
    if env:
        chain.append(env)
    now = datetime.now(timezone.utc)
    with db_module.SessionLocal() as db:
        rows = db.scalars(select(AiProvider).where(AiProvider.enabled.is_(True)).order_by(AiProvider.priority, AiProvider.id))
        for p in rows:
            until = p.cooldown_until
            if until is not None and until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until is not None and until > now:
                continue
            chain.append(ProviderConfig(p.id, p.preset, p.name, p.base_url, p.api_key or "", p.model))
    return chain


def record_attempts(attempts) -> None:
    """Save how each provider did, and rest the ones that hit a limit."""
    from datetime import datetime, timedelta, timezone

    from app import db as db_module
    from app.models import AiProvider

    now = datetime.now(timezone.utc)
    with db_module.SessionLocal() as db:
        for a in attempts:
            p = db.get(AiProvider, a.provider_id) if a.provider_id else None
            if p is None:
                continue
            p.last_used_at = now
            p.last_ok = a.outcome == "ok"
            p.last_status = "Answered." if a.outcome == "ok" else a.message[:300]
            p.cooldown_until = now + timedelta(seconds=a.cooldown_seconds) if a.cooldown_seconds else None
        db.commit()


def llm_enabled() -> bool:
    try:
        return bool(provider_chain())
    except Exception:
        return bool(env_provider())


def answer_full(message: str, history=None):
    """Answer with the first AI provider that works; fall back to built-in mode. Returns a ChainResult."""
    from app.ai import ChainResult, run_chain

    try:
        chain = provider_chain()
    except Exception:  # e.g. settings table missing: still answer
        chain = [p for p in [env_provider()] if p]
    if not chain:
        return ChainResult(answer_offline(message))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-8:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:2000]})
    messages.append({"role": "user", "content": message})
    result = run_chain(chain, messages, TOOL_SCHEMAS, execute_tool, lambda: answer_offline(message))
    try:
        record_attempts(result.attempts)
    except Exception:
        pass
    return result


def answer(message: str) -> str:
    return answer_full(message).reply
