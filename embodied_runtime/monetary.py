"""Bounded decimal money and OpenRouter's allow-listed response accounting.

OpenRouter usage.cost is an account charge in USD-denominated credits. See
docs/openrouter-cost-accounting.md for sources and BYOK limitations.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Any
from urllib.parse import urlsplit


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def parse_usd(value: object) -> Decimal | None:
    """Accept finite nonnegative numbers, without unbounded parsing/formatting.

    Limit text to 128 characters, coefficients to 40 digits, exponents to
    [-24, 12], and amounts to less than 10**13 USD. Never coerce bool/objects.
    """
    if type(value) not in (str, int, float, Decimal):
        return None
    if type(value) is int and value.bit_length() > 140:
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        raw = str(value)
        if len(raw) > 128 or _NUMBER.fullmatch(raw) is None:
            return None
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return None
    if not amount.is_finite() or amount < 0:
        return None
    parts = amount.as_tuple()
    if len(parts.digits) > 40 or not -24 <= parts.exponent <= 12 or amount.adjusted() > 12:
        return None
    return amount.copy_abs()  # canonicalize negative decimal zero


def add_usd(left: Decimal, right: Decimal) -> Decimal:
    """Sum exactly, independent of the ambient decimal context."""
    with localcontext() as context:
        context.prec = max(left.adjusted(), right.adjusted(), 0) - min(
            left.as_tuple().exponent, right.as_tuple().exponent, 0) + 3
        return left + right


def usd_string(value: Decimal) -> str:
    """Preserve all monetary precision, with at least six fractional digits."""
    raw = format(value, "f")
    integer, _, fraction = raw.partition(".")
    return integer + "." + fraction.ljust(6, "0")


def _field(owner: Any, name: str) -> Any:
    if isinstance(owner, Mapping):
        return owner.get(name)
    value = getattr(owner, name, None)
    if value is None:
        # OpenAI SDK BaseModel stores provider extensions in model_extra.
        extra = getattr(owner, "model_extra", None)
        if isinstance(extra, Mapping):
            return extra.get(name)
    return value


def is_openrouter_endpoint(base_url: str) -> bool:
    """Only trust the official HTTPS API origin and API base path."""
    try:
        url = urlsplit(base_url)
        return (url.scheme == "https" and url.hostname == "openrouter.ai"
                and url.port in (None, 443) and url.path.rstrip("/") == "/api/v1"
                and url.username is None and url.password is None
                and not url.query and not url.fragment)
    except ValueError:
        return False


def openrouter_cost(response: Any, *, base_url: str) -> Decimal | None:
    """Extract only usage.cost; never upstream_inference_cost or other metadata."""
    if not is_openrouter_endpoint(base_url):
        return None
    return parse_usd(_field(_field(response, "usage"), "cost"))
