"""A reconciliation's bank statement — art. 32 (the lawyer's decisions of 2026-11-10).

``models/releve_conciliation`` is the one door; both registers' routes
(``routes/_releve_conciliation``, served by ``trust_bp`` and ``admin_bp``)
and the worksheet partial (``components/_releve_conciliation.html``) sit on
it. Everything runs through the REAL models, blueprints and templates, on
the fake Firestore (``tests/_fake_firestore.py``) and the fake Cloud
Storage (``tests/_fake_gcs.py``: bytes, generations, create-only sessions);
what is asserted is what the STORE and the BUCKET hold, what the browser
receives, and what the logs carry. Every case runs on BOTH registers.

1. the rules — one statement per reconciliation; a brouillon attaches,
   replaces, removes; completing requires it; a complétée without one takes
   one (the catch-up), then it is frozen;
2. the upload — the session and the attachment end to end, the refusals,
   foreign staging shapes refused both ways;
3. « Consulter » — the signed redirect and its failures;
4. the pages — the worksheet's states, the « Relevés manquants » list;
5. the connector — never reachable; no file name ever logged.
"""

import ast
import html
import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import piece_identite as pi
    from models import releve_conciliation as rc
    from models import trust
    import routes.admin_ledger as ra
    import routes.trust as rt
    from mcp import disclosure

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBlob, FakeBucket  # noqa: E402
from tests._releve import joindre  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
UID = "u1"
PERIOD_END = datetime(2026, 10, 31, tzinfo=UTC)
PDF = b"%PDF-1.7\n" + b"releve de compte fictif " * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"image " * 40
SIGNED_URL = "https://storage.googleapis.com/fake-bucket/releve?X-Goog-Signature=abc"

#: Each register: its key, its URL prefix, its model and its account seed.
REGISTERS = {
    rc.TRUST: ("/fideicommis", trust,
               {"name": "Compte fictif", "account_type": "général"}),
    rc.ADMIN: ("/administration", al,
               {"name": "Opérations fictives", "account_type": "opérations"}),
}
both = pytest.mark.parametrize("register", list(REGISTERS))


# ── Harness ──────────────────────────────────────────────────────────────


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(rc.storage, "bucket", lambda: fake)
    return fake


@pytest.fixture
def signed(monkeypatch):
    calls: list = []

    def _sign(blob, params, expiry_minutes=15):
        calls.append({"name": blob.name, "params": dict(params),
                      "minutes": expiry_minutes})
        return SIGNED_URL

    monkeypatch.setattr(rc, "sign_blob_url", _sign)
    return calls


def _app() -> Flask:
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, jsattr=jsattr,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "")
    app.register_blueprint(rt.trust_bp)
    app.register_blueprint(ra.admin_bp)
    return app


@pytest.fixture
def web(db, bucket):
    client = _app().test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def _seed(db, register, rec_id="rec1", *, status="brouillon", **extra):
    """An account and one reconciliation of *register* — a statement only
    when *extra* carries one."""
    _prefix, model, account = REGISTERS[register]
    db.seed(f"{model.ACCOUNTS_COLLECTION}/acc1", {
        "id": "acc1", "status": "actif", "etag": "e-acc", "book_balance": 0,
        "bank_balance": 0, **account})
    rec = {"id": rec_id, "account_id": "acc1", "period_end": PERIOD_END,
           "statement_balance": 0, "book_balance": 0, "variance": 0,
           # Each register's totals (the completed summary prints its own).
           "outstanding_cheques_total": 0, "deposits_in_transit_total": 0,
           "outstanding_total": 0, "in_transit_total": 0,
           "status": status, "completed_date": None,
           "cleared_transaction_ids": [], "notes": "", "etag": "e-rec"}
    rec.update(extra)
    db.seed(f"{rc.REGISTERS[register]}/{rec_id}", rec)
    return rec


def _stored(db, register, rec_id="rec1") -> dict:
    return db.peek_collection(rc.REGISTERS[register]).get(rec_id) or {}


def _url(register, rec_id="rec1", tail="") -> str:
    return f"{REGISTERS[register][0]}/conciliations/{rec_id}{tail}"


def _upload(web, bucket, register, data=PDF, name="Relevé octobre.pdf",
            rec_id="rec1"):
    """The page's flow: open the session, play the PUT, finalize."""
    opened = web.post(_url(register, rec_id, "/releve/televersement"),
                      json={"name": name, "size": len(data)})
    assert opened.status_code == 200, opened.get_json()
    body = opened.get_json()
    bucket.complete_session(body["url"], data)
    return web.post(_url(register, rec_id, "/releve"), json={"objet": body["objet"]})


def _object_names(bucket) -> list:
    return sorted(bucket.objects)


# ══════════════════════════════════════════════════════════════════════
# 1. The rules
# ══════════════════════════════════════════════════════════════════════


def test_the_registers_are_the_models_collections():
    assert rc.REGISTERS == {rc.TRUST: trust.RECONCILIATIONS_COLLECTION,
                            rc.ADMIN: al.RECONCILIATIONS_COLLECTION}
    assert len(rc.FIELDS) == 6 and all(f.startswith("statement_") for f in rc.FIELDS)
    # The module restates the statuses it cannot import (One source per
    # list): pinned to both registers' own vocabulary.
    assert ({rc.STATUS_DRAFT, rc.STATUS_COMPLETED}
            == set(trust.VALID_RECONCILIATION_STATUSES)
            == set(al.RECONCILIATION_STATUS_LABELS))
    # The completion's refusals are the module's sentences, on both sides.
    for model in (trust, al):
        assert model._ABORT_MESSAGES["conciliation_sans_relevé"] == rc.REQUIRED_TO_COMPLETE
        assert model._ABORT_MESSAGES["conciliation_relevé_modifié"] == rc.STATEMENT_CHANGED


def test_the_predicates_hold_the_lawyer_s_decisions():
    draft = {"status": "brouillon"}
    done = {"status": "complétée"}
    joined = {"statement_storage_path": "users/u1/x"}
    assert rc.accepts_statement(draft) and rc.accepts_statement({**draft, **joined})
    assert rc.accepts_statement(done)                    # the catch-up
    assert not rc.accepts_statement({**done, **joined})  # frozen
    assert rc.removable({**draft, **joined})
    assert not rc.removable({**done, **joined}) and not rc.removable(draft)
    assert rc.statement_missing(done)                    # a legacy completion
    assert not rc.statement_missing({**done, **joined}) and not rc.statement_missing(draft)


def test_the_formats_label_is_the_shared_rule_over_the_module_s_mapping():
    """« PDF, JPG ou PNG » comes from utils/format_fr.formats_label — the rule
    the proofs and the receipts share since 2026-11-10 — over MIME_TYPES, the
    mapping that decides; no copy of the rule lives in the module."""
    from utils.format_fr import formats_label
    assert rc.FORMATS_LABEL == formats_label(rc.MIME_TYPES) == "PDF, JPG ou PNG"
    assert rc.TYPE_REFUSED.endswith("Formats acceptés : PDF, JPG ou PNG.")
    assert not hasattr(rc, "_formats_label")


@both
def test_completing_requires_the_statement(db, bucket, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    done, errors = model.complete_reconciliation("rec1", [])
    assert done is None and errors == [rc.REQUIRED_TO_COMPLETE]
    assert _stored(db, register)["status"] == "brouillon"
    fields = joindre(model, "rec1", bucket=bucket)
    done, errors = model.complete_reconciliation("rec1", [])
    assert errors == [] and done["status"] == "complétée"
    stored = _stored(db, register)
    assert stored["status"] == "complétée"
    assert {k: stored[k] for k in rc.FIELDS} == fields


@both
def test_completing_checks_the_object_it_freezes(db, bucket, register):
    """A record naming a statement is not enough: the completion freezes the
    BYTES, so a missing object, a path not of the module's shape and a
    failed check each refuse (the review of 2026-11-10: a reconciliation
    frozen on an erased object could never be repaired)."""
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    fields = joindre(model, "rec1")                          # no object
    assert model.complete_reconciliation("rec1", []) == (
        None, [rc.OBJECT_MISSING_TO_COMPLETE])
    db.seed(f"{rc.REGISTERS[register]}/rec1", {
        **_stored(db, register), "statement_storage_path":
        fields["statement_storage_path"].replace("conciliations", "autre")})
    assert model.complete_reconciliation("rec1", []) == (
        None, [rc.PATH_REFUSED_TO_COMPLETE])
    assert _stored(db, register)["status"] == "brouillon"


@both
def test_a_failed_object_check_refuses_the_completion(db, register, monkeypatch):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    joindre(model, "rec1")

    def _down():
        raise RuntimeError("storage down")

    monkeypatch.setattr(rc.storage, "bucket", _down)
    done, errors = model.complete_reconciliation("rec1", [])
    assert done is None and errors == [rc.concurrency.READ_UNAVAILABLE_ERROR]
    assert _stored(db, register)["status"] == "brouillon"


@both
def test_a_statement_replaced_after_the_pre_pass_stops_the_completion(db, bucket, register,
                                                                     monkeypatch):
    """The pre-pass checked statement A's object; B replaced it before the
    transaction: completion refuses (it would freeze B unchecked) — and the
    record keeps B. The old code also spread the stale pre-pass copy over
    the record, which would have put A back."""
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    joindre(model, "rec1", bucket=bucket)
    stale = model.get_reconciliation("rec1")
    replaced = "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    db.seed(f"{rc.REGISTERS[register]}/rec1", {
        **_stored(db, register), "statement_id": replaced,
        "statement_storage_path": rc.storage_path(UID, register, "rec1", replaced, ".pdf")})
    monkeypatch.setattr(model, "get_reconciliation", lambda rec_id: dict(stale))
    done, errors = model.complete_reconciliation("rec1", [])
    assert done is None and errors == [rc.STATEMENT_CHANGED]
    stored = _stored(db, register)
    assert stored["status"] == "brouillon" and stored["statement_id"] == replaced


@both
def test_a_statement_removed_after_the_pre_pass_stops_the_completion(db, bucket, register,
                                                                    monkeypatch):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    joindre(model, "rec1", bucket=bucket)
    stale = model.get_reconciliation("rec1")
    db.seed(f"{rc.REGISTERS[register]}/rec1",
            {k: v for k, v in _stored(db, register).items() if k not in rc.FIELDS})
    monkeypatch.setattr(model, "get_reconciliation", lambda rec_id: dict(stale))
    done, errors = model.complete_reconciliation("rec1", [])
    assert done is None and errors == [rc.REQUIRED_TO_COMPLETE]
    assert _stored(db, register)["status"] == "brouillon"


# ══════════════════════════════════════════════════════════════════════
# 2. The upload
# ══════════════════════════════════════════════════════════════════════


@both
def test_a_draft_takes_its_statement_browser_to_bucket(web, db, bucket, register):
    _seed(db, register)
    opened = web.post(_url(register, tail="/releve/televersement"),
                      json={"name": "Relevé octobre.PDF", "size": len(PDF)})
    assert opened.status_code == 200
    body = opened.get_json()
    session = bucket.sessions[body["url"]]
    assert session["content_type"] == "application/pdf"
    assert session["size"] == len(PDF) and session["if_generation_match"] == 0
    assert session["origin"] == "http://localhost"
    staged = body["objet"]
    assert staged.startswith(f"staging/{UID}/releve/") and staged.endswith("/upload.pdf")
    bucket.complete_session(body["url"], PDF)
    attached = web.post(_url(register, tail="/releve"), json={"objet": staged})
    assert attached.status_code == 200
    assert attached.get_json() == {
        "ok": True, "suivant": _url(register) + "#releve"}
    stored = _stored(db, register)
    sid = stored["statement_id"]
    path = f"users/{UID}/{register}/conciliations/rec1/releve-{sid}.pdf"
    assert stored["statement_storage_path"] == path
    assert stored["statement_file_type"] == "application/pdf"
    assert stored["statement_file_size"] == len(PDF)
    assert isinstance(stored["statement_attached_at"], datetime)
    assert stored["etag"] != "e-rec"
    # Ids only: the file's name is stored nowhere and names no object.
    assert "octobre" not in repr(stored) and _object_names(bucket) == [path]
    assert bucket.objects[path].content_type == "application/pdf"


@both
@pytest.mark.parametrize("payload, sentence", [
    ({"name": "releve.docx", "size": 10}, rc.TYPE_REFUSED),
    ({"name": "releve", "size": 10}, rc.TYPE_REFUSED),
    ({"name": "releve.pdf", "size": 0}, rc.SIZE_REFUSED),
    ({"name": "releve.pdf", "size": rc.MAX_SIZE + 1}, rc.SIZE_REFUSED),
    ({"name": "releve.pdf", "size": "12"}, rc.SIZE_REFUSED),
])
def test_a_session_is_refused_before_a_byte_moves(web, db, bucket, register,
                                                  payload, sentence):
    _seed(db, register)
    answer = web.post(_url(register, tail="/releve/televersement"), json=payload)
    assert answer.status_code == 422 and answer.get_json() == {"erreur": sentence}
    assert bucket.sessions == {}


@both
def test_an_unknown_reconciliation_opens_no_session(web, db, bucket, register):
    answer = web.post(_url(register, "nope", "/releve/televersement"),
                      json={"name": "releve.pdf", "size": 10})
    assert answer.status_code == 422 and answer.get_json() == {"erreur": rc.NOT_FOUND}
    assert bucket.sessions == {}


@both
def test_content_that_lies_about_its_type_is_refused(web, db, bucket, register):
    _seed(db, register)
    answer = _upload(web, bucket, register, data=PNG, name="releve.pdf")
    assert answer.status_code == 422
    assert answer.get_json() == {"erreur": rc.CONTENT_MISMATCH}
    assert "statement_id" not in _stored(db, register)
    assert _object_names(bucket) == []          # staging consumed, nothing copied


@both
def test_a_draft_replaces_its_statement_and_the_old_object_goes(web, db, bucket, register):
    _seed(db, register)
    assert _upload(web, bucket, register).status_code == 200
    first = _stored(db, register)["statement_storage_path"]
    assert _upload(web, bucket, register, data=PNG, name="scan.png").status_code == 200
    stored = _stored(db, register)
    assert stored["statement_storage_path"] != first
    assert stored["statement_file_type"] == "image/png"
    assert _object_names(bucket) == [stored["statement_storage_path"]]


@both
def test_a_replayed_finalization_attaches_nothing_new(web, db, bucket, register):
    _seed(db, register)
    opened = web.post(_url(register, tail="/releve/televersement"),
                      json={"name": "releve.pdf", "size": len(PDF)}).get_json()
    bucket.complete_session(opened["url"], PDF)
    first = web.post(_url(register, tail="/releve"), json={"objet": opened["objet"]})
    again = web.post(_url(register, tail="/releve"), json={"objet": opened["objet"]})
    assert first.status_code == again.status_code == 200
    assert len(_object_names(bucket)) == 1


@both
def test_a_completed_reconciliation_catches_up_once_then_is_frozen(web, db, bucket,
                                                                 register, caplog):
    _seed(db, register, status="complétée")
    assert rc.statement_missing(_stored(db, register))
    caplog.set_level(logging.INFO)
    assert _upload(web, bucket, register).status_code == 200
    stored = _stored(db, register)
    assert not rc.statement_missing(stored) and stored["status"] == "complétée"
    event = [r for r in caplog.records
             if getattr(r, "json_fields", {}).get("event", "").endswith(
                 "reconciliation_statement_attached")]
    assert event and event[0].json_fields["catch_up"] is True
    # Frozen: no second session, no replacement, no removal.
    answer = web.post(_url(register, tail="/releve/televersement"),
                      json={"name": "releve.pdf", "size": len(PDF)})
    assert answer.status_code == 422 and answer.get_json() == {"erreur": rc.FROZEN}
    removed = web.post(_url(register, tail="/releve/retirer"))
    assert removed.status_code == 302
    assert parse_qs(urlparse(removed.headers["Location"]).query)["releve_erreur"] == ["fige"]
    assert _stored(db, register)["statement_storage_path"] == stored["statement_storage_path"]
    assert _object_names(bucket) == [stored["statement_storage_path"]]


@both
def test_a_frozen_statement_refuses_a_finalization_and_keeps_its_object(db, bucket,
                                                                        register):
    """An upload opened while the reconciliation still accepted one, filed
    after it was frozen: refused, and only THIS call's copy is erased."""
    _seed(db, register)
    opened = rc.open_statement_upload(register, "rec1", "r.pdf", len(PDF), UID,
                                      origin="http://localhost")
    bucket.complete_session(opened.doc["url"], PDF)
    _prefix, model, _account = REGISTERS[register]
    fields = joindre(model, "rec1")
    db.seed(f"{rc.REGISTERS[register]}/rec1", {**_stored(db, register),
                                               "status": "complétée"})
    bucket.put(fields["statement_storage_path"], PDF, content_type="application/pdf")
    outcome = rc.file_staged_statement(register, "rec1", opened.doc["objet"], UID)
    assert outcome.code == rc.CODE_REFUS and outcome.errors == [rc.FROZEN]
    assert _object_names(bucket) == [fields["statement_storage_path"]]


@both
def test_a_draft_s_removal_clears_the_record_then_erases_the_object(web, db, bucket,
                                                                  register):
    _seed(db, register)
    assert _upload(web, bucket, register).status_code == 200
    removed = web.post(_url(register, tail="/releve/retirer"))
    assert removed.status_code == 302
    assert removed.headers["Location"] == _url(register) + "?releve=retire#releve"
    stored = _stored(db, register)
    assert not any(k in stored for k in rc.FIELDS)
    assert _object_names(bucket) == []
    # A double tap: the second POST finds nothing — a neutral notice, never
    # a red alert over a removal that worked.
    again = web.post(_url(register, tail="/releve/retirer"))
    assert again.headers["Location"] == _url(register) + "?releve=aucun#releve"
    page = html.unescape(web.get(_url(register) + "?releve=aucun").get_data(as_text=True))
    assert rc.NOTHING_TO_REMOVE in page and 'role="alert"' not in page


@both
def test_an_abandoned_draft_takes_its_statement_object_along(web, db, bucket, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    assert _upload(web, bucket, register).status_code == 200
    ok, errors = model.delete_reconciliation("rec1")
    assert ok and errors == []
    assert _stored(db, register) == {} and _object_names(bucket) == []


@both
def test_foreign_staging_shapes_are_refused_both_ways(web, db, bucket, register):
    """Each foreign shape EXISTS (a real object, a canonical id): refused at
    400, and left untouched — a « forme » refusal consumes nothing."""
    _seed(db, register)
    sid = "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b"
    foreigners = (f"staging/{UID}/identite/{sid}/upload.pdf",     # a proof's
                  f"staging/{UID}/{sid}/recu.pdf",                # receipt / document
                  f"staging/{UID}/mcp/{sid}/upload.pdf",          # the connector's
                  f"staging/autre-uid/releve/{sid}/upload.pdf",   # another uid's
                  f"staging/{UID}/releve/{sid}/upload.PDF")       # upper-case extension
    for foreign in foreigners:
        bucket.put(foreign, PDF, content_type="application/pdf")
    for foreign in foreigners:
        answer = web.post(_url(register, tail="/releve"), json={"objet": foreign})
        assert answer.status_code == 400, foreign
    assert _object_names(bucket) == sorted(foreigners)
    assert not rc.has_statement(_stored(db, register))
    ours = rc.staging_object(UID, "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b", ".pdf")
    assert pi.parse_staging_object(ours, UID) is None
    # The documents' and receipts' finalizers take exactly four segments.
    assert len(ours.split("/")) == 5


# ══════════════════════════════════════════════════════════════════════
# 3. « Consulter »
# ══════════════════════════════════════════════════════════════════════


@both
def test_consulter_redirects_to_a_15_minute_inline_signed_url(web, db, bucket, signed,
                                                             register):
    _seed(db, register)
    assert _upload(web, bucket, register).status_code == 200
    answer = web.get(_url(register, tail="/releve"))
    assert answer.status_code == 302 and answer.headers["Location"] == SIGNED_URL
    (call,) = signed
    assert call["minutes"] == 15
    assert call["name"] == _stored(db, register)["statement_storage_path"]
    disposition = call["params"]["response-content-disposition"]
    assert disposition.startswith("inline") and "releve-2026-10.pdf" in disposition
    assert call["params"]["response-content-type"] == "application/pdf"


@both
def test_consulter_refuses_an_altered_path_and_says_a_missing_file(web, db, bucket,
                                                                  signed, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    fields = joindre(model, "rec1")
    answer = web.get(_url(register, tail="/releve"))          # no object
    assert parse_qs(urlparse(answer.headers["Location"]).query)["releve_erreur"] == ["fichier"]
    db.seed(f"{rc.REGISTERS[register]}/rec1", {
        **_stored(db, register),
        "statement_storage_path": fields["statement_storage_path"].replace(
            "conciliations", "autre")})
    answer = web.get(_url(register, tail="/releve"))
    assert parse_qs(urlparse(answer.headers["Location"]).query)["releve_erreur"] == ["chemin"]
    assert signed == []


# ══════════════════════════════════════════════════════════════════════
# 4. The pages
# ══════════════════════════════════════════════════════════════════════


def _worksheet(web, register) -> str:
    answer = web.get(_url(register))
    assert answer.status_code == 200
    return answer.get_data(as_text=True)


@both
def test_the_worksheet_shows_each_state(web, db, bucket, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    page = _worksheet(web, register)
    assert 'id="releve"' in page and "Joindre le relevé" in page
    assert rc.REQUIRED_TO_COMPLETE in page
    assert "releveJoint: false" in page
    # « Compléter » waits for the statement AND for its upload to end (the
    # uploader is another Alpine root: it signals « releve-occupe »).
    assert ':disabled="varianceCents !== 0 || !releveJoint || releveOccupe"' in page
    assert '@releve-occupe.window="releveOccupe = $event.detail"' in page
    assert "this.$dispatch('releve-occupe', occupe);" in page
    assert 'id="releve-conciliation-config"' in page
    # The token the script sends CARRIES its value (an empty one would pass
    # every test and refuse every upload in production), and the picker
    # takes the module's types.
    assert '<input type="hidden" name="csrf_token" value="tok" x-ref="csrf">' in page
    assert "'X-CSRFToken': this.$refs.csrf.value," in page
    assert f'accept="{rc.ACCEPT}"' in page
    assert '"figer": false' in page
    assert "Le brouillon sera supprimé." in page
    joindre(model, "rec1")
    page = _worksheet(web, register)
    assert "Remplacer le relevé" in page and "Retirer le relevé" in page
    assert "releveJoint: true" in page and rc.REQUIRED_TO_COMPLETE not in page
    # « Abandonner » says the statement goes with the draft.
    assert "Le brouillon et son relevé joint seront supprimés." in page
    # « Retirer le relevé » posts once (a double tap posted twice).
    assert '@submit="envoi = true"' in page and ':disabled="envoi"' in page
    db.seed(f"{rc.REGISTERS[register]}/rec1", {**_stored(db, register),
                                               "status": "complétée"})
    page = _worksheet(web, register)
    assert "Figé" in page and "Consulter" in page
    assert "Retirer le relevé" not in page and "Remplacer le relevé" not in page
    assert 'id="releve-conciliation-config"' not in page
    db.seed(f"{rc.REGISTERS[register]}/rec1",
            {k: v for k, v in _stored(db, register).items() if k not in rc.FIELDS})
    page = _worksheet(web, register)
    assert "Relevé manquant" in page and "Joindre le relevé" in page
    # The catch-up FREEZES what it attaches: the script asks first.
    assert '"figer": true' in page and "Joindre et figer" in page
    assert "if (CONFIG.figer === true) {" in page


@both
def test_the_worksheet_says_only_closed_codes(web, db, bucket, register):
    _seed(db, register)
    page = web.get(_url(register) + "?releve_erreur=<b>x</b>&releve=autre").get_data(
        as_text=True)
    assert "<b>x</b>" not in page and 'role="alert"' not in page
    page = web.get(_url(register) + "?releve=retire").get_data(as_text=True)
    assert "Le relevé a été retiré." in page


@both
def test_the_list_counts_and_filters_the_missing_statements(web, db, bucket, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register, "a", status="complétée")
    _seed(db, register, "b", status="complétée")
    _seed(db, register, "c", status="complétée")
    joindre(model, "c")
    _seed(db, register, "d")
    page = web.get(f"{REGISTERS[register][0]}/conciliations/").get_data(as_text=True)
    assert "2 conciliations complétées sans relevé bancaire" in page
    assert page.count("Relevé manquant") == 2
    filtered = web.get(f"{REGISTERS[register][0]}/conciliations/?releve=manquant"
                       ).get_data(as_text=True)
    assert filtered.count('/conciliations/a"') == 1 and filtered.count('/conciliations/b"') == 1
    assert '/conciliations/c"' not in filtered and '/conciliations/d"' not in filtered
    other = web.get(f"{REGISTERS[register][0]}/conciliations/?releve=nimporte"
                    ).get_data(as_text=True)
    assert '/conciliations/d"' in other          # an unknown value is no filter


# ══════════════════════════════════════════════════════════════════════
# 5. The connector, and the logs
# ══════════════════════════════════════════════════════════════════════


def _reaches(tree: ast.AST) -> set:
    """The statement doors a source REACHES: imports, and any name or
    attribute named after them — ``trust.releve_conciliation.consult_url``
    reaches the module without importing it (models.trust imports it)."""
    doors = {"releve_conciliation", "_releve_conciliation"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.rsplit(".", 1)[-1] for a in node.names} & doors
        elif isinstance(node, ast.ImportFrom):
            found |= ({(node.module or "").rsplit(".", 1)[-1]}
                      | {a.name for a in node.names}) & doors
        elif isinstance(node, ast.Name) and node.id in doors:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in doors:
            found.add(node.attr)
    return found


def test_no_connector_side_module_reaches_the_statement_doors():
    seen = 0
    for package in ("mcp", "services", "dav", "utils"):
        for path in (_ATHENA / package).rglob("*.py"):
            seen += 1
            reached = _reaches(ast.parse(path.read_text(encoding="utf-8")))
            assert not reached, (path, reached)
    assert seen > 40
    # The sweep sees what it claims: each form is caught, prose is not.
    for planted in ("from models import releve_conciliation",
                    "import models.releve_conciliation as r",
                    "from routes import _releve_conciliation",
                    "trust_model.releve_conciliation.consult_url(r, x)"):
        assert _reaches(ast.parse(planted)), planted
    assert not _reaches(ast.parse('"""releve_conciliation, in prose"""'))


def test_the_never_rules_forbid_the_statement_writers_and_its_signer():
    keys = {n.key: n for n in disclosure.NEVERS}
    setup = keys["register_setup"]
    for name in ("open_statement_upload", "file_staged_statement",
                 "remove_statement", "erase_statement_object"):
        assert name in setup.forbidden and hasattr(rc, name), name
    # The short screen (2026-11-10) names it in three words; the English
    # statement (INSTRUCTIONS) says all three gestures.
    assert "(relevé bancaire compris)" in setup.fr
    assert "attaches, replaces or removes its bank statement" in setup.en
    # The model signer of a statement's (and a proof's) READ link.
    assert "consult_url" in keys["link"].forbidden and hasattr(rc, "consult_url")


# ══════════════════════════════════════════════════════════════════════
# 6. The races the review of 2026-11-10 reproduced — fixed
# ══════════════════════════════════════════════════════════════════════


def _open_and_put(bucket, register, data=PDF, name="r.pdf", rec_id="rec1"):
    opened = rc.open_statement_upload(register, rec_id, name, len(data), UID,
                                      origin="http://localhost")
    assert opened.code == rc.CODE_OK, opened
    bucket.complete_session(opened.doc["url"], data)
    return opened.doc


def _break_commit(monkeypatch, db, register, *, apply, reads_fail=False, kind=None):
    """The next commit writing *register*'s reconciliations RAISES — applied
    first when *apply* (a lost answer) — and, with *reads_fail*, every read
    fails until ``state["down"]`` is reset."""
    server = db._fake_server
    real_commit, real_get = server.commit, server.batch_get_documents
    state = {"left": 1, "down": False}
    collection = rc.REGISTERS[register]

    def _targets(request):
        for write in request.get("writes") or []:
            pb = getattr(write, "_pb", write)
            if (server.rel(server._write_name(pb)).startswith(collection + "/")
                    and (kind is None or server._op_kind(pb) == kind)):
                return True
        return False

    def _commit(request, metadata=None, **kwargs):
        if state["left"] and _targets(request):
            state["left"] -= 1
            if apply:
                real_commit(request, metadata=metadata, **kwargs)
            if reads_fail:
                state["down"] = True
            raise gexc.ServiceUnavailable("commit answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    def _get(request, metadata=None, **kwargs):
        if state["down"]:
            raise gexc.ServiceUnavailable("store unavailable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state


def _logged(caplog) -> str:
    return " ".join(r.getMessage() + repr(getattr(r, "json_fields", ""))
                    for r in caplog.records)


def _fail_first_object_patch(monkeypatch):
    real_patch = FakeBlob.patch
    failed = {"done": False}

    def _patch(self, *args, **kwargs):
        if not failed["done"] and self.name.startswith("users/"):
            failed["done"] = True
            raise gexc.ServiceUnavailable("transient")
        return real_patch(self, *args, **kwargs)

    monkeypatch.setattr(FakeBlob, "patch", _patch)


@both
@pytest.mark.parametrize("status", ["brouillon", "complétée"])
def test_a_rollback_never_erases_an_object_a_parallel_call_adopted(db, bucket, register,
                                                                  monkeypatch, status):
    """A1 created the object; its type patch failed; the page's re-post (A2)
    of the SAME upload adopts the object and commits right after A1 checked
    the record. A1's erase is pinned to the metageneration it last saw, and
    the adoption's patch moved it: the object stays — the reconciliation
    (a draft, or a catch-up already completed) never points at nothing."""
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register, status=status)
    doc = _open_and_put(bucket, register)
    _fail_first_object_patch(monkeypatch)
    real_erase = rc._erase_generation
    seen = {}

    def _erase(path, generation, metageneration, ids):
        seen["a2"] = rc.file_staged_statement(register, "rec1", doc["objet"], UID).code
        return real_erase(path, generation, metageneration, ids)

    monkeypatch.setattr(rc, "_erase_generation", _erase)
    first = rc.file_staged_statement(register, "rec1", doc["objet"], UID)
    assert first.code == rc.CODE_INDISPONIBLE and seen["a2"] == rc.CODE_JOINT
    rec = _stored(db, register)
    assert rec["statement_storage_path"] in bucket.objects
    if status == "brouillon":
        monkeypatch.setattr(rc, "_erase_generation", real_erase)
        done, errors = model.complete_reconciliation("rec1", [])
        assert errors == [] and done["status"] == "complétée"


def test_the_rollback_erase_is_pinned_to_its_own_metageneration(bucket, caplog):
    path = "users/u1/fideicommis/conciliations/rec1/releve-x.pdf"
    obj = bucket.put(path, PDF, content_type="application/pdf")
    caplog.set_level(logging.WARNING)
    rc._erase_generation(path, obj.generation, obj.metageneration + 1,
                         {"reconciliation_id": "rec1", "statement_id": "s1"})
    assert path in bucket.objects                  # another call patched it
    # The object stays: named, for an operator (the second review).
    assert "rollback erase skipped: rec1 / s1" in _logged(caplog)
    rc._erase_generation(path, obj.generation, obj.metageneration, {})
    assert path not in bucket.objects              # this call's own pair
    rc._erase_generation(path, None, None, {})     # nothing of this call's: no-op


@both
def test_a_completion_refuses_a_statement_whose_object_is_gone(db, bucket, register):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    joindre(model, "rec1")                          # the record only
    assert model.complete_reconciliation("rec1", []) == (
        None, [rc.OBJECT_MISSING_TO_COMPLETE])
    rec = _stored(db, register)
    assert rec["status"] == "brouillon" and rc.accepts_statement(rec)


@both
def test_a_lost_abandon_answer_is_settled_and_its_statement_erased(db, bucket, register,
                                                                  monkeypatch):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    doc = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", doc["objet"], UID).code == rc.CODE_JOINT
    _break_commit(monkeypatch, db, register, apply=True, kind="delete")
    ok, errors = model.delete_reconciliation("rec1")
    assert (ok, errors) == (True, [])               # settled: it landed
    assert _stored(db, register) == {} and _object_names(bucket) == []


@both
def test_an_unsettled_abandon_names_the_statement(db, bucket, register, monkeypatch, caplog):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    doc = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", doc["objet"], UID).code == rc.CODE_JOINT
    sid = _stored(db, register)["statement_id"]
    caplog.set_level(logging.DEBUG)
    _break_commit(monkeypatch, db, register, apply=True, reads_fail=True, kind="delete")
    ok, _errors = model.delete_reconciliation("rec1")
    assert ok is False
    (line,) = [r for r in caplog.records if r.getMessage().endswith("abandon uncertain")]
    assert line.json_fields["register"] == register
    assert line.json_fields["statement_id"] == sid


@both
def test_an_uncertain_replacement_names_the_statement_it_replaced(db, bucket, register,
                                                                 monkeypatch, caplog):
    _seed(db, register)
    first = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", first["objet"], UID).code == rc.CODE_JOINT
    old_sid = _stored(db, register)["statement_id"]
    second = _open_and_put(bucket, register, data=PNG, name="s.png")
    caplog.set_level(logging.DEBUG)
    state = _break_commit(monkeypatch, db, register, apply=True, reads_fail=True,
                          kind="update")
    assert rc.file_staged_statement(register, "rec1", second["objet"], UID).code == (
        rc.CODE_INCERTAIN)
    state["down"] = False
    (uncertain,) = [r for r in caplog.records
                    if r.getMessage() == "reconciliation statement: attachment uncertain"]
    assert uncertain.json_fields["previous_statement_id"] == old_sid
    # The page's one re-post settles it.
    assert rc.file_staged_statement(register, "rec1", second["objet"], UID).code == (
        rc.CODE_REJOUE)


@both
def test_an_uncertain_removal_keeps_the_object_and_names_it(db, bucket, register,
                                                          monkeypatch, caplog):
    _seed(db, register)
    doc = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", doc["objet"], UID).code == rc.CODE_JOINT
    sid = _stored(db, register)["statement_id"]
    path = _stored(db, register)["statement_storage_path"]
    caplog.set_level(logging.DEBUG)
    state = _break_commit(monkeypatch, db, register, apply=True, reads_fail=True,
                          kind="update")
    assert rc.remove_statement(register, "rec1").code == rc.CODE_INCERTAIN
    state["down"] = False
    assert path in bucket.objects
    assert "removal uncertain" in _logged(caplog) and sid in _logged(caplog)


@both
def test_an_erase_the_record_points_at_again_raises_the_alarm(db, bucket, register,
                                                             monkeypatch, caplog):
    """A removal's erase vs the page's re-post of an « incertain »
    finalization of the SAME statement, landing between the removal's commit
    and its erase: the record points at the erased path again — an ERROR an
    operator acts on within the soft delete's 7 days; and the draft cannot
    complete on it (its object is gone), so it can still be replaced."""
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    doc = _open_and_put(bucket, register)
    state = _break_commit(monkeypatch, db, register, apply=True, reads_fail=True,
                          kind="update")
    assert rc.file_staged_statement(register, "rec1", doc["objet"], UID).code == (
        rc.CODE_INCERTAIN)
    state["down"] = False
    real_erase = rc.erase_statement_object
    seen = {}

    def _erase(register_, rec):
        seen["repost"] = rc.file_staged_statement(register, "rec1", doc["objet"], UID).code
        return real_erase(register_, rec)

    monkeypatch.setattr(rc, "erase_statement_object", _erase)
    caplog.set_level(logging.DEBUG)
    assert rc.remove_statement(register, "rec1").code == rc.CODE_RETIRE
    assert seen["repost"] == rc.CODE_JOINT
    assert "erased object referenced" in _logged(caplog)
    assert model.complete_reconciliation("rec1", []) == (
        None, [rc.OBJECT_MISSING_TO_COMPLETE])


@both
def test_a_completion_between_the_pre_read_and_the_transaction_freezes_the_first(
        db, bucket, register):
    """The reconciliation is completed (on its first statement) after the
    finalization's pre-read, before its transaction: the transaction's own
    re-check refuses FROZEN, and only this call's copy is erased."""
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    first = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", first["objet"], UID).code == rc.CODE_JOINT
    kept = _stored(db, register)["statement_storage_path"]
    second = _open_and_put(bucket, register, data=PNG, name="s.png")

    def _complete_now(dest, source):
        bucket.rewrite_hooks.clear()
        assert model.complete_reconciliation("rec1", [])[1] == []

    bucket.rewrite_hooks.append(_complete_now)
    out = rc.file_staged_statement(register, "rec1", second["objet"], UID)
    assert out.code == rc.CODE_REFUS and out.errors == [rc.FROZEN]
    assert _stored(db, register)["statement_storage_path"] == kept
    assert _object_names(bucket) == [kept]


@both
@pytest.mark.parametrize("apply", [True, False])
def test_a_lost_commit_answer_is_settled_by_a_re_read(db, bucket, register, monkeypatch,
                                                      apply):
    _seed(db, register)
    first = _open_and_put(bucket, register)
    assert rc.file_staged_statement(register, "rec1", first["objet"], UID).code == rc.CODE_JOINT
    old = _stored(db, register)["statement_storage_path"]
    second = _open_and_put(bucket, register, data=PNG, name="s.png")
    _break_commit(monkeypatch, db, register, apply=apply, kind="update")
    out = rc.file_staged_statement(register, "rec1", second["objet"], UID)
    rec = _stored(db, register)
    if apply:       # landed: « joint », the replaced object erased
        assert out.code == rc.CODE_JOINT
        assert _object_names(bucket) == [rec["statement_storage_path"]]
    else:           # absent: this call's copy erased, the first kept
        assert out.code == rc.CODE_INDISPONIBLE
        assert rec["statement_storage_path"] == old and _object_names(bucket) == [old]
    _break_commit(monkeypatch, db, register, apply=apply, kind="update")
    out = rc.remove_statement(register, "rec1")
    rec = _stored(db, register)
    if apply:
        assert out.code == rc.CODE_RETIRE and not rc.has_statement(rec)
        assert _object_names(bucket) == []
    else:
        assert out.code == rc.CODE_INDISPONIBLE and rc.has_statement(rec)


@both
def test_an_erase_refuses_a_stored_path_not_of_the_module_s_shape(db, bucket, register,
                                                                 caplog):
    foreign = f"users/{UID}/parties/p1/pieces_identite/x.pdf"
    bucket.put(foreign, PDF, content_type="application/pdf")
    caplog.set_level(logging.DEBUG)
    rc.erase_statement_object(register, {
        "id": "rec1", "statement_id": "3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b",
        "statement_storage_path": foreign})
    assert foreign in bucket.objects and "stored path refused" in _logged(caplog)


# ══════════════════════════════════════════════════════════════════════
# 7. Rules 5 and 7, and the account pages
# ══════════════════════════════════════════════════════════════════════


@both
def test_days_and_stamps_follow_rules_5_and_7(web, db, bucket, signed, register):
    """The period's end is a date-only value (its UTC day: November, never
    Montréal's October 31); the attachment is an instant, stored in UTC and
    shown on its Montréal day; the removal stamps the record (Rule 7)."""
    _seed(db, register, period_end=datetime(2026, 11, 1, tzinfo=UTC))
    assert _upload(web, bucket, register).status_code == 200
    stored = _stored(db, register)
    assert stored["statement_attached_at"].utcoffset().total_seconds() == 0
    web.get(_url(register, tail="/releve"))
    assert "releve-2026-11.pdf" in signed[0]["params"]["response-content-disposition"]
    db.seed(f"{rc.REGISTERS[register]}/rec1", {
        **stored, "statement_attached_at": datetime(2026, 11, 2, 1, 30, tzinfo=UTC)})
    assert "joint le 1er novembre 2026" in _worksheet(web, register)
    etag = _stored(db, register)["etag"]
    web.post(_url(register, tail="/releve/retirer"))
    after = _stored(db, register)
    assert after["etag"] != etag and after["updated_via"] == "web"


@both
def test_the_account_page_badges_a_missing_statement(web, db, bucket, register):
    prefix, model, _account = REGISTERS[register]
    _seed(db, register, "a", status="complétée")
    _seed(db, register, "b", status="complétée")
    joindre(model, "b")
    page = web.get(f"{prefix}/comptes/acc1").get_data(as_text=True)
    assert page.count("Relevé manquant") == 1


@both
def test_no_file_name_is_ever_logged(web, db, bucket, register, caplog):
    _seed(db, register)
    caplog.set_level(logging.DEBUG)
    assert _upload(web, bucket, register, name="Tremblay-relevé.pdf").status_code == 200
    web.post(_url(register, tail="/releve/retirer"))
    logged = " ".join(r.getMessage() + repr(getattr(r, "json_fields", ""))
                      for r in caplog.records)
    assert "Tremblay" not in logged and "storage.example" not in logged


@both
@pytest.mark.parametrize("status", ["brouillon", "complétée"])
def test_an_adopter_that_patched_first_keeps_the_object(db, bucket, register,
                                                       monkeypatch, status):
    """The second review's ordering: A creates the object; B (the page's
    re-post of the SAME upload) gets 412, adopts and PATCHES it before A's
    own patch; A's commit fails, unapplied; then B commits. A's own patch is
    pinned to the metageneration its copy produced, so it answers 412 and A
    treats the object as adopted: its rollback erases nothing, and the
    reconciliation (a draft, or a catch-up — then frozen) points at bytes
    that exist. Threads, each wait bounded."""
    import threading

    _seed(db, register, status=status)
    doc = _open_and_put(bucket, register)
    real_patch = FakeBlob.patch
    b_patched, a_done = threading.Event(), threading.Event()
    state = {"first": True, "b_thread": None, "b_out": None}

    def run_b():
        state["b_out"] = rc.file_staged_statement(register, "rec1", doc["objet"], UID)

    def _patch(self, *args, **kwargs):
        if not self.name.startswith("users/"):
            return real_patch(self, *args, **kwargs)
        if threading.current_thread() is threading.main_thread():
            if state["first"]:
                state["first"] = False
                state["b_thread"] = threading.Thread(target=run_b)
                state["b_thread"].start()
                assert b_patched.wait(10)
            return real_patch(self, *args, **kwargs)
        out = real_patch(self, *args, **kwargs)        # B's adoption patch
        b_patched.set()
        assert a_done.wait(10)
        return out

    monkeypatch.setattr(FakeBlob, "patch", _patch)
    _break_commit(monkeypatch, db, register, apply=False, kind="update")
    first = rc.file_staged_statement(register, "rec1", doc["objet"], UID)
    a_done.set()
    state["b_thread"].join(10)
    assert not state["b_thread"].is_alive()
    assert first.code == rc.CODE_INDISPONIBLE and state["b_out"].code == rc.CODE_JOINT
    rec = _stored(db, register)
    assert rec["statement_storage_path"] in bucket.objects


def test_an_uncertain_upload_keeps_the_completion_blocked():
    """After « incertain », the page cannot know which statement the record
    holds: from then on NO outcome — a later refusal included (the third
    review) — signals « done »; only a reload re-reads the record, and the
    uncertain sentence stays on screen."""
    src = (_ATHENA / "templates" / "components" / "_releve_conciliation.html").read_text(
        encoding="utf-8")
    script = src[src.index("function releveConciliation() {"):]
    # The ONE place that releases « Compléter » …
    assert script.count("this.signaler(false);") == 1
    envoyer = script[script.index("    async envoyer() {"):]
    envoyer = envoyer[:envoyer.index("\n    },")]
    # … sits after a guard that a sticky flag closes for the page's lifetime.
    sticky = envoyer.index("if (this.fichier.statut === 'incertain') { this.incertain = true; }")
    assert sticky < envoyer.index("if (this.incertain) {") < envoyer.index(
        "this.signaler(false);")
    guard = envoyer[envoyer.index("if (this.incertain) {"):envoyer.index("this.signaler(false);")]
    assert "return;" in guard
    assert "incertain: false," in script and "incertainTexte: CONFIG.incertain," in script
    # Shown once: while the file's own line says it, the sticky one waits.
    sticky_line = ("<p x-show=\"incertain && !(fichier && fichier.statut === 'incertain')\""
                   " x-cloak class=\"mt-2 text-xs text-amber-700\" x-text=\"incertainTexte\"></p>")
    assert sticky_line in src
    # « avant toute autre action » — restarting, or completing a draft;
    # true of a catch-up too, whose reconciliation is already complete.
    assert "avant toute autre action" in rc.FILING_UNCERTAIN


@both
def test_an_unsettled_abandon_of_a_draft_without_statement_names_no_statement(
        db, bucket, register, monkeypatch, caplog):
    _prefix, model, _account = REGISTERS[register]
    _seed(db, register)
    caplog.set_level(logging.DEBUG)
    _break_commit(monkeypatch, db, register, apply=True, reads_fail=True, kind="delete")
    ok, _errors = model.delete_reconciliation("rec1")
    assert ok is False
    (line,) = [r for r in caplog.records if r.getMessage().endswith("abandon uncertain")]
    assert line.json_fields["register"] == register
    assert "statement_id" not in line.json_fields
