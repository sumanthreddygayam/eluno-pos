"""Stores, staff and customers as the till sees them. No Shopify or SMT field names here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Tuple

from .errors import InvalidRequest

CHANNELS = ("walk_in", "whatsapp_booked", "online_pickup")


def normalize_phone(raw: str) -> str:
    """Accepts '+91 90000 00005', '9000000005', '919000000005' -> '+919000000005'."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit():
        raise InvalidRequest("phone %r is not a phone number" % raw, code="bad_phone")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) != 12 or not digits.startswith("91"):
        raise InvalidRequest("phone %r is not a 10-digit Indian mobile number" % raw, code="bad_phone")
    return "+" + digits


def validate_channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise InvalidRequest("channel must be one of %s, got %r" % (", ".join(CHANNELS), channel), code="bad_channel")
    return channel


@dataclass(frozen=True)
class Store:
    location_id: int
    store_code: str
    name: str
    address: str
    invoice_prefix: Optional[str]
    kind: str  # "store" | "warehouse"
    state_code: str = ""

    @property
    def is_billing_store(self) -> bool:
        return self.kind == "store" and bool(self.invoice_prefix)

    @property
    def credit_note_prefix(self) -> str:
        return (self.invoice_prefix or "") + "CN/"


@dataclass(frozen=True)
class Staff:
    staff_id: str
    name: str
    role: str  # sales | optometrist | store_manager
    store_code: str
    badges: Tuple[str, ...] = ()
    can_override_discount: bool = False

    @property
    def is_progressive_expert(self) -> bool:
        return "progressive_expert" in self.badges


@dataclass(frozen=True)
class Customer:
    customer_id: int
    first_name: str
    last_name: str
    phone: str
    tags: FrozenSet[str] = field(default_factory=frozenset)
    lifetime_value: int = 0

    @property
    def name(self) -> str:
        return ("%s %s" % (self.first_name, self.last_name)).strip()

    @property
    def is_priority(self) -> bool:
        return "priority" in self.tags

    @property
    def campaigns(self) -> List[str]:
        return sorted(t.split(":", 1)[1] for t in self.tags if t.startswith("campaign:"))

    def billing_flags(self) -> List[str]:
        """What the billing screen shows next to the name, same cohorts as the queue tool."""
        flags = []
        if self.is_priority:
            flags.append("PRIORITY")
        flags += ["CAMPAIGN:" + c for c in self.campaigns]
        if "family" in self.tags:
            flags.append("FAMILY")
        return flags
