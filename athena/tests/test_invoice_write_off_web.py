"""The write-off in the web app — lot 3b of « Factures : retard, radiation,
intérêts » (the lawyer's decision 8 of 2026-11-12: a price reduction the
client gets a numbered credit note for, booked as a bad debt).

« Radier le solde » (GET/POST ``/factures/<id>/radiation``) and « Annuler la
radiation » (POST ``/factures/<id>/radiation/<event_id>/annulation``), each
the request adapter of its model door (``invoice.write_off_remainder``,
``invoice.reverse_write_off``); the invoice sheet's status label, « Radié »
line and « Radiations » section; the list's label; and the two payment
pickers, which skip an invoice where a radiation stands.

Through the real routes, models and templates on the shared fake Firestore
(``tests/_fake_firestore``: the real client, its transactions and the real
``transactional`` retry loop), « today » frozen at the one seam
(``tests/_frozen_clock``) on a day of a fictitious year; every amount is
invented.
"""

import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from html import unescape
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import concurrency
    from models import invoice as invoice_model
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.parties as parties_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests import test_compiled_classes as compiled  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
INV = "inv-web-1"
PATH = f"invoices/{INV}"
ETAG = "cccccccc-1111-4222-8333-444444444444"
RIVAL = "dddddddd-1111-4222-8333-444444444444"
# Monday 10 March 2031, 11:00 in Montréal: the invoice below, due on 3
# February, has been late since the 4th.
TODAY = datetime(2031, 3, 10, 15, 0, tzinfo=UTC)
# 750,00 $ of fees, TPS 5 %, TVQ 9,975 % — invented.
SUBTOTAL, GST, QST, TOTAL = 75000, 3750, 7481, 86231
WHEN = datetime(2031, 1, 3, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    freeze_deadlines(monkeypatch, TODAY)
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (invoices_routes.invoices_bp, admin_ledger_routes.admin_bp,
               dossiers_routes.dossiers_bp, parties_routes.parties_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _invoice(**over) -> dict:
    """An issued invoice of a fictitious year, overdue on TODAY."""
    doc = {
        **invoice_model._default_doc(),
        "id": INV, "invoice_number": "2031-F042", "status": "en_retard",
        "dossier_id": "dos-9", "dossier_file_number": "2031-009",
        "dossier_title": "Exemple c. Témoin", "client_id": "cli-9",
        "client_name": "Cliente Exemple", "date": WHEN,
        "due_date": datetime(2031, 2, 3, tzinfo=UTC),
        "subtotal_fees": SUBTOTAL, "subtotal": SUBTOTAL,
        "gst_rate": 500, "gst_amount": GST, "qst_rate": 9975, "qst_amount": QST,
        "total": TOTAL, "retainer_applied": 0, "amount_due": TOTAL,
        "amount_paid": 0, "paid_date": None, "etag": ETAG,
        "created_via": "web", "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    return doc


def _seed(fake, **over) -> dict:
    fake.seed(PATH, _invoice(**over))
    return fake.peek(PATH)


def _radiation(eid: str = "rad-1", *, number: str = "2031-NC001",
               day: int = 5, amount: int = TOTAL, reason: str = "") -> dict:
    return {"id": eid, "kind": "radiation",
            "date": datetime(2031, 3, day, tzinfo=UTC), "amount": amount,
            "gst_amount": GST if amount == TOTAL else 0,
            "qst_amount": QST if amount == TOTAL else 0,
            "tax_claimed": amount == TOTAL, "reason": reason, "reverses_id": "",
            "credit_note_number": number,
            "created_at": datetime(2031, 3, day, 15, tzinfo=UTC), "created_via": "web"}


def _contre_passation(of: dict, eid: str = "cp-1", day: int = 6) -> dict:
    return {"id": eid, "kind": "contre_passation",
            "date": datetime(2031, 3, day, tzinfo=UTC),
            **{k: of[k] for k in ("amount", "gst_amount", "qst_amount", "tax_claimed")},
            "reason": "", "reverses_id": of["id"],
            "created_at": datetime(2031, 3, day, 15, tzinfo=UTC), "created_via": "web"}


_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')


def _page(client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code, resp.location)
    return resp.get_data(as_text=True)


def _query(resp) -> dict:
    assert resp.status_code == 302, resp.status_code
    loc = urlparse(resp.location)
    assert loc.path == f"/factures/{INV}"
    return {k: v[0] for k, v in parse_qs(loc.query).items()}


def _post_write_off(client, **form):
    data = {"expected_etag": ETAG, "reason": "", "claim_tax": "on", **form}
    return client.post(f"/factures/{INV}/radiation",
                       data={k: v for k, v in data.items() if v is not None})


# ══════════════════════════════════════════════════════════════════════
# 1. « Radier le solde » — the form
# ══════════════════════════════════════════════════════════════════════


def test_the_form_shows_the_models_figures_and_reserves_no_number(fake, client):
    """The remainder, its TPS and TVQ as the model computes them — never
    typed —, the day it will carry, and the credit note's number « given
    when it is recorded »: the counter is neither read nor written."""
    _seed(fake, amount_paid=30000)
    fake.reset_logs()
    html = unescape(_page(client, f"/factures/{INV}/radiation"))
    remainder = TOTAL - 30000
    gst, qst = invoice_model.write_off_tax_parts(fake.peek(PATH), remainder)
    assert (gst, qst) == (2445, 4878)
    assert format_cents_fr(remainder) in html
    assert format_cents_fr(gst) in html and format_cents_fr(qst) in html
    assert "2031-03-10 (aujourd'hui)" in html
    assert "son numéro sera attribué à l'enregistrement" in html
    assert "le suivant de la série 2031-NC" in html
    assert "2031-NC001" not in html
    assert _ETAG_INPUT.findall(html) == [ETAG]
    assert 'name="csrf_token" value="tok"' in html
    assert re.search(r'<input type="checkbox" name="claim_tax"\s+checked', html)
    assert "Réclamer la TPS/TVQ (créance irrécouvrable)" in html
    assert '<textarea id="reason" name="reason"' in html
    assert fake.commits == []
    assert not [r for r in fake.reads if any("counters/" in p for p in r.paths)]


@pytest.mark.parametrize("over, needle", [
    ({"status": "envoyée", "due_date": datetime(2031, 4, 3, tzinfo=UTC)},
     "L'échéance de cette facture n'est pas passée"),
    ({"status": "brouillon"}, "encore un brouillon"),
    ({"status": "payée", "write_offs": [_radiation()]}, "« Soldée par radiation »"),
    ({"amount_paid": "huit cents"}, "Le montant payé enregistré sur cette facture est illisible"),
], ids=["not-yet-due", "brouillon", "already-written-off", "unreadable"])
def test_the_form_is_refused_back_to_the_sheet_when_the_model_would_refuse(
        fake, client, over, needle):
    _seed(fake, **over)
    resp = client.get(f"/factures/{INV}/radiation")
    assert needle in _query(resp)["erreur"]
    assert "huit cents" not in _query(resp)["erreur"]


def test_an_unknown_invoice_goes_to_the_list(fake, client):
    resp = client.get("/factures/inv-absente/radiation")
    assert resp.status_code == 302 and urlparse(resp.location).path == "/factures/"


def test_unreadable_tax_figures_are_said_never_guessed(fake, client):
    _seed(fake, gst_amount="37,50")
    html = unescape(_page(client, f"/factures/{INV}/radiation"))
    assert "illisibles" in html and "décochez-la" in html


# ══════════════════════════════════════════════════════════════════════
# 2. « Radier le solde » — the POST
# ══════════════════════════════════════════════════════════════════════


def test_writing_off_numbers_the_credit_note_and_returns_to_the_sheet(fake, client):
    _seed(fake)
    resp = _post_write_off(client, reason="Cliente introuvable.\r\nDossier clos.")
    assert _query(resp) == {"message": "Solde radié : note de crédit 2031-NC001."}
    stored = fake.peek(PATH)
    (event,) = stored["write_offs"]
    assert (event["kind"], event["amount"], event["gst_amount"], event["qst_amount"],
            event["tax_claimed"], event["credit_note_number"]) == (
        "radiation", TOTAL, GST, QST, True, "2031-NC001")
    assert event["reason"] == "Cliente introuvable.\nDossier clos."
    assert event["created_via"] == "web" and stored["updated_via"] == "web"
    assert stored["status"] == "payée"
    assert fake.peek("counters/notes-credit-2031")["seq"] == 1


def test_the_unchecked_box_claims_no_tax(fake, client):
    _seed(fake)
    resp = _post_write_off(client, claim_tax=None)
    assert "2031-NC001" in _query(resp)["message"]
    (event,) = fake.peek(PATH)["write_offs"]
    assert (event["tax_claimed"], event["gst_amount"], event["qst_amount"]) == (False, 0, 0)


def test_a_stale_page_re_renders_at_200_with_the_banner_and_the_current_version(
        fake, client):
    """The invoice changed since the page was rendered: nothing written, the
    form again — the amber banner, the CURRENT version and figures, what the
    lawyer typed (Rule 8: htmx swaps only a 2xx)."""
    _seed(fake, etag=RIVAL, amount_paid=10000)
    resp = _post_write_off(client, reason="Motif conservé", claim_tax=None)
    assert resp.status_code == 200
    html = unescape(resp.get_data(as_text=True))
    assert "Cet élément a été modifié entre-temps." in html
    assert _ETAG_INPUT.findall(html) == [RIVAL]
    assert format_cents_fr(TOTAL - 10000) in html
    assert ">Motif conservé</textarea>" in html
    assert not re.search(r'name="claim_tax"\s+checked', html)
    assert "write_offs" not in fake.peek(PATH)
    assert fake.peek("counters/notes-credit-2031") is None


def test_a_refused_reason_re_renders_with_the_submitted_version_and_text(fake, client):
    _seed(fake)
    long_reason = "x" * 501
    resp = _post_write_off(client, reason=long_reason)
    assert resp.status_code == 200
    html = unescape(resp.get_data(as_text=True))
    assert "dépasse 500 caractères (501)" in html
    assert _ETAG_INPUT.findall(html) == [ETAG]
    assert f">{long_reason}</textarea>" in html
    assert "write_offs" not in fake.peek(PATH)


def test_a_second_submit_goes_back_to_the_sheet_saying_why(fake, client):
    """The same page posted twice: the second is stale AND the invoice is
    settled — there is no form to show any more, the sheet says why."""
    _seed(fake)
    assert "2031-NC001" in _query(_post_write_off(client))["message"]
    erreur = _query(_post_write_off(client))["erreur"]
    assert "« Soldée par radiation »" in erreur
    assert len(fake.peek(PATH)["write_offs"]) == 1
    assert fake.peek("counters/notes-credit-2031")["seq"] == 1


def test_a_post_without_a_version_is_refused_and_writes_nothing(fake, client):
    _seed(fake)
    resp = _post_write_off(client, expected_etag=None)
    assert resp.status_code == 200
    html = unescape(resp.get_data(as_text=True))
    assert invoice_model.WRITE_OFF_VERSION_REQUIRED in html
    assert "write_offs" not in fake.peek(PATH)


# ══════════════════════════════════════════════════════════════════════
# 3. The invoice sheet
# ══════════════════════════════════════════════════════════════════════


def test_an_overdue_sheet_offers_radier_le_solde_and_a_current_one_does_not(fake, client):
    _seed(fake)
    html = _page(client, f"/factures/{INV}")
    assert f'href="/factures/{INV}/radiation"' in html and "Radier le solde" in html
    _seed(fake, status="envoyée", due_date=datetime(2031, 4, 3, tzinfo=UTC))
    html = _page(client, f"/factures/{INV}")
    assert "Radier le solde" not in html
    assert "Radiations" not in html            # no event, no section


def test_a_written_off_sheet_says_how_it_was_settled_and_lists_the_radiation(fake, client):
    _seed(fake, status="payée", amount_paid=30000,
          write_offs=[_radiation(amount=TOTAL - 30000, reason="Cliente introuvable.")])
    html = unescape(_page(client, f"/factures/{INV}"))
    assert "Payée en partie — solde radié" in html
    assert "Radier le solde" not in html
    assert "Radié (notes de crédit)" in html and f"-{format_cents_fr(TOTAL - 30000)}" in html
    assert "Radiation · note de crédit 2031-NC001" in html and ">en vigueur<" in html
    assert "2031-03-05" in html and "Cliente introuvable." in html
    assert "TPS (non réclamée)" in html          # this one claimed none
    # « Annuler la radiation »: its form, the version, the token, a confirmation.
    action = f'action="/factures/{INV}/radiation/rad-1/annulation"'
    assert action in html
    form = html[html.index(action):html.index("</form>", html.index(action))]
    assert 'data-confirm="Annuler la radiation de' in form
    assert "La note de crédit 2031-NC001 gardera son numéro, annulée" in form
    assert _ETAG_INPUT.findall(form) == [ETAG]
    assert 'name="csrf_token" value="tok"' in form and 'name="reason"' in form
    # No payment is offered on it: the registers would refuse it.
    text = " ".join(html.split())
    assert "si le client paie, annulez d'abord la radiation (ci-dessous)" in text
    assert "en choisissant cette facture dans la liste" not in text


def test_a_cancelled_radiation_reads_annulee_and_offers_nothing(fake, client):
    rad = _radiation()
    _seed(fake, write_offs=[rad, _contre_passation(rad)])
    html = unescape(_page(client, f"/factures/{INV}"))
    assert ">annulée<" in html
    assert "Annulation de radiation · note de crédit 2031-NC001" in html
    assert "/annulation" not in html
    assert "Radier le solde" in html             # overdue again: it may be
    assert "Radié (notes de crédit)" not in html
    assert "En retard" in html


def test_unreadable_radiations_are_said_never_an_absence(fake, client):
    _seed(fake, write_offs=[{"id": "x"}])
    html = unescape(_page(client, f"/factures/{INV}"))
    assert "Les radiations enregistrées sur cette facture sont illisibles" in html
    assert "/annulation" not in html and "Radier le solde" not in html


# ══════════════════════════════════════════════════════════════════════
# 4. « Annuler la radiation »
# ══════════════════════════════════════════════════════════════════════


def test_cancelling_keeps_the_number_and_reopens_the_invoice(fake, client):
    _seed(fake)
    _post_write_off(client)
    stored = fake.peek(PATH)
    rad = stored["write_offs"][0]
    resp = client.post(f"/factures/{INV}/radiation/{rad['id']}/annulation",
                       data={"expected_etag": stored["etag"],
                             "reason": "Le client a payé."})
    assert _query(resp) == {"message": (
        "Radiation annulée : la note de crédit 2031-NC001 est annulée, et la "
        "facture est « En retard ».")}
    stored = fake.peek(PATH)
    first, cancel = stored["write_offs"]
    assert first == rad and "credit_note_number" not in cancel
    assert (cancel["kind"], cancel["reverses_id"], cancel["reason"], cancel["created_via"]) == (
        "contre_passation", rad["id"], "Le client a payé.", "web")
    assert stored["status"] == "en_retard"
    # Written off again: the NEXT number — never reused.
    resp = _post_write_off(client, expected_etag=stored["etag"])
    assert _query(resp)["message"] == "Solde radié : note de crédit 2031-NC002."


def test_a_stale_cancellation_goes_back_to_the_sheet_and_writes_nothing(fake, client):
    _seed(fake, status="payée", write_offs=[_radiation()])
    before = fake.peek(PATH)
    resp = client.post(f"/factures/{INV}/radiation/rad-1/annulation",
                       data={"expected_etag": RIVAL})
    assert _query(resp)["erreur"] == concurrency.STALE_ETAG_ERROR
    assert fake.peek(PATH) == before


def test_a_cancellation_of_nothing_standing_is_refused_by_the_door(fake, client):
    rad = _radiation()
    _seed(fake, write_offs=[rad, _contre_passation(rad)])
    resp = client.post(f"/factures/{INV}/radiation/rad-1/annulation",
                       data={"expected_etag": ETAG})
    assert "déjà annulée" in _query(resp)["erreur"]


# ══════════════════════════════════════════════════════════════════════
# 5. The list, the pickers, the vocabulary, the markup
# ══════════════════════════════════════════════════════════════════════


def test_the_list_row_says_soldee_par_radiation(fake, client):
    _seed(fake, status="payée", write_offs=[_radiation()])
    html = unescape(client.get("/factures/", headers={"HX-Request": "true"})
                    .get_data(as_text=True))
    assert "Soldée par radiation" in html


def _picker_invoices() -> list[dict]:
    """Reopened since its payment was reversed: « en_retard », 300,00 $
    owed, a radiation of 562,31 $ still standing — and its witness."""
    standing = _invoice(id="r", invoice_number="2031-F050", amount_paid=0,
                        write_offs=[_radiation(amount=TOTAL - 30000)])
    witness = _invoice(id="w", invoice_number="2031-F051")
    return [standing, witness]


def test_the_administration_picker_skips_an_invoice_where_a_radiation_stands(monkeypatch):
    invoices = _picker_invoices()
    assert invoice_model.balance_of(invoices[0]) == 30000      # it would be offered
    monkeypatch.setattr(invoice_model, "list_invoices",
                        lambda status_filter=None, **kw: [
                            i for i in invoices if i["status"] == status_filter])
    assert [r["id"] for r in admin_ledger_routes._factures_impayees()] == ["w"]


def test_the_trust_picker_skips_an_invoice_where_a_radiation_stands(monkeypatch):
    invoices = _picker_invoices()
    monkeypatch.setattr(invoice_model, "list_invoices", lambda dossier_id=None: invoices)
    assert [r["invoice_number"] for r in trust_routes._factures_emises("dos-9")] == [
        "2031-F051"]


def test_the_kind_labels_follow_the_models_vocabulary():
    assert tuple(invoices_routes.WRITE_OFF_KIND_LABELS) == invoice_model.WRITE_OFF_KINDS


def test_the_new_markup_uses_compiled_classes_and_44_px_controls(fake, client):
    """Change Impact item 6: every class the new pages render compiles. Rule
    9: each new control is py-3 around a text-sm label (12 + 20 + 12 px) —
    the form's two buttons, the sheet's « Radier le solde » and « Annuler la
    radiation », the checkbox's label."""
    def _content(url: str) -> str:
        """The page's own block — base.html's shell is swept elsewhere."""
        page = _page(client, url)
        return page[page.index("<main"):page.index("</main>")]

    _seed(fake)
    form = _content(f"/factures/{INV}/radiation")
    sheet = _content(f"/factures/{INV}")
    _seed(fake, status="payée", write_offs=[_radiation()])
    written_off = _content(f"/factures/{INV}")
    classes = {c for page in (form, sheet, written_off)
               for attr in re.findall(r'class="([^"]+)"', page) for c in attr.split()}
    assert {"py-3", "divide-y", "bg-red-600"} <= classes        # not vacuous
    css = compiled._artifact(_ATHENA / "static" / "vendor").read_text(encoding="utf-8")
    selectors = compiled.compiled_selectors(css)
    assert sorted(c for c in classes if not compiled.is_compiled(c, selectors)) == []

    def _control(page: str, label: str) -> set:
        tag = re.search(r'<(?:a|button|label)\b[^>]*class="([^"]*)"[^>]*>\s*'
                        + re.escape(label), page)
        assert tag, label
        return set(tag.group(1).split())

    for page, label in ((form, "Abandonner"), (form, "Radier le solde"),
                        (sheet, "Radier le solde"),
                        (written_off, "Annuler la radiation")):
        assert {"py-3", "text-sm"} <= _control(page, label), label
    label = re.search(r'<label class="([^"]*)">\s*<input type="checkbox" name="claim_tax"',
                      form)
    assert label and {"py-3", "text-sm"} <= set(label.group(1).split())
