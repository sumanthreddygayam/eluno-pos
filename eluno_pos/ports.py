"""The boundary. The application layer talks only to these interfaces; adapters implement them.

Replacing Shopify in 2027 means writing another `CommercePlatform`, nothing in `domain/` or
`app/` changes. The same holds for the store tool and the payment terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from .domain.bill import Bill
from .domain.catalog import Catalog
from .domain.dayend import SaleRecord
from .domain.parties import Customer, Staff, Store
from .domain.prescription import Prescription
from .domain.refund import Refund

try:  # Python 3.8+
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore


# ---------------------------------------------------------------- integration errors
class IntegrationError(Exception):
    """An external system failed. `retryable` says whether trying again later can succeed."""

    def __init__(self, service: str, message: str, status: Optional[int] = None, body=None, retryable: bool = True):
        super().__init__("%s: %s" % (service, message))
        self.service, self.status, self.body, self.retryable = service, status, body, retryable


class UpstreamRejected(IntegrationError):
    """4xx: the request is wrong; retrying the same request will fail again."""

    def __init__(self, service: str, message: str, status: int, body=None):
        super().__init__(service, message, status, body, retryable=False)


class AmbiguousWrite(IntegrationError):
    """A write whose outcome is unknown (timeout, connection drop, 5xx after sending).
    The caller must find out whether it happened before trying again."""


# ---------------------------------------------------------------- DTOs crossing the boundary
@dataclass(frozen=True)
class OrderReceipt:
    order_id: int
    order_name: str
    sale_txn_id: Optional[int]
    line_ids: Dict[str, int]  # our line_ref -> platform line id
    adopted: bool = False  # True when an earlier attempt had already created it


@dataclass(frozen=True)
class RefundReceipt:
    refund_id: int
    adopted: bool = False


@dataclass(frozen=True)
class RefundQuote:
    maximum_refundable: int
    lines: Dict[int, Tuple[int, int]]  # platform line id -> (subtotal, tax)


@dataclass(frozen=True)
class Charge:
    charge_id: str
    status: str
    amount: int
    method: str
    refunded: int = 0


@dataclass(frozen=True)
class TerminalRefund:
    refund_id: str
    status: str
    amount: int
    recovered: bool = False  # True when found by checking the charge after an ambiguous call


# ---------------------------------------------------------------- ports
class CommercePlatform(Protocol):
    """System of record for customers, products, stock, orders and refunds (Shopify today)."""

    def find_customer_by_phone(self, phone: str) -> Optional[Customer]: ...
    def create_customer(self, first_name: str, last_name: str, phone: str, email: Optional[str], note: str) -> Customer: ...
    def stores(self) -> List[Store]: ...
    def catalog(self) -> Catalog: ...
    def stock_levels(self, inventory_item_ids: Sequence[int]) -> Dict[Tuple[int, int], int]: ...
    def create_order(self, bill: Bill, store: Store, may_exist: bool) -> OrderReceipt: ...
    def find_order(self, invoice_no: str, store: Store, around: Optional[date] = None) -> Optional[Bill]: ...
    def quote_refund(self, bill: Bill, refund: Refund) -> RefundQuote: ...
    def create_refund(self, bill: Bill, refund: Refund, may_exist: bool, reason_text: str = "") -> RefundReceipt: ...
    def sales_for_day(self, store: Store, day: date, refund_lookback_days: int) -> List[SaleRecord]: ...
    def highest_document_seq(self, store: Store, series: str) -> int: ...


class PrescriptionSource(Protocol):
    """Where prescriptions live (the store tool today)."""

    def prescriptions(self, phone: str) -> List[Prescription]: ...
    def add_prescription(self, rx: Prescription, recorded_by: str) -> Prescription: ...


class StaffDirectory(Protocol):
    def staff(self, store_code: str) -> List[Staff]: ...


class PaymentTerminal(Protocol):
    def charge(self, amount: int, method: str, order_ref: str) -> Charge: ...
    def get_charge(self, charge_id: str) -> Charge: ...
    def refund(self, charge_id: str, amount: int, reason: str) -> TerminalRefund: ...
