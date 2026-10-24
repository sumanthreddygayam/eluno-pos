"""Two kinds of configuration:

  Settings        - where things are (URLs, database path). From environment variables.
  BusinessConfig  - what the business decided (tax rates, offers, reason codes, thresholds).
                    From JSON files in config/, validated at load. Changing a rate or an offer is
                    a data change, never a code change.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .domain.errors import ConfigError, InvalidRequest
from .domain.offers import OfferBook, OfferEngine
from .domain.tax import TaxTable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Settings:
    base_url: str = "http://127.0.0.1:8787"  # not "localhost": on Windows that tries IPv6 first; the mock is IPv4-only (+2 s per call)
    db_path: str = os.path.join(ROOT, "var", "eluno_pos.db")
    config_dir: str = os.path.join(ROOT, "config")
    http_timeout: float = 8.0
    business_date: Optional[str] = None  # demo/test only: pin the calendar date (YYYY-MM-DD); production leaves it unset

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls(base_url=os.environ.get("ELUNO_POS_BASE_URL", cls.base_url),
                db_path=os.environ.get("ELUNO_POS_DB", cls.db_path),
                config_dir=os.environ.get("ELUNO_POS_CONFIG_DIR", cls.config_dir),
                http_timeout=float(os.environ.get("ELUNO_POS_HTTP_TIMEOUT", cls.http_timeout)),
                business_date=os.environ.get("ELUNO_POS_BUSINESS_DATE") or None)
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s


def _load(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ConfigError("missing config file %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("%s is not valid JSON: %s" % (path, e))


@dataclass
class BusinessConfig:
    taxes: TaxTable
    offers: OfferBook
    reason_codes: Dict[str, Dict[str, str]]
    settings: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: str) -> "BusinessConfig":
        taxes = TaxTable.from_json(_load(os.path.join(config_dir, "tax_rates.json")))
        raw_offers = _load(os.path.join(config_dir, "offers.json"))
        if not isinstance(raw_offers, list):
            raise ConfigError("offers.json must be a list of offers")
        reasons = {k: v for k, v in _load(os.path.join(config_dir, "reason_codes.json")).items() if isinstance(v, dict)}
        settings = _load(os.path.join(config_dir, "pos_settings.json"))
        return cls(taxes=taxes, offers=OfferBook.from_json(raw_offers), reason_codes=reasons, settings=settings)

    def setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def offer_engine(self) -> OfferEngine:
        return OfferEngine(self.offers, policy=str(self.setting("offer_policy", "best_for_customer")),
                           override_reason_codes=list(self.reason_codes.get("discount_override", {})))

    def require_reason(self, kind: str, code: str) -> str:
        allowed = self.reason_codes.get(kind, {})
        if code not in allowed:
            raise InvalidRequest("Reason code %r is not valid for %s. Use one of: %s." % (code, kind.replace("_", " "), ", ".join(sorted(allowed))),
                                 code="bad_reason_code")
        return code

    def problems(self) -> List[Tuple[str, str]]:
        """Everything a deploy-time config check should fail on."""
        out = [("offer:%s" % oid, why) for oid, why in self.offers.rejected]
        if self.setting("offer_policy", "best_for_customer") not in ("best_for_customer", "priority_first"):
            out.append(("pos_settings", "offer_policy must be best_for_customer or priority_first"))
        for needed in ("discount_override", "refund", "remake", "remake_cost_bearer", "paid_out", "paid_in", "void"):
            if not self.reason_codes.get(needed):
                out.append(("reason_codes", "no codes for %s" % needed))
        return out
