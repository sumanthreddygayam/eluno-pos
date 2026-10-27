"""Shopify Admin REST adapter: the only module that knows Shopify's shapes.

Mapping is in pure functions (order_payload, refund_payload, bill_from_order, sale_record_from_order)
so it is tested without a server; ShopifyAdmin adds HTTP, caching and idempotency.

Idempotency: Shopify order and refund creation are not idempotent. Our key is the invoice number
(gapless, allocated locally before the first attempt) written to note_attributes; for refunds it is
the credit note number. Before any retry whose first attempt might have landed, we look the key up
in Shopify and adopt what we find instead of creating a duplicate.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.bill import SYNCED, Bill, BillLine
from ..domain.catalog import Catalog, Frame, Lens, LensChoice, RxLimits
from ..domain.dayend import IST, RefundRecord, SaleRecord
from ..domain.documents import parse_seq
from ..domain.money import from_decimal_str as paise, to_decimal_str as dec
from ..domain.optics import normalize_index, to_cd
from ..domain.parties import Customer, Store
from ..domain.refund import Refund
from ..ports import AmbiguousWrite, IntegrationError, OrderReceipt, RefundQuote, RefundReceipt
from .http import HttpClient

log = logging.getLogger("eluno_pos.shopify")

GATEWAY = {"upi": "razorpay_terminal", "card": "razorpay_terminal", "cash": "cash"}
SOURCE_NAME = "eluno-pos"


# =================================================================== pure mapping: Shopify -> domain
def _attrs(items: Optional[list]) -> Dict[str, str]:
    return {a.get("name"): a.get("value") for a in (items or [])}


def customer_from(c: dict) -> Customer:
    return Customer(customer_id=int(c["id"]), first_name=c.get("first_name") or "", last_name=c.get("last_name") or "",
                    phone=c.get("phone") or "", tags=frozenset(t.strip() for t in (c.get("tags") or "").split(",") if t.strip()),
                    lifetime_value=paise(c.get("total_spent") or "0.00"))


def store_from(loc: dict) -> Store:
    e = loc.get("eluno") or {}
    return Store(location_id=int(loc["id"]), store_code=e.get("store_code") or str(loc["id"]), name=loc.get("name", ""),
                 address=loc.get("address1", ""), invoice_prefix=e.get("invoice_prefix"), kind=e.get("kind", "store"),
                 state_code=e.get("state_code", ""))


def catalog_from(products: Sequence[dict]) -> Catalog:
    frames: List[Frame] = []
    lenses: List[Lens] = []
    for p in products:
        for v in p.get("variants", []):
            e = v.get("eluno") or {}
            if p.get("product_type") == "Frame":
                frames.append(Frame(variant_id=v["id"], product_id=p["id"], sku=v["sku"], title=p["title"], variant_title=v.get("title") or "",
                                    mrp=paise(v["price"]), hsn=e["hsn"], category=e.get("category", "optical"), rx_able=bool(e.get("rx_able")),
                                    inventory_item_id=v.get("inventory_item_id"), sellable=p.get("status") == "active"))
            elif p.get("product_type") == "Lens":
                lim = e["rx_limits"]
                lenses.append(Lens(variant_id=v["id"], product_id=p["id"], sku=v["sku"], title=p["title"], lens_type=e["lens_type"],
                                   index=normalize_index(e["index"]), coating=e["coating"], mrp=paise(v["price"]), hsn=e["hsn"],
                                   limits=RxLimits(to_cd(lim["sph_min"]), to_cd(lim["sph_max"]), to_cd(lim["cyl_abs_max"]),
                                                   bool(lim["add_required"]), to_cd(lim["add_min"]), to_cd(lim["add_max"])),
                                   lab_tat_days=int(e.get("lab_tat_days", 0))))
    return Catalog(frames, lenses)


def _lens_description(lens_config: Optional[str], rx_id: Optional[str], fallback: str) -> str:
    try:
        c = LensChoice.parse(lens_config or "")
    except ValueError:
        return fallback
    return "%s %s %s lens" % (c.lens_type.replace("_", " ").title(), c.index, c.coating.replace("_", " ")) + (", Rx %s" % rx_id if rx_id else "")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def bill_from_order(order: dict, store: Store, catalog: Optional[Catalog] = None) -> Bill:
    """Read a POS order back from Shopify (pre-cutover orders, reprints, refunds of old bills)."""
    na = _attrs(order.get("note_attributes"))
    apps = order.get("discount_applications") or []
    lines: List[BillLine] = []
    seen: Dict[str, int] = {}
    for li in order.get("line_items", []):
        if int(li.get("quantity", 1)) != 1:
            raise IntegrationError("shopify", "order %s line %s has quantity %s; POS lines are one unit each" % (order.get("name"), li.get("sku"), li.get("quantity")), retryable=False)
        props = _attrs(li.get("properties"))
        kind = props.get("line_type") or ("lens" if catalog and li.get("variant_id") in catalog.lenses_by_variant else "frame")
        pair_ref = int(props.get("pair_ref") or 1)
        ref = props.get("_line_ref") or "%d%s" % (pair_ref, "F" if kind == "frame" else "L")
        if ref in seen:
            seen[ref] += 1
            ref = "%s%d" % (ref, seen[ref])
        else:
            seen[ref] = 1
        taxes = {t["title"]: t for t in li.get("tax_lines", [])}
        cgst = paise(taxes["CGST"]["price"]) if "CGST" in taxes else 0
        sgst = paise(taxes["SGST"]["price"]) if "SGST" in taxes else 0
        rate = next(iter(taxes.values()))["rate"] if taxes else 0
        gross = paise(li["price"]) - paise(li.get("total_discount") or "0.00")
        frame = catalog.frames_by_variant.get(li.get("variant_id")) if catalog else None
        inv_item = frame.inventory_item_id if frame else None
        discounts = [[apps[a["discount_application_index"]]["title"] if a.get("discount_application_index") is not None and a["discount_application_index"] < len(apps) else "DISCOUNT",
                      paise(a["amount"])] for a in li.get("discount_allocations", [])]
        title, vt = li.get("title", ""), li.get("variant_title") or ""
        desc = _lens_description(props.get("lens_config"), props.get("rx_id"), title) if kind == "lens" else ("%s %s" % (title, vt)).strip()
        lines.append(BillLine(line_ref=ref, pair_ref=pair_ref, kind=kind, sku=li.get("sku", ""), variant_id=li.get("variant_id"), title=title,
                              variant_title=vt, description=desc, hsn=props.get("hsn") or "", rate_bps=int(round(float(rate) * 20000)),
                              mrp=paise(li["price"]), discounts=discounts, taxable=gross - cgst - sgst, cgst=cgst, sgst=sgst,
                              inventory_item_id=inv_item, lens_config=props.get("lens_config"), rx_id=props.get("rx_id"), rx_json=props.get("rx"),
                              wearer_customer_id=int(props["for_customer_id"]) if props.get("for_customer_id") else None,
                              wearer_name=props.get("wearer"), shopify_line_id=li.get("id")))
    sale = next((t for t in order.get("transactions", []) if t.get("kind") == "sale" and t.get("status") == "success"), None)
    cust = order.get("customer") or {}
    created = _parse_ts(order["created_at"]).astimezone(IST)
    return Bill(
        bill_id=na.get("pos_bill_id") or "shopify-%s" % order["id"], store_code=na.get("store_code") or store.store_code,
        location_id=order.get("location_id") or store.location_id, customer_id=cust.get("id") or 0,
        customer_name=("%s %s" % (cust.get("first_name") or "", cust.get("last_name") or "")).strip(), customer_phone=cust.get("phone") or "",
        staff_id=na.get("staff_id") or "", channel=na.get("channel") or "walk_in", business_date=created.date().isoformat(), lines=lines,
        kind="remake" if na.get("parent_invoice_no") else "sale", status=SYNCED,
        offers=[{"offer_id": a.get("title"), "name": a.get("description") or a.get("title"), "amount": paise(a.get("value") or "0.00")} for a in apps],
        payment_method=na.get("payment_method") or ("none" if not sale else "upi"), charge_id=(sale or {}).get("authorization"),
        invoice_no=na.get("invoice_no"), paid_at=created.isoformat(), shopify_order_id=order["id"], shopify_order_name=order.get("name"),
        shopify_sale_txn_id=(sale or {}).get("id"), parent_invoice_no=na.get("parent_invoice_no"),
        remake_reason=na.get("remake_reason"), remake_cost_bearer=na.get("remake_cost_bearer"),
        created_at=created.isoformat(), updated_at=created.isoformat(), source="imported")


def sale_record_from_order(order: dict) -> Optional[SaleRecord]:
    """A POS order (one carrying an invoice number) for the day-end report; None for anything else."""
    na = _attrs(order.get("note_attributes"))
    if not na.get("invoice_no"):
        return None
    apps = order.get("discount_applications") or []
    mrp = discount = cgst = sgst = 0
    by_offer: Dict[str, int] = {}
    for li in order.get("line_items", []):
        mrp += paise(li["price"]) * int(li.get("quantity", 1))
        discount += paise(li.get("total_discount") or "0.00")
        for t in li.get("tax_lines", []):
            if t.get("title") == "CGST":
                cgst += paise(t["price"])
            elif t.get("title") == "SGST":
                sgst += paise(t["price"])
        for a in li.get("discount_allocations", []):
            idx = a.get("discount_application_index")
            title = apps[idx]["title"] if idx is not None and idx < len(apps) else "DISCOUNT"
            by_offer[title] = by_offer.get(title, 0) + paise(a["amount"])
    refunds = []
    for rf in order.get("refunds", []):
        ra = _attrs(rf.get("note_attributes"))
        refunds.append(RefundRecord(
            refund_id=rf["id"], created_at=_parse_ts(rf["created_at"]),
            amount=sum(paise(t["amount"]) for t in rf.get("transactions", []) if t.get("kind") == "refund"),
            taxable=sum(paise(x["subtotal"]) for x in rf.get("refund_line_items", []) if x.get("subtotal")),
            tax=sum(paise(x["total_tax"]) for x in rf.get("refund_line_items", []) if x.get("total_tax")),
            credit_note_no=ra.get("credit_note_no"), reason_code=ra.get("reason_code")))
    net = mrp - discount
    return SaleRecord(
        order_id=order["id"], order_name=order.get("name", ""), invoice_no=na["invoice_no"], store_code=na.get("store_code", ""),
        location_id=order.get("location_id"), created_at=_parse_ts(order["created_at"]), channel=na.get("channel", "unknown"),
        payment_method=na.get("payment_method", "unknown"), staff_id=na.get("staff_id"), mrp=mrp, discount=discount, net=net,
        taxable=net - cgst - sgst, cgst=cgst, sgst=sgst,
        captured=sum(paise(t["amount"]) for t in order.get("transactions", []) if t.get("kind") == "sale" and t.get("status") == "success"),
        discounts_by_offer=tuple(sorted(by_offer.items())), parent_invoice_no=na.get("parent_invoice_no"), refunds=tuple(refunds))


# =================================================================== pure mapping: domain -> Shopify
def order_payload(bill: Bill) -> dict:
    apps: List[dict] = []
    app_index: Dict[str, int] = {}
    for o in bill.offers:
        lines_with = [l for l in bill.lines if any(oid == o["offer_id"] for oid, _ in l.discounts)]
        if not lines_with:
            continue
        app_index[o["offer_id"]] = len(apps)
        apps.append({"type": "manual", "title": o["offer_id"], "description": o.get("name") or o["offer_id"], "value": dec(o["amount"]),
                     "value_type": "fixed_amount", "allocation_method": "each" if len(lines_with) == 1 else "across",
                     "target_selection": "explicit", "target_type": "line_item"})
    line_items = []
    for l in bill.lines:
        props = [("line_type", l.kind), ("hsn", l.hsn), ("pair_ref", str(l.pair_ref))]
        if l.kind == "lens":
            props += [("rx_id", l.rx_id), ("rx", l.rx_json), ("lens_config", l.lens_config)]
        if l.wearer_customer_id and l.wearer_customer_id != bill.customer_id:
            props += [("for_customer_id", str(l.wearer_customer_id)), ("wearer", l.wearer_name)]
        props.append(("_line_ref", l.line_ref))
        half = l.rate_bps / 20000
        line_items.append({
            "variant_id": l.variant_id, "quantity": 1, "price": dec(l.mrp), "total_discount": dec(l.discount), "taxable": True,
            "tax_lines": [{"title": "CGST", "rate": half, "price": dec(l.cgst)}, {"title": "SGST", "rate": half, "price": dec(l.sgst)}],
            "discount_allocations": [{"amount": dec(a), "discount_application_index": app_index[oid]} for oid, a in l.discounts if a],
            "properties": [{"name": n, "value": v} for n, v in props if v is not None],
        })
    attrs = [("invoice_no", bill.invoice_no), ("store_code", bill.store_code), ("channel", bill.channel), ("staff_id", bill.staff_id),
             ("payment_method", bill.payment_method), ("pos_bill_id", bill.bill_id)]
    if bill.offers:
        attrs.append(("offers", ",".join(o["offer_id"] for o in bill.offers)))
    if bill.manual_discount:
        attrs += [("discount_approver", bill.manual_discount["approver_id"]), ("discount_reason_code", bill.manual_discount["reason_code"])]
    if bill.parent_invoice_no:
        attrs += [("parent_invoice_no", bill.parent_invoice_no), ("remake_reason", bill.remake_reason), ("remake_cost_bearer", bill.remake_cost_bearer)]
    tags = ["pos", bill.channel] + (["remake", "parent:%s" % bill.parent_invoice_no] if bill.parent_invoice_no else [])
    order: Dict[str, Any] = {
        "taxes_included": True, "currency": "INR", "location_id": bill.location_id, "source_name": SOURCE_NAME,
        "source_identifier": bill.invoice_no, "processed_at": bill.paid_at,
        # The frame is in the customer's hands and the money is taken: never let a stale stock count reject the order.
        "inventory_behaviour": "decrement_ignoring_policy" if any(l.restockable for l in bill.lines) else "bypass",
        "customer": {"id": bill.customer_id}, "tags": ",".join(tags), "send_receipt": False,
        "note_attributes": [{"name": n, "value": v} for n, v in attrs if v is not None],
        "line_items": line_items, "discount_applications": apps,
    }
    if bill.total > 0:
        order["transactions"] = [{"kind": "sale", "status": "success", "gateway": GATEWAY[bill.payment_method or "cash"],
                                  "amount": dec(bill.total), "authorization": bill.charge_id or bill.invoice_no}]
    return {"order": order}


def refund_payload(bill: Bill, refund: Refund, reason_text: str = "") -> dict:
    items = []
    for l in refund.lines:
        item: Dict[str, Any] = {"line_item_id": l.shopify_line_id, "quantity": 1, "restock_type": "return" if l.restock else "no_restock"}
        if l.restock:
            item["location_id"] = l.location_id
        items.append(item)
    attrs = [("credit_note_no", refund.credit_note_no), ("reason_code", refund.reason_code), ("staff_id", refund.staff_id),
             ("approver_id", refund.approver_id), ("pos_refund_id", refund.refund_id), ("invoice_no", bill.invoice_no)]
    return {"refund": {
        "note": reason_text or refund.reason_code, "notify": False,
        "note_attributes": [{"name": n, "value": v} for n, v in attrs if v],
        "refund_line_items": items,
        "transactions": [{"kind": "refund", "gateway": GATEWAY[refund.method], "amount": dec(refund.amount),
                          "parent_id": bill.shopify_sale_txn_id, "authorization": refund.terminal_refund_id or refund.credit_note_no}],
    }}


def receipt_from(order: dict, bill: Bill, adopted: bool = False) -> OrderReceipt:
    line_ids: Dict[str, int] = {}
    for li in order.get("line_items", []):
        ref = _attrs(li.get("properties")).get("_line_ref")
        if ref:
            line_ids[ref] = li["id"]
    if len(line_ids) != len(bill.lines):  # fall back to position: Shopify keeps line order
        line_ids = {l.line_ref: li["id"] for l, li in zip(bill.lines, order.get("line_items", []))}
    sale = next((t for t in order.get("transactions", []) if t.get("kind") == "sale"), None)
    return OrderReceipt(order_id=order["id"], order_name=order.get("name", ""), sale_txn_id=(sale or {}).get("id"), line_ids=line_ids, adopted=adopted)


# =================================================================== adapter
class ShopifyAdmin:
    WRITE_ATTEMPTS = 4

    def __init__(self, client: HttpClient, api_version: str = "2026-10", catalog_ttl: float = 300.0):
        self.client = client
        self.api = "admin/api/%s/" % api_version
        self.catalog_ttl = catalog_ttl
        self._catalog: Optional[Tuple[float, Catalog]] = None
        self._stores: Optional[List[Store]] = None

    # ------------------------------------------------------------ reads
    def find_customer_by_phone(self, phone: str) -> Optional[Customer]:
        d = self.client.get(self.api + "customers/search.json", {"query": "phone:%s" % phone})
        matches = [c for c in (d or {}).get("customers", []) if c.get("phone") == phone]
        return customer_from(matches[0]) if matches else None

    def create_customer(self, first_name: str, last_name: str, phone: str, email: Optional[str], note: str) -> Customer:
        """POST customers.json. A lost answer may mean Shopify created it, so look the phone up before trying again."""
        payload = {"customer": {"first_name": first_name, "last_name": last_name, "phone": phone, "email": email or None,
                                "note": note, "tags": "", "verified_email": False}}
        last: Optional[Exception] = None
        for _ in range(self.WRITE_ATTEMPTS):
            try:
                return customer_from(self.client.post(self.api + "customers.json", payload, idempotent=False)["customer"])
            except AmbiguousWrite as e:
                last = e
                log.warning("customer create for %s ambiguous (%s); checking Shopify before retrying", phone, e)
                found = self.find_customer_by_phone(phone)
                if found:
                    return found
        raise IntegrationError("shopify", "could not create customer %s: %s" % (phone, last))

    def stores(self) -> List[Store]:
        if self._stores is None:
            self._stores = [store_from(l) for l in self.client.get(self.api + "locations.json")["locations"] if l.get("active", True)]
        return self._stores

    def catalog(self) -> Catalog:
        now = time.monotonic()
        if self._catalog is None or now - self._catalog[0] > self.catalog_ttl:
            products = []
            for t in ("Frame", "Lens"):
                products += self.client.get_all(self.api + "products.json", {"product_type": t, "limit": 250}, "products")
            self._catalog = (now, catalog_from(products))
        return self._catalog[1]

    def stock_levels(self, inventory_item_ids: Sequence[int]) -> Dict[Tuple[int, int], int]:
        ids = sorted({i for i in inventory_item_ids if i})
        if not ids:
            return {}
        rows = self.client.get_all(self.api + "inventory_levels.json", {"inventory_item_ids": ",".join(map(str, ids)), "limit": 250}, "inventory_levels")
        return {(r["inventory_item_id"], r["location_id"]): int(r["available"]) for r in rows}

    def _orders(self, store: Store, start: Optional[date] = None, end: Optional[date] = None) -> List[dict]:
        params: Dict[str, Any] = {"location_id": store.location_id, "status": "any", "limit": 250}
        if start:
            params["created_at_min"] = "%sT00:00:00+05:30" % start.isoformat()
        if end:
            params["created_at_max"] = "%sT23:59:59+05:30" % end.isoformat()
        return self.client.get_all(self.api + "orders.json", params, "orders")

    def _find_order_raw(self, invoice_no: str, store: Store, around: Optional[date]) -> Optional[dict]:
        # created_at filters are only a narrowing hint (+/- a day absorbs clock and timezone skew)
        orders = self._orders(store, around - timedelta(days=1), around + timedelta(days=1)) if around else self._orders(store)
        return next((o for o in orders if _attrs(o.get("note_attributes")).get("invoice_no") == invoice_no), None)

    def find_order(self, invoice_no: str, store: Store, around: Optional[date] = None) -> Optional[Bill]:
        raw = self._find_order_raw(invoice_no, store, around)
        return bill_from_order(raw, store, self.catalog()) if raw else None

    def sales_for_day(self, store: Store, day: date, refund_lookback_days: int = 90) -> List[SaleRecord]:
        orders = self._orders(store, day - timedelta(days=refund_lookback_days), day + timedelta(days=1))
        return [r for r in (sale_record_from_order(o) for o in orders) if r is not None]

    def highest_document_seq(self, store: Store, series: str) -> int:
        best = 0
        for o in self._orders(store):
            for n in [_attrs(o.get("note_attributes")).get("invoice_no")] + [_attrs(r.get("note_attributes")).get("credit_note_no") for r in o.get("refunds", [])]:
                s = parse_seq(n or "", series)
                if s and s > best:
                    best = s
        return best

    # ------------------------------------------------------------ writes
    def create_order(self, bill: Bill, store: Store, may_exist: bool) -> OrderReceipt:
        if not bill.invoice_no:
            raise ValueError("an order is only created for a bill with an invoice number")
        around = date.fromisoformat(bill.business_date)
        if may_exist:
            found = self._find_order_raw(bill.invoice_no, store, around)
            if found:
                log.warning("order for %s already in Shopify (%s); adopting it", bill.invoice_no, found.get("name"))
                return receipt_from(found, bill, adopted=True)
        payload = order_payload(bill)
        last: Optional[Exception] = None
        for _ in range(self.WRITE_ATTEMPTS):
            try:
                return receipt_from(self.client.post(self.api + "orders.json", payload, idempotent=False)["order"], bill)
            except AmbiguousWrite as e:
                last = e
                log.warning("order create for %s ambiguous (%s); checking Shopify before retrying", bill.invoice_no, e)
                found = self._find_order_raw(bill.invoice_no, store, around)
                if found:
                    return receipt_from(found, bill, adopted=True)
        raise IntegrationError("shopify", "could not create order for %s: %s" % (bill.invoice_no, last))

    def quote_refund(self, bill: Bill, refund: Refund) -> RefundQuote:
        body = {"refund": {"refund_line_items": [{"line_item_id": l.shopify_line_id, "quantity": 1,
                                                  "restock_type": "return" if l.restock else "no_restock",
                                                  **({"location_id": l.location_id} if l.restock else {})} for l in refund.lines]}}
        d = self.client.post(self.api + "orders/%d/refunds/calculate.json" % bill.shopify_order_id, body, idempotent=True)["refund"]
        tx = (d.get("transactions") or [{}])[0]
        return RefundQuote(maximum_refundable=paise(tx.get("maximum_refundable") or "0.00"),
                           lines={x["line_item_id"]: (paise(x["subtotal"]), paise(x["total_tax"])) for x in d.get("refund_line_items", [])})

    def _find_refund(self, bill: Bill, credit_note_no: str) -> Optional[dict]:
        order = self.client.get(self.api + "orders/%d.json" % bill.shopify_order_id)["order"]
        return next((r for r in order.get("refunds", []) if _attrs(r.get("note_attributes")).get("credit_note_no") == credit_note_no), None)

    def create_refund(self, bill: Bill, refund: Refund, may_exist: bool, reason_text: str = "") -> RefundReceipt:
        if may_exist:
            found = self._find_refund(bill, refund.credit_note_no or "")
            if found:
                return RefundReceipt(found["id"], adopted=True)
        payload = refund_payload(bill, refund, reason_text)
        last: Optional[Exception] = None
        for _ in range(self.WRITE_ATTEMPTS):
            try:
                return RefundReceipt(self.client.post(self.api + "orders/%d/refunds.json" % bill.shopify_order_id, payload, idempotent=False)["refund"]["id"])
            except AmbiguousWrite as e:
                last = e
                found = self._find_refund(bill, refund.credit_note_no or "")
                if found:
                    return RefundReceipt(found["id"], adopted=True)
        raise IntegrationError("shopify", "could not create refund %s: %s" % (refund.credit_note_no, last))
