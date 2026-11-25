"""English-language formatting helpers for bilingual `.docx` generation.

Pure — no Firestore, no Flask, no models — matching :mod:`utils.format_fr`
for English-language templates and billing notices (Lot D, feature 1i).
"""

from datetime import date
from decimal import Decimal
from typing import Optional

_ENGLISH_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_date_en(value: date) -> str:
    """Format a date in North-American English long form (``November 24, 2026``)."""
    return f"{_ENGLISH_MONTHS[value.month - 1]} {value.day}, {value.year}"


def format_cents_en(cents: Optional[int]) -> str:
    """Format integer cents as English Canadian dollars (``150000`` → ``"$1,500.00"``)."""
    n = int(cents or 0)
    sign = "-" if n < 0 else ""
    whole, dec = divmod(abs(n), 100)
    return f"{sign}${whole:,}.{dec:02d}"


def format_cents_en_parens(cents: Optional[int]) -> str:
    """Parenthesized deduction in English (``115000`` → ``"($1,150.00)"``)."""
    return f"({format_cents_en(abs(int(cents or 0)))})"


def format_hours_en(hours: Optional[float]) -> str:
    """``0.5`` → ``"0.50"`` (two decimals, period separator, no unit)."""
    return f"{float(hours or 0):.2f}"


def format_rate_en(rate: int, scale: int) -> str:
    """A stored tax-rate integer as an English percentage (``500/100`` → ``"5%"``)."""
    pct = Decimal(int(rate)) / Decimal(int(scale))
    text = f"{pct:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"
