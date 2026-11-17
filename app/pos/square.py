"""Square connector (https://developer.squareup.com).

Setup in the Square Developer Dashboard:
  * Create an application and copy its access token (sandbox or production).
  * Note the Location ID of the store to sync.
  * Optional, for instant updates: add a webhook subscription for `order.updated`
    pointing at StockSmart's webhook URL and copy its signature key.
"""
import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from app.pos.base import CatalogItem, FieldSpec, PosAdapter, PosError, SaleLine, WebhookResult, to_int_qty

SQUARE_VERSION = "2025-01-23"
BASES = {"production": "https://connect.squareup.com", "sandbox": "https://connect.squareupsandbox.com"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class SquareAdapter(PosAdapter):
    provider = "square"
    label = "Square"
    description = "Square Point of Sale, Square for Retail and Square Online orders."
    supports_polling = supports_push = supports_catalog = True
    fields = [
        FieldSpec("environment", "Environment", choices=[("production", "Production"), ("sandbox", "Sandbox (testing)")], default="production"),
        FieldSpec("access_token", "Access token", "From your app in the Square Developer Dashboard. Needs ORDERS_READ, ITEMS_READ, INVENTORY_WRITE.", secret=True),
        FieldSpec("location_id", "Location ID", "The store location to sync. Find it under Locations in the dashboard."),
        FieldSpec("webhook_secret", "Webhook signature key", "Optional. Only needed for instant updates through webhooks.", secret=True, required=False),
    ]
    webhook_help = "In the Square Developer Dashboard, add a webhook subscription for the order.updated event with this URL, then paste its signature key here."

    def __init__(self, settings, http=None):
        super().__init__(settings, http)
        self._sku_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    @property
    def base(self) -> str:
        return BASES.get(self.setting("environment"), BASES["production"])

    def _headers(self) -> dict:
        if not self.setting("access_token"):
            raise PosError("Add a Square access token first.")
        return {"Authorization": f"Bearer {self.setting('access_token')}", "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json"}

    # ---------- operations ----------
    def check(self) -> str:
        data = self._call("GET", f"{self.base}/v2/locations/{self.setting('location_id')}", self._headers())
        loc = (data or {}).get("location") or {}
        return f"Connected to Square location “{loc.get('name', self.setting('location_id'))}”."

    def fetch_sales(self, since: datetime) -> Tuple[List[SaleLine], Optional[datetime]]:
        body = {
            "location_ids": [self.setting("location_id")],
            "limit": 100,
            "query": {
                "filter": {
                    "state_filter": {"states": ["COMPLETED"]},
                    "date_time_filter": {"closed_at": {"start_at": _iso(since)}},
                },
                "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"},
            },
        }
        lines: List[SaleLine] = []
        newest = since
        cursor = None
        for _ in range(50):  # safety cap: 5,000 orders per sync
            if cursor:
                body["cursor"] = cursor
            data = self._call("POST", f"{self.base}/v2/orders/search", self._headers(), json_body=body) or {}
            for order in data.get("orders", []):
                lines.extend(self.order_lines(order))
                closed = _parse_time(order.get("closed_at"))
                if closed and closed > newest:
                    newest = closed
            cursor = data.get("cursor")
            if not cursor:
                break
        self._fill_skus(lines)
        return lines, newest

    def order_lines(self, order: dict) -> List[SaleLine]:
        """Turn one Square order into sale lines (sales positive, returns negative)."""
        if order.get("state") != "COMPLETED":
            return []
        oid = order.get("id", "")
        out = []
        for li in order.get("line_items") or []:
            qty = to_int_qty(li.get("quantity"))
            if qty and li.get("catalog_object_id"):  # skip custom amounts that aren't catalog items
                name = " – ".join(x for x in [li.get("name"), li.get("variation_name")] if x and x != "Regular")
                out.append(SaleLine(f"{oid}:{li.get('uid')}", qty, oid, li["catalog_object_id"], None, name or None))
        for ret in order.get("returns") or []:
            for li in ret.get("return_line_items") or []:
                qty = to_int_qty(li.get("quantity"))
                if qty and li.get("catalog_object_id"):
                    out.append(SaleLine(f"{oid}:return:{li.get('uid')}", -qty, oid, li["catalog_object_id"], None, li.get("name")))
        return out

    def _fill_skus(self, lines: List[SaleLine]) -> None:
        """Square line items carry the variation id but not the SKU; look SKUs up in the catalog."""
        missing = sorted({l.external_item_id for l in lines if l.external_item_id and l.external_item_id not in self._sku_cache})
        for i in range(0, len(missing), 1000):
            data = self._call("POST", f"{self.base}/v2/catalog/batch-retrieve", self._headers(),
                              json_body={"object_ids": missing[i:i + 1000]}) or {}
            for obj in data.get("objects") or []:
                v = obj.get("item_variation_data") or {}
                self._sku_cache[obj["id"]] = (v.get("sku"), v.get("name"))
        for l in lines:
            sku, _ = self._sku_cache.get(l.external_item_id or "", (None, None))
            l.sku = l.sku or sku

    def list_catalog(self) -> List[CatalogItem]:
        items: List[CatalogItem] = []
        cursor = None
        for _ in range(100):
            params = {"types": "ITEM"}
            if cursor:
                params["cursor"] = cursor
            data = self._call("GET", f"{self.base}/v2/catalog/list", self._headers(), params=params) or {}
            for obj in data.get("objects") or []:
                item = obj.get("item_data") or {}
                for var in item.get("variations") or []:
                    v = var.get("item_variation_data") or {}
                    vname = v.get("name")
                    name = item.get("name", "") + (f" – {vname}" if vname and vname != "Regular" else "")
                    items.append(CatalogItem(var["id"], name, v.get("sku") or None))
            cursor = data.get("cursor")
            if not cursor:
                break
        return items

    def push_stock(self, counts: Iterable[Tuple[str, int]]) -> int:
        now = _iso(datetime.now(timezone.utc))
        changes = [{
            "type": "PHYSICAL_COUNT",
            "physical_count": {"catalog_object_id": item_id, "state": "IN_STOCK", "location_id": self.setting("location_id"),
                               "quantity": str(max(0, int(qty))), "occurred_at": now},
        } for item_id, qty in counts]
        for i in range(0, len(changes), 100):
            self._call("POST", f"{self.base}/v2/inventory/changes/batch-create", self._headers(),
                       json_body={"idempotency_key": str(uuid.uuid4()), "changes": changes[i:i + 100]})
        return len(changes)

    # ---------- webhooks ----------
    def verify_webhook(self, headers: Dict[str, str], body: bytes, url: str) -> bool:
        key = self.setting("webhook_secret")
        given = headers.get("x-square-hmacsha256-signature", "")
        if not key or not given:
            return False
        digest = hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), given)

    def handle_webhook(self, payload) -> WebhookResult:
        if not isinstance(payload, dict) or payload.get("type") not in ("order.updated", "order.created"):
            return WebhookResult(message="Ignored a Square event that isn't about orders.")
        obj = ((payload.get("data") or {}).get("object") or {})
        info = obj.get("order_updated") or obj.get("order_created") or {}
        if info.get("state") != "COMPLETED":
            return WebhookResult()
        if info.get("location_id") and info["location_id"] != self.setting("location_id"):
            return WebhookResult()
        data = self._call("GET", f"{self.base}/v2/orders/{info.get('order_id')}", self._headers()) or {}
        lines = self.order_lines(data.get("order") or {})
        self._fill_skus(lines)
        return WebhookResult(lines=lines)

