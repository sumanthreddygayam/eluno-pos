"""Offers are data (config/offers.json). This module turns the loose JSON into validated rules
and decides which apply to a cart.

Semantics (documented in docs/DECISIONS.md):
  * An offer applies on its dates (inclusive), when `active`, when every condition holds.
  * `stackable: false` means the offer is used alone. Stackable offers combine with each other.
    The candidates are therefore: each eligible non-stackable offer on its own, and the set of all
    eligible stackable offers together (adding a stackable offer never lowers the saving).
  * Policy `best_for_customer` (default) picks the candidate that saves the customer the most;
    ties go to the candidate holding the lower `priority` number. Policy `priority_first` picks the
    candidate holding the lowest priority number. The policy is a setting, not code.
  * Within a stack, offers apply in priority order (lower number first), each on what is left of
    the line after earlier offers. A discount never takes a line below zero.
  * Percent discounts round half-up per line. Flat discounts on a pair or cart are split across the
    lines pro rata to their remaining value (largest remainder, exact to the paisa), because GST is
    computed per line and the split decides the tax.
  * Offers that need a role (manager override) are only considered when a manager explicitly asks,
    with a reason code; they then compete like any other offer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .errors import InvalidRequest, PermissionDenied
from .money import allocate, format_inr, percent_of
from .optics import normalize_index
from .parties import Staff

SCOPES = ("frame", "lens", "pair", "cart")
POLICIES = ("best_for_customer", "priority_first")
_TOP_KEYS = {"offer_id", "name", "active", "valid_from", "valid_to", "stackable", "priority", "rule", "description"}
_RULE_KEYS = {"applies_to", "condition", "discount", "max_discount_paise", "requires"}
_COMMON_CONDITIONS = {"customer_tag", "min_pairs_in_cart"}
_SCOPE_CONDITIONS = {
    "frame": _COMMON_CONDITIONS,
    "lens": _COMMON_CONDITIONS | {"lens_type", "index_min", "coating"},
    "pair": _COMMON_CONDITIONS | {"has_frame_and_lens"},
    "cart": _COMMON_CONDITIONS,
}
_DISCOUNT_KEYS = {"type", "value_bps", "value_paise", "value_bps_max", "on"}
_TARGET_MODIFIERS = {"lowest_priced_lens_line": "lens"}


@dataclass(frozen=True)
class OfferLine:
    """The view of a cart line the offer engine needs, nothing more."""
    line_ref: str
    pair_ref: int
    kind: str  # frame | lens
    mrp: int
    lens_type: Optional[str] = None
    index: Optional[str] = None
    coating: Optional[str] = None


@dataclass(frozen=True)
class Conditions:
    customer_tag: Optional[str] = None
    min_pairs_in_cart: Optional[int] = None
    lens_type: Optional[str] = None
    index_min: Optional[str] = None
    coating: Optional[str] = None
    has_frame_and_lens: Optional[bool] = None


@dataclass(frozen=True)
class Discount:
    kind: str  # percent | flat | manual_percent
    value: int  # bps for percent, paise for flat, max bps for manual_percent
    on: Optional[str] = None


@dataclass(frozen=True)
class Offer:
    offer_id: str
    name: str
    active: bool
    valid_from: date
    valid_to: date
    stackable: bool
    priority: int
    scope: str
    conditions: Conditions
    discount: Discount
    max_discount: Optional[int] = None
    requires_role: Optional[str] = None
    requires_reason_code: bool = False

    @property
    def is_manual(self) -> bool:
        return self.requires_role is not None


@dataclass(frozen=True)
class ManualDiscount:
    offer_id: str
    bps: int
    approver: Staff
    reason_code: str


@dataclass(frozen=True)
class OfferContext:
    today: date
    customer_tags: FrozenSet[str] = frozenset()
    store_code: Optional[str] = None
    manual: Optional[ManualDiscount] = None


@dataclass(frozen=True)
class OfferAllocation:
    line_ref: str
    offer_id: str
    amount: int


@dataclass(frozen=True)
class OfferDecision:
    offer_id: str
    name: str
    applied: bool
    amount: int
    why: str

    def to_dict(self) -> dict:
        return {"offer_id": self.offer_id, "name": self.name, "applied": self.applied, "amount_paise": self.amount, "why": self.why}


@dataclass(frozen=True)
class OfferEvaluation:
    allocations: Tuple[OfferAllocation, ...] = ()
    decisions: Tuple[OfferDecision, ...] = ()
    policy: str = "best_for_customer"
    manual: Optional[ManualDiscount] = None

    @property
    def total(self) -> int:
        return sum(a.amount for a in self.allocations)

    def for_line(self, line_ref: str) -> List[Tuple[str, int]]:
        return [(a.offer_id, a.amount) for a in self.allocations if a.line_ref == line_ref and a.amount]

    @property
    def applied(self) -> List[OfferDecision]:
        return [d for d in self.decisions if d.applied]


class OfferConfigError(Exception):
    pass


def _parse_date(v, what: str) -> date:
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise OfferConfigError("%s must be YYYY-MM-DD, got %r" % (what, v))


def _int(v, what: str, lo: int = 0, hi: Optional[int] = None) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise OfferConfigError("%s must be an integer, got %r" % (what, v))
    if v < lo or (hi is not None and v > hi):
        raise OfferConfigError("%s=%d out of range" % (what, v))
    return v


def parse_offer(raw: dict) -> Offer:
    if not isinstance(raw, dict):
        raise OfferConfigError("offer must be an object")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise OfferConfigError("unknown keys %s" % sorted(unknown))
    oid = raw.get("offer_id")
    if not isinstance(oid, str) or not oid:
        raise OfferConfigError("offer_id is required")
    rule = raw.get("rule")
    if not isinstance(rule, dict):
        raise OfferConfigError("rule is required")
    unknown = set(rule) - _RULE_KEYS
    if unknown:
        raise OfferConfigError("unknown rule keys %s" % sorted(unknown))
    scope = rule.get("applies_to")
    if scope not in SCOPES:
        raise OfferConfigError("applies_to must be one of %s, got %r" % (SCOPES, scope))

    cond_raw = rule.get("condition") or {}
    unknown = set(cond_raw) - _SCOPE_CONDITIONS[scope]
    if unknown:
        raise OfferConfigError("conditions %s are not supported for scope %r" % (sorted(unknown), scope))
    if "has_frame_and_lens" in cond_raw and not isinstance(cond_raw["has_frame_and_lens"], bool):
        raise OfferConfigError("has_frame_and_lens must be true/false")
    conditions = Conditions(
        customer_tag=cond_raw.get("customer_tag"),
        min_pairs_in_cart=_int(cond_raw["min_pairs_in_cart"], "min_pairs_in_cart", 1) if "min_pairs_in_cart" in cond_raw else None,
        lens_type=cond_raw.get("lens_type"),
        index_min=normalize_index(cond_raw["index_min"]) if "index_min" in cond_raw else None,
        coating=cond_raw.get("coating"),
        has_frame_and_lens=cond_raw.get("has_frame_and_lens"),
    )

    d = rule.get("discount")
    if not isinstance(d, dict):
        raise OfferConfigError("discount is required")
    unknown = set(d) - _DISCOUNT_KEYS
    if unknown:
        raise OfferConfigError("unknown discount keys %s" % sorted(unknown))
    on = d.get("on")
    if on is not None and _TARGET_MODIFIERS.get(on) != scope:
        raise OfferConfigError("discount.on=%r is not valid for scope %r" % (on, scope))
    requires = rule.get("requires") or {}
    if set(requires) - {"role", "reason_code"}:
        raise OfferConfigError("unknown requires keys %s" % sorted(set(requires) - {"role", "reason_code"}))
    if d.get("type") == "percent" and "value_bps" in d:
        discount = Discount("percent", _int(d["value_bps"], "value_bps", 1, 10000), on)
    elif d.get("type") == "percent" and "value_bps_max" in d:
        if not requires.get("role"):
            raise OfferConfigError("an open-ended percent (value_bps_max) needs requires.role: no manual discount without an approver")
        discount = Discount("manual_percent", _int(d["value_bps_max"], "value_bps_max", 1, 10000), on)
    elif d.get("type") == "flat" and "value_paise" in d:
        discount = Discount("flat", _int(d["value_paise"], "value_paise", 1), on)
    else:
        raise OfferConfigError("discount must be percent(value_bps | value_bps_max) or flat(value_paise), got %r" % d)

    valid_from = _parse_date(raw.get("valid_from"), "valid_from")
    valid_to = _parse_date(raw.get("valid_to"), "valid_to")
    if valid_from > valid_to:
        raise OfferConfigError("valid_from is after valid_to")
    for k in ("active", "stackable"):
        if not isinstance(raw.get(k), bool):
            raise OfferConfigError("%s must be true/false" % k)
    return Offer(
        offer_id=oid, name=str(raw.get("name") or oid), active=raw["active"], valid_from=valid_from, valid_to=valid_to,
        stackable=raw["stackable"], priority=_int(raw.get("priority"), "priority"), scope=scope, conditions=conditions,
        discount=discount,
        max_discount=_int(rule["max_discount_paise"], "max_discount_paise", 1) if "max_discount_paise" in rule else None,
        requires_role=requires.get("role"), requires_reason_code=bool(requires.get("reason_code")),
    )


@dataclass
class OfferBook:
    offers: List[Offer] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)  # (offer_id, why)

    @classmethod
    def from_json(cls, raw_offers: Iterable[dict]) -> "OfferBook":
        book = cls()
        seen = set()
        for raw in raw_offers:
            oid = raw.get("offer_id", "?") if isinstance(raw, dict) else "?"
            try:
                offer = parse_offer(raw)
                if offer.offer_id in seen:
                    raise OfferConfigError("duplicate offer_id")
                seen.add(offer.offer_id)
                book.offers.append(offer)
            except OfferConfigError as e:
                book.rejected.append((oid, str(e)))
        return book

    def get(self, offer_id: str) -> Optional[Offer]:
        return next((o for o in self.offers if o.offer_id == offer_id), None)


@dataclass
class _Outcome:
    offers: List[Offer]
    allocations: List[OfferAllocation]
    per_offer: Dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.per_offer.values())

    @property
    def min_priority(self) -> int:
        return min(o.priority for o in self.offers)

    @property
    def label(self) -> str:
        return " + ".join(o.offer_id for o in self.offers)


class OfferEngine:
    def __init__(self, book: OfferBook, policy: str = "best_for_customer",
                 override_reason_codes: Sequence[str] = ()):
        if policy not in POLICIES:
            raise ValueError("offer policy must be one of %s" % (POLICIES,))
        self.book = book
        self.policy = policy
        self.override_reason_codes = set(override_reason_codes)

    # ---------------------------------------------------------------- eligibility
    def _check_manual(self, ctx: OfferContext) -> Optional[Offer]:
        m = ctx.manual
        if m is None:
            return None
        offer = self.book.get(m.offer_id)
        if offer is None or not offer.is_manual:
            raise InvalidRequest("%s is not a manual discount offer." % m.offer_id, code="bad_manual_offer")
        if offer.requires_role and (m.approver.role != offer.requires_role or not m.approver.can_override_discount):
            raise PermissionDenied("%s (%s) cannot approve %s; it needs a %s." % (m.approver.staff_id, m.approver.role, offer.offer_id, offer.requires_role),
                                   code="approver_not_allowed")
        if ctx.store_code and m.approver.store_code != ctx.store_code:
            raise PermissionDenied("Approver %s belongs to %s, not %s." % (m.approver.staff_id, m.approver.store_code, ctx.store_code), code="approver_wrong_store")
        if offer.requires_reason_code and (not m.reason_code or (self.override_reason_codes and m.reason_code not in self.override_reason_codes)):
            raise InvalidRequest("%s needs a reason code, one of: %s." % (offer.offer_id, ", ".join(sorted(self.override_reason_codes))), code="reason_code_required")
        if not (0 < m.bps <= offer.discount.value):
            raise InvalidRequest("%s allows up to %s%%, asked for %s%%." % (offer.offer_id, _pct(offer.discount.value), _pct(m.bps)), code="manual_discount_too_high")
        return offer

    def _ineligible(self, o: Offer, lines: Sequence[OfferLine], ctx: OfferContext) -> Optional[str]:
        if not o.active:
            return "inactive"
        if not (o.valid_from <= ctx.today <= o.valid_to):
            return "not valid on %s (runs %s to %s)" % (ctx.today, o.valid_from, o.valid_to)
        if o.is_manual and (ctx.manual is None or ctx.manual.offer_id != o.offer_id):
            return "manual discount; only with a %s's approval and a reason code" % (o.requires_role or "manager")
        c = o.conditions
        if c.customer_tag and c.customer_tag not in ctx.customer_tags:
            return "customer is not tagged %s" % c.customer_tag
        if c.min_pairs_in_cart:
            pairs = len({l.pair_ref for l in lines if l.kind == "lens"})
            if pairs < c.min_pairs_in_cart:
                return "needs %d pairs with lenses, cart has %d" % (c.min_pairs_in_cart, pairs)
        return None

    def _targets(self, o: Offer, lines: Sequence[OfferLine]) -> List[List[str]]:
        """Groups of line refs the discount is computed over (one group per line, pair or cart)."""
        c = o.conditions
        if o.scope == "cart":
            return [[l.line_ref for l in lines]] if lines else []
        if o.scope == "frame":
            return [[l.line_ref] for l in lines if l.kind == "frame"]
        if o.scope == "lens":
            def ok(l: OfferLine) -> bool:
                return (l.kind == "lens"
                        and (c.lens_type is None or l.lens_type == c.lens_type)
                        and (c.coating is None or l.coating == c.coating)
                        and (c.index_min is None or (l.index is not None and Decimal(l.index) >= Decimal(c.index_min))))
            matched = [l for l in lines if ok(l)]
            if o.discount.on == "lowest_priced_lens_line" and matched:
                # the cheaper lens is the "second pair"; on a tie the later pair is the second one
                matched = [min(matched, key=lambda l: (l.mrp, -l.pair_ref))]
            return [[l.line_ref] for l in matched]
        groups: Dict[int, List[OfferLine]] = {}
        for l in lines:
            groups.setdefault(l.pair_ref, []).append(l)
        out = []
        for _, ls in sorted(groups.items()):
            kinds = {l.kind for l in ls}
            if c.has_frame_and_lens and not {"frame", "lens"} <= kinds:
                continue
            out.append([l.line_ref for l in ls])
        return out

    # ---------------------------------------------------------------- application
    def _apply(self, offers: List[Offer], targets: Dict[str, List[List[str]]], lines: Sequence[OfferLine],
               ctx: OfferContext) -> _Outcome:
        remaining = {l.line_ref: l.mrp for l in lines}
        allocations: List[OfferAllocation] = []
        per_offer: Dict[str, int] = {}
        for o in sorted(offers, key=lambda o: (o.priority, o.offer_id)):
            amounts: Dict[str, int] = {}
            for group in targets[o.offer_id]:
                if o.discount.kind in ("percent", "manual_percent"):
                    bps = o.discount.value if o.discount.kind == "percent" else ctx.manual.bps  # type: ignore[union-attr]
                    for ref in group:
                        amounts[ref] = amounts.get(ref, 0) + percent_of(remaining[ref], bps)
                else:
                    pool = sum(remaining[r] for r in group)
                    for ref, amt in zip(group, allocate(min(o.discount.value, pool), [remaining[r] for r in group])):
                        amounts[ref] = amounts.get(ref, 0) + amt
            refs = list(amounts)
            if o.max_discount is not None and sum(amounts.values()) > o.max_discount:
                amounts = dict(zip(refs, allocate(o.max_discount, [amounts[r] for r in refs])))
            for ref in refs:
                amt = min(amounts[ref], remaining[ref])
                if amt:
                    remaining[ref] -= amt
                    allocations.append(OfferAllocation(ref, o.offer_id, amt))
                per_offer[o.offer_id] = per_offer.get(o.offer_id, 0) + amt
        return _Outcome(offers, allocations, per_offer)

    def _pick(self, outcomes: List[_Outcome]) -> _Outcome:
        if self.policy == "priority_first":
            return min(outcomes, key=lambda r: (r.min_priority, -r.total, len(r.offers), r.label))
        return min(outcomes, key=lambda r: (-r.total, r.min_priority, len(r.offers), r.label))

    def evaluate(self, lines: Sequence[OfferLine], ctx: OfferContext) -> OfferEvaluation:
        self._check_manual(ctx)
        decisions: Dict[str, OfferDecision] = {}
        eligible: List[Offer] = []
        targets: Dict[str, List[List[str]]] = {}
        for o in sorted(self.book.offers, key=lambda o: (o.priority, o.offer_id)):
            why = self._ineligible(o, lines, ctx)
            if why is None:
                t = self._targets(o, lines)
                if not t:
                    why = "no qualifying %s in the cart" % {"frame": "frame", "lens": "lens", "pair": "frame + lens pair", "cart": "line"}[o.scope]
                else:
                    targets[o.offer_id] = t
            if why:
                decisions[o.offer_id] = OfferDecision(o.offer_id, o.name, False, 0, why)
            else:
                eligible.append(o)
        if not eligible:
            return OfferEvaluation((), tuple(decisions[o.offer_id] for o in self._ordered()), self.policy, ctx.manual)

        stack = [o for o in eligible if o.stackable]
        candidates = [[o] for o in eligible if not o.stackable] + ([stack] if stack else [])
        outcomes = [self._apply(c, targets, lines, ctx) for c in candidates]
        best = self._pick(outcomes)
        chosen_ids = {o.offer_id for o in best.offers}
        for out in outcomes:
            for o in out.offers:
                amt = out.per_offer.get(o.offer_id, 0)
                if o.offer_id in chosen_ids:
                    why = ("applied" + (", capped at %s" % _rs(o.max_discount) if o.max_discount and amt == o.max_discount else "")) if amt else "nothing left to discount after higher-priority offers"
                    decisions[o.offer_id] = OfferDecision(o.offer_id, o.name, amt > 0, amt, why)
                elif o.offer_id not in decisions:
                    decisions[o.offer_id] = OfferDecision(o.offer_id, o.name, False, 0, self._lost_why(o, out, best))
        return OfferEvaluation(tuple(best.allocations), tuple(decisions[o.offer_id] for o in self._ordered()), self.policy, ctx.manual)

    def _lost_why(self, o: Offer, out: _Outcome, best: _Outcome) -> str:
        rule = "does not combine with other offers" if not o.stackable else "%s cannot be combined with it" % best.label
        if self.policy == "priority_first":
            return "%s; %s has higher priority (%s vs %s)" % (rule, best.label, _rs(best.total), _rs(out.total))
        return "%s; %s saves the customer more (%s vs %s)" % (rule, best.label, _rs(best.total), _rs(out.total))

    def _ordered(self) -> List[Offer]:
        return sorted(self.book.offers, key=lambda o: (o.priority, o.offer_id))


def _rs(paise: Optional[int]) -> str:
    return "Rs %s" % format_inr(paise or 0)


def _pct(bps: int) -> str:
    return str(Decimal(bps) / 100).rstrip("0").rstrip(".") if bps % 100 else str(bps // 100)
