"""Le connecteur tient les contacts — lot 4b (famille CONTACTS, D6, D7).

* ``update_partie_mandataire`` — UNE représentation d'un contact à la fois,
  par les aides du modèle (lot 4a) : la liste est rebâtie depuis celle qui
  est STOCKÉE, les règles aller et retour du modèle s'appliquent à tous les
  chemins, un détachement retire un LIEN (le mandataire reste) et se
  journalise.
* ``record_kyc_status`` — la vérification d'identité ou de conflits
  INSCRITE par Claude, toujours de source « mcp », donc PRÉSUMÉE : la fiche
  l'affiche « à confirmer », le rapport de couverture la garde ouverte, et
  seul le juriste la confirme. Jamais par-dessus une vérification que le
  juriste a consignée ou confirmée. Ses notes s'AJOUTENT sous une ligne
  datée, et la chaîne STOCKÉE entière est vérifiée (le piège de TAG_RE qui
  enjambe la jointure).
* Le vocabulaire de 2026-11 (section 4) : « identifié » s'inscrit présumé
  comme « vérifié » ; « exempté », retiré, n'est plus ni offert ni accepté,
  et une valeur héritée se lit « identifié » — décidée, donc hors de portée
  du connecteur quand le juriste l'a consignée.

Tout passe par le vrai client Firestore au-dessus du faux serveur partagé ;
on relit ce qui est STOCKÉ.
"""

import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import dossier as dossier_model
    from models import partie as partie_model
    from models import provenance
    from utils import deadlines, kyc

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, write_support)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.")
                   or n in ("dav.sync", "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    return install(monkeypatch, *modules)


def _contact(db, pid, first, last, **over):
    doc = {**partie_model._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": first, "last_name": last,
           "etag": f"e-{pid}", "created_at": DT, "updated_at": DT}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)


def _stored(db, pid):
    return db.peek(f"parties/{pid}")


def _ctag(db):
    return (db.peek("dav_sync/parties") or {}).get("ctag")


@pytest.fixture
def represented(db):
    """Jean (client) represented by m1 (a tutor); m2 a candidate of the same
    role; o1 an organization; a1 an individual of another role."""
    _contact(db, "m1", "Marc", "Roy")
    _contact(db, "m2", "Anne", "Gagnon")
    _contact(db, "o1", "", "", type="organization",
             organization_name="Béton Nord inc.")
    _contact(db, "a1", "Paul", "Adverse", contact_role="partie_adverse")
    _contact(db, "p1", "Jean", "Tremblay", mandataires=[
        {"id": "m1", "kind": "tuteur", "notes": "Jugement du 2025-02-01"}])
    return "p1"


# ══════════════════════════════════════════════════════════════════════
# 1. update_partie_mandataire
# ══════════════════════════════════════════════════════════════════════


def test_the_kind_literal_is_the_model_s_vocabulary():
    enum = tools.TOOLS["update_partie_mandataire"]["input_schema"][
        "properties"]["kind"]["enum"]
    assert enum == list(partie_model.MANDATAIRE_KIND_LABELS)


def test_add_appends_to_the_stored_list_and_bumps_the_addressbook(db, represented):
    before = _ctag(db)
    payload = handlers.update_partie_mandataire({
        "action": "add", "partie_id": "p1", "mandataire_partie_id": "m2",
        "kind": "mandataire", "notes": "Procuration du 2026-01-10"})

    stored = _stored(db, "p1")
    assert stored["mandataires"] == [
        {"id": "m1", "kind": "tuteur", "notes": "Jugement du 2025-02-01"},
        {"id": "m2", "kind": "mandataire",
         "notes": "Procuration du 2026-01-10"},
    ]
    assert stored["updated_via"] == "mcp"
    assert _ctag(db) != before
    assert payload["outcome"] == "applied"
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is True
    assert payload["mandataire"] == {"partie_id": "m2", "kind": "mandataire",
                                     "has_notes": True}
    assert payload["mandataires_count"] == 2
    assert payload["entity"]["etag"] == stored["etag"]


def test_the_same_representation_again_writes_nothing_and_never_bumps(db, represented):
    before = _ctag(db)
    db.reset_logs()
    payload = handlers.update_partie_mandataire({
        "action": "add", "partie_id": "p1", "mandataire_partie_id": "m1",
        "kind": "tuteur", "notes": "Jugement du 2025-02-01"})
    assert db.commits == []
    assert _ctag(db) == before
    assert payload["outcome"] == "unchanged"
    assert payload["ctag_bumped"] is False and payload["dav_synced"] is False
    assert any("déjà ainsi" in w for w in payload["warnings"])


def test_adding_a_listed_mandataire_otherwise_points_to_update(db, represented):
    with pytest.raises(tools.ToolArgumentError, match="action update"):
        handlers.update_partie_mandataire({
            "action": "add", "partie_id": "p1", "mandataire_partie_id": "m1",
            "kind": "curateur"})


@pytest.mark.parametrize("mid, fragment", [
    ("o1", "personne physique"),
    ("a1", "même rôle"),
    ("p1", "propre mandataire"),
    ("ghost", "list_parties"),
])
def test_add_meets_the_model_s_rules(db, represented, mid, fragment):
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.update_partie_mandataire({
            "action": "add", "partie_id": "p1", "mandataire_partie_id": mid,
            "kind": "mandataire"})
    assert _stored(db, "p1") == before


def test_add_needs_a_kind(db, represented):
    with pytest.raises(tools.ToolArgumentError, match="`kind` est requis"):
        handlers.update_partie_mandataire({
            "action": "add", "partie_id": "p1", "mandataire_partie_id": "m2"})


def test_update_corrects_one_representation_and_leaves_the_others(db, represented):
    handlers.update_partie_mandataire({
        "action": "add", "partie_id": "p1", "mandataire_partie_id": "m2",
        "kind": "mandataire"})
    payload = handlers.update_partie_mandataire({
        "action": "update", "partie_id": "p1", "mandataire_partie_id": "m2",
        "kind": "curateur", "notes": "Nommé le 2026-02-02"})
    entries = _stored(db, "p1")["mandataires"]
    assert entries[0] == {"id": "m1", "kind": "tuteur",
                          "notes": "Jugement du 2025-02-01"}
    assert entries[1] == {"id": "m2", "kind": "curateur",
                          "notes": "Nommé le 2026-02-02"}
    assert payload["outcome"] == "applied"


def test_notes_that_would_be_altered_are_refused_never_stripped(db, represented):
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError, match="chevrons"):
        handlers.update_partie_mandataire({
            "action": "update", "partie_id": "p1",
            "mandataire_partie_id": "m1", "notes": "voir <b>acte</b>"})
    assert _stored(db, "p1") == before


def test_remove_detaches_the_link_journals_it_and_keeps_the_contact(db, represented):
    payload = handlers.update_partie_mandataire({
        "action": "remove", "partie_id": "p1", "mandataire_partie_id": "m1"})

    assert _stored(db, "p1")["mandataires"] == []
    assert _stored(db, "m1") is not None          # the contact stays
    assert payload["journaled"] is True
    assert payload["mandataire"] == {"partie_id": "m1", "kind": "tuteur",
                                     "has_notes": True}
    rows = list(db.peek_collection("audit_events").values())
    assert [(r["entity_type"], r["entity_id"], r["snapshot_min"]["title"])
            for r in rows] == [("mandataire", "m1", "Jean Tremblay")]
    assert any("LIEN est retiré" in w for w in payload["warnings"])


def test_removing_an_unlisted_mandataire_is_refused_pointing_to_get_partie(db, represented):
    with pytest.raises(tools.ToolArgumentError, match="get_partie"):
        handlers.update_partie_mandataire({
            "action": "remove", "partie_id": "p1",
            "mandataire_partie_id": "m2"})


@pytest.mark.parametrize("extra, stray", [
    ({"action": "remove", "kind": "tuteur"}, "kind"),
    ({"action": "remove", "notes": ""}, "notes"),
])
def test_an_argument_the_action_does_not_take_is_refused(db, represented, extra, stray):
    with pytest.raises(tools.ToolArgumentError, match=f"`{stray}`"):
        handlers.update_partie_mandataire({
            "partie_id": "p1", "mandataire_partie_id": "m1", **extra})


def test_a_stale_version_is_refused_and_writes_nothing(db, represented):
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.update_partie_mandataire({
            "action": "remove", "partie_id": "p1",
            "mandataire_partie_id": "m1", "expected_etag": "e-vieille"})
    assert err.value.reason == "stale_etag"
    assert _stored(db, "p1") == before


# ══════════════════════════════════════════════════════════════════════
# 2. record_kyc_status — PRESUMED, never over the lawyer
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status", kyc.DECIDED[kyc.FIELD_IDENTITY])
def test_an_inscription_is_presumed_and_says_so(db, caplog, status):
    """Every DECIDED identity status Claude inscribes is presumed — the
    2026-11 « identifié » exactly as « vérifié »: Claude's, unconfirmed,
    the coverage check left open."""
    _contact(db, "p1", "Jean", "Tremblay")
    before = _ctag(db)
    with caplog.at_level(logging.INFO, logger="pallas.partie"):
        payload = handlers.record_kyc_status({
            "partie_id": "p1", "check": "identity", "status": status})

    stored = _stored(db, "p1")
    assert stored["identity_verified"] == status
    assert stored["identity_verified_source"] == "mcp"
    assert stored["identity_verified_confirmed_at"] is None
    assert stored["identity_verified_date"] is not None
    assert kyc.is_presumed(stored, kyc.FIELD_IDENTITY)
    assert not kyc.is_decided(stored, kyc.FIELD_IDENTITY)   # coverage: open
    assert _ctag(db) != before
    assert payload["outcome"] == "applied"
    assert payload["kyc"]["presumed"] is True
    assert payload["kyc"]["confirmation_required"] is True
    assert payload["kyc"]["source"] == "mcp"
    assert payload["kyc"]["status_before"] == "non_vérifié"
    assert payload["kyc"]["status_after"] == status
    assert any("PRÉSUMÉE" in w and "à confirmer" in w
               for w in payload["warnings"])
    events = [r.json_fields for r in caplog.records
              if getattr(r, "json_fields", {}).get("event") == "kyc_recorded"]
    assert events == [{
        "event": "kyc_recorded", "partie_id": "p1",
        "field": "identity_verified", "via": "mcp", "status_changed": True,
        "presumed": True, "notes_appended": False}]


def test_the_coverage_report_keeps_an_inscription_open(db):
    _contact(db, "p1", "Jean", "Tremblay")
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Roy",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}]})
    assert errors == []
    handlers.record_kyc_status({"partie_id": "p1", "check": "conflict",
                                "status": "vérifié"})

    report = handlers.get_coverage_report({"checks": ["CONFLIT_NON_VERIFIE"]})

    findings = [f for i in report["items"] for f in i["findings"]]
    assert [f["code"] for f in findings] == ["CONFLIT_NON_VERIFIE"]
    assert "inscrite(s) par Claude" in findings[0]["detail"]


@pytest.mark.parametrize("status", kyc.DECIDED[kyc.FIELD_IDENTITY])
def test_the_coverage_report_keeps_an_identity_inscription_open_until_confirmed(
        db, status):
    """The identity variant, over every decided identity status: Claude's
    « identifié » (2026-11) is PRESUMED like its « vérifié » — the finding
    stays open and names Claude — and only the lawyer's « Confirmer »
    closes it (the model call the fiche's route makes: web provenance,
    ``par=juriste``)."""
    _contact(db, "p1", "Jean", "Tremblay")
    _dossier, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Roy",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}]})
    assert errors == []
    handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                "status": status})

    def findings():
        report = handlers.get_coverage_report(
            {"checks": ["IDENTITE_NON_VERIFIEE"]})
        assert report["data_completeness"]["kyc_checked"] is True
        return [f for i in report["items"] for f in i["findings"]]

    open_ = findings()
    assert [f["code"] for f in open_] == ["IDENTITE_NON_VERIFIEE"]
    assert "Dont 1 inscrite(s) par Claude" in open_[0]["detail"]

    with provenance.writing_via("web"):
        _doc, errors = partie_model.confirm_kyc_status(
            "p1", kyc.FIELD_IDENTITY, par=kyc.SOURCE_JURISTE,
            expected_etag=_stored(db, "p1")["etag"])
    assert errors == []
    assert _stored(db, "p1")["identity_verified"] == status
    assert findings() == []


@pytest.mark.parametrize("stored", [
    # The lawyer decided it (legacy: no source at all).
    {"identity_verified": "vérifié", "identity_verified_date": DT},
    {"identity_verified": "identifié", "identity_verified_date": DT,
     "identity_verified_source": "juriste"},
    # Claude inscribed it, the lawyer CONFIRMED it: his attestation now.
    {"identity_verified": "vérifié", "identity_verified_date": DT,
     "identity_verified_source": "mcp", "identity_verified_confirmed_at": DT,
     "identity_verified_confirmed_by": "juriste"},
])
@pytest.mark.parametrize("call", [
    {"status": "non_vérifié"},
    {"status": "identifié"},
    {"status": "vérifié", "notes": "Ajout sur une décision du juriste."},
])
def test_record_kyc_status_never_changes_or_confirms_the_lawyer_s_attestation(
        db, stored, call):
    """The « kyc » promise of the consent screen (mcp/disclosure). Over a
    check the lawyer decided or confirmed, every connector write — a new
    status, a withdrawal, even notes alone — is refused and nothing is
    written; and no connector path can confirm an inscription."""
    _contact(db, "p1", "Jean", "Tremblay", **stored)
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                    **call})
    assert err.value.reason == "kyc_lawyer_attestation"
    assert _stored(db, "p1") == before
    # The confirmation is the lawyer's gesture alone: under the connector's
    # provenance the model refuses it, whatever the version presented.
    with provenance.writing_via("mcp", tool="record_kyc_status"):
        _doc, errors = partie_model.confirm_kyc_status(
            "p1", kyc.FIELD_IDENTITY, par="claude",
            expected_etag=before["etag"])
    assert errors == [partie_model.KYC_CONFIRM_APP_ONLY]
    assert _stored(db, "p1") == before


def test_a_lawyer_decision_landing_before_the_commit_is_never_overwritten(db, monkeypatch):
    """The check is made on the handler's read; the write is compare-and-set
    against it — a decision the lawyer records in between refuses the call
    instead of being replaced by Claude's inscription."""
    _contact(db, "p1", "Jean", "Tremblay")
    real = partie_model.get_partie
    fired = []

    def racing(pid):
        doc = real(pid)
        if not fired:
            fired.append(True)
            db.external_write("parties/p1", {
                **db.peek("parties/p1"), "identity_verified": "identifié",
                "identity_verified_source": "juriste", "etag": "e-juriste"})
        return doc

    monkeypatch.setattr(partie_model, "get_partie", racing)
    with pytest.raises(tools.ToolArgumentError):
        handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                    "status": "vérifié"})
    stored = _stored(db, "p1")
    assert stored["identity_verified"] == "identifié"
    assert stored["identity_verified_source"] == "juriste"


def test_claude_may_change_or_withdraw_its_own_presumed_inscription(db):
    _contact(db, "p1", "Jean", "Tremblay")
    handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                "status": "vérifié"})
    handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                "status": "identifié"})
    assert _stored(db, "p1")["identity_verified"] == "identifié"
    assert kyc.is_presumed(_stored(db, "p1"), kyc.FIELD_IDENTITY)

    payload = handlers.record_kyc_status({"partie_id": "p1",
                                          "check": "identity",
                                          "status": "non_vérifié"})
    stored = _stored(db, "p1")
    assert stored["identity_verified"] == "non_vérifié"
    assert stored["identity_verified_source"] == ""
    assert stored["identity_verified_date"] is None
    assert payload["kyc"]["presumed"] is False
    # The withdrawal names the status as the fiche labels it (2026-11).
    assert any("retirée" in w and "redevient « À faire »" in w
               for w in payload["warnings"])
    assert not any("non vérifiée" in w for w in payload["warnings"])


def test_notes_are_appended_under_a_dated_line_never_replacing(db):
    _contact(db, "p1", "Jean", "Tremblay",
             conflict_check_notes="Recherche faite au registre, 2026-01.")
    payload = handlers.record_kyc_status({
        "partie_id": "p1", "check": "conflict", "status": "vérifié",
        "notes": "Aucun lien avec la partie adverse."})

    stored = _stored(db, "p1")["conflict_check_notes"]
    day = deadlines.today_mtl().isoformat()
    assert stored == (
        "Recherche faite au registre, 2026-01.\n\n"
        f"[{day} — inscrit par Claude] Aucun lien avec la partie adverse.")
    assert payload["kyc"]["notes_appended"] is True
    assert "Aucun lien" not in repr(payload)      # never quoted back


def test_the_whole_stored_string_is_checked_across_the_join(db):
    """TAG_RE spans lines: an unpaired « < » already stored plus a « > » in
    the addition would delete ACROSS the join. Each half survives alone;
    the combination is refused, and nothing is written."""
    _contact(db, "p1", "Jean", "Tremblay",
             identity_verified_notes="Seuil < 10 000 $")
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError, match="déjà enregistrées"):
        handlers.record_kyc_status({
            "partie_id": "p1", "check": "identity", "status": "vérifié",
            "notes": "Montant > seuil vérifié"})
    assert _stored(db, "p1") == before


def test_notes_past_the_stored_ceiling_are_refused_never_truncated(db):
    _contact(db, "p1", "Jean", "Tremblay",
             identity_verified_notes="x" * 1500)
    with pytest.raises(tools.ToolArgumentError, match="2000"):
        handlers.record_kyc_status({
            "partie_id": "p1", "check": "identity", "status": "vérifié",
            "notes": "y" * 600})


@pytest.mark.parametrize("check, status, allowed", [
    ("identity", "conflit_détecté", "identifié, vérifié, non_vérifié."),
    # « identifié » is the identity check's alone; the status enum is ONE
    # list for both checks, so the schema lets it through and the handler
    # refuses it here.
    ("conflict", "identifié", "non_vérifié, vérifié, conflit_détecté."),
], ids=["identity", "conflict"])
def test_a_status_outside_the_check_s_vocabulary_is_refused_by_name(
        db, check, status, allowed):
    _contact(db, "p1", "Jean", "Tremblay")
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError,
                       match=re.escape(f"« {check} », {allowed}")):
        handlers.record_kyc_status({"partie_id": "p1", "check": check,
                                    "status": status})
    assert _stored(db, "p1") == before


def test_the_status_enum_is_derived_from_the_model_vocabulary():
    enum = tools.TOOLS["record_kyc_status"]["input_schema"]["properties"][
        "status"]["enum"]
    assert set(enum) == set(kyc.IDENTITY_STATUSES) | set(kyc.CONFLICT_STATUSES)
    assert set(partie_model.VALID_IDENTITY_STATUSES) <= set(enum)
    assert set(partie_model.VALID_CONFLICT_STATUSES) <= set(enum)


def test_only_a_client_carries_a_compliance_inscription(db):
    _contact(db, "a1", "Paul", "Adverse", contact_role="partie_adverse")
    before = _stored(db, "a1")
    with pytest.raises(tools.ToolArgumentError, match="que pour un client"):
        handlers.record_kyc_status({"partie_id": "a1", "check": "identity",
                                    "status": "vérifié"})
    assert _stored(db, "a1") == before


def test_a_dossier_client_of_another_role_is_a_client(db):
    """The fiche shows Conformité to any client of a dossier whatever its
    contact_role (routes/parties), so the inscription is visible there."""
    _contact(db, "t1", "Luc", "Témoin", contact_role="témoin")
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-003", "title": "Témoin c. X",
        "clients": [{"id": "t1", "name": "Luc Témoin", "roles": []}]})
    assert errors == []
    payload = handlers.record_kyc_status({"partie_id": "t1",
                                          "check": "conflict",
                                          "status": "vérifié"})
    assert payload["kyc"]["presumed"] is True


def test_unreadable_dossiers_refuse_a_non_client_role(db, monkeypatch):
    _contact(db, "t1", "Luc", "Témoin", contact_role="témoin")

    def boom(_pid):
        raise RuntimeError("down")

    monkeypatch.setattr(dossier_model, "list_dossiers_for_partie_strict", boom)
    with pytest.raises(tools.ToolArgumentError, match="pas pu être lus"):
        handlers.record_kyc_status({"partie_id": "t1", "check": "identity",
                                    "status": "vérifié"})


def test_the_same_status_without_notes_writes_nothing(db):
    _contact(db, "p1", "Jean", "Tremblay")
    handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                "status": "vérifié"})
    before = _ctag(db)
    db.reset_logs()
    payload = handlers.record_kyc_status({"partie_id": "p1",
                                          "check": "identity",
                                          "status": "vérifié"})
    assert db.commits == []
    assert _ctag(db) == before
    assert payload["outcome"] == "unchanged"
    assert payload["ctag_bumped"] is False


def test_a_detected_conflict_is_flagged_for_the_lawyer(db):
    _contact(db, "p1", "Jean", "Tremblay")
    payload = handlers.record_kyc_status({"partie_id": "p1",
                                          "check": "conflict",
                                          "status": "conflit_détecté"})
    assert any("CONFLIT D'INTÉRÊTS" in w for w in payload["warnings"])


def test_get_partie_says_who_decided_each_check(db):
    _contact(db, "p1", "Jean", "Tremblay", conflict_check="vérifié",
             conflict_check_date=DT)
    handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                "status": "vérifié"})
    card = handlers.get_partie({"partie_id": "p1"})["partie"]
    assert card["identity_verified_source"] == "mcp"
    assert card["identity_verified_presumed"] is True
    assert card["identity_verified_confirmed_at"] is None
    assert card["conflict_check_source"] == "juriste"
    assert card["conflict_check_presumed"] is False


def test_the_contact_edit_tools_still_refuse_compliance_and_mandataires():
    """The dedicated tools are the ONLY path: create_partie / update_partie
    never take a compliance status or the mandataires list."""
    for name in ("create_partie", "update_partie"):
        props = tools.TOOLS[name]["input_schema"]["properties"]
        for field in ("identity_verified", "conflict_check", "mandataires",
                      "identity_verified_source"):
            assert field not in props, (name, field)


# ══════════════════════════════════════════════════════════════════════
# 3. Review of step 3 — the texts quote the fiche, and an alarm never
#    leaves in silence
# ══════════════════════════════════════════════════════════════════════


with mock.patch("google.cloud.firestore.Client"):
    from mcp import disclosure  # noqa: E402


def _fiche_view(partie: dict) -> dict:
    """The Conformité view as the ROUTE composes it — the fiche's text."""
    with mock.patch("google.cloud.firestore.Client"):
        import routes.parties as parties_routes
    with mock.patch.object(parties_routes, "_compliance_signer",
                           lambda: "Me Test"):
        return parties_routes._kyc_view(partie)


@pytest.mark.parametrize("status", kyc.DECIDED[kyc.FIELD_IDENTITY])
def test_the_presumed_warning_quotes_the_fiche_as_it_is_composed(db, status):
    """The warning promised « Vérifié (présumé) — inscrit par Claude, à
    confirmer », a line the fiche never shows: it renders the badge and
    « inscrit par Claude le <jour> — à confirmer ». Quoted as composed —
    for every decided identity status, so the 2026-11 « identifié » reads
    « Identifié (présumé) » in both."""
    _contact(db, "p1", "Jean", "Tremblay")
    payload = handlers.record_kyc_status({
        "partie_id": "p1", "check": "identity", "status": status})

    view = _fiche_view(_stored(db, "p1"))["identite"]
    assert view["presumed"] is True
    assert view["label"] == f"{kyc.STATUS_LABELS[status]} (présumé)"
    warning = next(w for w in payload["warnings"] if "PRÉSUMÉE" in w)
    assert f"« {view['label']} »" in warning
    assert f"« {view['attribution']} »" in warning
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the fiche's
    # line was quoted by the tool description AND the CONTACTS paragraph of
    # INSTRUCTIONS. The paragraph became a one-line index entry — the quote
    # lives in the description alone now; the index line still never
    # carries the wrong one.
    description = tools.TOOLS["record_kyc_status"]["description"]
    assert "inscrit par Claude, à confirmer" not in description
    assert "« inscrit par Claude le … — à confirmer »" in description
    index_line = next(f for f in disclosure.FAMILIES
                      if f.key == "contacts").instructions_en
    assert "inscrit par Claude, à confirmer" not in index_line


def test_replacing_a_presumed_conflict_is_said_to_the_lawyer(db):
    """A presumed « conflit détecté » is an alarm the lawyer may not have
    seen yet: Claude replacing its own inscription — with « vérifié » or a
    withdrawal — must say so, never pass in silence."""
    _contact(db, "p1", "Jean", "Tremblay")
    handlers.record_kyc_status({"partie_id": "p1", "check": "conflict",
                                "status": "conflit_détecté"})

    for status in ("vérifié", "non_vérifié"):
        handlers.record_kyc_status({"partie_id": "p1", "check": "conflict",
                                    "status": "conflit_détecté"})
        payload = handlers.record_kyc_status({
            "partie_id": "p1", "check": "conflict", "status": status})
        assert payload["kyc"]["status_before"] == "conflit_détecté"
        assert any("remplacez un CONFLIT D'INTÉRÊTS présumé" in w
                   for w in payload["warnings"]), status

    # …and the same conflict inscribed again is not a replacement.
    handlers.record_kyc_status({"partie_id": "p1", "check": "conflict",
                                "status": "conflit_détecté"})
    again = handlers.record_kyc_status({
        "partie_id": "p1", "check": "conflict", "status": "conflit_détecté",
        "notes": "Relu."})
    assert not any("remplacez" in w for w in again["warnings"])


@pytest.mark.parametrize("call", [
    lambda: handlers.record_kyc_status({
        "partie_id": "p1", "check": "identity", "status": "vérifié"}),
    lambda: handlers.update_partie_mandataire({
        "action": "remove", "partie_id": "p1",
        "mandataire_partie_id": "m1"}),
], ids=["record_kyc_status", "update_partie_mandataire"])
def test_an_unreadable_contact_is_never_answered_introuvable(
        db, represented, monkeypatch, call):
    """Both tools read the contact through the fail-open get_partie: an
    outage answered « Contact introuvable … Utilisez list_parties », sending
    the caller hunting for — or re-creating — a contact that exists. Read
    strictly now; nothing is written either way."""
    before = _stored(db, "p1")
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if any(str(n).endswith("parties/p1") for n in request["documents"]):
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    with pytest.raises(tools.ToolArgumentError) as err:
        call()
    assert "pas pu être lu" in str(err.value)
    assert "introuvable" not in str(err.value)
    monkeypatch.setattr(server, "batch_get_documents", real)
    assert _stored(db, "p1") == before


def test_an_unreadable_named_contact_is_never_answered_introuvable(
        db, represented, monkeypatch):
    """The contact a link NAMES — a mandataire to add, a party's lawyer —
    was checked through get_partie too: an outage answered « introuvable »."""
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Roy",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}]})
    assert errors == []
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if any(str(n).endswith("parties/m2") for n in request["documents"]):
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    for call in (
        lambda: handlers.update_partie_mandataire({
            "action": "add", "partie_id": "p1",
            "mandataire_partie_id": "m2", "kind": "mandataire"}),
        lambda: handlers.update_dossier_party({
            "action": "update", "dossier_id": doc["id"], "partie_id": "p1",
            "avocat_partie_id": "m2"}),
    ):
        with pytest.raises(tools.ToolArgumentError) as err:
            call()
        assert "pas pu être lu" in str(err.value)
        assert "introuvable" not in str(err.value)


def test_a_detected_conflict_is_the_caller_s_to_report(db):
    """Review of E3: the CONTACTS prose moved into this tool's description
    turned « a detected conflict you inscribe MUST BE reported to the lawyer
    at once » into « … IS reported … » — a claim that the system notifies
    him. Nothing does: the result's warning tells the CALLER to say it, and
    the fiche merely shows it in red. A model told the alarm is raised for
    it would stay silent on the one it must raise. Fails on 3191774."""
    _contact(db, "p1", "Jean", "Tremblay")
    payload = handlers.record_kyc_status({"partie_id": "p1", "check": "conflict",
                                          "status": "conflit_détecté"})
    assert any("signalez-le au juriste sans délai" in w
               for w in payload["warnings"])
    description = tools.TOOLS["record_kyc_status"]["description"]
    assert "is reported to the lawyer" not in description
    assert "tell the lawyer at once — nothing notifies him" in description


# ══════════════════════════════════════════════════════════════════════
# 4. The 2026-11 vocabulary — « identifié » enters, « exempté » retires
#    (utils/kyc: RETIRED_STATUSES, CHECK_FIELDS, choices)
# ══════════════════════════════════════════════════════════════════════


def test_exempte_is_neither_offered_nor_accepted(db):
    """« exempté » is RETIRED: the status enum no longer offers it, the
    schema check endpoint._tools_call runs before any handler refuses it,
    and the handler — the line behind the schema — refuses it naming its
    successor « identifié », writing nothing. Fails on 2b0fbdc: the enum
    offered it and the handler inscribed a presumed exemption."""
    schema = tools.TOOLS["record_kyc_status"]["input_schema"]
    assert "exempté" not in schema["properties"]["status"]["enum"]
    errors = tools.validate_args(schema, {
        "partie_id": "p1", "check": "identity", "status": "exempté"})
    assert len(errors) == 1, errors
    assert errors[0].startswith("`status` must be one of:"), errors
    assert "'identifié'" in errors[0]

    _contact(db, "p1", "Jean", "Tremblay")
    before = _stored(db, "p1")
    db.reset_logs()
    with pytest.raises(tools.ToolArgumentError, match="identifié"):
        handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                    "status": "exempté"})
    assert db.commits == []
    assert _stored(db, "p1") == before


@pytest.mark.parametrize("provenance_keys, source, presumed, confirmed_at", [
    # The lawyer's, recorded before provenance existed: no source at all.
    ({}, "juriste", False, None),
    # Claude's, still presumed.
    ({"identity_verified_source": "mcp"}, "mcp", True, None),
    # Claude's, confirmed by the lawyer.
    ({"identity_verified_source": "mcp", "identity_verified_confirmed_at": DT,
      "identity_verified_confirmed_by": "juriste"}, "mcp", False, DT),
], ids=["lawyer", "presumed", "confirmed"])
def test_get_partie_reads_a_stored_exemption_as_identifie(
        db, provenance_keys, source, presumed, confirmed_at):
    """A stored « exempté » — until the migration script runs, or from a
    restored backup — reads « identifié » on the card get_partie returns,
    never a status record_kyc_status no longer accepts; its date, source
    and confirmation are KEPT: Claude's presumed one stays presumed, the
    lawyer's stays his. The read writes nothing. Fails on 2b0fbdc, which
    knew no retired status: the card said « exempté »."""
    _contact(db, "p1", "Jean", "Tremblay", identity_verified="exempté",
             identity_verified_date=DT, **provenance_keys)
    db.reset_logs()

    card = handlers.get_partie({"partie_id": "p1"})["partie"]

    assert card["identity_verified"] == "identifié"
    assert card["identity_verified_date"] == tools.iso_mtl(DT)
    assert card["identity_verified_source"] == source
    assert card["identity_verified_presumed"] is presumed
    assert card["identity_verified_confirmed_at"] == tools.iso_mtl(confirmed_at)
    assert db.commits == []
    assert _stored(db, "p1")["identity_verified"] == "exempté"


@pytest.mark.parametrize("stored", [
    # The lawyer's, recorded before provenance existed: no source at all.
    {},
    {"identity_verified_source": "juriste"},
], ids=["legacy-no-source", "juriste"])
@pytest.mark.parametrize("call", [
    {"status": "identifié"},
    {"status": "vérifié"},
    {"status": "non_vérifié"},
    {"status": "identifié", "notes": "Ajout sur une décision du juriste."},
], ids=["successor", "verifie", "withdrawal", "notes"])
def test_a_legacy_lawyer_exemption_is_still_his_attestation(db, stored, call):
    """« exempté » left the vocabulary, not the store: the lawyer's stored
    one reads « identifié », DECIDED, so every connector write over it —
    its successor too, a withdrawal, notes alone — is refused as his
    attestation, and nothing is written. Read raw, it would be a status no
    vocabulary knows: an undecided check Claude could overwrite. Fails on
    2b0fbdc for the « identifié » calls: that vocabulary refused the status
    by name, never as the lawyer's attestation."""
    _contact(db, "p1", "Jean", "Tremblay", identity_verified="exempté",
             identity_verified_date=DT, **stored)
    before = _stored(db, "p1")
    with pytest.raises(tools.ToolArgumentError) as err:
        handlers.record_kyc_status({"partie_id": "p1", "check": "identity",
                                    **call})
    assert err.value.reason == "kyc_lawyer_attestation"
    assert _stored(db, "p1") == before


@pytest.mark.parametrize("status", ["exempté", "identifié"],
                         ids=["legacy-exempte", "identifie"])
def test_the_lawyer_s_identification_closes_the_coverage_check_under_either_key(
        db, status):
    """Through the real handler — get_parties_bulk, then the coverage
    predicates: a client the lawyer recorded « exempté » (kept in the store
    until the migration script runs) reads « identifié », DECIDED — no
    false manquement on a deontological check. A control client « À
    faire » in another dossier proves the check ran. Fails on 2b0fbdc for
    « identifié »: that vocabulary did not know it, and raised
    IDENTITE_NON_VERIFIEE over the lawyer's identification."""
    _contact(db, "p1", "Jean", "Tremblay", identity_verified=status,
             identity_verified_date=DT, identity_verified_source="juriste")
    _contact(db, "p2", "Anne", "Gagnon")
    for number, pid, name in (("2026-001", "p1", "Jean Tremblay"),
                              ("2026-002", "p2", "Anne Gagnon")):
        _dossier, errors = dossier_model.create_dossier({
            "file_number": number, "title": f"{name} c. Roy",
            "clients": [{"id": pid, "name": name, "roles": ["demandeur"]}]})
        assert errors == []

    report = handlers.get_coverage_report(
        {"checks": ["IDENTITE_NON_VERIFIEE"]})

    assert report["data_completeness"]["kyc_checked"] is True
    flagged = [(item["file_number"], [f["code"] for f in item["findings"]])
               for item in report["items"]]
    assert flagged == [("2026-002", ["IDENTITE_NON_VERIFIEE"])]


def test_the_status_description_derives_every_label_and_names_no_retired_status():
    """The tool's texts are DERIVED from utils/kyc (one source per list):
    the status description lists each check's statuses in display order,
    each with the label the fiche shows; the description names the decided
    list, and « À faire » as the fiche's label of non_vérifié. Nothing in
    the tool names the retired « exempté ». Fails on 2b0fbdc: a
    hand-written « identity: non_vérifié, vérifié, exempté. » without a
    single label."""
    spec = tools.TOOLS["record_kyc_status"]
    status_text = spec["input_schema"]["properties"]["status"]["description"]
    for check, field in kyc.CHECK_FIELDS.items():
        listed = ", ".join(f"{key} (« {label} »)"
                           for key, label in kyc.choices(field))
        assert f"{check}: {listed}." in status_text, check
    decided = ", ".join(dict.fromkeys(
        kyc.DECIDED[kyc.FIELD_IDENTITY] + kyc.DECIDED[kyc.FIELD_CONFLICT]))
    assert f"a decided status — {decided} — whose" in spec["description"]
    todo = kyc.STATUS_LABELS[kyc.NON_VERIFIE]
    assert f"{kyc.NON_VERIFIE} (« {todo} ») withdraws" in spec["description"]
    assert "exempt" not in repr(spec).lower()


def test_the_check_names_come_from_the_kyc_module_alone():
    """record_kyc_status's `check` enum and its handler's map were two hand
    copies of one mapping; both read utils/kyc.CHECK_FIELDS now, which names
    every compliance field. Fails on 2b0fbdc: no CHECK_FIELDS there — a
    literal ["identity", "conflict"] beside a dict of the handler's own."""
    check = tools.TOOLS["record_kyc_status"]["input_schema"]["properties"][
        "check"]
    assert check["enum"] == list(kyc.CHECK_FIELDS)
    assert handlers._KYC_CHECK_FIELDS is kyc.CHECK_FIELDS
    assert sorted(kyc.CHECK_FIELDS.values()) == sorted(kyc.FIELDS)
