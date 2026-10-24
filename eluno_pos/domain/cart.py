"""A cart is a list of pairs. A pair is a frame and/or a lens for one wearer.

  frame + lens  -> spectacles (the common bill)
  lens only     -> customer's own frame
  frame only    -> sunglasses, or a frame with plano demo lenses
Each pair can have its own wearer and prescription, which is how one bill serves a family
(one Shopify order has one customer; the wearer is carried on the lens line).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .catalog import Frame, Lens
from .issues import BLOCK, Issue
from .offers import OfferLine
from .parties import Customer, Staff, Store
from .prescription import RxSelection


@dataclass(frozen=True)
class Pair:
    pair_ref: int
    wearer: Customer
    rx: RxSelection
    frame: Optional[Frame] = None
    lens: Optional[Lens] = None


@dataclass(frozen=True)
class CartLine:
    line_ref: str  # "1F", "1L", "2F", ...
    pair_ref: int
    kind: str  # frame | lens
    item: object  # Frame | Lens
    wearer: Customer
    rx_selection: RxSelection

    @property
    def mrp(self) -> int:
        return self.item.mrp  # type: ignore[attr-defined]

    @property
    def hsn(self) -> str:
        return self.item.hsn  # type: ignore[attr-defined]

    @property
    def description(self) -> str:
        if self.kind == "lens":
            rx = self.rx_selection.chosen
            return self.item.description(rx.rx_id if rx else None)  # type: ignore[attr-defined]
        return self.item.description  # type: ignore[attr-defined]

    def offer_line(self) -> OfferLine:
        if self.kind == "lens":
            l: Lens = self.item  # type: ignore[assignment]
            return OfferLine(self.line_ref, self.pair_ref, "lens", l.mrp, l.lens_type, l.index, l.coating)
        return OfferLine(self.line_ref, self.pair_ref, "frame", self.mrp)


@dataclass(frozen=True)
class Cart:
    store: Store
    customer: Customer
    staff: Staff
    channel: str
    today: date
    pairs: Tuple[Pair, ...]

    def lines(self) -> List[CartLine]:
        out = []
        for p in self.pairs:
            if p.frame:
                out.append(CartLine("%dF" % p.pair_ref, p.pair_ref, "frame", p.frame, p.wearer, p.rx))
            if p.lens:
                out.append(CartLine("%dL" % p.pair_ref, p.pair_ref, "lens", p.lens, p.wearer, p.rx))
        return out


def structural_issues(cart: Cart) -> List[Issue]:
    issues: List[Issue] = []
    if not cart.pairs:
        issues.append(Issue("CART_EMPTY", BLOCK, "The cart has no items."))
    for p in cart.pairs:
        if p.frame is None and p.lens is None:
            issues.append(Issue("PAIR_EMPTY", BLOCK, "Pair %d has neither a frame nor a lens." % p.pair_ref, p.pair_ref))
        if p.frame is not None and not p.frame.sellable:
            issues.append(Issue("FRAME_DISCONTINUED", BLOCK, "%s (%s) is archived and can no longer be sold." % (p.frame.description, p.frame.sku), p.pair_ref,
                                hint="Pick a current frame."))
        if p.frame is not None and p.lens is not None and not p.frame.rx_able:
            issues.append(Issue("FRAME_NOT_RX_ABLE", BLOCK, "%s (%s) cannot take prescription lenses." % (p.frame.description, p.frame.sku), p.pair_ref,
                                hint="Choose an Rx-able frame, or sell the sunglasses without a lens."))
    return issues


def stock_issues(cart: Cart, levels: Dict[Tuple[int, int], int], stores_by_location: Dict[int, Store],
                 held: Optional[Dict[Tuple[int, int], int]] = None) -> List[Issue]:
    """Frames are stocked per store. `levels` maps (inventory_item_id, location_id) -> available in Shopify;
    `held` is what other tills are taking payment for right now and Shopify does not know about yet."""
    held = held or {}
    issues: List[Issue] = []
    needed: Dict[int, List[Pair]] = {}
    for p in cart.pairs:
        if p.frame is not None and p.frame.inventory_item_id is not None:
            needed.setdefault(p.frame.inventory_item_id, []).append(p)
    for item_id, pairs in needed.items():
        in_shopify = levels.get((item_id, cart.store.location_id), 0)
        on_hold = held.get((item_id, cart.store.location_id), 0)
        have = in_shopify - on_hold
        if have >= len(pairs):
            continue
        frame = pairs[0].frame
        if in_shopify >= len(pairs):  # it is on the shelf, but another till is already charging for it
            issues.append(Issue("STOCK_HELD", BLOCK, "%s (%s): %d at %s, but %d is reserved by a bill on another till "
                                "(payment in progress, or failed and not yet cancelled); %d needed." % (
                frame.description, frame.sku, in_shopify, cart.store.store_code, on_hold, len(pairs)), pairs[0].pair_ref,  # type: ignore[union-attr]
                "Wait until that bill is paid or cancelled, or pick another frame."))
            continue
        elsewhere = sorted(((stores_by_location[loc].store_code, n) for (iid, loc), n in levels.items()
                            if iid == item_id and loc != cart.store.location_id and n > 0 and loc in stores_by_location),
                           key=lambda x: -x[1])
        hint = ("Available at " + ", ".join("%s (%d)" % s for s in elsewhere) + "; transfer it or pick another frame.") if elsewhere else "Out of stock everywhere; pick another frame."
        issues.append(Issue("OUT_OF_STOCK", BLOCK, "%s (%s): %d in stock at %s, %d needed." % (
            frame.description, frame.sku, max(have, 0), cart.store.store_code, len(pairs)), pairs[0].pair_ref, hint))  # type: ignore[union-attr]
    return issues
