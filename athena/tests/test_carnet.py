"""Suppliers stay off the phone: the address book's one door (2026-11-11).

DavX5's address book carries every contact except the suppliers (the
lawyer's decision of 2026-11-11). ``services/carnet.py`` is the one door
every contact write passes through. It records a tombstone for a contact
the address book does not carry, removes it for one it carries, and bumps
the CTag in the same commit. The CardDAV handlers never list, serve or
accept a supplier.

These tests run the real CardDAV handlers, routes and models on
``tests/_fake_firestore.py``, then read back what is STORED. A sweep
proves that no address-book signal is sent around the door. Every name
here is invented.
"""

import ast
import os
import xml.etree.ElementTree as ET
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.write_support as write_support
    from models import partie as pm
    import routes.parties as parties_routes
    from services import carnet

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import contact_roles  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

_ = (write_support,)

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
CARD = "urn:ietf:params:xml:ns:carddav"
SYNC_BODY = ('<D:sync-collection xmlns:D="DAV:"><D:sync-token></D:sync-token>'
             '<D:prop><D:getetag/></D:prop></D:sync-collection>')
QUERY_BODY = (f'<C:addressbook-query xmlns:C="{CARD}" xmlns:D="DAV:">'
              '<D:prop><D:getetag/></D:prop></C:addressbook-query>')


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _contact(pid: str, role: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({
        "id": pid, "type": "organization", "contact_role": role,
        "organization_name": f"Entreprise fictive {pid}",
        "etag": f"etag-{pid}", "vcard_uid": f"uid-{pid}",
    })
    doc.update(over)
    return doc


@pytest.fixture
def book(db):
    """A client and a supplier."""
    db.seed("parties/cli", _contact("cli", "client"))
    db.seed("parties/fou", _contact("fou", "fournisseur"))
    return db


def _tombstone(db, pid: str):
    return db.peek(f"dav_sync/parties/tombstones/{pid}")


def _ctag(db) -> str:
    return (db.peek("dav_sync/parties") or {}).get("ctag", "")


@pytest.fixture
def dav(db, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


@pytest.fixture
def web(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    app.register_blueprint(parties_routes.parties_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _vcard(uid: str, label: str, name: str = "Entreprise fictive") -> bytes:
    return "\r\n".join([
        "BEGIN:VCARD", "VERSION:4.0", f"UID:{uid}", f"FN:{name}",
        f"ORG:{name}", f"CATEGORIES:{label}", "END:VCARD", "",
    ]).encode("utf-8")


# ── The door ───────────────────────────────────────────────────────────────


def test_in_address_book_leaves_out_the_suppliers_only():
    for role in contact_roles.ROLES:
        assert contact_roles.in_address_book({"contact_role": role}) is (
            role != "fournisseur"), role
    assert contact_roles.in_address_book({}) is True
    assert contact_roles.in_address_book(None) is True


def test_a_supplier_write_records_its_tombstone_in_the_bump_commit(db):
    db.reset_logs()
    token = carnet.apres_ecriture_contact(_contact("fou", "fournisseur"))
    # ONE commit: the bump and the tombstone reach the phone or neither does.
    assert len(db.commits) == 1
    assert _ctag(db) == token
    assert _tombstone(db, "fou")["sync_token"] == token


def test_a_contact_back_in_the_book_loses_its_tombstone(db):
    db.seed("dav_sync/parties/tombstones/cli",
            {"deleted_at": datetime(2031, 1, 1, tzinfo=UTC), "sync_token": "t0"})
    db.reset_logs()
    token = carnet.apres_ecriture_contact(_contact("cli", "client"))
    assert len(db.commits) == 1
    assert _tombstone(db, "cli") is None
    assert _ctag(db) == token


def test_a_write_without_contact_still_bumps(db):
    before = _ctag(db)
    token = carnet.apres_ecriture_contact(None)
    assert token and token != before and _ctag(db) == token


def test_a_delete_records_its_tombstone_in_the_bump_commit(db):
    db.reset_logs()
    token = carnet.apres_suppression_contact("gone")
    assert len(db.commits) == 1
    assert _tombstone(db, "gone")["sync_token"] == token
    assert _ctag(db) == token


def test_a_failed_commit_changes_nothing(db, monkeypatch):
    before = _ctag(db)

    class _Boom:
        def __getattr__(self, name):
            return lambda *a, **k: None

        def commit(self):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(dav_sync.db, "batch", lambda: _Boom())
    with pytest.raises(RuntimeError):
        carnet.apres_ecriture_contact(_contact("fou", "fournisseur"))
    assert _ctag(db) == before and _tombstone(db, "fou") is None


def test_the_door_writes_its_tombstones_only_in_its_commit(db, monkeypatch):
    """Never a tombstone written apart from the bump (a separate write that
    failed after the bump would leave a supplier on the phone for good)."""
    def _apart(*_args, **_kwargs):
        raise AssertionError("a tombstone written outside the bump's commit")

    for name in ("record_tombstone", "remove_tombstone", "bump_ctag",
                 "record_tombstones_bulk", "remove_tombstones_bulk"):
        monkeypatch.setattr(dav_sync, name, _apart)
    carnet.apres_ecriture_contact(_contact("fou", "fournisseur"),
                                  _contact("cli", "client"))
    carnet.apres_suppression_contact("gone")
    assert _tombstone(db, "fou") and _tombstone(db, "gone")


def test_update_membership_refuses_more_than_one_batch():
    with pytest.raises(ValueError):
        dav_sync.update_membership("parties", present=["x"] * 300,
                                   absent=["y"] * 300)


# ── CardDAV never serves a supplier ────────────────────────────────────────


def _responses(xml: bytes) -> dict:
    """href → « listed » (a propstat with its etag) or « deleted » (a 404
    status), from a multistatus body."""
    out = {}
    for resp in ET.fromstring(xml).iter("{DAV:}response"):
        href = resp.findtext("{DAV:}href")
        status = resp.findtext("{DAV:}status") or ""
        out[href] = "deleted" if "404" in status else "listed"
    return out


@pytest.mark.parametrize("method, headers, body", [
    ("PROPFIND", {"Depth": "1"}, None),
    ("REPORT", {}, QUERY_BODY),
])
def test_the_listings_never_carry_a_supplier(book, dav, method, headers, body):
    resp = dav.open("/dav/addressbook/", method=method,
                    headers={**AUTH, **headers}, data=body)
    assert resp.status_code == 207
    seen = _responses(resp.data)
    assert seen.get("/dav/addressbook/cli.vcf") == "listed"
    assert "/dav/addressbook/fou.vcf" not in seen


def test_a_full_sync_reports_every_supplier_deleted_even_without_tombstone(
        book, dav):
    """No tombstone at all (the door's commit failed, or it was pruned): the
    same read still tells the phone to drop the supplier's card."""
    assert _tombstone(book, "fou") is None
    resp = dav.open("/dav/addressbook/", method="REPORT", headers=AUTH,
                    data=SYNC_BODY)
    assert resp.status_code == 207
    seen = _responses(resp.data)
    assert seen.get("/dav/addressbook/cli.vcf") == "listed"
    assert seen.get("/dav/addressbook/fou.vcf") == "deleted"
    # Once only, beside its tombstone.
    carnet.apres_ecriture_contact(book.peek("parties/fou"))
    body = dav.open("/dav/addressbook/", method="REPORT", headers=AUTH,
                    data=SYNC_BODY).get_data(as_text=True)
    assert body.count("/dav/addressbook/fou.vcf") == 1


def test_a_supplier_href_answers_404_and_a_phone_delete_spares_it(book, dav):
    assert dav.get("/dav/addressbook/fou.vcf", headers=AUTH).status_code == 404
    multiget = dav.open(
        "/dav/addressbook/", method="REPORT", headers=AUTH,
        data=(f'<C:addressbook-multiget xmlns:C="{CARD}" xmlns:D="DAV:">'
              '<D:prop><D:getetag/></D:prop>'
              '<D:href>/dav/addressbook/fou.vcf</D:href>'
              '</C:addressbook-multiget>'))
    assert multiget.status_code == 207 and b"404" in multiget.data
    assert b"BEGIN:VCARD" not in multiget.data
    resp = dav.delete("/dav/addressbook/fou.vcf", headers=AUTH)
    assert resp.status_code == 404
    assert book.peek("parties/fou")["contact_role"] == "fournisseur"


def test_a_phone_card_naming_a_supplier_is_refused(book, dav):
    before = book.peek("parties/cli")
    # An edit of a client's card that names « Fournisseur ».
    resp = dav.put("/dav/addressbook/cli.vcf", data=_vcard("uid-cli", "Fournisseur"),
                   headers={**AUTH, "Content-Type": "text/vcard"})
    assert resp.status_code == 422
    assert "jamais depuis le téléphone" in resp.get_data(as_text=True)
    assert book.peek("parties/cli") == before
    # A new card that names it.
    resp = dav.put("/dav/addressbook/neuf.vcf", data=_vcard("uid-neuf", "Fournisseur"),
                   headers={**AUTH, "Content-Type": "text/vcard"})
    assert resp.status_code == 422
    assert book.peek("parties/neuf") is None


def test_a_stale_phone_copy_of_a_supplier_is_refused_unchanged(book, dav):
    before = book.peek("parties/fou")
    for headers in ({"If-Match": '"etag-fou"'}, {}):
        resp = dav.put("/dav/addressbook/fou.vcf", data=_vcard("uid-fou", "Autre"),
                       headers={**AUTH, "Content-Type": "text/vcard", **headers})
        assert resp.status_code == 412
    assert book.peek("parties/fou") == before


def test_a_phone_write_of_a_client_passes_the_door(book, dav):
    resp = dav.put("/dav/addressbook/cli.vcf",
                   data=_vcard("uid-cli", "Client", "Entreprise fictive cli"),
                   headers={**AUTH, "Content-Type": "text/vcard",
                            "If-Match": '"etag-cli"'})
    assert resp.status_code == 204
    assert _ctag(book) and _tombstone(book, "cli") is None


# ── A role change moves the card ───────────────────────────────────────────


def _sync(dav):
    return dav.open("/dav/addressbook/", method="REPORT", headers=AUTH,
                    data=SYNC_BODY).get_data(as_text=True)


def test_a_web_switch_to_supplier_leaves_the_phone_and_back_returns(db, web, dav):
    db.seed("parties/aut", _contact("aut", "autre"))
    assert "/dav/addressbook/aut.vcf" in _sync(dav)
    resp = web.post("/parties/aut", data={
        "type": "organization", "contact_role": "fournisseur",
        "organization_name": "Entreprise fictive aut", "expected_etag": "etag-aut",
    })
    assert resp.status_code in (302, 303)
    stone = _tombstone(db, "aut")
    assert stone and stone["sync_token"] == _ctag(db)
    report = _sync(dav)
    # Reported deleted (a 404 status), never listed with an etag.
    assert "/dav/addressbook/aut.vcf" in report and "404" in report
    propfind = dav.open("/dav/addressbook/", method="PROPFIND",
                        headers={**AUTH, "Depth": "1"}).get_data(as_text=True)
    assert "/dav/addressbook/aut.vcf" not in propfind

    etag = db.peek("parties/aut")["etag"]
    resp = web.post("/parties/aut", data={
        "type": "organization", "contact_role": "autre",
        "organization_name": "Entreprise fictive aut", "expected_etag": etag,
    })
    assert resp.status_code in (302, 303)
    assert _tombstone(db, "aut") is None
    propfind = dav.open("/dav/addressbook/", method="PROPFIND",
                        headers={**AUTH, "Depth": "1"}).get_data(as_text=True)
    assert "/dav/addressbook/aut.vcf" in propfind


def test_a_connector_switch_to_supplier_leaves_the_phone(db):
    db.seed("parties/aut", _contact("aut", "autre"))
    payload = handlers.update_partie({"partie_id": "aut",
                                      "contact_role": "fournisseur"})
    assert payload["ctag_bumped"] is True
    # Bumped, yet never on the phone: dav_synced says visibility.
    assert payload["dav_synced"] is False
    assert _tombstone(db, "aut")["sync_token"] == _ctag(db)


def test_a_web_delete_records_its_tombstone(db, web):
    db.seed("parties/cli", _contact("cli", "client"))
    resp = web.post("/parties/cli/delete")
    assert resp.status_code in (302, 303)
    assert db.peek("parties/cli") is None
    assert _tombstone(db, "cli")["sync_token"] == _ctag(db)


def test_the_plugin_tells_claude_which_contacts_the_phone_carries():
    """The skill's « Téléphone » line names the contacts the phone leaves out:
    the one sentence Claude reads on where a contact lands."""
    skill = (_ATHENA.parent / "plugin" / "athena" / "skills" / "athena"
             / "SKILL.md").read_text(encoding="utf-8")
    line = next(l for l in skill.splitlines() if l.startswith("- Téléphone :"))
    assert contact_roles.PHONE_EXCLUDED_ROLES == {"fournisseur"}
    assert "les contacts sauf les fournisseurs" in line


# ── No address-book signal around the door ─────────────────────────────────

# The address-book signals: a CTag bump or a tombstone change.
_SIGNALS = frozenset({
    "bump_ctag", "bump_ctag_in_batch", "record_tombstone", "remove_tombstone",
    "record_tombstones_in_batch", "remove_tombstones_in_batch",
    "record_tombstones_bulk", "remove_tombstones_bulk", "update_membership",
})
# Where one may name the address book: the door, and two one-shot
# migrations already applied that rewrote contacts without changing a role
# (they stage the bump in their own batches).
_ALLOWED = frozenset({
    "services/carnet.py",
    "scripts/migrate_compliance_statuses.py",
    "scripts/migrate_contact_titles.py",
})


def _address_book_signals(sources: dict) -> list:
    """`path:line` of every call to an address-book signal whose collection
    is « parties » — the literal, or a module constant holding it — outside
    ``_ALLOWED``."""
    out = []
    for path, source in sorted(sources.items()):
        if path in _ALLOWED:
            continue
        tree = ast.parse(source)
        constants = {
            target.id
            for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and node.value.value == "parties"
            for target in node.targets if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _SIGNALS:
                continue
            args = list(node.args) + [k.value for k in node.keywords]
            if any((isinstance(a, ast.Constant) and a.value == "parties")
                   or (isinstance(a, ast.Name) and a.id in constants)
                   for a in args):
                out.append(f"{path}:{node.lineno}")
    return out


def _app_sources() -> dict:
    return {
        path.relative_to(_ATHENA).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_ATHENA.rglob("*.py"))
        if "tests" not in path.relative_to(_ATHENA).parts
        and ".venv" not in path.parts and "node_modules" not in path.parts
    }


def test_no_address_book_signal_goes_around_the_door():
    sources = _app_sources()
    assert "services/carnet.py" in sources and "dav/carddav.py" in sources
    assert _address_book_signals(sources) == []


def test_the_sweep_flags_a_bare_signal_and_spares_other_collections():
    planted = {
        "routes/plante.py": (
            "from dav.sync import bump_ctag, record_tombstone\n"
            "COLLECTION_NAME = 'parties'\n"
            "def a():\n    bump_ctag('parties')\n"
            "def b():\n    record_tombstone(COLLECTION_NAME, 'x')\n"
            "def c(sync_name):\n    bump_ctag(sync_name)\n"
            "def d():\n    bump_ctag('dossier:1')\n"
        ),
        "services/carnet.py": "def e():\n    bump_ctag('parties')\n",
    }
    assert _address_book_signals(planted) == ["routes/plante.py:4",
                                              "routes/plante.py:6"]
