"""La moitié DÉTECTER du provisionneur — éprouvée sans gcloud ni réseau.

Trois propriétés portent ce fichier, et chacune répare une classe d'erreur
que ce dépôt a déjà payée ailleurs :

* **Un échec de sonde n'est JAMAIS une absence.** C'est la doctrine de
  ``get_coverage_report`` : un rapport ne fabrique pas un manquement à partir
  d'une lecture ratée. Sans elle, la toute première exécution réelle — où
  ``gcloud`` n'était pas résolvable depuis ``subprocess`` — aurait déclaré
  seize ressources absentes sur un projet entièrement provisionné.
* **Les colonnes se découpent sur la TABULATION, vides comprises.** Un
  ``str.split()`` nu décale tout ce qui suit une colonne vide ; mesuré le
  2026-09-13, cela a fait rapporter « accès uniforme DÉSACTIVÉ » sur un seau
  conforme. Un contrôle qui accuse à tort est pire qu'aucun contrôle.
* **Le corpus de production est REJOUÉ.** ``tests/fixtures/gcloud/
  production.json`` porte la sortie réelle de chaque sonde, caviardée de
  l'identifiant du propriétaire, enregistrée le 2026-09-13 contre un
  déploiement dont on sait qu'il est complet. Chaque ressource constatable
  doit y lire ``present`` — c'est le seul test de ce fichier qui éprouve les
  GABARITS de commande et pas seulement la logique.

Les conventions du corpus : l'identifiant de projet devient ``projet-temoin``
(« sa-portail ») ; et, depuis le 2026-11-08 (la surveillance de sécurité),
les identifiants NUMÉRIQUES d'une politique d'alerte ou d'un canal de
notification deviennent des valeurs SYNTHÉTIQUES, un chiffre répété, un par
politique — le dépôt est public, et un verdict ne lit que leur nombre.
"""

import io
import json
import os
import sys

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

from utils.deployment_inventory import RESOURCES  # noqa: E402
from utils.deployment_probe import (  # noqa: E402
    ABSENT,
    DRIFT,
    INCONNU,
    MANUEL,
    PRESENT,
    Run,
    colonne,
    colonnes,
    exit_code,
    expected_index_count,
    gcloud_path,
    probe,
    probe_all,
    render_plan,
    Result,
)

_FIXTURE = os.path.join(_ATHENA, "tests", "fixtures", "gcloud",
                        "production.json")
TAB = chr(9)


def _corpus() -> dict:
    return json.load(io.open(_FIXTURE, encoding="utf-8"))


def _res(key: str):
    return next(r for r in RESOURCES if r.key == key)


def _runner(stdout: str = "", ok: bool = True, stderr: str = ""):
    """Un lanceur qui rend toujours la même chose. C'est ce seam qui rend
    presque tout ce module éprouvable sans identifiants."""
    def run(_argv):
        return Run(ok, stdout, stderr)
    return run


# ── La règle qui compte ──────────────────────────────────────────────────


def test_a_FAILED_probe_is_never_reported_as_an_absent_resource():
    """La première exécution réelle l'a prouvée utile avant même qu'on l'ait
    éprouvée : `subprocess` sur Windows n'ajoute que `.exe`, jamais `.cmd`,
    donc `gcloud` était injoignable — et le script a dit « rien n'a pu être
    constaté » sur dix ressources qui existaient toutes. Rapporter une absence
    aurait envoyé l'exploitant provisionner ce qui était déjà là."""
    for r in RESOURCES:
        if r.manual:
            continue
        for stderr in ("gcloud est introuvable", "PERMISSION_DENIED",
                       "credentials have expired", "délai dépassé (90 s)",
                       "has not been used in project",
                       "NOT_FOUND: Metric x does not exist.", ""):
            res = probe(r, "p", "reg", runner=_runner(ok=False, stderr=stderr))
            assert res.state == INCONNU, (r.key, stderr, res.state)
            assert res.state != ABSENT


def test_a_probe_failure_names_a_REASON_the_operator_can_act_on():
    cas = {
        "gcloud est introuvable": "introuvable",
        "PERMISSION_DENIED: caller lacks permission": "permission",
        "Reauthentication required, please run gcloud auth login": "identifiants",
        "délai dépassé (90 s)": "délai",
        "Cloud Tasks API has not been used in project": "API",
    }
    r = _res("app-engine")
    for stderr, attendu in cas.items():
        res = probe(r, "p", "reg", runner=_runner(ok=False, stderr=stderr))
        assert attendu.lower() in res.detail.lower(), (stderr, res.detail)


def test_a_NOT_FOUND_says_so_and_stays_UNKNOWN_whatever_the_name():
    """Le message de gcloud NOMME la ressource. Jusqu'au 2026-11-08, le mot
    `disabled` du compteur `security_control_disabled` faisait lire SON
    NOT_FOUND comme « l'API nécessaire est désactivée » : la ligne même de
    l'inventaire qui existe pour dire ce compteur absent envoyait activer une
    API déjà active. NOT_FOUND se dit maintenant — et reste INCONNU : un
    projet mal tapé répond pareil, donc rien n'est constaté."""
    from utils.deployment_inventory import SECURITY_METRICS

    for m in SECURITY_METRICS:
        stderr = ("ERROR: (gcloud.logging.metrics.describe) NOT_FOUND: "
                  "Metric " + m.metric + " does not exist.")
        res = probe(_res(m.metric_key), "p", "",
                    runner=_runner(ok=False, stderr=stderr))
        assert res.state == INCONNU, (m.metric, res.state)
        assert "NOT_FOUND" in res.detail, (m.metric, res.detail)
        assert "API" not in res.detail, (m.metric, res.detail)
    # L'API désactivée se dit encore — par les tournures de Google, jamais
    # par un mot isolé qu'un nom de ressource pourrait porter.
    r = _res("metrique-controle-desactive")
    for stderr in (
        "Cloud Logging API has not been used in project 123 before or it "
        "is disabled.",
        "API [logging.googleapis.com] not enabled on project [p].",
        "Error: reason: SERVICE_DISABLED",
    ):
        res = probe(r, "p", "", runner=_runner(ok=False, stderr=stderr))
        assert res.state == INCONNU, (stderr, res.state)
        assert "API" in res.detail, (stderr, res.detail)


def test_an_empty_stdout_on_a_SUCCESSFUL_probe_is_a_real_absence():
    """La règle précédente ne doit pas avaler le vrai cas : une commande qui
    réussit et ne rend rien dit bien que la ressource n'est pas là."""
    for cle in ("app-engine", "firestore-defaut", "seau-quarantaine",
                "file-portail", "sa-portail"):
        res = probe(_res(cle), "p", "reg", runner=_runner(""))
        assert res.state == ABSENT, (cle, res.state, res.detail)


# ── Le piège des colonnes ────────────────────────────────────────────────


def test_columns_split_on_TAB_and_keep_the_empty_ones():
    ligne = "A" + TAB + TAB + "C"
    assert colonnes(ligne) == ["A", "", "C"]
    assert colonne(colonnes(ligne), 1) == ""
    assert colonne(colonnes(ligne), 9) == ""
    # Le retour chariot de Windows ne doit pas coller à la dernière colonne.
    assert colonnes("A" + TAB + "B" + chr(13)) == ["A", "B"]


def test_an_empty_middle_column_does_not_shift_the_bucket_verdict():
    """LE défaut du 2026-09-13, épinglé. Le projet demandait
    `uniform_bucket_level_access.enabled`, champ qui n'existe pas ; gcloud
    rendait une colonne VIDE ; un `split()` nu lisait donc « enforced » comme
    la valeur de l'accès uniforme et criait au loup sur un seau conforme."""
    sortie = "NORTHAMERICA-NORTHEAST1" + TAB + TAB + "enforced"
    res = probe(_res("seau-quarantaine"), "p", "northamerica-northeast1",
                runner=_runner(sortie))
    # La colonne vide se lit comme « pas renseigné », donc on ne conclut RIEN
    # sur l'accès uniforme — et surtout pas l'inverse de ce qui est écrit.
    assert res.state == PRESENT, res.detail
    assert "DÉSACTIVÉ" not in res.detail


def test_the_bucket_projection_asks_for_a_field_that_EXISTS():
    """L'autre moitié du même incident : le gabarit lui-même était faux."""
    detect = " ".join(_res("seau-quarantaine").detect)
    assert "uniform_bucket_level_access," in detect or \
           detect.endswith("uniform_bucket_level_access")
    assert "uniform_bucket_level_access.enabled" not in detect


# ── Les verdicts, un par un ──────────────────────────────────────────────


def test_app_engine_region_is_authoritative_and_a_mismatch_is_drift():
    ctx = {"region_demandee": "europe-west1"}
    res = probe(_res("app-engine"), "p", "europe-west1",
                runner=_runner("northamerica-northeast1"), ctx=ctx)
    assert res.state == DRIFT
    assert "DÉFINITIVE" in res.detail
    # Et la région RÉELLE est retenue pour les ressources suivantes.
    assert ctx["region_reelle"] == "northamerica-northeast1"


def test_firestore_refuses_datastore_mode_and_a_foreign_region():
    ctx = {"region_reelle": "northamerica-northeast1"}
    r = _res("firestore-defaut")
    mauvais_mode = probe(r, "p", "", ctx=dict(ctx), runner=_runner(
        "northamerica-northeast1" + TAB + "DATASTORE_MODE"))
    assert mauvais_mode.state == DRIFT and "NATIVE" in mauvais_mode.detail
    ailleurs = probe(r, "p", "", ctx=dict(ctx), runner=_runner(
        "europe-west1" + TAB + "FIRESTORE_NATIVE"))
    assert ailleurs.state == DRIFT and "europe-west1" in ailleurs.detail


def test_the_index_count_is_DERIVED_from_the_versioned_file():
    attendu = expected_index_count()
    assert isinstance(attendu, int) and attendu > 0
    r = _res("index-composites")
    tous = probe(r, "p", "", ctx={"index_attendus": attendu},
                 runner=_runner("\n".join(["READY"] * attendu)))
    assert tous.state == PRESENT
    un_de_moins = probe(r, "p", "", ctx={"index_attendus": attendu},
                        runner=_runner("\n".join(["READY"] * (attendu - 1)
                                                 + ["CREATING"])))
    assert un_de_moins.state == DRIFT
    assert "LISTE VIDE" in un_de_moins.detail


def test_an_unreadable_index_file_yields_UNKNOWN_never_zero_expected():
    """« zéro index attendu » est une affirmation confiante et fausse — la
    forme du défaut « Page 7 / 0 »."""
    assert expected_index_count("/chemin/qui/nexiste/pas") is None
    res = probe(_res("index-composites"), "p", "",
                ctx={"index_attendus": None}, runner=_runner("READY"))
    assert res.state == INCONNU


def test_the_firewall_verdict_is_about_ORDER_not_presence():
    r = _res("pare-feu-interne")
    bon = (("10" + TAB + "ALLOW" + TAB + "0.1.0.2/32") + "\n"
           + ("100" + TAB + "ALLOW" + TAB + "173.245.48.0/20") + "\n"
           + ("2147483647" + TAB + "DENY" + TAB + "*"))
    assert probe(r, "p", "", runner=_runner(bon)).state == PRESENT

    # Autorisée, mais SOUS le refus par défaut : jamais atteinte.
    sous = (("100" + TAB + "DENY" + TAB + "*") + "\n"
            + ("500" + TAB + "ALLOW" + TAB + "0.1.0.2/32"))
    res = probe(r, "p", "", runner=_runner(sous))
    assert res.state == DRIFT and "SOUS le refus" in res.detail

    # Absente : le mode de défaillance silencieux que §7 enseignait.
    sans = (("100" + TAB + "ALLOW" + TAB + "173.245.48.0/20") + "\n"
            + ("2147483647" + TAB + "DENY" + TAB + "*"))
    res = probe(r, "p", "", runner=_runner(sans))
    assert res.state == DRIFT and "SILENCE" in res.detail

    # Aucun refus par défaut : l'origine est joignable en direct.
    ouvert = "10" + TAB + "ALLOW" + TAB + "0.1.0.2/32"
    res = probe(r, "p", "", runner=_runner(ouvert))
    assert res.state == DRIFT and "refus par défaut" in res.detail


def test_a_paused_queue_is_drift_and_says_why_it_looks_like_slowness():
    res = probe(_res("file-portail"), "p", "", runner=_runner("PAUSED"))
    assert res.state == DRIFT
    assert "quinze minutes" in res.detail


def test_the_lifecycle_verdict_compares_against_the_versioned_policy():
    r = _res("cycle-de-vie-quarantaine")
    bon = json.dumps({"lifecycle_config": {"rule": [
        {"action": {"type": "Delete"},
         "condition": {"age": 90, "matchesPrefix": ["submissions/"]}},
        {"action": {"type": "Delete"},
         "condition": {"age": 365, "matchesPrefix": ["archive/"]}},
    ]}})
    assert probe(r, "p", "", runner=_runner(bon)).state == PRESENT

    aucune = json.dumps({"lifecycle_config": {}})
    res = probe(r, "p", "", runner=_runner(aucune))
    assert res.state == ABSENT and "POUR TOUJOURS" in res.detail

    trop_court = bon.replace('"age": 365', '"age": 7')
    res = probe(r, "p", "", runner=_runner(trop_court))
    assert res.state == DRIFT and "archive/" in res.detail


def test_a_manual_step_is_never_probed():
    """Une étape de console n'a pas de commande ; l'inventer serait prétendre
    la vérifier."""
    appels = []

    def espion(argv):
        appels.append(argv)
        return Run(True, "peu importe")

    for r in RESOURCES:
        if not r.manual:
            continue
        assert probe(r, "p", "reg", runner=espion).state == MANUEL
    assert appels == []


# ── Le plan, le code de sortie, et le corpus ─────────────────────────────


def test_exit_code_separates_BROKEN_from_NOT_DONE_YET():
    """Un CI qui confond « va cliquer dans une console » avec « quelque chose
    est cassé » finit par ignorer les deux."""
    assert exit_code([Result("a", PRESENT)]) == 0
    assert exit_code([Result("a", PRESENT), Result("b", MANUEL)]) == 2
    assert exit_code([Result("a", PRESENT), Result("b", ABSENT)]) == 2
    assert exit_code([Result("a", PRESENT), Result("b", INCONNU)]) == 2
    # La dérive l'emporte : elle dit qu'une ressource CONTREDIT le code.
    assert exit_code([Result("a", ABSENT), Result("b", DRIFT)]) == 1


def test_the_plan_lists_only_what_remains():
    resultats = [Result(r.key, PRESENT) for r in RESOURCES]
    assert "rien à faire" in render_plan(resultats)
    partiel = [Result(r.key, PRESENT if r.key != "file-portail" else ABSENT)
               for r in RESOURCES]
    texte = render_plan(partiel)
    assert "file-portail" in texte
    assert "app-engine" not in texte


def test_the_plan_marks_the_irreversible_steps():
    resultats = [Result(r.key, ABSENT if not r.manual else MANUEL)
                 for r in RESOURCES]
    texte = render_plan(resultats)
    for r in RESOURCES:
        if r.irreversible:
            i = texte.index(r.key)
            assert "DÉFINITIF" in texte[i:i + 160], r.key


# Les ressources constatables dont la sortie n'a PAS ENCORE été relevée sur
# le déploiement vivant. Le corpus ne contient que des sorties RÉELLES : y
# écrire la sortie qu'on ATTEND serait fabriquer la preuve même que ce corpus
# existe pour apporter. Une ressource en attente est donc rejouée comme une
# sonde qui échoue — et doit se lire INCONNU, jamais ABSENT, ce qui éprouve au
# passage la règle qui compte. Relever sa sortie (DEPLOYMENT.md §15, la
# commande en lecture seule), la verser au corpus, puis la retirer d'ici :
# le test suivant refuse une entrée périmée.
_CORPUS_EN_ATTENTE: set = {"index-composites"}
# « index-composites » en attente depuis le 2026-11-12 : la page Documents
# déclare (category ASC, created_at DESC) sur `documents`, le 38e composite.
# La sortie relevée le 2026-09-13 (37 READY) ne décrit plus un déploiement
# complet, et y AJOUTER un READY serait fabriquer la preuve. Une fois l'index
# déployé et « Enabled », relever la sortie réelle de la sonde (`gcloud
# firestore indexes composite list --project=… --format=value(state)`), la
# verser au corpus, puis retirer la clé d'ici.
# Vide du 2026-11-08 au 2026-11-12 : les ressources de la surveillance de sécurité
# (DEPLOYMENT.md §6.10) ont été relevées ce jour-là sur le déploiement vivant
# par les commandes en lecture seule de la sonde — les compteurs à nouveau,
# avec leur filtre, quand la sonde s'est mise à le comparer —, et versées au
# corpus caviardées (projet → `projet-temoin`, jusque dans les filtres ;
# identifiants des politiques et du canal → valeurs synthétiques, voir le
# docstring).
# Vide aussi du 2026-09-28 au 2026-11-08 : « cycle-de-vie-staging » (lot 2A,
# étape T5) avait été relevé ce jour-là sur le seau canonique vivant par la
# commande en lecture seule de la sonde, et versé au corpus tel quel.


def test_the_recorded_production_corpus_replays_to_PRESENT():
    """Le seul test qui éprouve les GABARITS et pas seulement la logique.

    Le corpus est la sortie RÉELLE de chaque sonde, enregistrée contre un
    déploiement dont on sait qu'il est complet (le 2026-09-13 ; le 2026-11-08
    pour la surveillance de sécurité), et caviardée selon les conventions du
    docstring du module. S'il cesse de rendre `present`, soit un
    verdict a dérivé, soit un gabarit de commande demande un champ que gcloud
    n'expose pas — le défaut exact que ce corpus existe pour attraper.
    """
    corpus = _corpus()

    def rejoueur(argv):
        # La clé se retrouve par la ressource dont le gabarit a produit cet
        # argv : on compare sur le projet témoin du corpus.
        for r in RESOURCES:
            if r.manual:
                continue
            if r.argv("projet-temoin", "northamerica-northeast1") == tuple(argv):
                if r.key in _CORPUS_EN_ATTENTE:
                    return Run(False, stderr="sortie non encore relevée")
                e = corpus[r.key]
                return Run(e["ok"], e["stdout"])
        raise AssertionError("argv hors corpus : " + repr(argv))

    resultats = probe_all("projet-temoin", "northamerica-northeast1",
                          runner=rejoueur)
    par_cle = {r.key: r for r in resultats}
    for r in RESOURCES:
        if r.manual:
            attendu = MANUEL
        elif r.key in _CORPUS_EN_ATTENTE:
            attendu = INCONNU
        else:
            attendu = PRESENT
        assert par_cle[r.key].state == attendu, (
            r.key, par_cle[r.key].state, par_cle[r.key].detail
        )


def test_the_corpus_waiting_list_is_neither_stale_nor_a_hiding_place():
    """Chaque entrée en attente est une ressource CONSTATABLE qui existe et
    qui n'est pas au corpus. Une fois sa sortie versée, l'entrée doit partir —
    sinon le test précédent cesserait de la rejouer pour de bon."""
    corpus = _corpus()
    constatables = {r.key for r in RESOURCES if not r.manual}
    for cle in _CORPUS_EN_ATTENTE:
        assert cle in constatables, (cle, "pas une ressource constatable")
        assert cle not in corpus, (cle, "au corpus : retirez-la de l'attente")
    # Et toute autre ressource constatable EST au corpus.
    assert constatables - _CORPUS_EN_ATTENTE <= set(corpus)


def test_the_canonical_staging_lifecycle_verdict():
    """Le seau canonique porte les documents des clients : `staging/` à
    7 jours, et aucune autre suppression d'objets VIVANTS."""
    r = _res("cycle-de-vie-staging")

    def regles(*liste):
        return json.dumps({"lifecycle_config": {"rule": list(liste)}})

    staging = {"action": {"type": "Delete"},
               "condition": {"age": 7, "matchesPrefix": ["staging/"]}}
    assert probe(r, "p", "", runner=_runner(regles(staging))).state == PRESENT

    # Le versionnement (versions NON COURANTES) ne touche aucun objet vivant.
    versions = {"action": {"type": "Delete"},
                "condition": {"daysSinceNoncurrentTime": 30}}
    assert probe(r, "p", "",
                 runner=_runner(regles(staging, versions))).state == PRESENT

    res = probe(r, "p", "", runner=_runner(regles()))
    assert res.state == ABSENT and "POUR TOUJOURS" in res.detail

    trop_long = dict(staging, condition={"age": 30,
                                         "matchesPrefix": ["staging/"]})
    res = probe(r, "p", "", runner=_runner(regles(trop_long)))
    assert res.state == DRIFT and "30" in res.detail

    # Une suppression SANS préfixe, ou sur `users/`, effacerait des documents.
    partout = {"action": {"type": "Delete"}, "condition": {"age": 7}}
    res = probe(r, "p", "", runner=_runner(regles(staging, partout)))
    assert res.state == DRIFT and "documents des clients" in res.detail
    documents = {"action": {"type": "Delete"},
                 "condition": {"age": 400, "matchesPrefix": ["users/"]}}
    res = probe(r, "p", "", runner=_runner(regles(staging, documents)))
    assert res.state == DRIFT and "users/" in res.detail

    # Le gabarit vise le seau CANONIQUE, pas celui de la quarantaine.
    argv = r.argv("mon-projet", "ma-region")
    assert "gs://mon-projet.firebasestorage.app" in argv
    assert not any("quarantaine" in a for a in argv)


# ── La surveillance de sécurité (DEPLOYMENT.md §6.10) ───────────────────


def test_every_detectable_resource_has_a_verdict():
    """Sans verdict, une sonde rend « inconnu » à JAMAIS, même sur une
    ressource en place — un rapport qui ne peut pas dire oui. Les lignes de
    la surveillance sont dérivées d'une table : leurs verdicts aussi, et
    ceci épingle qu'aucune n'est oubliée."""
    from utils.deployment_probe import _VERDICTS

    sans = [r.key for r in RESOURCES if not r.manual and r.key not in _VERDICTS]
    assert sans == []
    for r in RESOURCES:
        if r.manual:
            continue
        res = probe(r, "p", "reg", runner=_runner("x"))
        assert "aucun verdict" not in res.detail, r.key


def test_a_security_metric_is_judged_on_what_it_COUNTS():
    """Exister ne suffit pas : un compteur dont le filtre nomme le journal
    d'un autre projet — l'adoptant qui a créé le sien d'un fichier non
    remplacé — existe et ne compte rien, en silence ; un compteur DÉSACTIVÉ
    ne produit aucun point. Le filtre attendu est celui du FICHIER, au
    projet sondé près, et l'égalité est EXACTE ; sans lui, ou sans filtre
    lu, rien n'est comparé, et le verdict ne dit pas PRESENT."""
    from utils.deployment_inventory import SECURITY_METRICS
    from utils.deployment_probe import expected_metric_filter

    for m in SECURITY_METRICS:
        r = _res(m.metric_key)
        # Le verdict lit le filtre en DEUXIÈME colonne et l'état en
        # troisième : la commande doit les demander là. Le corpus rejoué ne
        # le voit pas — il rend la sortie relevée quoi que la commande
        # demande.
        assert r.detect[-1] == "--format=value(name,filter,disabled)", r.detect
        attendu = expected_metric_filter(m.metric, "p")
        assert attendu and attendu.startswith('logName="projects/p/logs/')
        ctx = {"filtres_attendus": {m.metric: attendu}}

        def etat(sortie, contexte=ctx):
            res = probe(r, "p", "", runner=_runner(sortie), ctx=contexte)
            return res.state, res.detail

        assert etat("") == (ABSENT, "introuvable")
        present = (PRESENT, "activé, compte ce que dit " + m.metric_file)
        assert etat(TAB.join([m.metric, attendu])) == present
        # Une colonne `disabled` vide (champ non posé) ne se juge pas.
        assert etat(TAB.join([m.metric, attendu, ""])) == present
        # Désactivé : il ne compte rien, quel que soit son filtre.
        state, detail = etat(TAB.join([m.metric, attendu, "True"]))
        assert state == DRIFT and "DÉSACTIVÉ" in detail
        # Le journal d'un autre projet, un autre événement, une clause en
        # plus, une négation — et, dans l'autre sens, la clause d'événement
        # sans son journal, le journal sans sa clause : l'égalité est
        # exacte, jamais une inclusion, ni dans un sens ni dans l'autre.
        journal, clause = attendu.split(" AND ", 1)
        for sortie in (
                TAB.join([m.metric, attendu.replace("projects/p/",
                                                     "projects/autre/")]),
                TAB.join([m.metric, attendu.replace('"' + m.events[0] + '"',
                                                     '"csrf_failure"')]),
                TAB.join([m.metric, attendu + " AND severity>=WARNING"]),
                TAB.join([m.metric, "NOT " + attendu]),
                TAB.join([m.metric, clause]),
                TAB.join([m.metric, journal])):
            state, detail = etat(sortie)
            assert state == DRIFT and m.metric_file in detail, sortie
        # Aucun filtre lu (une projection ratée) : rien n'est comparé — un
        # contrôle qui accuse à tort est pire qu'aucun.
        state, detail = etat(m.metric)
        assert state == INCONNU and "colonne vide" in detail
        # Sans filtre attendu, la comparaison n'a pas eu lieu : INCONNU.
        for contexte in ({}, {"filtres_attendus": {m.metric: None}}):
            state, detail = etat(TAB.join([m.metric, attendu]), contexte)
            assert state == INCONNU and "pas été comparé" in detail


def test_the_expected_counter_filter_is_DERIVED_from_its_file(tmp_path):
    """Le filtre du fichier, le projet de son `logName` remplacé par le
    projet sondé ; illisible ou d'une autre forme, ``None`` — jamais un
    filtre deviné."""
    from utils.deployment_inventory import SECURITY_METRICS
    from utils.deployment_probe import _REPO_ROOT, expected_metric_filter

    for m in SECURITY_METRICS:
        with io.open(os.path.join(_REPO_ROOT, *m.metric_file.split("/")),
                     encoding="utf-8") as fh:
            fichier = json.load(fh)["filter"]
        attendu = expected_metric_filter(m.metric, "mon-projet")
        assert attendu.startswith('logName="projects/mon-projet/logs/')
        assert attendu.split("/logs/", 1)[1] == fichier.split("/logs/", 1)[1]

    m = SECURITY_METRICS[0]
    dossier = tmp_path / "monitoring" / "metrics"
    dossier.mkdir(parents=True)
    with io.open(os.path.join(_REPO_ROOT, *m.metric_file.split("/")),
                 "rb") as fh:
        vrai = fh.read()
    # Le témoin : la copie exacte du vrai fichier se lit.
    (dossier / (m.metric + ".json")).write_bytes(vrai)
    lu = expected_metric_filter(m.metric, "p", str(tmp_path))
    assert lu and lu.startswith('logName="projects/p/logs/')
    # Absent, puis le MÊME fichier derrière une BOM (illisible : ce n'est
    # plus du JSON), puis d'une autre forme — sans `logName`, pas une
    # chaîne : chaque fois None. Lu AVANT l'assert
    # (py/side-effect-in-assert).
    (dossier / (m.metric + ".json")).unlink()
    lu = expected_metric_filter(m.metric, "p", str(tmp_path))
    assert lu is None
    for contenu in (
            b"\xef\xbb\xbf" + vrai,
            json.dumps({"filter": 'jsonPayload.event="redirect_rejected"'})
            .encode("utf-8"),
            json.dumps({"filter": 7}).encode("utf-8")):
        (dossier / (m.metric + ".json")).write_bytes(contenu)
        lu = expected_metric_filter(m.metric, "p", str(tmp_path))
        assert lu is None, contenu

    # Un séparateur de lignes, à n'importe quelle place — au milieu comme
    # en dernière position — et de n'importe quelle sorte que `splitlines`
    # reconnaît : None — la sonde ne lit que la première ligne de gcloud, et
    # le dirait en dérive à jamais. Le témoin sur une ligne se lit, pour que
    # chaque variante ne prouve que son séparateur.
    une_ligne = 'logName="projects/p/logs/x" AND jsonPayload.event=("y" OR "z")'
    (dossier / (m.metric + ".json")).write_bytes(
        json.dumps({"filter": une_ligne}).encode("utf-8"))
    lu = expected_metric_filter(m.metric, "p", str(tmp_path))
    assert lu == une_ligne
    for variante in (
            une_ligne.replace('"y" OR', '"y"' + chr(10) + 'OR'),
            une_ligne.replace('"y" OR', '"y"' + chr(13) + 'OR'),
            une_ligne.replace('"y" OR', '"y"' + chr(0x2028) + 'OR'),
            une_ligne.replace("/logs/x", "/logs/x" + chr(10) + "y"),
            une_ligne + chr(13),
            une_ligne + chr(0x85)):
        (dossier / (m.metric + ".json")).write_bytes(
            json.dumps({"filter": variante}).encode("utf-8"))
        lu = expected_metric_filter(m.metric, "p", str(tmp_path))
        assert lu is None, repr(variante)


def test_a_counter_created_from_another_projects_file_is_DRIFT():
    """Le corpus rejoué pour un AUTRE projet : chaque compteur y nomme le
    journal de `projet-temoin`, donc aucun ne compterait les lignes de
    `mon-projet` — la sonde le dit, au lieu de « présent »."""
    from utils.deployment_inventory import SECURITY_METRICS
    from utils.deployment_probe import expected_metric_filter

    corpus = _corpus()
    for m in SECURITY_METRICS:
        e = corpus[m.metric_key]
        ctx = {"filtres_attendus": {
            m.metric: expected_metric_filter(m.metric, "mon-projet")}}
        res = probe(_res(m.metric_key), "mon-projet", "",
                    runner=_runner(e["stdout"], e["ok"]), ctx=ctx)
        assert res.state == DRIFT, (m.metric_key, res.detail)
        # Et le même relevé, pour le projet qu'il nomme, est PRESENT.
        ctx = {"filtres_attendus": {
            m.metric: expected_metric_filter(m.metric, "projet-temoin")}}
        res = probe(_res(m.metric_key), "projet-temoin", "",
                    runner=_runner(e["stdout"], e["ok"]), ctx=ctx)
        assert res.state == PRESENT, (m.metric_key, res.detail)


def test_an_alert_policy_is_judged_on_what_a_firing_would_do():
    """Une politique en double écrit deux fois ; désactivée ou sans canal,
    elle ne prévient personne. Une colonne `enabled` vide ne se juge pas."""
    from utils.deployment_inventory import SECURITY_METRICS

    canal = "projects/p/notificationChannels/123"
    for m in [x for x in SECURITY_METRICS if x.policy]:
        r = _res(m.policy_key)

        def etat(sortie):
            res = probe(r, "p", "", runner=_runner(sortie))
            return res.state, res.detail

        nom = "projects/p/alertPolicies/1"
        assert etat("") == (ABSENT, "aucune politique ne surveille ce compteur")
        assert etat(TAB.join([nom, "True", canal])) == (
            PRESENT, "activée, 1 canal(aux) de notification")
        assert etat(TAB.join([nom, "True", canal + ";" + canal + "9"]))[1] == (
            "activée, 2 canal(aux) de notification")
        # Une colonne `enabled` absente ne se juge pas…
        assert etat(TAB.join([nom, "", canal]))[0] == PRESENT
        # …mais une politique désactivée, sans canal ou en double, si.
        state, detail = etat(TAB.join([nom, "False", canal]))
        assert state == DRIFT and "DÉSACTIVÉE" in detail
        state, detail = etat(TAB.join([nom, "True", ""]))
        assert state == DRIFT and "aucun canal" in detail
        state, detail = etat(TAB.join([nom, "True"]))
        assert state == DRIFT and "aucun canal" in detail
        deux = chr(10).join([TAB.join([nom, "True", canal]),
                             TAB.join([nom + "2", "True", canal])])
        state, detail = etat(deux)
        assert state == DRIFT and "2 politiques" in detail


def test_the_corpus_carries_no_owner_literal_and_no_payload():
    """Un corpus de test est un fichier versionné. Celui-ci vient d'un
    déploiement vivant, donc il se caviarde — et aucune commande de ce module
    ne lisant de secret, il n'a rien d'autre à cacher."""
    from utils.deployment_inventory import OWNER_LITERALS

    brut = io.open(_FIXTURE, encoding="utf-8").read()
    for cle, valeur in OWNER_LITERALS.items():
        if cle == "App Engine region":
            continue  # la région n'est pas un identifiant, et elle est LUE
        assert valeur not in brut, (cle, "littéral du propriétaire au corpus")
    for mot in ("BEGIN ", "PRIVATE", "password", "$2b$"):
        assert mot.lower() not in brut.lower(), mot


def test_the_corpus_policy_and_channel_ids_are_synthetic():
    """Le dépôt est public : un identifiant numérique de politique ou de
    canal relevé en production n'entre pas au corpus — un chiffre répété le
    remplace, distinct par politique. Les verdicts n'en lisent que le
    NOMBRE (une politique, un canal), donc rien n'est perdu."""
    politiques, canaux = [], []
    for e in _corpus().values():
        for ligne in e["stdout"].splitlines():
            for champ in colonnes(ligne):
                for valeur in champ.split(";"):
                    if "/alertPolicies/" in valeur:
                        politiques.append(valeur.rsplit("/", 1)[1])
                    elif "/notificationChannels/" in valeur:
                        canaux.append(valeur.rsplit("/", 1)[1])
    # Pas vide : chaque politique de §6.10 est au corpus.
    from utils.deployment_inventory import SECURITY_METRICS

    attendues = len([m for m in SECURITY_METRICS if m.policy])
    assert attendues and len(politiques) >= attendues and canaux, (
        politiques, canaux)
    for identifiant in politiques + canaux:
        assert identifiant.isdigit() and len(set(identifiant)) == 1, (
            identifiant, "identifiant réel au corpus : caviardez-le")
    assert len(set(politiques)) == len(politiques), politiques


def test_gcloud_is_resolved_to_a_full_path_outside_the_working_directory():
    """Ce module EXÉCUTE ce qu'il résout. Accepter une résolution dans le
    répertoire courant reviendrait à laisser un dépôt cloné fournir son
    propre « gcloud »."""
    chemin, motif = gcloud_path()
    assert chemin or motif, "ni chemin ni motif — le silence n'est pas un état"
    if chemin:
        assert os.path.isabs(chemin) or os.environ.get("GCLOUD_BIN")
        assert os.path.dirname(os.path.abspath(chemin)) != \
            os.path.abspath(os.getcwd())


def test_no_web_surface_may_import_the_probe():
    """La sonde lance des PROCESSUS. Une route Flask qui l'importerait
    mettrait un `subprocess` de quatre-vingt-dix secondes sur le chemin d'une
    requête servie par deux travailleurs et quatre fils — et `gcloud` n'existe
    de toute façon pas sur App Engine. La séparation est donc structurelle :
    le rapport partagé vit dans `config_checks`, que la page consomme ; la
    sonde reste un outil de terminal.
    """
    import ast

    interdits = []
    for dossier in ("routes", "utils", "models", "mcp", "client"):
        racine = os.path.join(_ATHENA, dossier)
        for base, _d, fichiers in os.walk(racine):
            if "__pycache__" in base:
                continue
            for nom in fichiers:
                if not nom.endswith(".py") or nom == "deployment_probe.py":
                    continue
                chemin = os.path.join(base, nom)
                arbre = ast.parse(io.open(chemin, encoding="utf-8").read())
                for n in ast.walk(arbre):
                    cible = ""
                    if isinstance(n, ast.ImportFrom):
                        cible = n.module or ""
                    elif isinstance(n, ast.Import):
                        cible = ",".join(a.name for a in n.names)
                    if "deployment_probe" in cible:
                        interdits.append(os.path.relpath(chemin, _ATHENA))
    assert not interdits, (
        "ces modules importent la sonde, qui lance des processus : "
        + repr(sorted(set(interdits)))
    )
