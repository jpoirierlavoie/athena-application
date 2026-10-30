"""CardDAV — titres reçus du téléphone et UID immuable (2026-10-30).

Contre les vraies routes PUT/GET de ``dav/carddav.py`` et le vrai client
Firestore (seul le serveur est faux, ``tests/_fake_firestore.py``). DavX5
échoue en silence : le reste se vérifie au ``curl`` puis sur l'appareil
(CLAUDE.md, composant nº 2 ; ``DEPLOYMENT.md`` §15).
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync  # its db is patched below
    from models import partie as pm

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

_ = (dav_sync,)

AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
RID = "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
HREF = f"/dav/addressbook/{RID}.vcf"


def _vcard(uid: str, n: str) -> str:
    return "\r\n".join([
        "BEGIN:VCARD", "VERSION:4.0", f"UID:{uid}", "FN:Anne Curl",
        f"N:{n}", "END:VCARD", "",
    ])


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def _put(client, body: str, **headers):
    return client.put(HREF, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/vcard", **headers})


def _etag(client) -> str:
    return client.get(HREF, headers=AUTH).headers["ETag"]


def test_un_put_de_mise_a_jour_ne_change_pas_l_uid(client):
    """Règle 11 : le parseur rendait l'UID de la carte et ``update_partie``
    le fusionnait — un client pouvait changer l'UID d'un contact."""
    assert _put(client, _vcard("uid-du-telephone", "Curl;Anne;;;")).status_code == 201
    resp = _put(client, _vcard("un-autre-uid", "Curl;Anne;;Me;"),
                **{"If-Match": _etag(client)})
    assert resp.status_code == 204
    body = client.get(HREF, headers=AUTH).get_data(as_text=True)
    assert "UID:uid-du-telephone" in body
    assert pm.get_partie_strict(RID)["vcard_uid"] == "uid-du-telephone"


def test_un_titre_du_telephone_est_normalise(client):
    assert _put(client, _vcard("u-1", "Curl;Anne;Josée;Dr.;Ad.,E.")).status_code == 201
    stored = pm.get_partie_strict(RID)
    assert (stored["prefix"], stored["middle_name"], stored["suffix"]) == (
        "Dr", "Josée", "Ad. E.")
    body = client.get(HREF, headers=AUTH).get_data(as_text=True).replace("\r\n ", "")
    assert "N:Curl;Anne;Josée;Dr;Ad. E." in body
    assert "FN:Anne Curl" in body


def test_un_titre_inconnu_du_telephone_n_est_pas_refuse(client):
    """Un 422 arrêterait la synchro DavX5 : le titre est gardé tel quel."""
    ctag_avant = dav_sync.get_ctag("parties")
    assert _put(client, _vcard("u-1", "Curl;Anne;;Dr.,Hon.;")).status_code == 201
    assert pm.get_partie_strict(RID)["prefix"] == "Dr. Hon."
    assert dav_sync.get_ctag("parties") != ctag_avant
