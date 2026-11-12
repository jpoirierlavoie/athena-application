"""Read-only preview: the invoices the first lateness sweep would mark today.

THIS SCRIPT READS PRODUCTION. Like the other Firestore scripts of
``scripts/``, it reaches Firestore through the Application Default
Credentials (``models.db``) — unless ``FIRESTORE_EMULATOR_HOST`` points at a
RUNNING emulator — and it WRITES NOTHING: no ``--apply``, no flag at all,
by design.

Lot 2 of « Factures : retard, radiation, intérêts » (2026-11-11) ships the
nightly sweep (``routes/taches_factures``) switched OFF
(``FACTURES_RETARD_ACTIF: "false"`` in app.yaml), because its first run
marks the whole backlog at once — the invoices imported from the previous
system included. This preview lists, before the switch goes on, exactly what
that run would mark on today's Montréal day: the SAME strict read
(``invoice.list_sent_invoices_strict``), the SAME rule
(``invoice.is_overdue`` through ``invoice.overdue_among``) and the SAME
order, longest-late first. A failed or truncated read raises: it never
prints « aucune ».

One line per invoice: its number, its due date as stored, that date
prorogued to the next business day on the civil calendar, the days late,
the balance and whether it was imported (``invoice.is_imported``) — never a
client's name. Then the count, the total balance, how many the first night
marks (``invoice.OVERDUE_SWEEP_CAP`` a run) and the ids whose lateness could
not be judged — over which it never says « aucune » unqualified: their
lateness is unknown, not absent. The lawyer reviews it; a one-line commit
then turns the switch on (DEPLOYMENT.md §15).

    python -m scripts.apercu_factures_echues
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional


def _stored_day(value: object) -> Optional[date]:
    """The calendar day a date-only field stands for — stored at midnight
    UTC, so its UTC date, never a Montréal conversion (Architecture Rule 5)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def preview_rows(invoices: list[dict], *, today: date) -> tuple[list[dict], list[str]]:
    """What the sweep would mark on *today*, in its order — PURE but for the
    model's log of an invoice it cannot judge. Each row: ``id``, ``number``,
    ``due`` (the stored due date's day), ``prorogued`` (the last day it was
    payable: that day moved to the next business day on the civil calendar —
    the eve of ``invoice.first_overdue_day``), ``days`` (late),
    ``balance_cents`` and ``imported``. Also the ids not judged."""
    from models import invoice as invoice_model

    late, unjudged = invoice_model.overdue_among(invoices, today=today)
    rows = []
    for invoice in late:
        rows.append({
            "id": str(invoice.get("id") or ""),
            "number": str(invoice.get("invoice_number") or ""),
            "due": _stored_day(invoice.get("due_date")),
            "prorogued": (invoice_model.first_overdue_day(invoice)
                          - timedelta(days=1)),
            "days": invoice_model.days_overdue(invoice, today=today),
            "balance_cents": invoice_model.balance_of(invoice),
            "imported": invoice_model.is_imported(invoice),
        })
    return rows, unjudged


def report(rows: list[dict], unjudged: list[str], *, today: date,
           seen: int, cap: int) -> list[str]:
    """The French lines the lawyer reads — PURE."""
    from utils.format_fr import format_cents_fr

    lines = [
        f"Aperçu du balayage des factures échues — {today.isoformat()} "
        "(jour de Montréal). LECTURE SEULE : rien n'a été écrit.",
        f"Factures envoyées lues : {seen}.",
        "",
    ]
    if not rows and not unjudged:
        lines.append("Aucune facture envoyée n'est en retard aujourd'hui : "
                     "le premier balayage n'en marquerait aucune.")
    elif not rows:
        # Never « none is late » over invoices it could not judge: their
        # lateness is unknown, not absent (review of lot 2).
        lines.append(
            f"Le retard de {len(unjudged)} facture(s) n'a pu être jugé "
            "(ci-dessous) ; aucune autre n'est en retard aujourd'hui : le "
            "premier balayage n'en marquerait aucune.")
    else:
        lines.append("Facture | échéance | reportée au | jours de retard | "
                     "solde | importée")
        for row in rows:
            lines.append(
                f"{row['number'] or '(sans n°)'} | "
                f"{row['due'].isoformat() if row['due'] else '—'} | "
                f"{row['prorogued'].isoformat() if row['prorogued'] else '—'} | "
                f"{row['days']} | {format_cents_fr(row['balance_cents'])} | "
                f"{'oui' if row['imported'] else 'non'}"
            )
        total = sum(row["balance_cents"] for row in rows)
        rest = max(0, len(rows) - cap)
        lines += [
            "",
            f"{len(rows)} facture(s) en retard, {format_cents_fr(total)} de "
            "solde en tout.",
            f"Le premier balayage en marquerait {min(len(rows), cap)} "
            f"(au plus {cap} par nuit, les plus anciennes d'abord)"
            + (f" ; {rest} attendrai{'en' if rest > 1 else ''}t les nuits "
               "suivantes." if rest else "."),
        ]
    if unjudged:
        lines += [
            "",
            f"{len(unjudged)} facture(s) dont le retard ne peut être jugé "
            "(un montant stocké illisible) — le balayage les compterait en "
            "erreur et n'y toucherait pas : " + ", ".join(unjudged) + ".",
        ]
    return lines


def main(argv: list[str]) -> int:
    if argv:
        print("Ce script ne prend aucun argument : il ne fait que lire.")
        return 2
    # Lazy: the Firestore client is built on first import of models.
    from models import invoice as invoice_model
    from utils import deadlines

    # The report is French and carries « — »: a Windows console in cp1252
    # would raise UnicodeEncodeError (scripts/exporter_plugin_athena's remedy).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # a wrapped stdout: cosmetic only, never data

    today = deadlines.today_mtl()
    invoices = invoice_model.list_sent_invoices_strict()
    rows, unjudged = preview_rows(invoices, today=today)
    for line in report(rows, unjudged, today=today, seen=len(invoices),
                       cap=invoice_model.OVERDUE_SWEEP_CAP):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
