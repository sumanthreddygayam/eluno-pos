"""Errors the core raises. Each carries a stable `code` and a message sales staff can act on."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class DomainError(Exception):
    code = "domain_error"
    http_status = 422

    def __init__(self, message: str, *, code: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class InvalidRequest(DomainError):
    code = "invalid_request"
    http_status = 400


class PermissionDenied(DomainError):
    code = "permission_denied"
    http_status = 403


class StateConflict(DomainError):
    """The operation is not allowed in the object's current state."""
    code = "state_conflict"
    http_status = 409


class ConfigError(DomainError):
    code = "config_error"
    http_status = 500


class BillBlocked(DomainError):
    """The cart has blocking issues (prescription, stock, ...). `issues` lists each with its fix."""
    code = "bill_blocked"

    def __init__(self, message: str, issues: List[Any]):
        super().__init__(message, details={"issues": [i.to_dict() for i in issues]})
        self.issues = issues


class PaymentFailed(DomainError):
    code = "payment_failed"
    http_status = 402
