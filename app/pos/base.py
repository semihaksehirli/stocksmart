"""Shared building blocks for POS connectors.

A connector ("adapter") knows how to talk to one POS system. It never touches the
database: it turns the POS's data into plain `SaleLine` / `CatalogItem` objects and
`app.pos.sync` decides what to do with them. Adding a new POS means writing one
adapter class and registering it in `app/pos/__init__.py`.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


class PosError(Exception):
    """A problem talking to a POS (bad credentials, network, unexpected reply)."""


@dataclass
class SaleLine:
    """One line of a POS sale or refund, normalised across providers."""

    external_id: str            # unique and stable per line, used to avoid double counting
    quantity: int               # units sold; negative for a refund/return
    order_id: str
    external_item_id: Optional[str] = None   # the POS's id for the item/variation
    sku: Optional[str] = None
    name: Optional[str] = None


@dataclass
class CatalogItem:
    external_item_id: str
    name: str
    sku: Optional[str] = None


@dataclass
class WebhookResult:
    lines: List[SaleLine] = field(default_factory=list)
    message: str = ""           # shown in the connection's status (e.g. a verification code)
    response: Optional[dict] = None  # JSON to send back to the POS, if it expects something


@dataclass
class FieldSpec:
    """Describes one setting the owner fills in when adding a connection."""

    key: str
    label: str
    help: str = ""
    secret: bool = False
    required: bool = True
    choices: Optional[List[Tuple[str, str]]] = None  # (value, label)
    default: str = ""


class HttpClient:
    """Tiny wrapper around httpx so adapters can be tested with a fake client."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def request(self, method: str, url: str, headers: Optional[dict] = None,
                params: Any = None, json_body: Any = None) -> Tuple[int, Any]:
        import httpx

        try:
            r = httpx.request(method, url, headers=headers, params=params, json=json_body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise PosError(f"Could not reach {url.split('/')[2]}: {exc}") from exc
        try:
            data = r.json() if r.content else None
        except ValueError:
            data = r.text
        return r.status_code, data


class PosAdapter:
    """Base class. Subclasses fill in the class attributes and override what they support."""

    provider = ""
    label = ""
    description = ""
    fields: List[FieldSpec] = []
    supports_polling = False      # can pull recent sales on demand / on a timer
    supports_push = False         # can set stock counts in the POS
    supports_catalog = False      # can list the POS's items to match them to products
    supports_webhooks = True
    webhook_help = ""

    def __init__(self, settings: Dict[str, str], http: Optional[HttpClient] = None):
        self.settings = {k: (v or "").strip() if isinstance(v, str) else v for k, v in settings.items()}
        self.http = http or HttpClient()

    # --- helpers ---
    def setting(self, key: str) -> str:
        value = self.settings.get(key) or ""
        if not value:
            spec = next((f for f in self.fields if f.key == key), None)
            if spec and spec.default:
                return spec.default
        return value

    def _call(self, method: str, url: str, headers: dict, params: Any = None, json_body: Any = None) -> Any:
        status, data = self.http.request(method, url, headers=headers, params=params, json_body=json_body)
        if status == 401 or status == 403:
            raise PosError(f"{self.label} rejected the access token ({status}). Check the token and its permissions.")
        if status >= 400:
            raise PosError(f"{self.label} returned an error ({status}): {_short(data)}")
        return data

    # --- operations (override) ---
    def check(self) -> str:
        """Confirm the credentials work. Returns a short description of what we connected to."""
        raise PosError("This connection can't be tested automatically.")

    def fetch_sales(self, since: datetime) -> Tuple[List[SaleLine], Optional[datetime]]:
        """Return sale lines changed since `since`, plus the timestamp to resume from next time."""
        raise PosError(f"{self.label} doesn't support pulling sales.")

    def list_catalog(self) -> List[CatalogItem]:
        raise PosError(f"{self.label} doesn't support listing items.")

    def push_stock(self, counts: Iterable[Tuple[str, int]]) -> int:
        """Set stock counts in the POS. `counts` is (external_item_id, quantity). Returns how many were sent."""
        raise PosError(f"{self.label} doesn't support updating stock.")

    def verify_webhook(self, headers: Dict[str, str], body: bytes, url: str) -> bool:
        return False

    def handle_webhook(self, payload: Any) -> WebhookResult:
        return WebhookResult()


def _short(data: Any, limit: int = 300) -> str:
    text = data if isinstance(data, str) else json.dumps(data)
    return text[:limit]


def to_int_qty(value: Any) -> int:
    """POS quantities can be strings or decimals ("2", "1.000"). Whole units only."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0
