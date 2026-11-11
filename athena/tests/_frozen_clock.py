"""Freeze ``utils.deadlines``' clock — the one seam of « today » — in a test.

``utils.deadlines`` reads the instant through its module-level name
``datetime`` (``today_mtl``, ``today_utc``, ``now_utc``), so a test swaps
that name for a subclass whose ``now`` answers a fixed instant. A BARE
subclass breaks the module's other use of the name: ``_as_date`` asks
``isinstance(value, datetime)`` of a stored date-only value, and a Firestore
``DatetimeWithNanoseconds`` is no instance of the test's subclass — it then
passes for a ``date`` and fails to compare with one (« '<' not supported »).
Lateness reads stored due dates through it since 2026-11-11
(``models/invoice.is_overdue``), so every test that freezes the clock over
an invoice needs the class below, whose ``isinstance`` stays true for every
real ``datetime``.

This file is test support: its name does not match ``test_*.py``.
"""

from datetime import datetime

_REAL_DATETIME = datetime


class _FrozenMeta(type):
    """``isinstance(x, frozen)`` is ``isinstance(x, datetime)`` — the frozen
    class only answers ``now`` differently."""

    def __instancecheck__(cls, instance) -> bool:
        return isinstance(instance, _REAL_DATETIME)

    def __subclasscheck__(cls, subclass) -> bool:
        return issubclass(subclass, _REAL_DATETIME)


def frozen_datetime(instant: datetime) -> type:
    """A ``datetime`` subclass whose ``now`` answers *instant* (tz-aware),
    converted to the zone asked for."""
    if instant.tzinfo is None:
        raise ValueError("the frozen instant must be tz-aware")

    class _Clock(datetime, metaclass=_FrozenMeta):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    return _Clock


def freeze_deadlines(monkeypatch, instant: datetime) -> None:
    """Every « today » and « now » of ``utils.deadlines`` reads *instant*
    for the rest of the test."""
    from utils import deadlines

    monkeypatch.setattr(deadlines, "datetime", frozen_datetime(instant))
