"""Contacts — titres, occupation, « Fonction » et vCard (2026-10-30).

Le modèle contre le VRAI client Firestore (``tests/_fake_firestore.py`` : seul
le serveur est faux), puis la sérialisation vCard en fonctions pures. Chaque
test échoue sur le code d'avant : titre inconnu stocké en silence par le web,
UID remplacé par un PUT, « Fonction » perdue à l'enregistrement, prénom en
liste qui fait lever ``display_name``, FN titré qui classe le carnet sous M,
adresse fantôme « Québec, Canada ».
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    from models import partie as pm
    from models import provenance

from tests._fake_firestore import install  # noqa: E402

_ = (dav_sync,)

AVANT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _seed(fake, pid="p1", **over) -> str:
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Jean",
           "last_name": "Tremblay", "vcard_uid": "uid-origine",
           "dav_href": f"/dav/addressbook/{pid}.vcf", "etag": "e0",
           "created_at": AVANT, "updated_at": AVANT}
    doc.update(over)
    fake.seed(f"parties/{pid}", doc)
    return pid


def _stored(fake, pid="p1") -> dict:
    return pm.get_partie_strict(pid)


# ── Le titre ──────────────────────────────────────────────────────────────


def test_une_graphie_connue_est_stockee_sous_sa_cle(fake):
    doc, errors = pm.create_partie(
        {"type": "individual", "last_name": "Roy", "prefix": "Docteure"})
    assert errors == []
    assert doc["prefix"] == "Dre"


def test_un_titre_inconnu_est_refuse_hors_du_telephone(fake):
    """Le web et le connecteur n'écrivent que des clés du vocabulaire."""
    doc, errors = pm.create_partie(
        {"type": "individual", "last_name": "Roy", "prefix": "Capitaine"})
    assert doc is None
    assert errors == [pm.PREFIX_INVALID]
    _seed(fake)
    _, errors = pm.update_partie("p1", {"prefix": "Capitaine"})
    assert errors == [pm.PREFIX_INVALID]
    assert _stored(fake)["prefix"] == ""


def test_un_titre_inconnu_venu_du_telephone_est_garde(fake):
    _seed(fake)
    with provenance.writing_via("dav"):
        _, errors = pm.update_partie("p1", {"prefix": ["Dr.", "Hon."]})
    assert errors == []
    assert _stored(fake)["prefix"] == "Dr. Hon."


def test_une_fiche_au_titre_inconnu_reste_modifiable(fake):
    """Le formulaire re-poste le titre inchangé : une règle nouvelle ne
    verrouille jamais une fiche ancienne."""
    _seed(fake, prefix="Capitaine")
    _, errors = pm.update_partie(
        "p1", {"prefix": "Capitaine", "first_name": "Jeanne"})
    assert errors == []
    assert _stored(fake)["first_name"] == "Jeanne"


def test_un_nom_en_liste_est_joint_avant_le_stockage(fake):
    _seed(fake)
    with provenance.writing_via("dav"):
        _, errors = pm.update_partie(
            "p1", {"first_name": ["Jean", "Paul"], "middle_name": ["Luc"],
                   "suffix": ["Ad.", "E."]})
    assert errors == []
    stored = _stored(fake)
    assert (stored["first_name"], stored["middle_name"], stored["suffix"]) == (
        "Jean Paul", "Luc", "Ad. E.")
    assert pm.formal_name(stored) == "Jean Paul Tremblay, Ad. E."


# ── L'occupation ──────────────────────────────────────────────────────────


def test_une_occupation_hors_liste_est_refusee(fake):
    _seed(fake)
    _, errors = pm.update_partie("p1", {"occupation": "astronaute"})
    assert errors == [pm.OCCUPATION_INVALID]
    _, errors = pm.update_partie("p1", {"occupation": "retraite"})
    assert errors == []
    assert _stored(fake)["occupation"] == "retraite"


def test_une_fiche_sans_occupation_s_enregistre(fake):
    """Les fiches anciennes n'ont pas la clé : rien ne l'exige."""
    doc = {**pm._default_doc(), "id": "p1", "type": "individual",
           "contact_role": "client", "last_name": "Tremblay",
           "vcard_uid": "uid-origine", "etag": "e0"}
    for cle in ("occupation", "middle_name", "suffix"):
        doc.pop(cle)
    fake.seed("parties/p1", doc)
    _, errors = pm.update_partie("p1", {"first_name": "Jeanne"})
    assert errors == []
    assert _stored(fake)["first_name"] == "Jeanne"


# ── « Fonction » ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("titre, fonction, attendu", [
    ("Avocate", "Associée", "Avocate — Associée"),
    ("", "Directeur des finances", "Directeur des finances"),
    ("Directrice des finances", "directrice", "Directrice des finances"),
    ("Avocate — Associée", "Associée", "Avocate — Associée"),   # idempotent
    ("Avocate", "", "Avocate"),
])
def test_la_fonction_se_replie_dans_le_poste(titre, fonction, attendu):
    assert pm.fold_job_role(titre, fonction) == attendu


def test_un_enregistrement_ne_perd_jamais_la_fonction(fake):
    """Avant le script de migration, une fiche qui porte encore « Fonction »
    l'écrit dans « Poste ou profession » et perd la clé — jamais le texte."""
    _seed(fake, job_title="Avocate", job_role="Associée")
    _, errors = pm.update_partie(
        "p1", {"job_title": "Avocate — Associée", "first_name": "Jeanne"})
    assert errors == []
    stored = _stored(fake)
    assert stored["job_title"] == "Avocate — Associée"
    assert "job_role" not in stored


# ── L'identité de la fiche (règles 6 et 11) ───────────────────────────────


def test_une_mise_a_jour_ne_change_jamais_l_uid_ni_l_id(fake):
    _seed(fake)
    _, errors = pm.update_partie(
        "p1", {"vcard_uid": "autre-uid", "id": "p2",
               "dav_href": "/dav/addressbook/p2.vcf", "first_name": "Jeanne"})
    assert errors == []
    stored = _stored(fake)
    assert stored["vcard_uid"] == "uid-origine"
    assert stored["id"] == "p1"
    assert stored["dav_href"] == "/dav/addressbook/p1.vcf"


# ── vCard ─────────────────────────────────────────────────────────────────


def _personne(**over) -> dict:
    doc = {**pm._default_doc(), "id": "p1", "type": "individual",
           "last_name": "Côté", "first_name": "Marie", "vcard_uid": "u-1",
           "updated_at": AVANT}
    doc.update(over)
    return doc


def _lignes(vcf: str) -> list[str]:
    return vcf.replace("\r\n ", "").splitlines()


def test_le_fn_d_une_personne_ne_porte_plus_le_titre():
    """Android classe le carnet par FN : « M. », « Mme », « Me » rangeaient
    la plupart des contacts sous la lettre M."""
    lignes = _lignes(pm.partie_to_vcard(_personne(prefix="M.")))
    assert "FN:Marie Côté" in lignes
    assert "N:Côté;Marie;;M.;" in lignes
    org = {**pm._default_doc(), "type": "organization",
           "organization_name": "Béton Nord inc.", "vcard_uid": "u-2"}
    assert "FN:Béton Nord inc." in _lignes(pm.partie_to_vcard(org))


def test_le_nom_complet_fait_l_aller_retour():
    vcf = pm.partie_to_vcard(_personne(
        prefix="L'hon.", suffix="j.c.s.", middle_name="Anne Josée"))
    assert "N:Côté;Marie;Anne Josée;L'hon.;j.c.s." in _lignes(vcf)
    data = pm.vcard_to_partie(vcf)
    assert (data["prefix"], data["middle_name"], data["suffix"]) == (
        "L'hon.", "Anne Josée", "j.c.s.")


def _carte(n: str, *extra: str) -> str:
    return "\r\n".join(["BEGIN:VCARD", "VERSION:4.0", "UID:u-9", "FN:x",
                        f"N:{n}", *extra, "END:VCARD", ""])


def test_un_composant_redecoupe_par_davx5_se_rejoint():
    """DavX5 redécoupe « Ad. E. » à l'espace et le renvoie en liste."""
    data = pm.vcard_to_partie(_carte("Côté;Marie;;Dr.,Hon.;Ad.,E."))
    assert data["prefix"] == "Dr. Hon."
    assert data["suffix"] == "Ad. E."


def test_un_n_a_sept_composants_se_lit():
    """RFC 9554 : second nom de famille et génération s'ajoutent."""
    data = pm.vcard_to_partie(_carte("Côté;Marie;;Dre;;Tremblay;III"))
    assert (data["last_name"], data["first_name"], data["prefix"]) == (
        "Côté", "Marie", "Dre")


def test_role_n_est_plus_ni_ecrit_ni_lu():
    vcf = pm.partie_to_vcard(_personne(job_title="Juge", job_role="Juge en chef"))
    assert not any(l.startswith("ROLE") for l in _lignes(vcf))
    assert "TITLE:Juge" in _lignes(vcf)
    assert "job_role" not in pm.vcard_to_partie(_carte("Côté;Marie;;;", "ROLE:Chef"))


def test_une_fiche_sans_adresse_n_envoie_plus_quebec_canada():
    """Province et pays par défaut ne sont pas un lieu."""
    vcf = pm.partie_to_vcard(_personne())
    assert not any(l.startswith("ADR") for l in _lignes(vcf))
    avec_unite = pm.partie_to_vcard(_personne(work_address_unit="Bureau 400"))
    assert any(l.startswith("ADR;TYPE=WORK") for l in _lignes(avec_unite))


def test_les_pronoms_sortent_en_pronouns_et_en_x_pronoun():
    lignes = _lignes(pm.partie_to_vcard(_personne(pronouns="elle")))
    assert "PRONOUNS:elle" in lignes and "X-PRONOUN:elle" in lignes


def test_pronouns_l_emporte_et_ses_parametres_se_lisent():
    data = pm.vcard_to_partie(_carte(
        "Côté;Marie;;;", "X-PRONOUN:iel", "PRONOUNS;LANGUAGE=fr:elle"))
    assert data["pronouns"] == "elle"
    data = pm.vcard_to_partie(_carte("Côté;Marie;;;", "X-PRONOUN;PREF=1:iel"))
    assert data["pronouns"] == "iel"
    assert "pronouns" not in pm.vcard_to_partie(_carte("Côté;Marie;;;"))
