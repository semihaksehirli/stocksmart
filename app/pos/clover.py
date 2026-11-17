"""Clover connector (https://docs.clover.com).

Setup:
  * Merchant ID: shown in the Clover web dashboard URL and under Account & Setup.
  * API token: Clover web dashboard → Account & Setup → API Tokens. Give it read access to
    Orders and Inventory, plus write access to Inventory if you want StockSmart to update stock.
  * Optional, for instant updates: a Clover app with webhooks for Orders. Clover first posts a
    verification code to StockSmart; it appears on the connection so you can paste it back into Clover.
"""
import hmac
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from app.pos.base import CatalogItem, FieldSpec, PosAdapter, PosError, SaleLine, WebhookResult

BASES = {
    "us": "https://api.clover.com",
    "eu": "https://api.eu.clover.com",
    "la": "https://api.la.clover.com",
    "sandbox": "https://apisandbox.dev.clover.com",
}
# Orders that count as sold. Refunded orders were sold first, then refunded line by line.
SOLD_STATES = {"PAID", "PARTIALLY_REFUNDED", "REFUNDED"}


class CloverAdapter(PosAdapter):
    provider = "clover"
    label = "Clover"
    description = "Clover Station, Mini, Flex and Clover Online Ordering."
    supports_polling = supports_push = supports_catalog = True
    fields = [
        FieldSpec("region", "Region", choices=[("us", "United States & Canada"), ("eu", "Europe"), ("la", "Latin America"), ("sandbox", "Sandbox (testing)")], default="us"),
        FieldSpec("merchant_id", "Merchant ID", "13 characters, shown in your Clover dashboard URL."),
        FieldSpec("access_token", "API token", "Clover dashboard → Account & Setup → API Tokens. Allow Orders read and Inventory read/write.", secret=True),
        FieldSpec("webhook_secret", "Webhook auth code", "Optional. The Clover Auth Code from your app's webhook settings.", secret=True, required=False),
    ]
    webhook_help = "In your Clover app's Webhooks settings, use this URL and subscribe to Orders. Clover sends a verification code here first; it will appear on this connection for you to copy back."

    def __init__(self, settings, http=None):
        super().__init__(settings, http)
        self._item_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    @property
    def base(self) -> str:
        mid = self.setting("merchant_id")
        if not mid:
            raise PosError("Add your Clover merchant ID first.")
        return f"{BASES.get(self.setting('region'), BASES['us'])}/v3/merchants/{mid}"

    def _headers(self) -> dict:
        if not self.setting("access_token"):
            raise PosError("Add a Clover API token first.")
        return {"Authorization": f"Bearer {self.setting('access_token')}", "Content-Type": "application/json"}

    # ---------- operations ----------
    def check(self) -> str:
        data = self._call("GET", self.base, self._headers()) or {}
        return f"Connected to Clover merchant “{data.get('name', self.setting('merchant_id'))}”."

    def fetch_sales(self, since: datetime) -> Tuple[List[SaleLine], Optional[datetime]]:
        since_ms = int(since.timestamp() * 1000)
        lines: List[SaleLine] = []
        newest_ms = since_ms
        offset = 0
        for _ in range(50):  # safety cap: 5,000 orders per sync
            params = [("filter", f"modifiedTime>={since_ms}"), ("expand", "lineItems"),
                      ("limit", "100"), ("offset", str(offset))]
            data = self._call("GET", f"{self.base}/orders", self._headers(), params=params) or {}
            orders = data.get("elements") or []
            for order in orders:
                lines.extend(self.order_lines(order))
                newest_ms = max(newest_ms, int(order.get("modifiedTime") or 0))
            if len(orders) < 100:
                break
            offset += 100
        self._fill_skus(lines)
        return lines, datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc)

    def order_lines(self, order: dict) -> List[SaleLine]:
        """Clover adds one line item per unit sold; a refunded line item has refunded=true."""
        if order.get("paymentState") not in SOLD_STATES:
            return []
        oid = order.get("id", "")
        out = []
        items = (order.get("lineItems") or {}).get("elements") or []
        for li in items:
            item_id = (li.get("item") or {}).get("id")
            if not item_id:  # custom/manual amount, not an inventory item
                continue
            out.append(SaleLine(f"{oid}:{li.get('id')}", 1, oid, item_id, None, li.get("name")))
            if li.get("refunded"):
                out.append(SaleLine(f"{oid}:{li.get('id')}:refund", -1, oid, item_id, None, li.get("name")))
        return out

    def _fill_skus(self, lines: List[SaleLine]) -> None:
        for l in lines:
            iid = l.external_item_id
            if iid and iid not in self._item_cache:
                try:
                    item = self._call("GET", f"{self.base}/items/{iid}", self._headers()) or {}
                    self._item_cache[iid] = (item.get("sku") or item.get("code") or None, item.get("name"))
                except PosError:
                    self._item_cache[iid] = (None, None)
            sku, _ = self._item_cache.get(iid or "", (None, None))
            l.sku = l.sku or sku

    def list_catalog(self) -> List[CatalogItem]:
        items: List[CatalogItem] = []
        offset = 0
        for _ in range(100):
            data = self._call("GET", f"{self.base}/items", self._headers(), params={"limit": "1000", "offset": str(offset)}) or {}
            batch = data.get("elements") or []
            for it in batch:
                items.append(CatalogItem(it["id"], it.get("name", ""), it.get("sku") or it.get("code") or None))
            if len(batch) < 1000:
                break
            offset += 1000
        return items

    def push_stock(self, counts: Iterable[Tuple[str, int]]) -> int:
        n = 0
        for item_id, qty in counts:
            self._call("POST", f"{self.base}/item_stocks/{item_id}", self._headers(), json_body={"quantity": max(0, int(qty))})
            n += 1
        return n

    # ---------- webhooks ----------
    def verify_webhook(self, headers: Dict[str, str], body: bytes, url: str) -> bool:
        key = self.setting("webhook_secret")
        given = headers.get("x-clover-auth", "")
        return bool(key and given) and hmac.compare_digest(key, given)

    def handle_webhook(self, payload) -> WebhookResult:
        if not isinstance(payload, dict):
            return WebhookResult()
        mid = self.setting("merchant_id")
        lines: List[SaleLine] = []
        for update in (payload.get("merchants") or {}).get(mid, []):
            obj = update.get("objectId", "")
            if not obj.startswith("O:") or update.get("type") == "DELETE":
                continue
            order = self._call("GET", f"{self.base}/orders/{obj[2:]}", self._headers(), params={"expand": "lineItems"}) or {}
            lines.extend(self.order_lines(order))
        self._fill_skus(lines)
        return WebhookResult(lines=lines)


def verification_code(payload) -> Optional[str]:
    """Clover's one-time verification request carries only {"verificationCode": "..."}."""
    if isinstance(payload, dict) and set(payload) == {"verificationCode"}:
        return str(payload["verificationCode"])
    return None
