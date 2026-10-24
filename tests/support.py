"""Test helpers: the starter-kit seed data mapped through the adapters' pure functions."""
from __future__ import annotations

import json
import os
from datetime import date
from functools import lru_cache
from typing import List, Optional

from eluno_pos.adapters.shopify import catalog_from, customer_from, store_from
from eluno_pos.adapters.smt import prescription_from, staff_from
from eluno_pos.config import ROOT
from eluno_pos.domain.catalog import Catalog
from eluno_pos.domain.offers import OfferBook, OfferEngine, OfferLine
from eluno_pos.domain.parties import Customer, Staff, Store
from eluno_pos.domain.prescription import Prescription
from eluno_pos.domain.tax import TaxTable

DATA = os.path.join(ROOT, "starter-kit", "data")
CONFIG = os.path.join(ROOT, "config")
D = date.fromisoformat


def load(name: str, base: str = DATA):
    with open(os.path.join(base, name), encoding="utf-8") as f:
        return json.load(f)


def orders() -> List[dict]:
    return load("orders_sample.json")


def order(name: str) -> dict:
    return next(o for o in orders() if o["name"] == name)


@lru_cache(maxsize=None)
def catalog() -> Catalog:
    return catalog_from(load("products.json"))


def taxes() -> TaxTable:
    return TaxTable.from_json(load("tax_rates.json", CONFIG))


def offer_book(raw: Optional[list] = None) -> OfferBook:
    return OfferBook.from_json(raw if raw is not None else load("offers.json", CONFIG))


def engine(policy: str = "best_for_customer", raw: Optional[list] = None) -> OfferEngine:
    return OfferEngine(offer_book(raw), policy, ["WALKOUT_SAVE", "PRICE_MATCH"])


def store(code: str) -> Store:
    return next(store_from(l) for l in load("locations.json") if l["eluno"]["store_code"] == code)


def customer(phone: str) -> Customer:
    return next(customer_from(c) for c in load("customers.json") if c["phone"] == phone)


def staff(staff_id: str) -> Staff:
    return next(staff_from(s) for s in load("staff.json") if s["staff_id"] == staff_id)


def prescriptions(phone: str) -> List[Prescription]:
    return [prescription_from(r) for r in load("prescriptions.json") if r["phone"] == phone]


def offer_lines(*items: str) -> List[OfferLine]:
    """offer_lines('FR-0003', 'progressive/1.60/blue_cut', '|', 'FR-0031', ...) - '|' starts the next pair."""
    from eluno_pos.domain.catalog import LensChoice
    out, pair = [], 1
    for it in items:
        if it == "|":
            pair += 1
            continue
        if it.startswith("FR-"):
            f = catalog().frame(it)
            out.append(OfferLine("%dF" % pair, pair, "frame", f.mrp))
        else:
            l = catalog().lens(LensChoice.parse(it))
            out.append(OfferLine("%dL" % pair, pair, "lens", l.mrp, l.lens_type, l.index, l.coating))
    return out
