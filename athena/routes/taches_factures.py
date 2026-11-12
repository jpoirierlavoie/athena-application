"""The daily lateness sweep — « en_retard » follows the due date.

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable
only by App Engine cron (cron.yaml: every day at 09:00, America/Toronto —
Montréal's canonical tz zone, of which America/Montreal is only a backward
alias; 09:00 since the lawyer's decision of 2026-11-12).
The ``X-Appengine-Cron: true`` header is STRIPPED from all external traffic,
so the in-handler guard is proof of origin — the fact
``security.is_appengine_internal_request`` relies on too. The module lives in
``routes/taches_*.py``, so every write it makes is stamped « cron »
(``models/provenance.via_for_blueprint_module``). Its one rule,
``/taches/factures/retards``, is two literal segments under the web task
list's ``/taches`` prefix, whose two-segment rules all end in ``edit``,
``delete``, ``toggle`` (``/taches/<task_id>/…``), ``csv`` or ``pdf``
(``/taches/export/…``): no rule of ``routes/tasks.py`` can match it, in
whichever order the two blueprints are registered.

Lot 2 of « Factures : retard, radiation, intérêts » (the lawyer's decisions
of 2026-11-11): lateness is a FACT. Every day at 09:00, each « envoyée »
invoice ``models/invoice.is_overdue`` says is late — a balance owing, and
its due date, prorogued to the next business day on the CIVIL calendar,
past in Montréal — is marked « en_retard » through
``invoice.update_status_report``: the door of the sheet's button and of the
connector's ``update_invoice``, which judges lateness again inside its own
transaction. Each mark is made against the etag the sweep read, so an
invoice paid or edited since is refused, never overwritten; its
``invoice_status_changed`` line carries the cause ``echeance``.

Bounded, and honest about what it did:

* a kill switch, ``Config.FACTURES_RETARD_ACTIF`` — app.yaml ships it
  "false" until the lawyer has reviewed the read-only preview
  (``scripts/apercu_factures_echues``): off, nothing is read or written;
* ONE strict read (``invoice.list_sent_invoices_strict``): a failed or
  truncated read writes nothing and says so, in its answer and in an ERROR
  line — never « nothing is late »;
* at most ``invoice.OVERDUE_SWEEP_CAP`` marks a run, the longest-late first
  (``invoice.overdue_among``, the preview's order too); the rest is counted
  (``remaining``) and waits for the next run;
* idempotent: a marked invoice is no longer « envoyée », so the next run
  never reads it again;
* it always answers 200 with JSON — a 5xx would only make the scheduler
  retry a run with nothing more to say — and logs ONE counter line,
  ``invoice_overdue_sweep`` (``OBSERVABILITY.md``).
"""

from flask import Blueprint, abort, jsonify, request

from config import Config
from utils.logging_setup import log_unexpected

taches_factures_bp = Blueprint(
    "taches_factures", __name__, url_prefix="/taches/factures"
)


@taches_factures_bp.get("/retards")
def retards():
    # Proof of origin: App Engine strips X-Appengine-* from all external
    # traffic; only a genuine cron dispatch carries this value.
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    if not Config.FACTURES_RETARD_ACTIF:
        # The kill switch: nothing read, nothing written. It stays in the
        # environment on purpose — a switch only the application could reach
        # is useless at the moment the application is what must be stopped.
        return jsonify({"actif": False})

    # LAZY imports (the routes/taches_outlook pattern): main.py imports this
    # module while it builds the application.
    from models import concurrency
    from models import invoice as invoice_model
    from utils import deadlines
    from utils.logging_setup import log_invoice_overdue_sweep

    today = deadlines.today_mtl()
    try:
        sent = invoice_model.list_sent_invoices_strict()
    except Exception as exc:
        # Nothing is known, so nothing is written. The class only: an
        # exception's text may quote a stored value.
        log_invoice_overdue_sweep(
            "failure", reason="lecture", error_type=type(exc).__name__)
        return jsonify({"actif": True, "erreur": "lecture"}), 200

    late, unjudged = invoice_model.overdue_among(sent, today=today)
    cap = invoice_model.OVERDUE_SWEEP_CAP
    counts = {
        "seen": len(sent),
        "overdue": len(late),
        "marked": 0,
        "refused": 0,
        "errors": len(unjudged),
        "remaining": max(0, len(late) - cap),
    }
    for invoice in late[:cap]:
        try:
            _written, errors = invoice_model.update_status_report(
                invoice["id"], "en_retard",
                expected_etag=concurrency.etag_of(invoice),
                cause="echeance",
            )
        except Exception:
            # The model answers every failure of its own; a raise here is a
            # defect, and one invoice's must not cost the run its line.
            log_unexpected("invoice overdue sweep: mark raised",
                           invoice_id=str(invoice.get("id") or ""))
            counts["errors"] += 1
            continue
        if not errors:
            counts["marked"] += 1
        elif errors == [invoice_model.STATUS_UPDATE_FAILED]:
            # The transaction raised: the next run re-reads and settles it.
            counts["errors"] += 1
        else:
            # Refused by the model — paid, edited or no longer late since
            # the read: its own invoice_refused line says why.
            counts["refused"] += 1

    log_invoice_overdue_sweep(**counts)
    return jsonify({"actif": True, **counts})
