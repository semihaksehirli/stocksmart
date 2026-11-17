"""Generic connector for any other POS (Toast, Lightspeed, Shopify POS, SumUp, Zettle, …).

Two ways in, no POS-specific code needed:
  1. Webhook: have the POS — or an automation tool such as Zapier, Make or n8n — POST
     JSON to StockSmart's webhook URL:
        {"order_id": "1042", "lines": [{"sku": "COF-001", "quantity": 2},
                                        {"sku": "SNK-002", "quantity": -1, "line_id": "r1"}]}
     Negative quantities are refunds. Sign the raw body with the connection's secret:
        X-StockSmart-Signature: sha256=<hex HMAC-SHA256 of the body>
  2. CSV import: upload a sales export with columns sku, quantity and (recommended) order_id.
"""
import csv
import hashlib
import hmac
import io
from typing import Dict, List

from app.pos.base import FieldSpec, PosAdapter, PosError, SaleLine, WebhookResult, to_int_qty


class GenericAdapter(PosAdapter):
    provider = "generic"
    label = "Other POS"
    description = "Any other POS, through a signed webhook (directly or via Zapier, Make or n8n) or a CSV sales export."
    fields = [
        FieldSpec("webhook_secret", "Webhook secret", "Any long random text. Use the same value when signing requests. Leave blank if you'll only import CSV files.", secret=True, required=False),
    ]
    webhook_help = "POST JSON to this URL with an X-StockSmart-Signature header (sha256=HMAC of the body using your secret). See the README for the format."

    def check(self) -> str:
        return "Ready. Send sales to the webhook URL or import a CSV file."

    def verify_webhook(self, headers: Dict[str, str], body: bytes, url: str) -> bool:
        key = self.setting("webhook_secret")
        given = headers.get("x-stocksmart-signature", "")
        if not key or not given:
            return False
        expected = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, given.strip())

    def handle_webhook(self, payload) -> WebhookResult:
        orders = payload if isinstance(payload, list) else [payload]
        lines: List[SaleLine] = []
        for order in orders:
            if not isinstance(order, dict) or not isinstance(order.get("lines"), list):
                raise PosError('Expected JSON like {"order_id": "...", "lines": [{"sku": "...", "quantity": 1}]}.')
            oid = str(order.get("order_id") or "")
            if not oid:
                raise PosError("Each order needs an order_id so it is only counted once.")
            for i, l in enumerate(order["lines"]):
                sku = str(l.get("sku") or "").strip()
                qty = to_int_qty(l.get("quantity"))
                if sku and qty:
                    lid = str(l.get("line_id") or i)
                    lines.append(SaleLine(f"{oid}:{lid}", qty, oid, l.get("item_id"), sku, l.get("name")))
        return WebhookResult(lines=lines)


def parse_sales_csv(text: str) -> List[SaleLine]:
    """Read a sales export. Needs sku and quantity columns; order_id (or similar) avoids double counting."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise PosError("The file is empty.")
    cols = {c.strip().lower().replace(" ", "_"): c for c in reader.fieldnames if c}

    def pick(*names):
        return next((cols[n] for n in names if n in cols), None)

    sku_c = pick("sku", "item_sku", "product_sku", "variant_sku", "code", "upc", "barcode")
    qty_c = pick("quantity", "qty", "units", "units_sold", "count", "item_quantity")
    oid_c = pick("order_id", "order", "order_number", "transaction_id", "receipt", "receipt_number", "ticket")
    line_c = pick("line_id", "line_item_id", "item_id")
    name_c = pick("name", "item", "item_name", "product", "product_name", "description")
    if not sku_c or not qty_c:
        raise PosError(f"The file needs a SKU column and a quantity column. Found: {', '.join(reader.fieldnames)}.")

    lines: List[SaleLine] = []
    for n, row in enumerate(reader, start=2):
        sku = (row.get(sku_c) or "").strip()
        qty = to_int_qty(row.get(qty_c))
        if not sku or not qty:
            continue
        oid = (row.get(oid_c) or "").strip() if oid_c else ""
        if oid:
            lid = (row.get(line_c) or "").strip() if line_c else ""
            ext = f"csv:{oid}:{lid or sku}"
        else:  # no order id: fall back to the row's content + position
            ext = "csv:" + hashlib.sha1(f"{n}|{sku}|{qty}|{text[:200]}".encode()).hexdigest()[:20]
        lines.append(SaleLine(ext, qty, oid or f"row {n}", None, sku, (row.get(name_c) or "").strip() if name_c else None))
    return lines
