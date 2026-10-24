"""Invoice and credit-note numbers: one gapless series per store (per prefix), separate from
Shopify's global order_number. The prefix comes from the store's location data
(ELU/HSR/FY26/), so a new financial year is a data change that starts a new series."""
from __future__ import annotations

import re
from typing import Optional

INVOICE = "invoice"
CREDIT_NOTE = "credit_note"
SEQ_WIDTH = 5


def series_key(prefix: str, kind: str) -> str:
    return prefix + ("CN/" if kind == CREDIT_NOTE else "")


def format_number(series: str, seq: int) -> str:
    return "%s%0*d" % (series, SEQ_WIDTH, seq)


def parse_seq(number: str, series: str) -> Optional[int]:
    """'ELU/HSR/FY26/00007' with series 'ELU/HSR/FY26/' -> 7; anything else -> None."""
    if not number or not number.startswith(series):
        return None
    tail = number[len(series):]
    return int(tail) if re.fullmatch(r"\d+", tail) else None
