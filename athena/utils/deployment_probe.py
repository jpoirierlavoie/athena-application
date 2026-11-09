"""Constater ce qui est provisionné — et rien d'autre.

La moitié DÉTECTER d'un provisionneur. Il n'y a pas d'``apply()`` ici, et
l'absence est **structurelle** plutôt que promise : ``deployment_inventory``
n'expose que des verbes de lecture, et un balayage de
``tests/test_deployment_inventory.py`` refuse par liste blanche tout verbe qui
n'est pas ``describe`` / ``list`` / ``get-iam-policy``. La raison est la leçon
du 2026-09-11, payée quatre fois dans une seule journée : une commande écrite
mais jamais exécutée est confiante et fausse, et la moitié qui MUTE ne peut pas
être éprouvée sans dépenser un projet jetable.

Trois règles portent ce module, et chacune répare une classe d'erreur que ce
dépôt a déjà commise ailleurs :

* **Un échec de sonde n'est JAMAIS une absence.** ``gcloud`` introuvable, des
  identifiants périmés, une API désactivée — tout cela rend ``INCONNU``, jamais
  ``ABSENT``. C'est la doctrine de ``get_coverage_report`` : un rapport ne doit
  pas fabriquer un manquement à partir d'une lecture ratée, sans quoi on envoie
  l'exploitant provisionner ce qui existe déjà.
* **Le projet est toujours EXPLICITE.** Aucune commande n'hérite de
  ``gcloud config`` : un projet actif périmé est la façon dont on inspecte —
  ou provisionne — chez quelqu'un d'autre.
* **Aucune charge ne transite par un argument.** Ce module ne lit aucun secret
  et n'en écrit aucun ; les seules valeurs qu'il manipule sont des noms, des
  régions et des états.

``runner`` est injectable, ce qui est ce qui rend la logique de verdict
testable sans ``gcloud``, sans identifiants et sans réseau — soit la majeure
partie de ce fichier.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

from utils.deployment_inventory import (
    APPENGINE_INTERNAL_CIDR,
    CANONICAL_STAGING_LIFECYCLE,
    PHASE_ORDER,
    QUARANTINE_LIFECYCLE,
    RESOURCES,
    SECURITY_METRICS,
    Resource,
    metric_path,
    provisioning_plan,
)
from utils.deployment_report import FAIL, OK, WARN, Report

SECTION_RESOURCES = "5. Provisioned resources"

PRESENT = "present"
ABSENT = "absent"
DRIFT = "drift"
INCONNU = "inconnu"
MANUEL = "manuel"

# Le délai d'une sonde. Généreux — `gcloud` démarre un interpréteur Python —
# mais BORNÉ : un provisionneur qui pend sur une commande est indiscernable
# d'un provisionneur en panne.
_TIMEOUT_S = 90


@dataclass(frozen=True)
class Result:
    """Le verdict d'UNE ressource."""

    key: str
    state: str
    detail: str = ""
    #: ce que la commande a réellement rendu, tronqué — jamais un secret,
    #: puisque aucune commande de ce module n'en lit un.
    observed: str = ""


@dataclass(frozen=True)
class Run:
    """Ce qu'un lanceur rend. Séparé de ``Result`` à dessein : l'un est un
    fait sur un processus, l'autre un jugement sur une ressource."""

    ok: bool
    stdout: str = ""
    stderr: str = ""


def gcloud_path() -> tuple:
    """Où est `gcloud`, et pourquoi on ne le trouve pas. Rend (chemin, motif).

    ⚠ Mesuré le 2026-09-13, et c'est le genre de défaut qui ne se voit qu'en
    exécutant : sur Windows, `subprocess` avec `shell=False` appelle
    `CreateProcess`, qui n'ajoute QUE `.exe` — jamais `.cmd`. Or le lanceur
    installé par le SDK est `gcloud.CMD`. Passer la chaîne « gcloud » lève
    donc `FileNotFoundError` alors que le SDK est là, authentifié, et que le
    même mot marche dans n'importe quel shell. On résout donc le chemin
    COMPLET avant d'exécuter, ce qui règle aussi l'ambiguïté de PATH.

    ⚠ Et on REFUSE une résolution dans le répertoire courant : sur Windows,
    `shutil.which` y regarde d'abord. Ce module exécute ce qu'il résout, donc
    accepter le répertoire courant reviendrait à laisser un dépôt cloné
    fournir son propre « gcloud ».
    """
    impose = os.environ.get("GCLOUD_BIN", "").strip()
    if impose:
        if os.path.isfile(impose):
            return impose, ""
        return "", "GCLOUD_BIN désigne « %s », qui n'est pas un fichier" % impose
    trouve = shutil.which("gcloud")
    if not trouve:
        return "", ("gcloud est introuvable — sur Windows, le lanceur du SDK "
                    "s'appelle gcloud.CMD ; posez GCLOUD_BIN si besoin")
    if os.path.dirname(os.path.abspath(trouve)) == os.path.abspath(os.getcwd()):
        return "", ("gcloud a été résolu DANS le répertoire courant — refusé. "
                    "Posez GCLOUD_BIN sur le vrai lanceur du SDK.")
    return trouve, ""


def _run(argv: tuple) -> Run:
    """Le lanceur réel. Le seul endroit de ce module qui touche l'extérieur."""
    chemin, motif = gcloud_path()
    if not chemin:
        return Run(False, stderr=motif)
    argv = (chemin,) + tuple(argv[1:])
    try:
        p = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
            shell=False,
        )
    except FileNotFoundError:
        return Run(False, stderr="gcloud introuvable : " + chemin)
    except subprocess.TimeoutExpired:
        return Run(False, stderr="délai dépassé (%d s)" % _TIMEOUT_S)
    except OSError as exc:  # pragma: no cover - dépend de l'OS
        return Run(False, stderr=type(exc).__name__)
    return Run(p.returncode == 0, (p.stdout or "").strip(),
               (p.stderr or "").strip())


# ── Lire ce qui est ATTENDU, plutôt que de le recopier ───────────────────

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_ATHENA_DIR)


def expected_index_count(repo_root: str = "") -> Optional[int]:
    """Combien d'index composites `firestore.indexes.json` déclare.

    DÉRIVÉ du fichier versionné, jamais recopié : c'est ce qui fait qu'ajouter
    un index et oublier de le déployer se voit. Rend ``None`` — jamais 0 — si
    le fichier est illisible : « zéro index attendu » est une affirmation
    confiante et fausse, la forme du défaut « Page 7 / 0 ».
    """
    chemin = os.path.join(repo_root or _REPO_ROOT, "firestore.indexes.json")
    try:
        with open(chemin, encoding="utf-8") as fh:
            return len(json.load(fh).get("indexes", []))
    except Exception:
        return None


# Le filtre d'un compteur : `logName="projects/<projet>/logs/<journal>" AND
# <clause>`. Le groupe garde tout ce qui suit le projet. Linéaire (CWE-1333) :
# `[^/"]+` ne peut franchir le `/` qui le suit, `[^"]+` le `"` qui le suit.
# Un filtre sur plusieurs lignes ne s'y présente jamais :
# ``expected_metric_filter`` le refuse avant (voir là).
_FILTRE_DE_COMPTEUR = re.compile(r'logName="projects/[^/"]+(/logs/[^"]+" AND .+)')


def expected_metric_filter(metric: str, project: str,
                           repo_root: str = "") -> Optional[str]:
    """Le filtre que ce compteur doit porter DANS CE PROJET.

    DÉRIVÉ de son fichier (`monitoring/metrics/<compteur>.json`), jamais
    recopié : le filtre du fichier, le projet de son `logName` remplacé par
    le projet sondé — un compteur ne voit que les lignes de son propre
    projet, donc un `logName` qui en nomme un autre ne compte rien. Rend
    ``None`` — jamais un filtre deviné — si le fichier est illisible ou n'a
    pas cette forme : la sonde le dit alors INCONNU. Un filtre qui porte un
    séparateur de lignes — un saut de ligne, mais aussi tout séparateur que
    ``str.splitlines`` reconnaît (CR, U+2028…), même en dernière position —
    n'a pas cette forme : la sonde ne lit que la première ligne de la sortie
    de gcloud, et le dirait en dérive à jamais.
    """
    chemin = os.path.join(repo_root or _REPO_ROOT, *metric_path(metric).split("/"))
    try:
        with open(chemin, encoding="utf-8") as fh:
            filtre = json.load(fh).get("filter")
    except Exception:
        return None
    if not isinstance(filtre, str) or filtre.splitlines() != [filtre]:
        return None
    trouve = _FILTRE_DE_COMPTEUR.fullmatch(filtre)
    if trouve is None:
        return None
    return 'logName="projects/' + project + trouve.group(1)


# ⚠ `gcloud --format=value(a,b,c)` sépare par des TABULATIONS et laisse la
# colonne d'un champ absent VIDE. Un `str.split()` nu réduit les blancs et
# DÉCALE donc tout ce qui suit d'un cran — mesuré le 2026-09-13 : le seau de
# quarantaine s'est fait rapporter « accès uniforme DÉSACTIVÉ » alors que la
# valeur lue était « enforced », la colonne de l'accès public glissée d'une
# place. Un contrôle qui accuse à tort est pire qu'aucun contrôle : on le
# désarme, et il ne dit plus rien le jour où il a raison.
#
# Deux règles en sortent, et elles vont ensemble : on découpe sur la
# TABULATION en gardant les vides, et le champ demandé doit EXISTER — le même
# incident venait pour moitié d'avoir projeté
# `uniform_bucket_level_access.enabled` là où gcloud expose
# `uniform_bucket_level_access`, un booléen nu.


def colonnes(ligne: str) -> list:
    """Les colonnes d'une ligne `value(...)`, vides comprises."""
    return [c.strip() for c in ligne.replace(chr(13), "").split(chr(9))]


def colonne(champs: list, i: int) -> str:
    """La i-ème colonne, ou la chaîne vide. Jamais d'IndexError : une colonne
    manquante est un fait à rapporter, pas une exception à lever."""
    return champs[i] if i < len(champs) else ""


# ── Les verdicts, un par ressource ───────────────────────────────────────
#
# Chacun reçoit la sortie BRUTE et rend (état, détail). Ils sont PURS :
# aucun n'appelle `gcloud`, ce qui les rend testables sans identifiants et
# sans réseau. C'est là que vivent presque toutes les décisions.


def _v_app_engine(run: Run, ctx: dict) -> tuple:
    region = run.stdout.strip()
    if not region:
        return ABSENT, "aucune application App Engine"
    ctx["region_reelle"] = region
    if ctx.get("region_demandee") and region != ctx["region_demandee"]:
        return DRIFT, (
            "l'application est en « %s » alors que l'inspection demandait "
            "« %s ». La région est DÉFINITIVE : c'est celle de l'application "
            "qui fait autorité, et tout le reste doit s'y conformer."
            % (region, ctx["region_demandee"])
        )
    return PRESENT, "région " + region


def _v_firestore(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    location, type_ = colonne(champs, 0), colonne(champs, 1)
    if not location:
        return ABSENT, "base absente"
    if type_ and type_ != "FIRESTORE_NATIVE":
        return DRIFT, "mode « %s » — il faut FIRESTORE_NATIVE" % type_
    attendue = ctx.get("region_reelle") or ctx.get("region_demandee")
    if attendue and location.lower() != attendue.lower():
        return DRIFT, (
            "en « %s » alors que l'application App Engine est en « %s »"
            % (location, attendue)
        )
    return PRESENT, location + " / " + (type_ or "type inconnu")


def _v_index(run: Run, ctx: dict) -> tuple:
    etats = [l.strip() for l in run.stdout.splitlines() if l.strip()]
    prets = sum(1 for e in etats if e.upper() == "READY")
    attendu = ctx.get("index_attendus")
    if attendu is None:
        return INCONNU, (
            "firestore.indexes.json est illisible — impossible de dire "
            "combien d'index sont attendus"
        )
    if not etats:
        return ABSENT, "aucun index composite (%d attendus)" % attendu
    if prets < attendu:
        return DRIFT, (
            "%d index READY sur %d déclarés%s. Tant qu'un index construit, la "
            "requête qu'il sert se dégrade en LISTE VIDE, jamais en erreur."
            % (prets, attendu,
               " (%d en construction)" % (len(etats) - prets)
               if len(etats) > prets else "")
        )
    return PRESENT, "%d index READY" % prets


def _v_ttl(run: Run, ctx: dict) -> tuple:
    champs = [c for c in colonnes(run.stdout.strip()) if c]
    if not champs:
        return ABSENT, "aucune politique TTL sur oauth_codes.expire_at"
    if champs[-1].upper() != "ACTIVE":
        return DRIFT, "état « %s »" % champs[-1]
    return PRESENT, "ACTIVE"


def _v_existe(run: Run, ctx: dict) -> tuple:
    if not run.stdout.strip():
        return ABSENT, "introuvable"
    return PRESENT, run.stdout.strip().splitlines()[0][:120]


def _v_compteur(metric: str) -> Callable:
    """Le verdict d'UN compteur log-based : il existe, activé, ET il compte ce
    que dit son fichier, dans le journal du projet sondé.

    Exister ne suffit pas, et c'est une panne silencieuse : un compteur créé
    d'un fichier dont le `logName` nomme un autre projet (l'adoptant qui n'a
    pas remplacé le littéral, §4.3), ou resté en arrière d'un fichier
    modifié sans la mise à jour de §6.10 (étape 5), existe et compte autre
    chose — ou rien ; un compteur DÉSACTIVÉ (`disabled`, posé hors du dépôt :
    le fichier ne porte pas ce champ) ne produit aucun point : rien n'est
    compté, et aucune alerte ne peut s'y déclencher. Le filtre attendu vient
    du contexte (`probe_all`
    le lit du fichier, ``expected_metric_filter``) ; absent, rien n'est
    comparé et le verdict le dit, INCONNU — jamais PRESENT sur une
    comparaison qui n'a pas eu lieu. Une colonne VIDE ne se juge pas non
    plus : un filtre est obligatoire, donc une colonne de filtre vide est
    une projection ratée (INCONNU), et une colonne `disabled` vide, un champ
    non posé — activé. Une sortie entièrement vide se lit ABSENT, comme
    partout dans ce module (une commande qui réussit sans rien rendre dit
    l'absence) : à un compteur absent, `describe` répond NOT_FOUND — un
    échec, donc INCONNU par ``probe``.
    """
    fichier = metric_path(metric)

    def verdict(run: Run, ctx: dict) -> tuple:
        lignes = [l for l in run.stdout.splitlines() if l.strip()]
        if not lignes:
            return ABSENT, "introuvable"
        champs = colonnes(lignes[0])
        filtre, desactive = colonne(champs, 1), colonne(champs, 2)
        if desactive.lower() in ("true", "yes"):
            return DRIFT, (
                "le compteur est DÉSACTIVÉ : il ne produit aucun point — rien "
                "n'est compté, et aucune alerte ne peut s'y déclencher "
                "(§6.10, étape 5)"
            )
        if not filtre:
            return INCONNU, (
                "le filtre du compteur n'a pas été lu (colonne vide) : rien "
                "n'a été comparé"
            )
        attendu = (ctx.get("filtres_attendus") or {}).get(metric)
        if not attendu:
            return INCONNU, (
                "le compteur existe, mais le filtre attendu n'a pas pu être "
                "lu de %s : ce qu'il compte n'a pas été comparé" % fichier
            )
        if filtre != attendu:
            return DRIFT, (
                "le compteur existe, mais son filtre n'est pas celui de %s "
                "dans ce projet : il compte autre chose, ou rien" % fichier
            )
        return PRESENT, "activé, compte ce que dit " + fichier

    verdict.__name__ = "_v_compteur_" + metric
    return verdict


def _v_seau(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    location = colonne(champs, 0)
    ubla = colonne(champs, 1)
    public = colonne(champs, 2)
    if not location:
        return ABSENT, "seau absent"
    griefs = []
    attendue = ctx.get("region_reelle") or ctx.get("region_demandee")
    if attendue and location.lower() != attendue.lower():
        griefs.append(
            "en « %s » alors que l'application est en « %s » — chaque octet "
            "d'un versement paierait alors de l'egress inter-régional"
            % (location, attendue)
        )
    if ubla and ubla.lower() not in ("true", "yes"):
        griefs.append("accès uniforme DÉSACTIVÉ")
    if public and public.lower() != "enforced":
        griefs.append("prévention de l'accès public « %s »" % public)
    if griefs:
        return DRIFT, " ; ".join(griefs)
    return PRESENT, location


def _v_cycle_de_vie(run: Run, ctx: dict) -> tuple:
    try:
        charge = json.loads(run.stdout or "{}")
    except ValueError:
        return INCONNU, "sortie JSON illisible"
    regles = ((charge.get("lifecycle_config") or charge.get("lifecycle") or {})
              .get("rule") or [])
    vus = {}
    for r in regles:
        cond = r.get("condition") or {}
        age = cond.get("age")
        for prefixe in cond.get("matchesPrefix") or []:
            if (r.get("action") or {}).get("type") == "Delete":
                vus[prefixe] = age
    if not vus:
        return ABSENT, (
            "aucune règle de cycle de vie — le matériel privilégié d'un "
            "client resterait en quarantaine POUR TOUJOURS"
        )
    griefs = []
    for regle in QUARANTINE_LIFECYCLE:
        p, attendu = regle["prefix"], regle["age_days"]
        if p not in vus:
            griefs.append("« %s » n'a aucune règle" % p)
        elif vus[p] != attendu:
            griefs.append("« %s » à %s jours au lieu de %d"
                          % (p, vus[p], attendu))
    if griefs:
        return DRIFT, " ; ".join(griefs)
    return PRESENT, ", ".join("%s %s j" % (p, a) for p, a in sorted(vus.items()))


# Conditions qui limitent une règle Delete aux versions NON COURANTES d'un
# objet (versionnement) : une telle règle ne touche jamais un objet vivant.
_CONDITIONS_NON_COURANTES = ("daysSinceNoncurrentTime", "numNewerVersions",
                             "noncurrentTimeBefore")


def _v_cycle_de_vie_staging(run: Run, ctx: dict) -> tuple:
    """Le seau CANONIQUE porte les documents des clients : on y veut
    `staging/` balayé à 7 jours, et RIEN d'autre qui supprime un objet
    vivant — une règle sans préfixe, ou sur un autre préfixe, effacerait des
    documents. Une règle limitée aux versions non courantes (versionnement)
    ne touche aucun objet vivant et reste permise."""
    try:
        charge = json.loads(run.stdout or "{}")
    except ValueError:
        return INCONNU, "sortie JSON illisible"
    regles = ((charge.get("lifecycle_config") or charge.get("lifecycle") or {})
              .get("rule") or [])
    ages = {}
    dangereuses = []
    for r in regles:
        if (r.get("action") or {}).get("type") != "Delete":
            continue
        cond = r.get("condition") or {}
        if cond.get("isLive") is False or any(
                k in cond for k in _CONDITIONS_NON_COURANTES):
            continue
        prefixes = cond.get("matchesPrefix") or []
        if not prefixes:
            dangereuses.append("(sans préfixe)")
        for prefixe in prefixes:
            if prefixe.startswith("staging/"):
                ages[prefixe] = cond.get("age")
            else:
                dangereuses.append("« %s »" % prefixe)
    if dangereuses:
        return DRIFT, (
            "une règle de suppression vise des objets VIVANTS hors staging/ "
            "%s — sur ce seau, ce sont les documents des clients"
            % ", ".join(dangereuses)
        )
    griefs = []
    for regle in CANONICAL_STAGING_LIFECYCLE:
        p, attendu = regle["prefix"], regle["age_days"]
        if p not in ages:
            return ABSENT, (
                "aucune règle sur « %s » — un envoi jamais finalisé y reste "
                "POUR TOUJOURS" % p
            )
        if ages[p] != attendu:
            griefs.append("« %s » à %s jours au lieu de %d"
                          % (p, ages[p], attendu))
    if griefs:
        return DRIFT, " ; ".join(griefs)
    return PRESENT, ", ".join("%s %s j" % (p, a) for p, a in sorted(ages.items()))


def _v_file(run: Run, ctx: dict) -> tuple:
    champs = colonnes(run.stdout)
    etat = colonne(champs, 0)
    if not etat:
        return ABSENT, "file absente"
    if etat.upper() == "PAUSED":
        return DRIFT, (
            "la file est EN PAUSE — les tâches s'accumulent sans être "
            "livrées, et le cron de réconciliation les réenfile toutes les "
            "quinze minutes sans jamais les faire passer"
        )
    if etat.upper() != "RUNNING":
        return DRIFT, "état « %s »" % etat
    return PRESENT, "RUNNING " + " ".join(c for c in champs[1:] if c)


def _v_pare_feu(run: Run, ctx: dict) -> tuple:
    """L'ORDRE est le fond du sujet, pas la présence.

    Une règle ALLOW sur l'adresse interne qui se trouve SOUS le refus par
    défaut ne sert à rien ; et sans elle, les trois tâches cron et toute la
    file sont coupées à la couche 1, en silence.
    """
    lignes = [colonnes(l) for l in run.stdout.splitlines() if l.strip()]
    if not lignes:
        return ABSENT, "aucune règle de pare-feu"
    interne = None
    refus_par_defaut = None
    for champs in lignes:
        if len(champs) < 3:
            continue
        try:
            priorite = int(champs[0])
        except ValueError:
            continue
        action, plage = champs[1].upper(), champs[2]
        if plage == APPENGINE_INTERNAL_CIDR and action == "ALLOW":
            interne = priorite
        if plage == "*" and action == "DENY":
            refus_par_defaut = priorite
    if interne is None:
        return DRIFT, (
            "%s n'est pas autorisé — les trois tâches cron et la file Cloud "
            "Tasks sont coupées à la couche 1, EN SILENCE. App Engine les "
            "expédie depuis cette adresse interne, qui n'est pas une plage "
            "Cloudflare." % APPENGINE_INTERNAL_CIDR
        )
    if refus_par_defaut is not None and interne >= refus_par_defaut:
        return DRIFT, (
            "%s est autorisé en priorité %d, SOUS le refus par défaut (%d) — "
            "donc jamais atteint"
            % (APPENGINE_INTERNAL_CIDR, interne, refus_par_defaut)
        )
    if refus_par_defaut is None:
        return DRIFT, (
            "aucun refus par défaut : l'origine est joignable directement, "
            "et le secret d'origine devient la seule couche restante"
        )
    return PRESENT, "%s en priorité %d, %d règles en tout" % (
        APPENGINE_INTERNAL_CIDR, interne, len(lignes))


def _v_alerte(run: Run, ctx: dict) -> tuple:
    """UNE politique, activée, qui écrit à quelqu'un.

    Trois défauts se lisent ici, et aucun ne se voit tant que rien ne se
    déclenche : une politique créée deux fois (`policies create` ne
    dédoublonne pas) écrit deux courriels par déclenchement ; une politique
    désactivée, ou sans canal, se déclencherait sans prévenir personne — le
    silence même que ces alertes existent pour rompre. Une colonne `enabled`
    VIDE ne se juge pas : un contrôle qui accuse à tort est pire qu'aucun.
    """
    lignes = [colonnes(l) for l in run.stdout.splitlines() if l.strip()]
    if not lignes:
        return ABSENT, "aucune politique ne surveille ce compteur"
    if len(lignes) > 1:
        return DRIFT, (
            "%d politiques surveillent le même compteur — chaque "
            "déclenchement écrirait %d fois" % (len(lignes), len(lignes))
        )
    activee, canaux = colonne(lignes[0], 1), colonne(lignes[0], 2)
    if activee.lower() in ("false", "no"):
        return DRIFT, "la politique est DÉSACTIVÉE : elle ne se déclencherait pas"
    nombre = len([c for c in canaux.split(";") if c.strip()])
    if not nombre:
        return DRIFT, (
            "aucun canal de notification : elle se déclencherait sans "
            "prévenir personne"
        )
    return PRESENT, "activée, %d canal(aux) de notification" % nombre


_VERDICTS: dict = {
    "app-engine": _v_app_engine,
    "firestore-defaut": _v_firestore,
    "firestore-portail": _v_firestore,
    "index-composites": _v_index,
    "ttl-firestore": _v_ttl,
    "sa-portail": _v_existe,
    "seau-quarantaine": _v_seau,
    "cycle-de-vie-quarantaine": _v_cycle_de_vie,
    "cycle-de-vie-staging": _v_cycle_de_vie_staging,
    "file-portail": _v_file,
    "pare-feu-interne": _v_pare_feu,
    # La surveillance de sécurité (§6.10) : un compteur se juge sur ce qu'il
    # compte (son filtre contre son fichier) ; une politique, sur ce qu'elle
    # ferait d'un déclenchement.
    **{m.metric_key: _v_compteur(m.metric) for m in SECURITY_METRICS},
    **{m.policy_key: _v_alerte for m in SECURITY_METRICS if m.policy},
}


def probe(resource: Resource, project: str, region: str, *,
          runner: Optional[Callable] = None,
          ctx: Optional[dict] = None) -> Result:
    """Constater UNE ressource. Ne mute jamais rien."""
    if resource.manual:
        return Result(resource.key, MANUEL, resource.expect)
    verdict = _VERDICTS.get(resource.key)
    if verdict is None:
        return Result(resource.key, INCONNU,
                      "aucun verdict n'est écrit pour cette ressource")
    run = (runner or _run)(resource.argv(project, region))
    if not run.ok:
        # ⚠ JAMAIS `ABSENT`. Une sonde qui échoue ne prouve rien sur la
        # ressource, et rapporter une absence enverrait l'exploitant
        # provisionner ce qui existe peut-être déjà.
        return Result(resource.key, INCONNU,
                      _motif_de_sonde(run.stderr), run.stderr[:200])
    etat, detail = verdict(run, ctx if ctx is not None else {})
    return Result(resource.key, etat, detail, run.stdout[:200])


def _motif_de_sonde(stderr: str) -> str:
    bas = (stderr or "").lower()
    if "introuvable" in bas or "refusé" in bas or "gcloud_bin" in bas:
        return stderr or "gcloud est introuvable — rien n'a pu être constaté"
    if "délai dépassé" in bas or "timeout" in bas:
        return "la commande a dépassé son délai — rien n'a pu être constaté"
    if "credential" in bas or "login" in bas or "unauthenticated" in bas:
        return ("identifiants absents ou périmés (`gcloud auth login`) — "
                "rien n'a pu être constaté")
    if "permission" in bas or "denied" in bas or "403" in bas:
        return ("permission refusée — la ressource existe peut-être ; "
                "l'inspection, elle, n'a pas eu lieu")
    # Le message de gcloud NOMME la ressource, et un nom peut porter
    # n'importe quel mot : jusqu'au 2026-11-08, un `disabled` nu lisait le
    # NOT_FOUND du compteur `security_control_disabled` comme « l'API est
    # désactivée », et envoyait activer une API déjà active. Des tournures
    # de Google, donc, jamais un mot isolé — et NOT_FOUND reste INCONNU : une
    # commande qui échoue ne prouve aucune absence (un projet mal tapé répond
    # pareil).
    if "not_found" in bas or "does not exist" in bas:
        return ("gcloud répond NOT_FOUND : la ressource est absente de ce "
                "projet, ou le projet est le mauvais — rien n'a pu être "
                "constaté")
    if ("has not been used" in bas or "service_disabled" in bas
            or "it is disabled" in bas or "not enabled on project" in bas):
        return "l'API nécessaire est désactivée — rien n'a pu être constaté"
    return "la commande a échoué — rien n'a pu être constaté"


def probe_all(project: str, region: str = "", *,
              runner: Optional[Callable] = None,
              repo_root: str = "") -> list:
    """Toutes les ressources, dans l'ordre des phases.

    Le contexte se remplit CHEMIN FAISANT : la région réelle est lue de
    l'application App Engine, puis c'est ELLE qui sert de référence aux bases,
    au seau et à la file — jamais celle passée en argument. La région d'une
    application est définitive ; c'est donc elle qui fait autorité, et pas ce
    que l'exploitant a tapé.
    """
    ctx = {
        "region_demandee": region,
        "index_attendus": expected_index_count(repo_root),
        "filtres_attendus": {
            m.metric: expected_metric_filter(m.metric, project, repo_root)
            for m in SECURITY_METRICS
        },
    }
    resultats = []
    for r in provisioning_plan():
        res = probe(r, project, region or ctx.get("region_reelle", ""),
                    runner=runner, ctx=ctx)
        resultats.append(res)
    return resultats


# ── Le rendu, dans la grammaire de rapport existante ─────────────────────

_NIVEAU = {
    PRESENT: OK,
    # Une ressource pas encore posée n'est pas une PANNE : c'est du travail
    # qui reste. Le code de sortie 2 la distingue d'un échec.
    ABSENT: WARN,
    # Elle existe et contredit ce que le code attend — ça, c'est un défaut.
    DRIFT: FAIL,
    INCONNU: WARN,
    MANUEL: WARN,
}


def check_resources(rpt: Report, project: str, region: str = "", *,
                    runner: Optional[Callable] = None,
                    repo_root: str = "") -> list:
    """Émettre les constats dans le rapport partagé."""
    rpt.section(SECTION_RESOURCES)
    resultats = probe_all(project, region, runner=runner, repo_root=repo_root)
    par_cle = {r.key: r for r in RESOURCES}
    for res in resultats:
        ressource = par_cle[res.key]
        if res.state == MANUEL:
            rpt.emit(WARN, "%s : à vérifier à la main — %s"
                     % (ressource.label, ressource.expect),
                     resource=res.key, state=res.state, phase=ressource.phase)
            continue
        message = "%s : %s" % (ressource.label, res.detail or res.state)
        if res.state in (ABSENT, DRIFT):
            message += " — " + ressource.symptom
        rpt.emit(_NIVEAU[res.state], message,
                 resource=res.key, state=res.state, phase=ressource.phase)
    return resultats


def render_plan(resultats: list) -> str:
    """Ce qui RESTE à faire, dans l'ordre — c'est le livrable.

    Cinq des huit défaillances connues d'un clone neuf sont des défaillances
    d'ORDRE, pas de commande ; c'est pourquoi le plan groupe par phase au lieu
    de rendre une liste à plat.
    """
    par_cle = {r.key: r for r in RESOURCES}
    restants = [r for r in resultats
                if r.state in (ABSENT, DRIFT, INCONNU, MANUEL)]
    if not restants:
        return "Plan : rien à faire — chaque ressource constatable est en place."
    lignes = ["Plan de provisionnement — ce qui reste, dans l'ordre", ""]
    # La colonne des identifiants se règle sur le plus long : ceux de la
    # surveillance (« metrique-requete-trop-volumineuse ») dépassent 26.
    largeur = max([26] + [len(r.key) for r in restants])
    for phase in PHASE_ORDER:
        du_lot = [r for r in restants if par_cle[r.key].phase == phase]
        if not du_lot:
            continue
        lignes.append("  [%s]" % phase)
        for res in du_lot:
            ressource = par_cle[res.key]
            marque = {
                ABSENT: "à poser  ", DRIFT: "À CORRIGER", INCONNU: "?        ",
                MANUEL: "à la main",
            }[res.state]
            irr = "  ⚠ DÉFINITIF" if ressource.irreversible else ""
            lignes.append("    %s  %-*s %s%s"
                          % (marque, largeur, ressource.key, ressource.label,
                             irr))
            if res.detail:
                lignes.append("                 %s" % res.detail)
        lignes.append("")
    return "\n".join(lignes).rstrip()


def exit_code(resultats: list) -> int:
    """0 tout est en place · 1 une dérive · 2 du travail reste.

    Le 2 distingué compte : «  va cliquer dans une console » n'est pas « quelque
    chose est cassé », et un CI qui les confond finit par ignorer les deux.
    """
    etats = {r.state for r in resultats}
    if DRIFT in etats:
        return 1
    if ABSENT in etats or INCONNU in etats or MANUEL in etats:
        return 2
    return 0


__all__ = [
    "ABSENT",
    "DRIFT",
    "INCONNU",
    "MANUEL",
    "PRESENT",
    "Result",
    "Run",
    "SECTION_RESOURCES",
    "check_resources",
    "colonne",
    "colonnes",
    "exit_code",
    "expected_index_count",
    "expected_metric_filter",
    "gcloud_path",
    "probe",
    "probe_all",
    "render_plan",
]
