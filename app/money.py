"""
Money handling for the reconciliation platform.

FINANCIAL INTEGRITY RULE
------------------------
Money is NEVER stored or computed as a float. Every monetary value in this
system is an integer number of *paise* (1/100 of a rupee). Floats introduce
representation error which, in a reconciliation system, silently manufactures
discrepancies -- exactly the class of bug this platform exists to detect.

All database columns holding money are ``BigInteger`` and are suffixed
``_paise`` so that the unit is unambiguous at every call site.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Sequence

# Default currency. The architecture supports multi-currency (spec s72); the
# currency code lives on Organization and travels with formatted output.
DEFAULT_CURRENCY = "INR"

CURRENCY_SYMBOLS = {
    "INR": "\u20b9",
    "USD": "$",
    "EUR": "\u20ac",
    "GBP": "\u00a3",
    "AED": "AED ",
    "NPR": "NPR ",
}

_CLEAN_RE = re.compile(r"[^0-9.\-+]")
# Trailing CR/DR markers used by Indian bank statements.
_CR_RE = re.compile(r"\b(cr|credit)\b\.?$", re.I)
_DR_RE = re.compile(r"\b(dr|debit)\b\.?$", re.I)


class MoneyError(ValueError):
    """Raised when a monetary string cannot be parsed unambiguously."""


def to_paise(value) -> int:
    """Convert a rupee-denominated value to integer paise.

    Accepts int, float (discouraged but tolerated at the boundary), Decimal or
    str. Raises :class:`MoneyError` rather than guessing when the input is not
    a coherent amount -- the system must never invent a financial value.
    """
    if value is None:
        raise MoneyError("Cannot convert None to an amount")
    if isinstance(value, int) and not isinstance(value, bool):
        return int(Decimal(value) * 100)
    if isinstance(value, Decimal):
        return int(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    if isinstance(value, float):
        dec = Decimal(str(value))
        return int(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    if isinstance(value, str):
        dec = parse_amount_decimal(value)
        return int(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    raise MoneyError(f"Unsupported amount type: {type(value)!r}")


def parse_amount_decimal(raw: str) -> Decimal:
    """Parse a human/bank-written amount string into a Decimal of rupees.

    Handles the messy reality of Indian bank statements and OCR output:
      - thousands separators, including the Indian lakh/crore grouping
        (``1,23,456.78``)
      - a currency symbol or ``Rs.``/``INR`` prefix
      - trailing ``Cr`` / ``Dr`` markers (the sign is returned; direction is
        decided by the caller, which has more context)
      - parenthesised negatives ``(1,200.00)``
      - OCR digit confusion for the decimal separator
    """
    if raw is None:
        raise MoneyError("Cannot parse empty amount")
    text = str(raw).strip()
    if not text:
        raise MoneyError("Cannot parse empty amount")

    negative = False

    # Parenthesised negative, e.g. "(1,200.00)"
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    # Trailing Cr/Dr markers. Dr implies a negative/outflow magnitude.
    if _DR_RE.search(text):
        negative = True
    text = _CR_RE.sub("", text)
    text = _DR_RE.sub("", text)

    # Strip currency markers.
    text = re.sub(r"(?i)\b(rs|inr|rupees)\b\.?", "", text)
    for sym in CURRENCY_SYMBOLS.values():
        text = text.replace(sym, "")
    text = text.strip()

    if text.startswith("-"):
        negative = True
    text = _CLEAN_RE.sub("", text)
    text = text.lstrip("+-")

    if not text:
        raise MoneyError(f"Cannot parse amount from {raw!r}")

    # More than one dot: treat all but the last as noise separators
    # (OCR frequently renders "1,234.00" as "1.234.00").
    if text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail

    # A trailing group of exactly 3 digits after a dot is a thousands
    # separator, not a decimal fraction ("1.234" -> 1234) ONLY when the
    # original string also contained a comma-grouped form. Without that
    # signal we must not guess: treat the dot as a decimal point.
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyError(f"Cannot parse amount from {raw!r}") from exc

    return -dec if negative else dec


def try_to_paise(value) -> Optional[int]:
    """Non-raising variant. Returns ``None`` when the value is unparseable.

    Used by document extraction, where an unreadable field must be recorded as
    *unknown* rather than coerced into a number.
    """
    try:
        return to_paise(value)
    except (MoneyError, InvalidOperation):
        return None


def to_rupees(paise: Optional[int]) -> Decimal:
    """Convert integer paise into a Decimal of rupees."""
    if paise is None:
        return Decimal("0.00")
    return (Decimal(int(paise)) / 100).quantize(Decimal("0.01"))


def format_money(paise: Optional[int], currency: str = DEFAULT_CURRENCY,
                 with_symbol: bool = True) -> str:
    """Format paise for display using the Indian digit grouping system.

    ``1234567890`` paise -> ``\u20b91,23,45,678.90``
    """
    if paise is None:
        return "\u2014"  # em dash: "not known", distinct from zero
    negative = paise < 0
    rupees = to_rupees(abs(int(paise)))
    whole, _, frac = f"{rupees:.2f}".partition(".")

    if currency == "INR":
        grouped = _indian_group(whole)
    else:
        grouped = f"{int(whole):,}"

    symbol = CURRENCY_SYMBOLS.get(currency, currency + " ") if with_symbol else ""
    sign = "-" if negative else ""
    return f"{sign}{symbol}{grouped}.{frac}"


def _indian_group(whole: str) -> str:
    """Group digits as 2,2,3 from the right (Indian numbering system)."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def paise_sum(values: Sequence[Optional[int]]) -> int:
    """Sum a sequence of paise values, treating ``None`` as zero."""
    return sum(int(v) for v in values if v is not None)


def split_evenly(total_paise: int, parts: int) -> list[int]:
    """Split an amount into ``parts`` whole-paise shares that sum EXACTLY
    to ``total_paise``.

    Remainder paise are distributed to the leading shares so that the
    allocation invariant (sum of allocations == transaction amount) can never
    be broken by rounding. See spec s69 (double-counting protection).
    """
    if parts <= 0:
        raise MoneyError("Cannot split into a non-positive number of parts")
    base, remainder = divmod(int(total_paise), parts)
    shares = [base] * parts
    for i in range(abs(remainder)):
        shares[i] += 1 if remainder > 0 else -1
    assert sum(shares) == int(total_paise), "split_evenly broke the sum invariant"
    return shares
