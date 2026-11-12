"""Read-only preview: the invoices the NEXT lateness sweep would mark.

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
the NEXT run would mark: the SAME strict read
(``invoice.list_sent_invoices_strict``), the SAME rule
(``invoice.is_overdue`` through ``invoice.overdue_among``) and the SAME
order, longest-late first — judged on THAT run's day, never on the day the
preview runs. The sweep runs at :data:`SWEEP_TIME`, Montréal time
(cron.yaml), and reads its own « today »: an invoice payable all day today
is late by tonight's run (:func:`next_sweep_night`). The preview used to
judge its own day, and said « none » of a night that marked one (second
review of lot 2). A failed or truncated read raises: it never prints
« aucune ».

That night is the switch's first only if the switch is on by then. Turned on
a later night, its first run also marks the invoices fallen due in between:
the report says so last, and the runbook runs the preview on the day the
switch commit is pushed (DEPLOYMENT.md §15).

One line per invoice: its number, its due date as stored, that date
prorogued to the next business day on the civil calendar, the days late that
night, the balance and whether it was imported (``invoice.is_imported``) —
never a client's name. Then the count, the total balance, how many are late
from that night only (payable until its eve), how many that run marks
(``invoice.OVERDUE_SWEEP_CAP`` a run) and the ids whose lateness could not
be judged — over which it never says « aucune » unqualified: their lateness
is unknown, not absent. The lawyer reviews it; a one-line commit then turns
the switch on (DEPLOYMENT.md §15).

    python -m scripts.apercu_factures_echues
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from tz import to_mtl

#: The hour of the sweep's job — cron.yaml's ``every day 00:20``,
#: America/Montreal — from which :func:`next_sweep_night` counts. Restated
#: for the computation; tests/test_taches_factures pins it to cron.yaml.
SWEEP_TIME = time(0, 20)

#: The last line of every report: what a switch turned on later changes.
_LATER_SWITCH = (
    "Ce balayage ne marque que si l'interrupteur FACTURES_RETARD_ACTIF est "
    "allumé d'ici là. Allumé une nuit plus tard, son premier balayage "
    "marquera aussi les factures devenues échues entre-temps : relancez cet "
    "aperçu le jour même de l'envoi de l'interrupteur.")


def next_sweep_night(now: datetime) -> date:
    """The day the first sweep run after the instant *now* judges: the
    Montréal day of the next :data:`SWEEP_TIME`, since a run reads its own
    ``today_mtl`` — *now*'s own Montréal day before that hour, the next one
    from it. PURE. *now* is tz-aware (``utils.deadlines.now_utc``) and read
    on Montréal's clock, never UTC's, whose day turns over at 19:00 (20:00
    in summer) there."""
    local = to_mtl(now)
    day = local.date()
    return day if local.time() < SWEEP_TIME else day + timedelta(days=1)


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


def preview_rows(invoices: list[dict], *, night: date) -> tuple[list[dict], list[str]]:
    """What the sweep run of *night* would mark — the day that run judges
    (:func:`next_sweep_night`) — in its order, PURE but for the model's log
    of an invoice it cannot judge. Each row: ``id``, ``number``, ``due`` (the
    stored due date's day), ``prorogued`` (the last day it was payable: that
    day moved to the next business day on the civil calendar — the eve of
    ``invoice.first_overdue_day``), ``days`` (late, that night),
    ``balance_cents`` and ``imported``. Also the ids not judged."""
    from models import invoice as invoice_model

    late, unjudged = invoice_model.overdue_among(invoices, today=night)
    rows = []
    for invoice in late:
        rows.append({
            "id": str(invoice.get("id") or ""),
            "number": str(invoice.get("invoice_number") or ""),
            "due": _stored_day(invoice.get("due_date")),
            "prorogued": (invoice_model.first_overdue_day(invoice)
                          - timedelta(days=1)),
            "days": invoice_model.days_overdue(invoice, today=night),
            "balance_cents": invoice_model.balance_of(invoice),
            "imported": invoice_model.is_imported(invoice),
        })
    return rows, unjudged


def report(rows: list[dict], unjudged: list[str], *, night: date,
           seen: int, cap: int) -> list[str]:
    """The French lines the lawyer reads — PURE. *night* is the day the
    predicted run judges (:func:`next_sweep_night`), named first; what a
    switch turned on a later night changes comes last."""
    from utils.format_fr import format_cents_fr

    lines = [
        "Aperçu du prochain balayage des factures échues : la nuit du "
        f"{night.isoformat()}, à {SWEEP_TIME.hour:02d} h "
        f"{SWEEP_TIME.minute:02d} (heure de Montréal). LECTURE SEULE : rien "
        "n'a été écrit.",
        f"Factures envoyées lues : {seen}.",
        "",
    ]
    if not rows and not unjudged:
        lines.append("Aucune facture envoyée ne serait en retard cette "
                     "nuit-là : le prochain balayage n'en marquerait aucune.")
    elif not rows:
        # Never « none is late » over invoices it could not judge: their
        # lateness is unknown, not absent (review of lot 2).
        lines.append(
            f"Le retard de {len(unjudged)} facture(s) n'a pu être jugé "
            "(ci-dessous) ; aucune autre ne serait en retard cette nuit-là : "
            "le prochain balayage n'en marquerait aucune.")
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
            f"{len(rows)} facture(s) en retard cette nuit-là, "
            f"{format_cents_fr(total)} de solde en tout.",
        ]
        # Late from that night only: payable all its eve — today, when the
        # preview runs in the daytime — so in no list of the day before.
        fresh = sum(1 for row in rows if row["days"] == 1)
        if fresh:
            lines.append(
                f"Dont {fresh} payable(s) jusqu'au "
                f"{(night - timedelta(days=1)).isoformat()} inclusivement, en "
                f"retard à compter du {night.isoformat()} seulement.")
        lines.append(
            f"Le prochain balayage en marquerait {min(len(rows), cap)} "
            f"(au plus {cap} par nuit, les plus anciennes d'abord)"
            + (f" ; {rest} attendrai{'en' if rest > 1 else ''}t les nuits "
               "suivantes." if rest else "."))
    if unjudged:
        lines += [
            "",
            f"{len(unjudged)} facture(s) dont le retard ne peut être jugé "
            "(un montant stocké illisible) — le balayage les compterait en "
            "erreur et n'y toucherait pas : " + ", ".join(unjudged) + ".",
        ]
    lines += ["", _LATER_SWITCH]
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

    # The day the NEXT run judges, never the day this runs (second review of
    # lot 2) — one read of the clock, at its one seam (utils.deadlines).
    night = next_sweep_night(deadlines.now_utc())
    invoices = invoice_model.list_sent_invoices_strict()
    rows, unjudged = preview_rows(invoices, night=night)
    for line in report(rows, unjudged, night=night, seen=len(invoices),
                       cap=invoice_model.OVERDUE_SWEEP_CAP):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
