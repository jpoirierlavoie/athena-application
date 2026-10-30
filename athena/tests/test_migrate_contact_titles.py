"""Le script unique ``scripts/migrate_contact_titles`` (2026-10-30).

Contre le VRAI client Firestore (seul le serveur est faux) : à blanc il
n'écrit rien et ne nomme personne ; ``--appliquer`` replie « Fonction »,
normalise le titre, estampille et renouvelle le CTag dans le même lot ;
``--resynchroniser`` réestampille tout en gardant l'ordre ; une fiche
modifiée entre la lecture et l'écriture fait échouer le lot, sans écrasement.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    from models import partie as pm
    import scripts.migrate_contact_titles as script

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, dav_sync, script)


def _seed(fake, pid, minutes, **over):
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Ginette",
           "last_name": "Labrecque", "vcard_uid": f"u-{pid}", "etag": f"e-{pid}",
           "created_at": T0, "updated_at": T0 + timedelta(minutes=minutes)}
    doc.update(over)
    fake.seed(f"parties/{pid}", doc)


def _legacy(fake):
    _seed(fake, "a", 1, job_title="Avocate", job_role="Associée")
    _seed(fake, "b", 2, prefix="Dr.")
    _seed(fake, "c", 3, prefix="Capitaine")
    _seed(fake, "d", 4)


def test_a_blanc_rien_n_est_ecrit_et_personne_n_est_nomme(fake, capsys):
    _legacy(fake)
    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert "Ginette" not in out and "Labrecque" not in out
    assert "a : « Avocate » + « Associée » → « Avocate — Associée »" in out
    assert "b : « Dr. » → « Dr »" in out
    assert "c : « Capitaine »" in out
    assert fake.peek("parties/a")["job_role"] == "Associée"
    assert fake.peek("parties/b")["etag"] == "e-b"


def test_appliquer_replie_normalise_et_renouvelle_le_ctag(fake):
    _legacy(fake)
    ctag = dav_sync.get_ctag("parties")
    assert script.main(["--appliquer"]) == 0
    a, b, c, d = (fake.peek(f"parties/{p}") for p in "abcd")
    assert a["job_title"] == "Avocate — Associée" and "job_role" not in a
    assert b["prefix"] == "Dr"
    assert c["prefix"] == "Capitaine"          # inconnu : gardé, signalé
    for doc in (a, b):
        assert doc["updated_via"] == "script" and doc["etag"] != f"e-{doc['id']}"
    assert d["etag"] == "e-d"                  # rien à corriger : intact
    assert dav_sync.get_ctag("parties") != ctag


def test_une_seconde_passe_n_a_rien_a_faire(fake, capsys):
    _legacy(fake)
    assert script.main(["--appliquer"]) == 0
    capsys.readouterr()
    assert script.main([]) == 0
    assert "Rien à faire." in capsys.readouterr().out


def test_resynchroniser_reestampille_tout_en_gardant_l_ordre(fake):
    _legacy(fake)
    assert script.main(["--appliquer", "--resynchroniser"]) == 0
    docs = [fake.peek(f"parties/{p}") for p in "abcd"]
    assert all(d["updated_via"] == "script" for d in docs)
    instants = [d["updated_at"] for d in docs]
    assert instants == sorted(instants) and len(set(instants)) == 4


def test_une_fiche_modifiee_pendant_la_passe_n_est_pas_ecrasee(fake, monkeypatch):
    _legacy(fake)
    decrire = script.describe

    def _puis_quelqu_un_modifie(plans, resync):
        n = decrire(plans, resync)
        fake.external_write("parties/b", {**fake.peek("parties/b"),
                                          "first_name": "Ginou"})
        return n

    monkeypatch.setattr(script, "describe", _puis_quelqu_un_modifie)
    assert script.main(["--appliquer"]) == 1
    assert fake.peek("parties/b")["prefix"] == "Dr."
    assert fake.peek("parties/b")["first_name"] == "Ginou"
    assert fake.peek("parties/a")["job_role"] == "Associée"   # même lot : rien


def test_aucun_contact_lu_est_un_echec(fake):
    assert script.main(["--appliquer"]) == 1
