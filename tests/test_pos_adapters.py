"""POS adapters: parsing, signatures and paging, using recorded-style payloads (no network)."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from app.pos.base import PosError
from app.pos.clover import CloverAdapter, verification_code
from app.pos.generic import GenericAdapter, parse_sales_csv
from app.pos.square import SquareAdapter


class FakeHttp:
    """Answers requests from a list of (method, url-substring, status, json) rules; records calls."""

    def __init__(self, rules):
        self.rules, self.calls = list(rules), []

    def request(self, method, url, headers=None, params=None, json_body=None):
        self.calls.append((method, url, params, json_body))
        for i, (m, frag, status, data) in enumerate(self.rules):
            if m == method and frag in url:
                if isinstance(data, list):  # a list = successive pages
                    page = data.pop(0)
                    if not data:
                        self.rules.pop(i)
                    return status, page
                return status, data
        raise AssertionError(f"unexpected call {method} {url}")


SQ = {"access_token": "tok", "location_id": "L1", "environment": "sandbox", "webhook_secret": "sigkey"}
SQ_ORDER = {
    "id": "ORD1", "state": "COMPLETED", "location_id": "L1", "closed_at": "2026-11-11T15:00:00Z",
    "line_items": [
        {"uid": "a", "catalog_object_id": "VAR1", "quantity": "2", "name": "House Blend", "variation_name": "Regular"},
        {"uid": "b", "quantity": "1", "name": "Custom amount"},  # no catalog id -> ignored
    ],
    "returns": [{"return_line_items": [{"uid": "r", "catalog_object_id": "VAR2", "quantity": "1", "name": "Oat Milk"}]}],
}


def test_square_order_lines_sales_and_returns():
    lines = SquareAdapter(SQ).order_lines(SQ_ORDER)
    assert [(l.external_id, l.quantity, l.external_item_id) for l in lines] == [("ORD1:a", 2, "VAR1"), ("ORD1:return:r", -1, "VAR2")]
    assert lines[0].name == "House Blend"
    assert SquareAdapter(SQ).order_lines({**SQ_ORDER, "state": "OPEN"}) == []


def test_square_fetch_sales_pages_and_fills_skus():
    page2 = {**SQ_ORDER, "id": "ORD2", "closed_at": "2026-11-11T16:30:00Z", "returns": []}
    http = FakeHttp([
        ("POST", "/v2/orders/search", 200, [{"orders": [SQ_ORDER], "cursor": "c1"}, {"orders": [page2]}]),
        ("POST", "/v2/catalog/batch-retrieve", 200, {"objects": [
            {"id": "VAR1", "item_variation_data": {"sku": "COF-001"}},
            {"id": "VAR2", "item_variation_data": {"sku": "DAI-002"}}]}),
    ])
    since = datetime(2026, 11, 11, tzinfo=timezone.utc)
    lines, newest = SquareAdapter(SQ, http).fetch_sales(since)
    assert [l.sku for l in lines] == ["COF-001", "DAI-002", "COF-001"]
    assert newest == datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)
    first = http.calls[0][3]
    assert first["location_ids"] == ["L1"] and first["query"]["sort"]["sort_field"] == "CLOSED_AT"
    assert first["query"]["filter"]["date_time_filter"]["closed_at"]["start_at"] == "2026-11-11T00:00:00Z"
    assert http.calls[1][3]["cursor"] == "c1"
    assert "squareupsandbox" in http.calls[0][1]


def test_square_webhook_signature():
    body = json.dumps({"type": "order.updated"}).encode()
    url = "https://shop.example/api/pos/webhooks/1"
    sig = base64.b64encode(hmac.new(b"sigkey", url.encode() + body, hashlib.sha256).digest()).decode()
    a = SquareAdapter(SQ)
    assert a.verify_webhook({"x-square-hmacsha256-signature": sig}, body, url)
    assert not a.verify_webhook({"x-square-hmacsha256-signature": sig}, body, url + "x")
    assert not a.verify_webhook({}, body, url)


def test_square_webhook_fetches_completed_order_only():
    http = FakeHttp([("GET", "/v2/orders/ORD1", 200, {"order": SQ_ORDER}),
                     ("POST", "/v2/catalog/batch-retrieve", 200, {"objects": []})])
    a = SquareAdapter(SQ, http)
    event = {"type": "order.updated", "data": {"object": {"order_updated": {"order_id": "ORD1", "state": "COMPLETED", "location_id": "L1"}}}}
    assert len(a.handle_webhook(event).lines) == 2
    open_event = {"type": "order.updated", "data": {"object": {"order_updated": {"order_id": "ORD1", "state": "OPEN"}}}}
    assert a.handle_webhook(open_event).lines == []
    other_loc = {"type": "order.updated", "data": {"object": {"order_updated": {"order_id": "X", "state": "COMPLETED", "location_id": "L9"}}}}
    assert a.handle_webhook(other_loc).lines == []


def test_square_push_stock_batches():
    http = FakeHttp([("POST", "/v2/inventory/changes/batch-create", 200, {})])
    sent = SquareAdapter(SQ, http).push_stock([("VAR%d" % i, i) for i in range(150)])
    assert sent == 150 and len(http.calls) == 2
    change = http.calls[0][3]["changes"][5]
    assert change["type"] == "PHYSICAL_COUNT" and change["physical_count"]["quantity"] == "5"
    assert change["physical_count"]["location_id"] == "L1"


def test_square_catalog_lists_variations():
    http = FakeHttp([("GET", "/v2/catalog/list", 200, {"objects": [{"item_data": {"name": "Latte", "variations": [
        {"id": "V1", "item_variation_data": {"name": "Small", "sku": "LAT-S"}},
        {"id": "V2", "item_variation_data": {"name": "Regular"}}]}}]})])
    items = SquareAdapter(SQ, http).list_catalog()
    assert [(i.external_item_id, i.name, i.sku) for i in items] == [("V1", "Latte – Small", "LAT-S"), ("V2", "Latte", None)]


def test_bad_token_gives_clear_error():
    http = FakeHttp([("GET", "/v2/locations/L1", 401, {"errors": []})])
    with pytest.raises(PosError, match="rejected the access token"):
        SquareAdapter(SQ, http).check()


CL = {"merchant_id": "M1", "access_token": "tok", "region": "us", "webhook_secret": "authcode"}
CL_ORDER = {"id": "O1", "paymentState": "PAID", "modifiedTime": 1794409200000, "lineItems": {"elements": [
    {"id": "L1", "name": "Chips", "item": {"id": "I1"}},
    {"id": "L2", "name": "Chips", "item": {"id": "I1"}, "refunded": True},
    {"id": "L3", "name": "Manual", "price": 100},
]}}


def test_clover_order_lines_units_and_refunds():
    lines = CloverAdapter(CL).order_lines(CL_ORDER)
    assert [(l.external_id, l.quantity) for l in lines] == [("O1:L1", 1), ("O1:L2", 1), ("O1:L2:refund", -1)]
    assert CloverAdapter(CL).order_lines({**CL_ORDER, "paymentState": "OPEN"}) == []


def test_clover_fetch_sales_and_skus():
    http = FakeHttp([("GET", "/v3/merchants/M1/orders", 200, {"elements": [CL_ORDER]}),
                     ("GET", "/v3/merchants/M1/items/I1", 200, {"id": "I1", "sku": "SNK-001"})])
    lines, newest = CloverAdapter(CL, http).fetch_sales(datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert {l.sku for l in lines} == {"SNK-001"}
    assert newest.timestamp() * 1000 == 1794409200000
    assert ("filter", "modifiedTime>=1793491200000") in http.calls[0][2]
    assert http.calls[0][1].startswith("https://api.clover.com/v3/merchants/M1/")
    assert sum(1 for c in http.calls if "/items/" in c[1]) == 1  # cached


def test_clover_webhook_auth_and_payload():
    a = CloverAdapter(CL, FakeHttp([("GET", "/orders/O1", 200, CL_ORDER), ("GET", "/items/I1", 200, {"sku": "SNK-001"})]))
    assert a.verify_webhook({"x-clover-auth": "authcode"}, b"{}", "")
    assert not a.verify_webhook({"x-clover-auth": "nope"}, b"{}", "")
    payload = {"appId": "A", "merchants": {"M1": [{"objectId": "O:O1", "type": "UPDATE", "ts": 1},
                                                  {"objectId": "I:I1", "type": "UPDATE", "ts": 1}],
                                           "OTHER": [{"objectId": "O:X", "type": "CREATE"}]}}
    assert len(a.handle_webhook(payload).lines) == 3
    assert verification_code({"verificationCode": "abc"}) == "abc"
    assert verification_code(payload) is None


def test_clover_push_stock():
    http = FakeHttp([("POST", "/item_stocks/", 200, {})])
    assert CloverAdapter(CL, http).push_stock([("I1", 7), ("I2", -3)]) == 2
    assert http.calls[0][1].endswith("/v3/merchants/M1/item_stocks/I1") and http.calls[1][3] == {"quantity": 0}


def test_generic_webhook_signature_and_lines():
    a = GenericAdapter({"webhook_secret": "s3cret"})
    body = b'{"order_id": "1042", "lines": [{"sku": "cof-001", "quantity": 2}, {"sku": "SNK-002", "quantity": -1, "line_id": "r1"}]}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert a.verify_webhook({"x-stocksmart-signature": sig}, body, "")
    assert not a.verify_webhook({"x-stocksmart-signature": "sha256=00"}, body, "")
    assert not GenericAdapter({}).verify_webhook({"x-stocksmart-signature": sig}, body, "")
    lines = a.handle_webhook(json.loads(body)).lines
    assert [(l.external_id, l.sku, l.quantity) for l in lines] == [("1042:0", "cof-001", 2), ("1042:r1", "SNK-002", -1)]
    with pytest.raises(PosError):
        a.handle_webhook({"lines": [{"sku": "X", "quantity": 1}]})


def test_csv_parsing():
    text = "﻿Order Number,Item Name,SKU,Qty\n1001,Chips,SNK-001,3\n1001,Bar,SNK-002,1\n1002,Refund,SNK-001,-1\n,blank,,\n"
    lines = parse_sales_csv(text)
    assert [(l.external_id, l.sku, l.quantity) for l in lines] == [
        ("csv:1001:SNK-001", "SNK-001", 3), ("csv:1001:SNK-002", "SNK-002", 1), ("csv:1002:SNK-001", "SNK-001", -1)]
    no_ids = parse_sales_csv("sku,quantity\nA-1,2\nA-1,2\n")
    assert len({l.external_id for l in no_ids}) == 2  # two identical rows are still two sales
    assert [l.external_id for l in parse_sales_csv("sku,quantity\nA-1,2\nA-1,2\n")] == [l.external_id for l in no_ids]
    with pytest.raises(PosError, match="SKU column"):
        parse_sales_csv("name,price\nx,1\n")
