"""Read-only preview: the invoices the NEXT lateness sweep would mark.

THIS SCRIPT READS PRODUCTION. Like the other Firestore scripts of
``scripts/``, it reaches Firestore through the Application Default
Credentials (``models.db``) — unless ``FIRESTORE_EMULATOR_HOST`` points at a
RUNNING emulator — and it WRITES NOTHING: no ``--apply``, no flag at all,
by design.

Lot 2 of « Factures : retard, radiation, intérêts » (2026-11-11) ships the
daily sweep (``routes/taches_factures``) switched OFF
(``FACTURES_RETARD_ACTIF: "false"`` in app.yaml), because its first run
marks the whole backlog at once — the invoices imported from the previous
system included. This preview lists, before the switch goes on, exactly what
the NEXT run would mark: the SAME strict read
(``invoice.list_sent_invoices_strict``), the SAME rule
(``invoice.is_overdue`` through ``invoice.overdue_among``) and the SAME
order, longest-late first — judged on THAT run's day, never on the day the
preview runs. The sweep runs every day at :data:`SWEEP_TIME`, Montréal time
(cron.yaml), and reads its own « today »: run after that hour, the preview
names the next day's run, by which an invoice payable all day today is late
(:func:`next_sweep_day`). The preview used to judge its own day, and said
« none » of a run that marked one (second review of lot 2). A failed or
truncated read raises: it never prints « aucune ».

That run is the switch's first only if the switch is SERVING by its
:data:`SWEEP_TIME` — its commit pushed, its version serving. Serving later,
its first run is a later day's, and the invoices fallen due in between, none
of them listed, are marked too — by that run or, past the per-run cap
(``invoice.OVERDUE_SWEEP_CAP``, the longest-late first), by the following
ones. So the report names that run by its day and hour (« le AAAA-MM-JJ, à
9 h »), says last that the list holds only for a switch serving by then and
to run it again otherwise, and, run before :data:`SWEEP_TIME` — the next run
is then that very morning's, too soon for a switch pushed after the preview
unless its version serves by that hour (:func:`next_run_is_today`) — says so
right after naming it. The runbook runs it after :data:`SWEEP_TIME`, and
again whenever the switch misses the run it named (DEPLOYMENT.md §15; third
review of lot 2: the rule used to key on the push's calendar day). An
invoice that turns « envoyée » before that run, or whose unreadable amount
is repaired, would be marked without ever being listed: the report says to
run it again then.

One line per invoice: its number, its due date as stored, that date
prorogued to the next business day on the civil calendar, the days late on
the run's day, the balance and whether it was imported
(``invoice.is_imported``) — never a client's name. Then the count, the total
balance, how many are late from that day only (payable until its eve), how
many that run marks (``invoice.OVERDUE_SWEEP_CAP`` a run) and the ids whose
lateness could not be judged — over which it never says « aucune »
unqualified: their lateness is unknown, not absent. The lawyer reviews it; a
one-line commit then turns the switch on (DEPLOYMENT.md §15).

    python -m scripts.apercu_factures_echues
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from tz import to_mtl
from utils import cron_schedule

#: The hour of the sweep's job — cron.yaml's ``every day 09:00``,
#: America/Toronto: Montréal's canonical tz zone, of which America/Montreal,
#: the name ``tz.MTL`` reads, is only a backward alias — from which
#: :func:`next_sweep_day` counts: the day's first pass since the lawyer's
#: decision of 2026-11-12, read from ``utils.cron_schedule``, the one source
#: that tests/test_cron_schedule pins to cron.yaml.
SWEEP_TIME = cron_schedule.daily_time()


def next_run_is_today(now: datetime) -> bool:
    """Whether the next run after the instant *now* is *now*'s own day —
    *now* before :data:`SWEEP_TIME`, Montréal time: that run, that very
    morning, judges *now*'s own day, too soon for a switch pushed after the
    preview unless its version serves by that hour (third review of lot 2).
    PURE; read on Montréal's clock, never UTC's, as :func:`next_sweep_day`,
    which it decides."""
    return to_mtl(now).time() < SWEEP_TIME


def next_sweep_day(now: datetime) -> date:
    """The day the first sweep run after the instant *now* judges: the
    Montréal day of the next :data:`SWEEP_TIME`, since a run reads its own
    ``today_mtl`` — *now*'s own Montréal day before that hour
    (:func:`next_run_is_today`), the next one from it. PURE. *now* is
    tz-aware (``utils.deadlines.now_utc``) and read on Montréal's clock,
    never UTC's, whose day turns over at 19:00 (20:00 in summer) there and
    whose 09:00 is Montréal's 04:00 (05:00 in summer)."""
    day = to_mtl(now).date()
    return day if next_run_is_today(now) else day + timedelta(days=1)


def _hour() -> str:
    """:data:`SWEEP_TIME` as the report writes it, the French way: « 9 h »
    (« 9 h 30 » were it a half hour)."""
    if SWEEP_TIME.minute:
        return f"{SWEEP_TIME.hour} h {SWEEP_TIME.minute:02d}"
    return f"{SWEEP_TIME.hour} h"


def _closing_lines(day: date) -> list[str]:
    """The last lines of every report — PURE: the deadline its list holds
    by (the switch serving before the run of *day*), then the invoices that
    may join that list before then; each says to run the preview again
    (third review of lot 2: the rule keyed on the push's calendar day, and
    an invoice that joined the list after the preview was marked unlisted).
    The invoices fallen due before a later first run are marked « ce jour-là
    ou les suivants »: by that run, or — past the per-run cap, which marks
    the longest-late first and leaves the newest in ``remaining`` — by the
    runs after it."""
    return [
        "Ce balayage ne marque cette liste que si l'interrupteur "
        "FACTURES_RETARD_ACTIF est allumé en production — son commit envoyé, "
        f"sa version servie — avant le {day.isoformat()} à {_hour()}. "
        "Sinon, son premier balayage sera celui d'un jour suivant, et les "
        "factures devenues échues entre-temps, que cette liste ne montre pas, "
        "seront marquées elles aussi, ce jour-là ou les suivants : relancez "
        f"alors cet aperçu après ce {_hour()}, et revoyez sa liste avant le "
        "balayage qu'il nommera.",
        "Relancez-le aussi après toute facture passée à « envoyée » d'ici là "
        "(un brouillon envoyé, une facture rouverte ou dont le retard est "
        f"retiré) : en retard le {day.isoformat()}, elle serait marquée sans "
        "avoir paru ici.",
    ]


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


def preview_rows(invoices: list[dict], *, day: date) -> tuple[list[dict], list[str]]:
    """What the sweep run of *day* would mark — the day that run judges
    (:func:`next_sweep_day`) — in its order, PURE but for the model's log
    of an invoice it cannot judge. Each row: ``id``, ``number``, ``due`` (the
    stored due date's day), ``prorogued`` (the last day it was payable: that
    day moved to the next business day on the civil calendar — the eve of
    ``invoice.first_overdue_day``), ``days`` (late, on *day*),
    ``balance_cents`` and ``imported``. Also the ids not judged."""
    from models import invoice as invoice_model

    late, unjudged = invoice_model.overdue_among(invoices, today=day)
    rows = []
    for invoice in late:
        rows.append({
            "id": str(invoice.get("id") or ""),
            "number": str(invoice.get("invoice_number") or ""),
            "due": _stored_day(invoice.get("due_date")),
            "prorogued": (invoice_model.first_overdue_day(invoice)
                          - timedelta(days=1)),
            "days": invoice_model.days_overdue(invoice, today=day),
            "balance_cents": invoice_model.balance_of(invoice),
            "imported": invoice_model.is_imported(invoice),
        })
    return rows, unjudged


def report(rows: list[dict], unjudged: list[str], *, day: date,
           runs_today: bool, seen: int, cap: int) -> list[str]:
    """The French lines the lawyer reads — PURE. *day* is the day the
    predicted run judges (:func:`next_sweep_day`), named first with that
    run's hour (« le *day*, à 9 h »); *runs_today*
    (:func:`next_run_is_today`) says right after that this run is that very
    morning's — too soon for a switch pushed after the preview, unless its
    version serves by then. The deadline the list holds by, and what changes
    it, come last (:func:`_closing_lines`)."""
    from utils.format_fr import format_cents_fr

    lines = [
        "Aperçu du prochain balayage des factures échues : le "
        f"{day.isoformat()}, à {_hour()} (heure de Montréal). LECTURE "
        "SEULE : rien n'a été écrit.",
    ]
    if runs_today:
        lines.append(
            f"Ce balayage est celui d'aujourd'hui, à {_hour()} : trop tôt pour "
            "un interrupteur envoyé après cet aperçu, à moins que sa version "
            "ne serve avant cette heure. Sinon, relancez cet aperçu après "
            f"{_hour()} : il nommera le balayage du lendemain.")
    lines += [f"Factures envoyées lues : {seen}.", ""]
    if not rows and not unjudged:
        lines.append("Aucune facture envoyée ne serait en retard ce "
                     "jour-là : le prochain balayage n'en marquerait aucune.")
    elif not rows:
        # Never « none is late » over invoices it could not judge: their
        # lateness is unknown, not absent (review of lot 2).
        lines.append(
            f"Le retard de {len(unjudged)} facture(s) n'a pu être jugé "
            "(ci-dessous) ; aucune autre ne serait en retard ce jour-là : "
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
            f"{len(rows)} facture(s) en retard ce jour-là, "
            f"{format_cents_fr(total)} de solde en tout.",
        ]
        # Late from that day only: payable all its eve — today, when the
        # preview runs after the sweep's hour — so on no earlier run's list.
        fresh = sum(1 for row in rows if row["days"] == 1)
        if fresh:
            lines.append(
                f"Dont {fresh} payable(s) jusqu'au "
                f"{(day - timedelta(days=1)).isoformat()} inclusivement, en "
                f"retard à compter du {day.isoformat()} seulement.")
        lines.append(
            f"Le prochain balayage en marquerait {min(len(rows), cap)} "
            f"(au plus {cap} par balayage, les plus anciennes d'abord)"
            + (f" ; {rest} attendrai{'en' if rest > 1 else ''}t les "
               "balayages suivants." if rest else "."))
    if unjudged:
        lines += [
            "",
            f"{len(unjudged)} facture(s) dont le retard ne peut être jugé "
            "(un montant stocké illisible) — le balayage les compterait en "
            "erreur et n'y toucherait pas : " + ", ".join(unjudged) + ". "
            "Réparez-les, puis relancez cet aperçu : une fois réparée, une "
            "facture en retard s'ajoute à la liste de l'aperçu.",
        ]
    lines += ["", *_closing_lines(day)]
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
    # lot 2), and whether that run is this very morning's (third) — one read
    # of the clock, at its one seam (utils.deadlines).
    now = deadlines.now_utc()
    day = next_sweep_day(now)
    invoices = invoice_model.list_sent_invoices_strict()
    rows, unjudged = preview_rows(invoices, day=day)
    for line in report(rows, unjudged, day=day,
                       runs_today=next_run_is_today(now), seen=len(invoices),
                       cap=invoice_model.OVERDUE_SWEEP_CAP):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
