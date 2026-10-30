"""Titres et occupation — les deux vocabulaires purs et leurs copies (2026-10-30).

``utils/civilite.py`` et ``utils/occupations.py`` sont la SEULE source : le
modèle, l'énumération du connecteur, l'appariement des conflits et les champs
de gabarit en dérivent. Chaque copie est épinglée ici, et chaque règle de
normalisation prouvée sur l'entrée qu'elle doit traiter.
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
    import mcp.tools as tools
    from models import partie as pm

from utils import civilite, occupations, rapprochement, template_fields  # noqa: E402


# ── Le vocabulaire des titres ─────────────────────────────────────────────


def test_la_liste_des_titres_est_celle_de_l_avocat():
    assert civilite.PREFIXES == ("M.", "Mme", "Me", "Dr", "Dre", "Pr", "Pre", "L'hon.")


@pytest.mark.parametrize("cle", civilite.PREFIXES)
def test_chaque_cle_et_son_libelle_reviennent_a_la_cle(cle):
    assert civilite.canonical_prefix(cle) == cle
    assert civilite.canonical_prefix(civilite.PREFIX_LABELS[cle]) == cle


@pytest.mark.parametrize("recu, attendu", [
    ("Dr.", "Dr"), ("dr", "Dr"), ("Docteur", "Dr"), ("Docteure", "Dre"),
    ("Dre.", "Dre"), ("Maître", "Me"), ("maitre", "Me"), ("Mr", "M."),
    ("Mr.", "M."), ("Monsieur", "M."), ("M", "M."), ("Madame", "Mme"),
    ("Mrs.", "Mme"), ("Ms", "Mme"), ("Prof.", "Pr"), ("Professeure", "Pre"),
    ("L’honorable", "L'hon."), ("l'hon", "L'hon."), ("Hon.", "L'hon."),
    ("The Honourable", "L'hon."),
])
def test_une_graphie_connue_devient_sa_cle(recu, attendu):
    assert civilite.canonical_prefix(recu) == attendu


def test_un_titre_inconnu_est_garde_tel_quel():
    """Le refuser briserait la synchro DavX5 ; le jeter perdrait la donnée."""
    assert civilite.canonical_prefix("Mlle") == "Mlle"
    assert civilite.canonical_prefix("Capitaine") == "Capitaine"


def test_une_liste_se_joint_comme_davx5_la_joint():
    """DavX5 joint un composant multiple par des espaces et le redécoupe au
    retour : « Ad. E. » part en une valeur et revient en deux."""
    assert civilite.as_text(["Ad.", "E."]) == "Ad. E."
    assert civilite.canonical_prefix(["Dr."]) == "Dr"
    assert civilite.canonical_prefix(["Dr.", "Hon."]) == "Dr. Hon."
    assert civilite.as_text(None) == "" and civilite.as_text(3) == ""


def test_aucune_graphie_ne_mene_a_deux_cles():
    """La table est bâtie à l'import et lève sur une collision ; on le prouve
    en provoquant une collision."""
    with mock.patch.dict(civilite._EXTRA_SPELLINGS, {"Docteur": "Pr"}):
        with pytest.raises(ValueError, match="two keys"):
            civilite._build_aliases()


@pytest.mark.parametrize("nom, attendu", [
    ("L'hon. Marie Côté", "Marie Côté"),
    ("Dre Julie Roy", "Julie Roy"),
    ("Pr Luc Gagnon", "Luc Gagnon"),
    ("Me Anne Roy", "Anne Roy"),
    ("Me", "Me"),                 # un titre seul n'est pas un nom
    ("Mex Roy", "Mex Roy"),       # jamais un startswith sur la clé
    ("Mme  Lise Roy ", "Lise Roy"),
])
def test_le_titre_se_retire_du_nom(nom, attendu):
    assert civilite.strip_prefix(nom) == attendu


def test_le_nom_complet_ne_porte_jamais_le_suffixe_ni_les_autres_prenoms():
    partie = {"type": "individual", "prefix": "L'hon.", "first_name": "Marie",
              "middle_name": "Anne", "last_name": "Côté", "suffix": "j.c.s."}
    assert civilite.full_name(partie, honorific=True) == "L'hon. Marie Côté"
    assert civilite.full_name(partie, honorific=False) == "Marie Côté"
    assert civilite.full_name({"type": "organization",
                               "organization_name": "Béton Nord inc.",
                               "prefix": "Dr"}, honorific=True) == "Béton Nord inc."


def test_un_nom_en_liste_ne_fait_plus_lever_display_name():
    """Un PUT CardDAV stockait des listes : `" ".join` levait TypeError."""
    partie = {"type": "individual", "prefix": ["Dr."], "first_name": ["Jean", "Paul"],
              "last_name": "Roy"}
    assert pm.display_name(partie) == "Dr. Jean Paul Roy"


# ── Les copies dérivées ───────────────────────────────────────────────────


def test_le_modele_le_connecteur_et_les_gabarits_lisent_la_meme_liste():
    assert pm.VALID_PREFIXES == civilite.PREFIXES + ("",)
    assert tools._PARTIE_PREFIXES == list(civilite.PREFIXES)
    assert pm.VALID_OCCUPATIONS == occupations.OCCUPATIONS + ("",)
    assert tools._PARTIE_OCCUPATIONS == list(occupations.OCCUPATIONS)
    assert template_fields._strip_civility_prefix("Pre Ève Roy") == "Ève Roy"


def test_les_titres_ne_creent_pas_de_rapprochement():
    """Chaque jeton de chaque titre est écarté — « L'hon. » donne « l » et
    « hon » ; « Pr » et « Pre » s'ajoutent à l'ancienne liste."""
    for titre in civilite.PREFIXES:
        for jeton in rapprochement._plier(titre).split():
            assert jeton in rapprochement._MOTS_VIDES, titre
    assert rapprochement.jetons("L'hon. Marie Côté") == {"marie", "cote"}
    assert rapprochement.jetons("Pre Ève Gagnon") == {"eve", "gagnon"}


# ── L'occupation ──────────────────────────────────────────────────────────


def test_la_liste_des_occupations_est_celle_de_l_avocat():
    assert occupations.OCCUPATIONS == (
        "salarie", "travailleur_autonome", "etudiant", "retraite",
        "sans_emploi", "benevole", "au_foyer", "autre",
    )
    assert occupations.WITHOUT_EMPLOYER <= set(occupations.OCCUPATIONS)
    assert set(occupations.ORGANIZATION_LABELS) <= set(occupations.OCCUPATIONS)


@pytest.mark.parametrize("occupation, libelle", [
    ("salarie", "Employeur"),
    ("travailleur_autonome", "Entreprise"),
    ("etudiant", "Établissement d'enseignement"),
    ("benevole", "Organisme"),
    ("retraite", "Employeur ou organisation"),
    ("", "Employeur ou organisation"),
])
def test_le_nom_de_l_organisation_suit_l_occupation(occupation, libelle):
    assert occupations.organization_label(occupation) == libelle


def test_les_modules_purs_n_importent_que_la_bibliotheque_standard():
    """Le portail les importe : ni Firestore ni Flask ne doit y entrer."""
    import ast
    import pathlib

    racine = pathlib.Path(__file__).resolve().parent.parent / "utils"
    for nom in ("civilite.py", "occupations.py"):
        arbre = ast.parse((racine / nom).read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if isinstance(noeud, ast.Import):
                modules = [a.name for a in noeud.names]
            elif isinstance(noeud, ast.ImportFrom):
                modules = [noeud.module or ""]
            else:
                continue
            for module in modules:
                assert module.split(".")[0] in sys.stdlib_module_names, (nom, module)
