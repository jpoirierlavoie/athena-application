"""La fiche d'un contact dit QUI a décidé une vérification — D7, lot 4a.

Une vérification d'identité ou de conflits décidée s'affichait « le … par
Me … », sans aucune provenance enregistrée : le jour où le connecteur peut
en inscrire une (lot 4b), l'inscription de Claude se serait lue, à l'écran,
exactement comme l'attestation du juriste. Désormais :

* une inscription PRÉSUMÉE (Claude, non confirmée) porte un badge AMBRE
  « Vérifié (présumé) », jamais le vert d'une identité vérifiée, et la
  mention « inscrit par Claude le … — à confirmer » — le nom du signataire
  n'y est JAMAIS accolé ;
* le bouton « Confirmer » (POST, CSRF, etag) est le seul chemin qui en fait
  l'attestation du juriste ;
* une confirmation se lit « confirmé le … par Me … (inscrit par Claude le …) ».

Vocabulaire de 2026-11 (D1-D3, section 5) : l'identité se lit « Identifié »
(le bleu qu'avait « Exempté »), « Vérifié » ou « À faire » ; un « exempté »
encore stocké se lit « Identifié », daté et signé. Les deux listes du
formulaire dérivent de ``utils/kyc`` et marquent la valeur courante côté
SERVEUR : la liste d'identité s'ouvre sur « Identifié », qu'une liste sans
option correspondante publierait comme l'attestation du juriste. D5 : une
création par la route, redirection suivie, se lit « le … par Me … » sur la
fiche — avec le VRAI signataire aussi, lu dans ``settings/cabinet``.

Vraies routes, vrais gabarits, faux Firestore partagé, VRAI filtre ``jsattr``
(``utils/html_attr`` — des tests lisent ``x-data``) ; on relit ce que le
navigateur reçoit et ce qui est STOCKÉ.
"""

import itertools
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from html import unescape
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    from models import dossier as dossier_model
    from models import partie as pm
    # settings/cabinet — what the REAL signer reads (utils.cabinet).
    from models import settings as settings_model
    import routes.doc_templates as doc_templates_routes
    import routes.dossiers as dossiers_routes
    import routes.parties as parties_routes
    import routes.reception as reception_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import kyc  # noqa: E402
from utils.format_fr import format_cents_fr, format_date_fr  # noqa: E402
from utils.html_attr import jsattr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, dossier_model, settings_model)

# The REAL signer, captured at import — before the `db` fixture stubs it
# for every test: the D5 variant reads settings/cabinet through it.
_REAL_COMPLIANCE_SIGNER = parties_routes._compliance_signer

UTC = timezone.utc
SIGNER = "Me Jason Test"
INSCRIBED = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
CONFIRMED = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(parties_routes, "_compliance_signer", lambda: SIGNER)
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    # The REAL jsattr (utils/html_attr: « a test that asserts on x-data
    # output registers THIS function »), never the `lambda v: v` stub.
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr, markdown=markdown_to_safe_html,
    )
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               doc_templates_routes.doc_templates_bp,
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed(db, pid="p1", **over):
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Jean",
           "last_name": "Tremblay", "etag": f"e-{pid}",
           "created_at": INSCRIBED, "updated_at": INSCRIBED}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return pid


PRESUMED = dict(identity_verified="vérifié", identity_verified_date=INSCRIBED,
                identity_verified_source="mcp")
CONFIRMED_BY_LAWYER = dict(
    identity_verified="vérifié", identity_verified_date=INSCRIBED,
    identity_verified_source="mcp", identity_verified_confirmed_at=CONFIRMED,
    identity_verified_confirmed_by="juriste")
LAWYERS = dict(identity_verified="identifié", identity_verified_date=INSCRIBED,
               identity_verified_source="")
# A record the migration script has not rewritten yet (D3): the retired
# status, with the lawyer's provenance.
LEGACY_EXEMPTE = dict(identity_verified="exempté",
                      identity_verified_date=INSCRIBED,
                      identity_verified_source="")


def _conformite(html: str) -> str:
    """The Conformité card only — its end is the next card's heading."""
    start = html.index(">Conformité</h2>")
    end = html.index("Dossiers associés", start)
    return html[start:end]


def _identite(html: str) -> str:
    block = _conformite(html)
    return block[block.index("Identité"):block.index("Conflit d")]


def _conflit(html: str) -> str:
    block = _conformite(html)
    return block[block.index("Conflit d"):]


_BADGE = re.compile(
    r'<span class="inline-flex px-2 py-0\.5 text-xs font-medium rounded-full '
    r'([^"]+)">\s*([^<]*?)\s*</span>')


def _badge(block: str) -> tuple[str, str]:
    """``(colour classes, label)`` of the first status badge in *block*."""
    m = _BADGE.search(block)
    assert m, "no badge"
    return m.group(1), unescape(m.group(2))


# The edit form's two compliance selects, by id, and the field each posts.
_SELECTS = (("kyc-statut-identite", kyc.FIELD_IDENTITY),
            ("kyc-statut-conflit", kyc.FIELD_CONFLICT))


def _options(html: str, select_id: str) -> list[tuple[str, str, bool]]:
    """``(value, label, selected)`` of each option of one select, in order —
    ``selected`` as the SERVER rendered it (no Alpine runs here)."""
    m = re.search(rf'<select id="{select_id}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"no select #{select_id}"
    return [(unescape(value), unescape(label).strip(), bool(selected))
            for value, selected, label in re.findall(
                r'<option value="([^"]*)"\s*(selected)?\s*>([^<]*)</option>',
                m.group(1))]


def _selected(html: str, select_id: str) -> list[str]:
    return [value for value, _label, sel in _options(html, select_id) if sel]


def _posted_as_rendered(html: str) -> dict[str, str]:
    """What a browser posts for each select when Alpine has not run: the
    option marked ``selected`` (the last one, if several), else the FIRST
    option — « Identifié », for the identity list."""
    posted = {}
    for select_id, field in _SELECTS:
        options = _options(html, select_id)
        selected = [value for value, _label, sel in options if sel]
        posted[field] = selected[-1] if selected else options[0][0]
    return posted


def _dots(html: str) -> list[str]:
    """The opening tag of every identity dot of a contact list."""
    return re.findall(r'<span [^>]*\bbg-orange-400\b[^>]*>', html)


def _row(html: str, pid: str) -> str:
    """One contact's row of the list (its link, up to the link's end)."""
    start = html.index(f'href="/parties/{pid}"')
    return html[start:html.index("</a>", start)]


# ══════════════════════════════════════════════════════════════════════
# 1. Le badge et la mention
# ══════════════════════════════════════════════════════════════════════


def test_a_presumed_inscription_is_amber_and_never_signed(client, db):
    pid = _seed(db, **PRESUMED)

    html = client.get(f"/parties/{pid}").get_data(as_text=True)

    block = _identite(html)
    assert "Vérifié (présumé)" in block
    assert "bg-amber-100 text-amber-700" in block
    assert "bg-green-100" not in block
    assert "inscrit par Claude le 25 septembre 2026 — à confirmer" in block
    assert SIGNER not in _conformite(html)


def test_a_presumed_conflict_keeps_the_red_of_an_alarm(client, db):
    """Amber is the colour of « Vérifié (présumé) »: a conflict Claude
    DETECTED painted amber would read, at a glance, like Claude's all-clear.
    Under-warning on a bar to acting is the failure that costs — the red
    stays, the « (présumé) » and the « à confirmer » say who inscribed it.
    (Failed on f5033ec: every presumed status was amber.)"""
    pid = _seed(db, conflict_check="conflit_détecté",
                conflict_check_date=INSCRIBED, conflict_check_source="mcp")

    html = client.get(f"/parties/{pid}").get_data(as_text=True)

    block = _conformite(html)
    block = block[block.index("Conflit d"):]
    assert "Conflit détecté (présumé)" in block
    assert "bg-red-100 text-red-700" in block
    assert "bg-amber-100" not in block
    assert "inscrit par Claude le 25 septembre 2026 — à confirmer" in block
    assert SIGNER not in block
    assert "/conformite/conflit/confirmer" in html


def test_the_confirm_form_carries_csrf_and_the_etag(client, db):
    pid = _seed(db, **PRESUMED)
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    form = re.search(
        r'<form method="post"\s+action="/parties/p1/conformite/identite/'
        r'confirmer"[^>]*>(.*?)</form>', html, re.S)
    assert form, "no Confirmer form"
    assert 'name="csrf_token" value="tok"' in form.group(1)
    assert 'name="expected_etag" value="e-p1"' in form.group(1)
    # Only the presumed check offers it.
    assert "/conformite/conflit/confirmer" not in html


def test_a_confirmed_inscription_names_both_acts(client, db):
    pid = _seed(db, **CONFIRMED_BY_LAWYER)
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert "bg-green-100 text-green-700" in block and "présumé" not in block
    assert (f"confirmé le 27 septembre 2026 par {SIGNER} "
            "(inscrit par Claude le 25 septembre 2026)") in block
    assert "Confirmer" not in block


def test_the_lawyer_s_own_attestation_reads_as_before(client, db):
    """« Identifié » (D2) takes the blue « Exempté » had — decided, yet never
    the green of a verified identity — and is signed like any attestation
    of the lawyer."""
    pid = _seed(db, **LAWYERS)
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert _badge(block) == ("bg-blue-100 text-blue-700", "Identifié")
    assert f"le 25 septembre 2026 par {SIGNER}" in block
    assert "Claude" not in block


def test_a_decided_status_without_a_date_never_breaks_the_fiche(client, db):
    """A contact created with a decided status before lot 4a has no date:
    the fiche renders the status, with no dated attribution."""
    pid = _seed(db, identity_verified="vérifié", identity_verified_date=None)
    resp = client.get(f"/parties/{pid}")
    assert resp.status_code == 200
    block = _identite(resp.get_data(as_text=True))
    assert "Vérifié" in block and " par " not in block


def test_a_presumed_status_without_a_date_still_says_claude(client, db):
    pid = _seed(db, identity_verified="vérifié", identity_verified_date=None,
                identity_verified_source="mcp")
    block = _identite(client.get(f"/parties/{pid}").get_data(as_text=True))
    assert "inscrit par Claude — à confirmer" in block
    assert SIGNER not in block


def test_the_section_shows_for_a_dossier_client_of_another_role(client, db):
    """The coverage report checks EVERY dossier client, whatever the
    contact's role — the fiche must show what it checks."""
    pid = _seed(db, contact_role="autre")
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "T", "status": "actif",
                            "clients": [{"id": pid, "name": "Jean Tremblay"}],
                            "client_ids": [pid]})
    html = client.get(f"/parties/{pid}").get_data(as_text=True)
    assert ">Conformité</h2>" in html

    other = _seed(db, pid="p2", contact_role="autre")
    assert ">Conformité</h2>" not in client.get(
        f"/parties/{other}").get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════
# 2. « Confirmer »
# ══════════════════════════════════════════════════════════════════════


def _ctag(db):
    return (db.peek("dav_sync/parties") or {}).get("ctag")


def test_confirming_turns_the_inscription_into_the_attestation(client, db):
    pid = _seed(db, **PRESUMED)
    before = _ctag(db)

    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-p1"})

    assert resp.status_code == 302
    assert "message=V%C3%A9rification+confirm%C3%A9e" in resp.headers["Location"]
    stored = db.peek(f"parties/{pid}")
    assert stored["identity_verified_confirmed_by"] == "juriste"
    assert kyc.is_decided(stored, kyc.FIELD_IDENTITY)
    assert _ctag(db) != before  # the etag moved: DavX5 must re-sync
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Vérification confirmée." in page


def test_a_stale_confirmation_is_refused_with_a_banner(client, db):
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-autre-version"})
    assert resp.status_code == 302 and "erreur=" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La fiche a changé depuis son affichage" in page
    assert kyc.is_presumed(db.peek(f"parties/{pid}"), kyc.FIELD_IDENTITY)


def test_a_confirmation_without_the_version_is_refused(client, db):
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={})
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La version de la fiche est requise pour confirmer" in page
    assert kyc.is_presumed(db.peek(f"parties/{pid}"), kyc.FIELD_IDENTITY)


def test_nothing_presumed_is_nothing_to_confirm(client, db):
    pid = _seed(db, **LAWYERS)
    resp = client.post(f"/parties/{pid}/conformite/identite/confirmer",
                       data={"expected_etag": "e-p1"})
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Rien à confirmer" in page


def test_an_unknown_check_or_contact_goes_to_the_list(client, db):
    pid = _seed(db, **PRESUMED)
    for path in (f"/parties/{pid}/conformite/autre/confirmer",
                 "/parties/p9/conformite/identite/confirmer"):
        resp = client.post(path, data={"expected_etag": "e-p1"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/parties/")


def test_the_confirm_route_is_a_post_only(client, db):
    pid = _seed(db, **PRESUMED)
    assert client.get(
        f"/parties/{pid}/conformite/identite/confirmer").status_code == 405


# ══════════════════════════════════════════════════════════════════════
# 3. Le formulaire du juriste
# ══════════════════════════════════════════════════════════════════════


def _form_post(pid, db, **over):
    stored = db.peek(f"parties/{pid}")
    data = {
        "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
        "identity_verified": stored["identity_verified"],
        "conflict_check": stored["conflict_check"],
        "expected_etag": stored["etag"],
    }
    data.update(over)
    return data


def test_a_form_resave_keeps_the_inscription_presumed(client, db):
    """Re-saving the fiche for an unrelated field never confirms."""
    pid = _seed(db, **PRESUMED)
    resp = client.post(f"/parties/{pid}",
                       data=_form_post(pid, db, email="jean@exemple.com"))
    assert resp.status_code == 302
    stored = db.peek(f"parties/{pid}")
    assert stored["email"] == "jean@exemple.com"
    assert kyc.is_presumed(stored, kyc.FIELD_IDENTITY)


def test_a_form_status_change_is_the_lawyer_s_decision(client, db):
    pid = _seed(db, **PRESUMED)
    client.post(f"/parties/{pid}",
                data=_form_post(pid, db, identity_verified="identifié"))
    stored = db.peek(f"parties/{pid}")
    assert stored["identity_verified_source"] == "juriste"
    assert kyc.is_decided(stored, kyc.FIELD_IDENTITY)


def test_the_edit_form_says_the_status_is_presumed(client, db):
    pid = _seed(db, **PRESUMED)
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert "Inscrit par Claude — à confirmer depuis la fiche." in html
    assert html.count("Inscrit par Claude") == 1  # the identity select only

    lawyer = _seed(db, pid="p2", **LAWYERS)
    assert "Inscrit par Claude" not in client.get(
        f"/parties/{lawyer}/edit").get_data(as_text=True)


def test_the_edit_form_offers_conformite_to_a_dossier_client(client, db):
    """A dossier client whose contact role is not « client »: the fiche
    shows its Conformité, so the form must let the lawyer decide it."""
    pid = _seed(db, contact_role="autre", **PRESUMED)
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "T", "status": "actif",
                            "clients": [{"id": pid, "name": "Jean Tremblay"}],
                            "client_ids": [pid]})
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert "isDossierClient: true," in html
    assert "contactRole === 'client' || isDossierClient" in html

    other = _seed(db, pid="p2", contact_role="autre")
    assert "isDossierClient: false," in client.get(
        f"/parties/{other}/edit").get_data(as_text=True)


def test_the_new_contact_form_renders_without_a_stored_record(client, db):
    assert client.get("/parties/new").status_code == 200


def test_a_web_creation_with_a_decided_status_is_the_lawyer_s(client, db):
    resp = client.post("/parties/", data={
        "type": "individual", "contact_role": "client",
        "last_name": "Roy", "identity_verified": "vérifié",
        "conflict_check": "non_vérifié",
    })
    assert resp.status_code == 302
    (stored,) = [p for p in db.peek_collection("parties").values()
                 if p.get("last_name") == "Roy"]
    assert stored["identity_verified_source"] == "juriste"
    assert stored["identity_verified_date"] is not None


def _create_decided(client, db, identite: str, conflit: str):
    """POST the new-contact form with both checks decided, follow the
    redirect; return the stored record and the fiche it leads to."""
    resp = client.post("/parties/", data={
        "type": "individual", "contact_role": "client",
        "first_name": "Luc", "last_name": "Roy",
        "identity_verified": identite, "conflict_check": conflit,
    })
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    (stored,) = [p for p in db.peek_collection("parties").values()
                 if p.get("last_name") == "Roy"]
    assert resp.headers["Location"].endswith(f"/parties/{stored['id']}")
    fiche = client.get(resp.headers["Location"])
    assert fiche.status_code == 200
    return stored, fiche.get_data(as_text=True)


@pytest.mark.parametrize("identite, conflit", list(itertools.product(
    kyc.DECIDED[kyc.FIELD_IDENTITY], kyc.DECIDED[kyc.FIELD_CONFLICT])))
def test_a_creation_with_decided_checks_is_signed_on_the_fiche(
    client, db, identite, conflit,
):
    """D5 — the route test the « glitch » lacked: a client created with
    both checks decided reads « le [jour de création] par Me … » in BOTH
    blocks of the fiche the redirect opens. (The fiches that show no name
    nor date are older data: created before the stamp, D4's script.)"""
    stored, fiche = _create_decided(client, db, identite, conflit)

    jour = format_date_fr(to_mtl(stored["created_at"]).date())
    for field, block, status in (
        (kyc.FIELD_IDENTITY, _identite(fiche), identite),
        (kyc.FIELD_CONFLICT, _conflit(fiche), conflit),
    ):
        assert stored[field] == status
        assert stored[kyc.date_key(field)] == stored["created_at"]
        assert stored[kyc.source_key(field)] == kyc.SOURCE_JURISTE
        assert _badge(block) == (parties_routes._KYC_BADGES[status],
                                 kyc.STATUS_LABELS[status])
        assert f"le {jour} par {SIGNER}" in block
        assert "Claude" not in block


@pytest.mark.parametrize("nom", ["Jason Poirier Lavoie",
                                 "Me Jason Poirier Lavoie"])
def test_the_real_signer_names_the_lawyer_once(client, db, monkeypatch, nom):
    """D5 with the REAL ``_compliance_signer``, reading the lawyer's name
    from a seeded settings/cabinet: « Me » is added when the name lacks it,
    never doubled (« Me Me ») when it carries it — and never the firm's
    name, which does not sign an attestation."""
    assert (_REAL_COMPLIANCE_SIGNER.__module__,
            _REAL_COMPLIANCE_SIGNER.__qualname__) == (
        "routes.parties", "_compliance_signer")
    monkeypatch.setattr(parties_routes, "_compliance_signer",
                        _REAL_COMPLIANCE_SIGNER)
    db.seed("settings/cabinet", {"nom": nom,
                                 "organisation": "Poirier Lavoie, avocat"})

    stored, fiche = _create_decided(
        client, db, kyc.DECIDED[kyc.FIELD_IDENTITY][0],
        kyc.DECIDED[kyc.FIELD_CONFLICT][0])

    jour = format_date_fr(to_mtl(stored["created_at"]).date())
    for block in (_identite(fiche), _conflit(fiche)):
        assert f"le {jour} par Me Jason Poirier Lavoie" in block
    conformite = _conformite(fiche)
    assert "Me Me" not in conformite
    assert "Poirier Lavoie, avocat" not in conformite
    # The name came from the seeded document — not from a deploy-time seed.
    assert any("settings/cabinet" in r.paths for r in db.reads)


def test_the_list_dot_stays_on_a_presumed_identity(client, db):
    _seed(db, **PRESUMED)
    _seed(db, pid="p2", last_name="Roy", **LAWYERS)
    html = client.get("/parties/?role=client",
                      headers={"HX-Request": "true"}).get_data(as_text=True)
    assert html.count("bg-orange-400") == 1
    (dot,) = _dots(html)
    for attribute in ('role="img"',
                      'aria-label="Identité inscrite par Claude — à confirmer"',
                      'title="Identité inscrite par Claude — à confirmer"'):
        assert attribute in dot, attribute


# ══════════════════════════════════════════════════════════════════════
# 4. Les classes du balisage existent dans l'artefact compilé
# ══════════════════════════════════════════════════════════════════════


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_new_markup_uses_only_compiled_classes(client, db):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    pid = _seed(db, **PRESUMED)
    # The lawyer's « Identifié » too: its blue moved from « Exempté » (D2).
    lawyer = _seed(db, pid="p2", **LAWYERS)
    snippets = [
        _conformite(client.get(f"/parties/{pid}").get_data(as_text=True)),
        _conformite(client.get(f"/parties/{lawyer}").get_data(as_text=True)),
    ]
    assert "bg-blue-100 text-blue-700" in snippets[1]
    edit = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    i = edit.index("Inscrit par Claude")
    snippets.append(edit[max(0, i - 200):i + 50])
    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert classes and not absent, absent


# ══════════════════════════════════════════════════════════════════════
# 5. Le vocabulaire de 2026-11 — « Identifié », « Vérifié », « À faire »
# ══════════════════════════════════════════════════════════════════════


def test_every_status_has_its_badge():
    """One badge per status of kyc.STATUS_LABELS — a status without one
    falls to the gray of an unknown value."""
    badges = parties_routes._KYC_BADGES
    assert set(badges) == set(kyc.STATUS_LABELS)
    # « Identifié » takes the blue « Exempté » had: decided, yet distinct
    # from Vérifié's green — every status keeps its own colour, and none
    # wears the amber of a presumed inscription.
    assert badges["identifié"] == "bg-blue-100 text-blue-700"
    assert len(set(badges.values())) == len(badges)
    assert parties_routes._KYC_PRESUMED_BADGE not in badges.values()


def test_a_legacy_exempte_reads_as_the_lawyer_s_identifie(client, db):
    """D3 — a stored « exempté » (the script has not run yet) reads as the
    « Identifié » it now is, on the fiche, in the form and in the list:
    the lawyer's attestation, dated and signed."""
    pid = _seed(db, **LEGACY_EXEMPTE)

    fiche = client.get(f"/parties/{pid}").get_data(as_text=True)
    block = _identite(fiche)
    assert _badge(block) == ("bg-blue-100 text-blue-700", "Identifié")
    assert f"le 25 septembre 2026 par {SIGNER}" in block
    assert "Claude" not in block
    assert "exempt" not in _conformite(fiche).lower()

    edit = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert _selected(edit, "kyc-statut-identite") == ["identifié"]
    assert f"identityVerified: {jsattr('identifié')}," in edit
    assert "(non reconnu)" not in edit

    # The list: no dot on it — beside an « À faire » client that has one.
    _seed(db, pid="p2", last_name="Roy")
    rows = client.get("/parties/?role=client",
                      headers={"HX-Request": "true"}).get_data(as_text=True)
    assert _dots(_row(rows, pid)) == []
    assert len(_dots(_row(rows, "p2"))) == 1


def test_resaving_a_legacy_exempte_fiche_keeps_its_attestation(client, db):
    """The form posts what it rendered — « identifié », no transition: the
    date and the author stay, and the full-document save writes the
    successor (the retired status's purge, models/partie)."""
    pid = _seed(db, **LEGACY_EXEMPTE)
    before = db.peek(f"parties/{pid}")
    edit = client.get(f"/parties/{pid}/edit").get_data(as_text=True)

    resp = client.post(f"/parties/{pid}", data=_form_post(
        pid, db, email="jean@exemple.com", **_posted_as_rendered(edit)))

    assert resp.status_code == 302
    after = db.peek(f"parties/{pid}")
    assert after["email"] == "jean@exemple.com"
    assert after[kyc.FIELD_IDENTITY] == "identifié"
    for key in (kyc.date_key(kyc.FIELD_IDENTITY),
                kyc.source_key(kyc.FIELD_IDENTITY)):
        assert after[key] == before[key], key
    assert kyc.is_decided(after, kyc.FIELD_IDENTITY)


def test_the_selects_offer_the_vocabulary_in_order(client, db):
    """Both selects derive from utils/kyc: every option, in order, with its
    label, each select named by its label — and the retired « exempté »
    nowhere, not even for a contact that still stores it."""
    legacy = _seed(db, **LEGACY_EXEMPTE)
    for url in ("/parties/new", f"/parties/{legacy}/edit"):
        html = client.get(url).get_data(as_text=True)
        for select_id, field in _SELECTS:
            assert [(value, label) for value, label, _sel
                    in _options(html, select_id)] == list(kyc.choices(field)), (
                url, field)
            assert f'<label for="{select_id}"' in html, (url, select_id)
        assert "exempt" not in html.lower(), url


def test_a_new_contact_opens_on_a_faire_in_both_selects(client, db):
    """The identity list now OPENS on « Identifié »: without a server-side
    ``selected``, a browser whose Alpine never ran would post it — a
    decided status, stamped as the lawyer's attestation on a contact
    nobody identified."""
    html = client.get("/parties/new").get_data(as_text=True)

    assert html.count(f'<option value="{kyc.NON_VERIFIE}" selected>') == 2
    for select_id, _field in _SELECTS:
        assert _selected(html, select_id) == [kyc.NON_VERIFIE], select_id
    assert '<option value="identifié" selected>' not in html
    assert f"identityVerified: {jsattr(kyc.NON_VERIFIE)}," in html
    assert f"conflictCheck: {jsattr(kyc.NON_VERIFIE)}," in html

    # Posted as rendered: « À faire » ×2, nothing stamped.
    resp = client.post("/parties/", data={
        "type": "individual", "contact_role": "client", "last_name": "Roy",
        **_posted_as_rendered(html)})
    assert resp.status_code == 302
    (stored,) = db.peek_collection("parties").values()
    for field in kyc.FIELDS:
        assert stored[field] == kyc.NON_VERIFIE
        assert stored[kyc.date_key(field)] is None


def test_an_unknown_stored_status_is_offered_and_kept(client, db):
    """A status outside the vocabulary (a hand edit, a restored backup) is
    offered as its own selected option, so the form re-posts it unchanged.
    Without it the identity list would post its first option, « Identifié »
    — an attestation stamped today in the lawyer's name — and the conflict
    list « À faire », clearing the check's date and author."""
    pid = _seed(db, identity_verified="à_vérifier",
                identity_verified_date=INSCRIBED,
                identity_verified_source="juriste",
                conflict_check="inconnu", conflict_check_date=INSCRIBED,
                conflict_check_source="juriste")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    for (select_id, field), value in zip(_SELECTS, ("à_vérifier", "inconnu")):
        assert [(v, label) for v, label, sel in _options(html, select_id)
                if sel] == [(value, f"{value} (non reconnu)")], field
    before = db.peek(f"parties/{pid}")

    resp = client.post(f"/parties/{pid}",
                       data=_form_post(pid, db, **_posted_as_rendered(html)))

    assert resp.status_code == 302
    after = db.peek(f"parties/{pid}")
    for field in kyc.FIELDS:
        for key in (field, kyc.date_key(field), kyc.source_key(field)):
            assert after[key] == before[key], key


@pytest.mark.parametrize("stored", [["vérifié"], True],
                         ids=["liste", "booleen"])
def test_a_non_string_stored_status_never_locks_the_fiche(client, db, stored):
    """A non-string status — only a hand edit stores one — is offered as its
    text, the one thing a select can post; re-posted unchanged, that text
    keeps the stored value AS IT WAS, so an unrelated correction saves
    instead of being refused « Statut de vérification invalide. »
    (« legacy data never locks a record »). Fails on a transition that
    compares the posted text with the raw stored value."""
    pid = _seed(db, identity_verified=stored,
                identity_verified_date=INSCRIBED,
                identity_verified_source="juriste")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert _selected(html, _SELECTS[0][0]) == [str(stored)]
    before = db.peek(f"parties/{pid}")

    resp = client.post(f"/parties/{pid}", data=_form_post(
        pid, db, email="jean@exemple.com", **_posted_as_rendered(html)))

    assert resp.status_code == 302
    after = db.peek(f"parties/{pid}")
    assert after["email"] == "jean@exemple.com"
    assert after["identity_verified"] == stored
    assert type(after["identity_verified"]) is type(stored)
    for key in (kyc.date_key(kyc.FIELD_IDENTITY),
                kyc.source_key(kyc.FIELD_IDENTITY)):
        assert after[key] == before[key], key


def test_a_tagged_unknown_status_is_kept_exactly(client, db):
    """A stored unknown status holding tag-like text is posted back as the
    browser received it, and the writer's sanitizer strips the posted copy:
    the record must keep its STORED value, or « <b>vérifié</b> » would be
    saved as a decided « vérifié » nobody chose, with no date and no source
    — closing the coverage check. Fails when the tolerance of an unchanged
    legacy value lets the sanitized copy through."""
    pid = _seed(db, identity_verified="<b>vérifié</b>",
                conflict_check="vérifié<br>")
    html = client.get(f"/parties/{pid}/edit").get_data(as_text=True)
    assert _selected(html, _SELECTS[0][0]) == ["<b>vérifié</b>"]
    assert _selected(html, _SELECTS[1][0]) == ["vérifié<br>"]
    before = db.peek(f"parties/{pid}")

    resp = client.post(f"/parties/{pid}", data=_form_post(
        pid, db, email="jean@exemple.com", **_posted_as_rendered(html)))

    assert resp.status_code == 302
    after = db.peek(f"parties/{pid}")
    assert after["email"] == "jean@exemple.com"
    for field in kyc.FIELDS:
        for key in (field, kyc.date_key(field), kyc.source_key(field)):
            assert after[key] == before[key], key
        assert not kyc.is_decided(after, field), field


@pytest.mark.parametrize("raw", ["absent", None, ""],
                         ids=["absente", "none", "vide"])
def test_x_data_opens_on_the_option_the_server_selected(client, db, raw):
    """Alpine's x-model overrides the server's ``selected`` as soon as it
    runs, so x-data must open on the SAME value. A stored status that is
    absent, None or blank is no option: echoing the raw value left the
    select without a match, and the browser then posts its FIRST option —
    « Identifié », in D1's order. Fails on the old x-data, which echoed the
    raw stored value."""
    doc = {**pm._default_doc(), "id": "p1", "type": "individual",
           "contact_role": "client", "first_name": "Jean",
           "last_name": "Tremblay", "etag": "e-p1",
           "created_at": INSCRIBED, "updated_at": INSCRIBED}
    for field in kyc.FIELDS:
        if raw == "absent":
            del doc[field]
        else:
            doc[field] = raw
    db.seed("parties/p1", doc)

    html = client.get("/parties/p1/edit").get_data(as_text=True)

    for (select_id, _field), name in zip(
            _SELECTS, ("identityVerified", "conflictCheck")):
        assert _selected(html, select_id) == [kyc.NON_VERIFIE], select_id
        assert f"{name}: {jsattr(kyc.NON_VERIFIE)}," in html, name


def test_the_reminder_keys_on_a_faire(client, db):
    """The form's reminder shows while the identity check is « À faire »:
    its x-if compares with the kept key, through the real jsattr — the
    value x-data opens on for a new contact."""
    html = client.get("/parties/new").get_data(as_text=True)
    m = re.search(r'<template x-if="identityVerified === ([^"]*)">(.*?)'
                  r'</template>', html, re.S)
    assert m, "no reminder"
    assert m.group(1) == str(jsattr(kyc.NON_VERIFIE))
    assert f"identityVerified: {m.group(1)}," in html
    assert ("Identité à faire : ce client n'est encore ni identifié ni "
            "vérifié.") in m.group(2)


def test_a_presumed_identifie_is_amber_with_its_confirm_button(client, db):
    pid = _seed(db, identity_verified="identifié",
                identity_verified_date=INSCRIBED,
                identity_verified_source="mcp")

    html = client.get(f"/parties/{pid}").get_data(as_text=True)

    block = _identite(html)
    assert _badge(block) == ("bg-amber-100 text-amber-700",
                             "Identifié (présumé)")
    assert "inscrit par Claude le 25 septembre 2026 — à confirmer" in block
    assert SIGNER not in _conformite(html)
    form = re.search(
        r'<form method="post"\s+action="/parties/p1/conformite/identite/'
        r'confirmer"[^>]*>(.*?)</form>', block, re.S)
    assert form, "no Confirmer form"
    assert "Confirmer" in form.group(1)
    assert 'name="expected_etag" value="e-p1"' in form.group(1)


def test_an_a_faire_client_s_dot_says_identite_a_faire(client, db):
    _seed(db)  # « À faire »: the model's default
    html = client.get("/parties/?role=client",
                      headers={"HX-Request": "true"}).get_data(as_text=True)
    (dot,) = _dots(html)
    for attribute in ('role="img"', 'aria-label="Identité à faire"',
                      'title="Identité à faire"'):
        assert attribute in dot, attribute
