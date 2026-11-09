"""Deployment inventory — the ONE table of what an instance needs.

PURE DATA AND PURE PREDICATES. Stdlib imports only: no ``google.*``, no
``subprocess``, no file I/O, no Flask. ``tests/test_deployment_inventory.py``
asserts that on the module's AST, and that property is what makes this module
safe to import from a Flask route, from a CLI script, and from a test with no
credentials — the ``mcp/coverage.py`` precedent.

WHY IT EXISTS. The same facts were kept by hand in several places and drifted,
which the old CLAUDE.md predicted in writing before it happened (`git show
abc63b4:CLAUDE.md`): it named three hand-maintained inventories that do not
update themselves, one of them being
``scripts/check_config.py``'s ``_SCAN_FILES``. That list then omitted
``portail.yaml`` and ``client/config.py``, both of which carry owner literals —
not an oversight in kind, but the expected behaviour of a hand-kept list. And
``DEPLOYMENT.md`` §4.2 said "the four Secret Manager secrets" from the day
``portail-secret-key`` shipped as a fifth and ``graph-client-secret`` as a
sixth.

So: one table here, and every reader imports it — the CLI checker, the
« Paramètres → Configuration » page, and the provisioning script.
``utils/template_fields.py``'s ``CATALOG`` is the existing precedent (pure data
in ``utils/``, consumed by routes AND pinned against a markdown doc by a test).

Paths are stored RELATIVE to the repo root, as plain strings. The caller joins
them, which is what keeps this module free of ``os`` and therefore trivially
pure.
"""

import re
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

# ── Services ─────────────────────────────────────────────────────────────
# Two App Engine services share this project. A value required by one is not
# necessarily required by the other, and GRAPH_* must NEVER reach the portal
# (spec L1 §8.1) — which is exactly the kind of fact a table can carry and a
# prose paragraph cannot.

SERVICE_DEFAULT = "default"
SERVICE_PORTAIL = "portail"
SERVICES: tuple[str, ...] = (SERVICE_DEFAULT, SERVICE_PORTAIL)


# ── Shape predicates ─────────────────────────────────────────────────────
# Each takes the DECODED value and returns a ShapeVerdict, or None when the
# shape is acceptable. Pure; no I/O; never logs; never returns the value —
# only COUNTS and categories computed from it.

SHAPE_FAIL = "fail"
SHAPE_WARN = "warn"


class ShapeVerdict(NamedTuple):
    """Une objection sur la forme, et SA GRAVITÉ.

    Le classement en deux crans porte le lot plutôt qu'il ne le décore : un
    contrôle qui fait rougir un déploiement qui FONCTIONNE est un contrôle
    qu'on cesse de lire, et un contrôle qu'on ne lit plus est exactement
    l'état dans lequel `cf-origin-secret` a passé des mois. FAIL est donc
    réservé à ce qui est cassé AUJOURD'HUI — la requête échoue, à chaque
    fois — et tout ce qui n'est qu'inhabituel répond WARN.
    """

    severity: str
    message: str


@dataclass(frozen=True)
class Shape:
    """Un prédicat de forme QUI PORTE SES BORNES.

    Les deux nombres ne sont pas décoratifs : la recette `gcloud` rendue à
    l'écran et la §6.4 de `DEPLOYMENT.md` disent toutes deux « le compte doit
    valoir tant », et ce compte doit être DÉRIVÉ du contrôle qui le vérifie.
    Une longueur recopiée à la main dans un mode d'emploi est exactement la
    dérive que cette table existe pour supprimer — c'est ce qui est arrivé à
    « les quatre secrets » de §4.2.

    Appelable, donc `secret.shape(valeur)` ne change de forme pour aucun
    appelant.
    """

    minimum: int
    maximum: int
    check: Callable[[str], Optional[ShapeVerdict]]

    def __call__(self, value: str) -> Optional[ShapeVerdict]:
        return self.check(value)


# \Z, never $ — Python's `$` ALSO matches just before a trailing newline,
# so `^…$` would accept the 61-character value this predicate exists to
# refuse. That is the same defect `normalize_email` was documented for, and
# the explicit length check below is the belt to this braces.
#
# `[0-9][0-9]`, never `\d\d` — Python's `\d` is UNICODE-aware, so
# `"$2b$" + "١٢" + "$" + "a"*53` is 60 characters long and MATCHED (measured
# 2026-09-11). It then fails `bcrypt.checkpw`, which `dav/dav_auth.py`
# swallows in a bare `except` — which is to say it walked straight through
# the predicate and into the silent-DavX5 failure the predicate exists to
# eliminate.
_BCRYPT_RE = re.compile(r"^\$2[aby]\$[0-9][0-9]\$[./A-Za-z0-9]{53}\Z")
_BCRYPT_LENGTH = 60


def _shape_bcrypt(value: str) -> Optional[ShapeVerdict]:
    """A bcrypt hash, exactly 60 characters.

    The single highest-value validation in the inventory. ``bcrypt.checkpw``
    recomputes 60 bytes; a 61-byte value (one stray newline) can never match,
    and DAV Basic Auth then fails with **no error anywhere** — not on the
    phone, not in the logs. It is the quietest failure mode in the
    application, and a shape check removes it by construction.
    """
    if len(value) != _BCRYPT_LENGTH or not _BCRYPT_RE.match(value):
        return ShapeVerdict(
            SHAPE_FAIL,
            f"Ce champ attend une EMPREINTE bcrypt de {_BCRYPT_LENGTH} "
            f"caractères (« $2b$… »), jamais un mot de passe en clair "
            f"(reçu : {len(value)} caractères).",
        )
    return None


# Ce dans quoi `secrets.token_urlsafe` puise, plus les deux marques non
# réservées restantes de la RFC 3986. Chaque secret de cette table est
# ENGENDRÉ — un jeton, une empreinte —, donc ce jeu est ce dont une valeur
# correcte est FAITE, et non une préférence de style.
_TOKEN_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789_.~-"
)


def _shape_ascii_token(minimum: int, maximum: int) -> Shape:
    """La longueur, ET le jeu de caractères que le nom promettait.

    `_shape_urlsafe` était une borne de LONGUEUR portant un nom de jeu de
    caractères, et l'écart n'était pas cosmétique.
    `security._enforce_origin_secret` appelle
    `hmac.compare_digest(supplied, secret)` sur **chaque** requête, avec deux
    `str` — et cela LÈVE `TypeError` sur une chaîne non ASCII (mesuré). Une
    seule lettre accentuée, une apostrophe typographique collée depuis Word
    ou une espace insécable dans `cf-origin-secret` répond donc **500 à
    chaque requête**, avec une trace par requête : strictement pire que le
    403 du saut de ligne déjà documenté — et l'ancien prédicat appelait cela
    « ok ».

    Un caractère de contrôle est la même espèce, d'un cran en dessous : un
    en-tête HTTP ne peut pas le transporter, donc la valeur reçue ne peut
    jamais égaler la valeur stockée et chaque requête répond 403.

    Tout le reste hors de `_TOKEN_SAFE` — une espace, « + », « / », « = »,
    une ponctuation — est ASCII et transmissible : ce peut très bien être un
    déploiement qui FONCTIONNE, dont le générateur a simplement produit du
    base64 standard. C'est donc un avertissement, jamais un échec.

    Aucun message ne rend la valeur : seulement des DÉCOMPTES et des
    catégories calculés sur elle.
    """

    def check(value: str) -> Optional[ShapeVerdict]:
        if not (minimum <= len(value) <= maximum):
            return ShapeVerdict(
                SHAPE_FAIL,
                f"Longueur inattendue : {len(value)} caractères "
                f"(attendu entre {minimum} et {maximum}).",
            )
        hors_ascii = sum(1 for c in value if ord(c) > 127)
        if hors_ascii:
            return ShapeVerdict(
                SHAPE_FAIL,
                f"{hors_ascii} caractère(s) NON ASCII — lettre accentuée, "
                "apostrophe typographique, espace insécable : la signature "
                "d'un copier-coller. `hmac.compare_digest` LÈVE sur une "
                "chaîne non ASCII, donc pour le secret d'origine c'est une "
                "erreur 500 à chaque requête, et non un 403.",
            )
        controles = sum(1 for c in value if ord(c) < 32 or ord(c) == 127)
        if controles:
            return ShapeVerdict(
                SHAPE_FAIL,
                f"{controles} caractère(s) de contrôle. Un en-tête HTTP ne "
                "peut pas en transporter : la valeur reçue ne pourra jamais "
                "égaler celle qui est stockée.",
            )
        espaces = sum(1 for c in value if c == " ")
        autres = sum(1 for c in value if c != " " and c not in _TOKEN_SAFE)
        if espaces or autres:
            parts = []
            if espaces:
                parts.append(f"{espaces} espace(s) à l'intérieur")
            if autres:
                parts.append(f"{autres} caractère(s) hors du jeu habituel")
            return ShapeVerdict(
                SHAPE_WARN,
                f"Inhabituel : {' et '.join(parts)}. Ce n'est pas forcément "
                "une panne — un générateur en base64 standard produit « + », "
                "« / » et « = » —, mais une espace est invisible dans une "
                "console et ne s'est probablement pas retrouvée là exprès.",
            )
        return None

    return Shape(minimum, maximum, check)


def stray_whitespace(value: str) -> Optional[str]:
    """Name the stray bytes, or None. Applies to EVERY secret.

    Nothing in ``config.py`` strips a secret, so surrounding whitespace
    becomes part of the value. For ``cf-origin-secret`` that is site-wide
    downtime: it is compared byte for byte with ``hmac.compare_digest``
    against a header a Cloudflare Transform Rule injects, and **a Transform
    Rule cannot emit a newline** — so every request answers 403 and the edge
    cannot be corrected to compensate.

    Reports the COUNT and the side, never the value.
    """
    if not value or value == value.strip():
        return None
    lead = len(value) - len(value.lstrip())
    trail = len(value) - len(value.rstrip())
    parts = []
    if lead:
        parts.append(f"{lead} au début")
    if trail:
        parts.append(f"{trail} à la fin")
    return (
        f"Caractères parasites : {' et '.join(parts)}. Un secret est comparé "
        "octet pour octet — ils en feraient partie."
    )


# ── Secrets — SIX, not four ──────────────────────────────────────────────

# D'OÙ vient la valeur. Ce n'est pas une étiquette : c'est ce qui DÉRIVE la
# première étape de la recette `gcloud`, et donc ce qui empêche une recette
# écrite à la main de conseiller un jour de « générer » une clé que la console
# Firebase est seule à pouvoir émettre.
ORIGIN_GENERATED = "genere"    # vous la frappez — `secrets.token_urlsafe`
ORIGIN_EXTERNAL = "console"    # Firebase / Entra vous la remettent
ORIGIN_PASSWORD = "empreinte"  # calculée d'un mot de passe que vous choisissez


@dataclass(frozen=True)
class Secret:
    """One Secret Manager secret.

    ``writable`` says whether « Paramètres → Déploiement » may add a version.
    It is enforced twice on purpose: here, and by binding
    ``roles/secretmanager.secretVersionAdder`` per secret rather than at
    project level — so a refusal survives a refactor of this table.
    """

    secret_id: str
    env_var: str
    label: str
    required_for: frozenset
    consequence: str
    writable: bool
    origin: str
    refusal: str = ""
    shape: Optional[Shape] = None

    @property
    def required(self) -> bool:
        return bool(self.required_for)


SECRETS: tuple[Secret, ...] = (
    Secret(
        secret_id="flask-secret-key",
        env_var="SECRET_KEY",
        label="Clé de signature des sessions Flask",
        required_for=frozenset({SERVICE_DEFAULT}),
        consequence="l'application NE DÉMARRE PAS",
        writable=False,
        origin=ORIGIN_GENERATED,
        refusal=(
            "Écriture refusée par conception. La faire tourner invalide TOUTES "
            "les sessions, dont la vôtre — vous seriez déconnecté par l'acte "
            "même et ne pourriez pas lire le résultat de la vérification. "
            "C'est aussi le seul secret dur : une mauvaise valeur empêche le "
            "démarrage, et on ne corrige pas depuis une application qui ne "
            "démarre pas. Et c'est le seul dont l'écriture permettrait de "
            "FORGER des sessions."
        ),
        shape=_shape_ascii_token(32, 256),
    ),
    Secret(
        secret_id="portail-secret-key",
        env_var="PORTAIL_SECRET_KEY",
        label="Clé de session du service « portail »",
        required_for=frozenset({SERVICE_PORTAIL}),
        consequence="le service « portail » NE DÉMARRE PAS",
        writable=False,
        origin=ORIGIN_GENERATED,
        refusal=(
            "Écriture refusée : ce secret appartient à un AUTRE service, dont "
            "le compte de service est distinct. Une mauvaise valeur ferait "
            "répondre 500 au portail — la seule surface où il y a des tiers. "
            "Et la séparation des deux clés EST la frontière de session entre "
            "les services : une page capable d'écrire les deux moitiés "
            "affaiblirait l'argument qui la justifie."
        ),
        shape=_shape_ascii_token(32, 256),
    ),
    Secret(
        secret_id="firebase-api-key",
        env_var="FIREBASE_API_KEY",
        label="Clé d'API navigateur Firebase",
        required_for=frozenset(),
        consequence="la page de connexion ne peut pas initialiser Firebase",
        writable=True,
        origin=ORIGIN_EXTERNAL,
        shape=_shape_ascii_token(30, 60),
    ),
    Secret(
        secret_id="dav-password-hash",
        env_var="DAV_PASSWORD_HASH",
        label="Empreinte bcrypt du mot de passe DAV",
        required_for=frozenset(),
        consequence=(
            "l'authentification DAV ne peut pas réussir — DavX5 cesse de "
            "synchroniser SANS message d'erreur"
        ),
        writable=True,
        origin=ORIGIN_PASSWORD,
        shape=Shape(_BCRYPT_LENGTH, _BCRYPT_LENGTH, _shape_bcrypt),
    ),
    Secret(
        secret_id="cf-origin-secret",
        env_var="CF_ORIGIN_SECRET",
        label="Secret d'origine Cloudflare",
        required_for=frozenset(),
        consequence=(
            "absent, vide ou illisible, le secret DÉSACTIVE le contrôle "
            "d'origine — l'accès direct à App Engine n'est plus bloqué, et "
            "origin_secret_disabled le signale par l'alerte « Sécurité — une "
            "protection est désactivée » ; présent mais différent de la "
            "Transform Rule, il fait refuser toute requête venue de "
            "Cloudflare (403)"
        ),
        writable=True,
        origin=ORIGIN_GENERATED,
        shape=_shape_ascii_token(32, 128),
    ),
    Secret(
        secret_id="graph-client-secret",
        env_var="GRAPH_CLIENT_SECRET",
        label="Secret client Microsoft Graph",
        required_for=frozenset(),
        consequence="le courriel sortant est désactivé",
        writable=True,
        origin=ORIGIN_EXTERNAL,
        shape=_shape_ascii_token(20, 256),
    ),
)

SECRET_IDS: tuple[str, ...] = tuple(s.secret_id for s in SECRETS)
WRITABLE_SECRET_IDS: tuple[str, ...] = tuple(
    s.secret_id for s in SECRETS if s.writable
)


def secret_by_id(secret_id: str) -> Optional[Secret]:
    for s in SECRETS:
        if s.secret_id == secret_id:
            return s
    return None


# ── La recette `gcloud`, ENGENDRÉE ───────────────────────────────────────
#
# Pourquoi engendrée plutôt qu'écrite : §6.4 de `DEPLOYMENT.md` portait trois
# défauts réels, et les trois sont de la famille « recopié à la main ». Elle
# faisait `hashpw(b'YOUR_DAV_PASSWORD', …)`, ce qui dépose le mot de passe DAV
# EN CLAIR dans `~/.bash_history`. Elle disait `secrets create` sur chaque
# ligne — juste au premier déploiement, faux pour toute rotation depuis. Et
# elle finissait par `| tr -d '\n'` pour éponger un saut de ligne que
# `sys.stdout.write` n'émet jamais, ce qui enseigne une superstition au lieu
# d'une règle.
#
# Un test épingle `DEPLOYMENT.md` §6.4 contre la sortie de cette fonction :
# c'est ce qui rend la page structurellement meilleure que le document, et
# non une seconde copie de lui.
#
# ⚠ BASH SEULEMENT, et ce n'est pas une préférence — c'est MESURÉ
# (2026-09-11, sur cette machine).
#
# **Git Bash convient.** `printf '%s' "$V" | <exe natif>` livre exactement les
# octets de `$V` : 3 pour « abc », 43 pour un jeton de 43 caractères. Le
# contrôle négatif fait le travail — `echo`, un heredoc et une chaîne ici
# livrent 4 octets, le supplément étant un `\n` NU, donc le tuyau MSYS→natif
# ne traduit pas non plus LF↔CRLF. Vérifié sur les 256 valeurs d'octet, à
# 200 Ko, et jusque dans `files.ReadStdinBytes()` de gcloud lui-même. Le
# relais `~/bin/gcloud` est un script bash qui `exec` le lanceur POSIX du SDK,
# lequel `exec` un `python.exe` natif : `gcloud.cmd` n'est jamais sur ce
# chemin, donc `cmd.exe` n'entre pas dans la chaîne.
#
# **PowerShell ne convient pas, et c'est pire que ce que ce commentaire
# disait.** « abc » arrive en 8 octets à un exécutable NATIF :
# `ef bb bf 61 62 63 0d 0a`. Mais sur Windows `gcloud` est lui-même un `.ps1`
# qui RE-TUYAUTE (`gcloud.ps1:118` — `$input | & "$exe_path"`), donc la charge
# subit DEUX passages et se fait terminer deux fois. Mesuré au bout, à travers
# le vrai `gcloud`, dans un vrai Secret Manager, le 2026-09-11 : dix
# caractères ASCII y sont devenus **16 octets**,
# `efbbbf 6162636465666768696a 0d 0d 0a` — la nomenclature, la charge, puis le
# CR resté du premier passage suivi du CRLF du second. Trois mécanismes, pas
# un :
#   1. un CRLF ajouté par OBJET du pipeline — inconditionnel, dans TOUTES les
#      formes essayées, y compris natif→natif et y compris quand le programme
#      amont n'émet rien ;
#   2. `$OutputEncoding` qui RÉENCODE la charge — et Windows PowerShell 5.1 le
#      règle par défaut sur **ASCII** (page de code 20127), donc une lettre
#      accentuée devient « ? » : une corruption SILENCIEUSE du contenu, pas
#      seulement un octet de queue ;
#   3. une NOMENCLATURE d'octets (BOM) de 3 octets en TÊTE, qui vient de
#      `[Console]::InputEncoding` — et non de `$OutputEncoding`, ce que ce
#      commentaire affirmait à tort.
# PowerShell 7 n'est pas installé ici : ne rien affirmer à son sujet.
#
# Conséquence sur le CONTRÔLE, et c'est le défaut que la mesure a trouvé : une
# nomenclature est en TÊTE, donc un `xxd | tail -1` ne peut pas la voir. Seul
# le COMPTE D'OCTETS voit les deux bouts (3 devant, 2 derrière). C'est
# pourquoi la recette compte plutôt que de regarder la fin.
#
# Google Cloud Shell reste une bonne seconde voie — bash, userland Linux
# propre, aucune trace sur le disque local, joignable depuis un téléphone.
# Ce qu'elle n'apporte PAS : une meilleure attribution au journal d'audit.
# Ce commentaire l'a prétendu ; c'est faux. La version 1 de `cf-origin-secret`
# a été écrite DEPUIS CETTE MACHINE par le gcloud local le 2026-08-11, et le
# journal d'audit l'attribue à la personne. Sur 400 jours, 26 écritures de
# secret sur 26 sont attribuées à l'humain et zéro au compte de service
# d'App Engine.


def expected_length_fr(secret: "Secret") -> str:
    """« exactement 60 » ou « entre 32 et 128 » — DÉRIVÉ, jamais tapé."""
    if secret.shape is None:
        return ""
    lo, hi = secret.shape.minimum, secret.shape.maximum
    return f"exactement {lo}" if lo == hi else f"entre {lo} et {hi}"


# La sonde d'interpréteur, et pourquoi elle ESSAIE au lieu de regarder.
# `command -v python3` ne suffit pas : sur Windows, `python3` est présent sur
# le PATH sous forme de raccourci d'exécution du Microsoft Store, qui sort en
# code 49 sans rien produire. Mesuré au pire endroit le 2026-09-11, lors d'un
# essai réel contre un secret jetable — la valeur engendrée était VIDE, et
# seule la comparaison de longueur a empêché d'écrire un `cf-origin-secret`
# vide, c'est-à-dire de DÉSACTIVER en silence tout le contrôle d'origine
# (`if not secret: return None`). Une sonde d'EXISTENCE aurait choisi le
# raccourci cassé ; une sonde d'EXÉCUTION choisit ce qui marche.
#
# `py` ferme la liste : le lanceur Windows répond là où les deux autres noms
# échouent. Sur cette machine, `python` tombe sur le venv du dépôt — où
# bcrypt est déjà installé.
_SONDE_PY = r"""PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi"""

_SONDE_FR = (
    "Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur "
    "Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur "
    "le PATH et sort en code 49 sans rien produire, si bien qu'un "
    "`command -v` le choisirait et que la valeur engendrée serait vide."
)


def _bornes(secret: "Secret") -> str:
    """Le test shell des bornes, DÉRIVÉ de `Shape`.

    Les bornes viennent du prédicat qui juge la valeur stockée, jamais d'un
    nombre recopié : l'avant-vol et l'après-vol ne peuvent donc pas diverger.
    """
    lo, hi = secret.shape.minimum, secret.shape.maximum
    if lo == hi:
        return f'[ "$N" -eq {lo} ]'
    return f'[ "$N" -ge {lo} ] && [ "$N" -le {hi} ]'


def _ecriture_gardee(secret, secret_id: str, projet: str, var: str) -> str:
    """L'écriture SOUS la garde, et la version capturée.

    Jusqu'au 2026-09-11 le contrôle affichait « REFUSER, ne rien écrire » et
    la commande d'écriture vivait EN DEHORS du `if` : coller le bloc écrivait
    quoi que dise le verdict. Le shell comparait ; il n'agissait pas. Une
    consigne de sécurité qui demande à un humain d'obéir à un mot imprimé,
    au moment précis où il tient un secret, n'est pas une garde.

    La version écrite est CAPTURÉE (`--format='value(name)'`) parce que la
    relecture doit viser celle-là et non `latest` — voir `_relecture_gardee`.
    """
    return (
        f"N=$(printf '%s' \"${var}\" | wc -c | tr -d ' ')\n"
        f"if {_bornes(secret)}; then\n"
        f"  VERSION=$(printf '%s' \"${var}\" | gcloud secrets versions add "
        f"{secret_id} \\\n"
        f"    --project={projet} --data-file=- --format='value(name)')\n"
        "  printf 'écrit : %s octets, version %s\\n' \"$N\" "
        '\"${VERSION##*/}\"\n'
        "else\n"
        '  VERSION=""\n'
        "  printf \"longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été "
        'écrit\\n\" \"$N\"\n'
        "fi"
    )


def _relecture_gardee(secret, secret_id: str, projet: str) -> str:
    """Relire LA VERSION QU'ON VIENT D'ÉCRIRE, jamais `latest`.

    `latest` est un piège de fausse assurance : si l'écriture échoue, il
    désigne la version PRÉCÉDENTE — qui a toutes les chances d'avoir la bonne
    longueur, puisqu'elle fonctionnait. Le contrôle d'après-vol répondait donc
    « conforme » sur une écriture qui n'avait pas eu lieu.
    """
    return (
        'if [ -z "$VERSION" ]; then\n'
        "  printf 'aucune version écrite — rien à relire\\n'\n"
        "else\n"
        '  N=$(gcloud secrets versions access \"${VERSION##*/}\" '
        f"--secret={secret_id} \\\n"
        f"    --project={projet} | wc -c | tr -d ' ')\n"
        f"  if {_bornes(secret)}; then\n"
        "    printf 'relu : %s octets — conforme\\n' \"$N\"\n"
        "  else\n"
        "    printf 'relu : %s octets — NE CORRESPOND PAS\\n' \"$N\"\n"
        "  fi\n"
        "fi"
    )


def gcloud_recipe(secret_id: str, project_id: str = "") -> list:
    """Les commandes qui écrivent une NOUVELLE VERSION de ce secret.

    Rend une liste de couples (commentaire français, commande). Table FERMÉE :
    un `secret_id` inconnu rend une liste vide, jamais une commande
    construite autour de lui — la page qui rend cela existe pour qu'on y
    copie des lignes de shell, donc une chaîne arbitraire ne doit pas pouvoir
    y devenir une commande.

    `project_id` vide rend `$PROJECT` plutôt qu'une chaîne vide interpolée :
    une commande à `--project=` vide vise le projet actif de `gcloud`, ce qui
    est précisément comment on écrit dans le projet de quelqu'un d'autre.

    Les cinq propriétés qui font la valeur de ces lignes :

    1. `IFS= read -rs` — la valeur ne paraît pas à l'écran, n'entre pas dans
       l'historique du shell, et `IFS=` empêche que les blancs de tête et de
       queue soient mangés (ce qui masquerait le défaut qu'on cherche).
       ⚠ `read` ne retient que la PREMIÈRE LIGNE. Les six secrets de cette
       table sont tous d'une seule ligne par construction (un jeton, une clé
       d'API, une empreinte bcrypt, un secret Entra), donc un collage
       multiligne signifie qu'on a collé la mauvaise chose — et le collage
       tronqué a l'air complet à l'écran. C'est le contrôle d'avant-vol qui l'attrape, et
       seulement parce qu'elle COMPARE au lieu de montrer.
    2. `printf '%s'` — `printf` est une PRIMITIVE du shell, donc la valeur ne
       passe jamais par `/proc/*/cmdline` ; et `'%s'` n'ajoute AUCUN saut de
       ligne, ce qui est tout le piège.
    3. `--data-file=-` — jamais `--data=`, qui déposerait la valeur dans
       `ps`.
    4. L'écho entre crochets — une espace de tête ou de queue devient
       VISIBLE, ce qu'aucune console ne montre autrement.
    5. Le COMPTE D'OCTETS, comparé par le shell, aux deux bouts du vol.
       C'est ce qui rend inutile un validateur côté navigateur : `.strip()` de
       Python et `.trim()` de JavaScript ne s'accordent pas sur six
       caractères, dont la MOM d'un collage Windows. Et c'est le seul contrôle
       qui voie une nomenclature d'octets en TÊTE aussi bien qu'un
       terminateur en queue — un `xxd | tail -1` ne regarde que la fin.

    `wc -c` compte des OCTETS, jamais des caractères — `${#VALEUR}` compterait
    des caractères et dirait 11 pour « café-secret », qui en fait 12. La
    recette n'emploie donc nulle part `${#…}`.
    """
    secret = secret_by_id(secret_id)
    if secret is None:
        return []
    # DÉSORMAIS l'identifiant CANONIQUE, plus l'argument : la garde ci-dessus
    # prouve déjà qu'ils sont égaux, mais lire la table plutôt que l'entrée
    # fait de « aucune chaîne arbitraire n'entre dans une commande » une
    # propriété de STRUCTURE, qui survivrait même au retrait de la garde.
    secret_id = secret.secret_id
    projet = project_id or "$PROJECT"
    attendu = expected_length_fr(secret)

    if secret.origin == ORIGIN_PASSWORD:
        return [
            (_SONDE_FR, _SONDE_PY),
            (
                "Vérifier que bcrypt est présent DANS cet interpréteur — et "
                "non ailleurs. L'ancienne ligne `-m pip install` ne marche "
                "pas partout : le venv de ce dépôt n'a pas `pip` du tout "
                "(« No module named pip », mesuré), et bcrypt y est déjà "
                "puisque c'est une dépendance épinglée. On constate donc au "
                "lieu d'installer à l'aveugle.",
                '"$PY" -c \'import bcrypt\' 2>/dev/null'
                " && printf 'bcrypt : présent\\n'"
                " || printf 'bcrypt ABSENT de cet interpréteur — "
                "à installer avant de continuer\\n'",
            ),
            (
                "Calculer l'empreinte. Le mot de passe n'est jamais un "
                "argument ni une variable — `getpass` le lit du terminal. "
                "L'EMPREINTE, elle, peut vivre dans une variable : c'est un "
                "condensé, et c'est ce qui permet de la mesurer AVANT de "
                "l'écrire, contrôle que la forme fusionnée précédente ne "
                "pouvait pas offrir.",
                'EMPREINTE=$("$PY" -c \'import bcrypt, getpass, sys; '
                "sys.stdout.write(bcrypt.hashpw(getpass.getpass("
                '"Mot de passe DAV : ").encode(), bcrypt.gensalt())'
                ".decode())')",
            ),
            (
                f"Mesurer, puis écrire SEULEMENT si la longueur est "
                f"{attendu}. Une empreinte de 61 octets n'égalera jamais les "
                "60 que `bcrypt.checkpw` recalcule, et DavX5 cesse alors de "
                "synchroniser sans un mot.",
                _ecriture_gardee(secret, secret_id, projet, "EMPREINTE"),
            ),
            ("Effacer l'empreinte de la session.", "unset EMPREINTE"),
            (
                "Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`, "
                "qui désignerait la version précédente si l'écriture avait "
                "échoué et rassurerait donc à tort.",
                _relecture_gardee(secret, secret_id, projet),
            ),
        ]

    etapes = []
    if secret.origin == ORIGIN_GENERATED:
        etapes.append((_SONDE_FR, _SONDE_PY))
        etapes.append((
            "Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` "
            "par discipline : aucune ligne de cette recette n'émet de saut de "
            "ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : "
            "c'est le générateur cryptographique de la bibliothèque standard, "
            "ce qui vaut de dépendre d'un interpréteur.",
            """VALEUR=$("$PY" -c 'import secrets, sys; """
            """sys.stdout.write(secrets.token_urlsafe(32))')""",
        ))
    else:
        etapes.append((
            "Coller la valeur remise par la console, puis Entrée. Rien ne "
            "s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger "
            "les blancs — s'il y en a, on veut les VOIR, pas les perdre en "
            "silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six "
            "secrets sont d'une seule ligne par construction, donc un collage "
            "multiligne signifie qu'on a collé la mauvaise chose — et le "
            "collage tronqué a l'air complet à l'écran. C'est la mesure qui "
            "l'attrape, pas l'œil.",
            "IFS= read -rs VALEUR",
        ))

    etapes.append((
        "Voir ce qui a réellement été saisi. Les crochets rendent visible une "
        "espace de tête ou de queue, qu'aucune console ne montre autrement.",
        'printf \'[%s]\\n\' "$VALEUR"',
    ))
    etapes.append((
        f"Mesurer, puis écrire SEULEMENT si la longueur est {attendu} octets. "
        "La mesure est une GARDE, pas un avis : la commande d'écriture vit "
        "DANS le `if`, donc coller le bloc entier ne peut pas écrire une "
        "valeur que la mesure vient de refuser. `printf` est une primitive du "
        "shell, donc la valeur ne devient l'argument d'aucun processus ; "
        "`--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; "
        "et `versions add`, jamais `secrets create` — le secret existe déjà.",
        _ecriture_gardee(secret, secret_id, projet, "VALEUR"),
    ))
    etapes.append((
        "Effacer la variable de la session.",
        "unset VALEUR",
    ))
    etapes.append((
        "Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si "
        "l'écriture avait échoué, `latest` désignerait la version précédente, "
        "qui a toutes les chances d'être de bonne longueur puisqu'elle "
        "fonctionnait : l'après-vol rassurerait alors sur une écriture qui "
        "n'a pas eu lieu.",
        _relecture_gardee(secret, secret_id, projet),
    ))
    return etapes


# ── Les API à activer ────────────────────────────────────────────────────
#
# §6.1 de DEPLOYMENT.md en listait DOUZE et en omettait six, dont une qui
# casse la toute première construction CI d'un adoptant : `cloudbuild.yaml`
# déploie `cron.yaml` sans condition, et sans `cloudscheduler` cette étape
# échoue `SERVICE_DISABLED` — APRÈS que trois services ont déjà été
# déployés. La liste vit donc ici, et §6.1 en est ENGENDRÉE : une API
# ajoutée au code se réclame d'elle-même dans le document, ou le test tombe.
#
# `reason` dit pourquoi elle est requise, `consumer` dit QUI l'appelle —
# parce qu'une liste d'API sans appelant ne se vérifie pas : on ne peut ni
# la retirer en confiance, ni diagnostiquer son absence.


@dataclass(frozen=True)
class Api:
    service: str
    reason: str
    consumer: str


REQUIRED_APIS: tuple[Api, ...] = (
    Api(
        service="appengine.googleapis.com",
        reason="Les deux services App Engine y tournent.",
        consumer="app.yaml, portail.yaml",
    ),
    Api(
        service="firestore.googleapis.com",
        reason=(
            "La base par défaut ET la base NOMMÉE « portail » — ce sont deux "
            "bases du même service."
        ),
        consumer="models/, client/services/invitations.py",
    ),
    Api(
        service="secretmanager.googleapis.com",
        reason=(
            "Les six secrets de §4.2. Sans elle, `config.py` lève à l'import "
            "et le service ne démarre pas du tout."
        ),
        consumer="config.py, client/config.py",
    ),
    Api(
        service="cloudbuild.googleapis.com",
        reason="Le déclencheur qui exécute la suite puis déploie.",
        consumer="cloudbuild.yaml",
    ),
    Api(
        service="iam.googleapis.com",
        reason="Les liaisons de rôles que §6.4 pose.",
        consumer="les commandes de §6.4",
    ),
    Api(
        service="iamcredentials.googleapis.com",
        reason=(
            "L'API `signBlob`. Sans elle l'auto-impersonation échoue et AUCUN "
            "URL signé n'est produit — en silence, et en PRODUCTION seulement : "
            "en local une clé de compte de service signe sur place, donc ce "
            "chemin n'est jamais emprunté avant le déploiement."
        ),
        consumer="models/document.sign_blob_url, models/doc_template",
    ),
    Api(
        service="firebase.googleapis.com",
        reason="La gestion du projet Firebase.",
        consumer="la CLI firebase",
    ),
    Api(
        service="firebaseappcheck.googleapis.com",
        reason="App Check, qui vérifie l'attestation des requêtes HTMX.",
        consumer="security.py",
    ),
    Api(
        service="firebaserules.googleapis.com",
        reason=(
            "Le déploiement des règles Firestore et Storage. Sans elle "
            "`firebase deploy --only firestore:rules,storage` échoue — et ces "
            "règles SONT le refus par défaut qui couvre chaque collection."
        ),
        consumer="firestore.rules, storage.rules",
    ),
    Api(
        service="firebasestorage.googleapis.com",
        reason="Le seau par défaut de Firebase Storage et ses règles.",
        consumer="firebase-admin.storage",
    ),
    Api(
        service="storage.googleapis.com",
        reason=(
            "L'API JSON de GCS elle-même : URL signés, sessions reprenables, "
            "ingestion par rewrite, composition du ZIP d'un dossier de "
            "classement. `firebasestorage` gère le seau ; c'est celle-ci qui "
            "déplace les octets."
        ),
        consumer="google-cloud-storage",
    ),
    Api(
        service="identitytoolkit.googleapis.com",
        reason=(
            "Firebase Auth — la session, la MFA, et le lien courriel du "
            "portail."
        ),
        consumer="auth.py, client/routes.py",
    ),
    Api(
        service="recaptchaenterprise.googleapis.com",
        reason="Le fournisseur d'attestation d'App Check.",
        consumer="security.py, base.html",
    ),
    Api(
        service="logging.googleapis.com",
        reason=(
            "Le journal structuré, par `CloudLoggingHandler`. Sans elle, il ne "
            "reste aucune trace agrégée de ce que la production a fait."
        ),
        consumer="utils/logging_setup.py",
    ),
    Api(
        service="cloudtrace.googleapis.com",
        reason=(
            "Doit rester activée À CÔTÉ de `telemetry` : la note de migration "
            "de Google est explicite — désactiver Cloud Trace fait JETER les "
            "traces envoyées à l'API Telemetry, en silence. C'est aussi elle "
            "qui sert la LECTURE des traces."
        ),
        consumer="utils/tracing_setup.py, la console",
    ),
    Api(
        service="telemetry.googleapis.com",
        reason="La destination OTLP des spans depuis le 2026-07-30.",
        consumer="utils/tracing_setup.py",
    ),
    Api(
        service="monitoring.googleapis.com",
        reason=(
            "Les politiques d'alerte de sécurité de §6.10 et le canal qui "
            "écrit au juriste. Sans elle aucune alerte ne se crée, et la "
            "sonde de ces politiques lit « inconnu »."
        ),
        consumer="DEPLOYMENT.md §6.10, scripts/provision.py",
    ),
    Api(
        service="cloudtasks.googleapis.com",
        reason="La file « portail » par laquelle le service public signale.",
        consumer="client/services/taches.py",
    ),
    Api(
        service="cloudscheduler.googleapis.com",
        reason=(
            "EXIGÉE par `gcloud app deploy cron.yaml`. Son absence fait "
            "échouer cette étape sur `SERVICE_DISABLED`, et `cloudbuild.yaml` "
            "la place en QUATRIÈME : l'échec arrive donc APRÈS que `default`, "
            "`portail` et `dispatch` sont déployés. La première construction "
            "d'un adoptant se termine rouge sur un déploiement à moitié fait."
        ),
        consumer="cloudbuild.yaml, cron.yaml",
    ),
)

API_SERVICES: tuple[str, ...] = tuple(a.service for a in REQUIRED_APIS)

# Combien d'API par ligne du bloc engendré. Deux tient sous 80 colonnes
# même avec les deux noms les plus longs de la table côte à côte ; trois
# débordait de dix caractères, dans un bloc qu'un adoptant copie à la main.
_APIS_PAR_LIGNE = 2


def services_enable_block(project_id: str = "") -> str:
    """La commande `gcloud services enable`, ENGENDRÉE depuis la table.

    Un `project_id` vide rend `$PROJECT`, la variable que §6.1 pose déjà —
    le même choix que `gcloud_recipe`, pour la même raison : le document ne
    doit pas porter l'identifiant de projet du propriétaire d'origine.
    """
    projet = project_id or "$PROJECT"
    noms = list(API_SERVICES)
    lignes = ["gcloud services enable " + chr(92)]
    for i in range(0, len(noms), _APIS_PAR_LIGNE):
        lignes.append("  " + " ".join(noms[i:i + _APIS_PAR_LIGNE]) + " " + chr(92))
    lignes.append("  --project=" + projet)
    return chr(10).join(lignes)


# ── Ce qu'il faut provisionner, et comment le CONSTATER ──────────────────
#
# Les API et les secrets ont leur table ; le reste — l'application App Engine,
# les DEUX bases Firestore, la file, le seau de quarantaine, le pare-feu, le
# compte de service du portail — vivait uniquement dans CLAUDE.md, en prose,
# et pour partie dans une spec qui n'est PAS dans le dépôt. Le cycle de vie du
# seau de quarantaine en est l'exemple net : la politique de rétention des
# téléversements d'un client — matériel privilégié — était citée comme « §12.3
# de la spec » et ne figurait dans aucun fichier versionné. Elle est ici,
# relevée sur le déploiement vivant le 2026-09-13.
#
# `detect` est un GABARIT D'ARGUMENTS, pas une commande : ce module reste pur
# (aucun `subprocess`, aucune E/S), et c'est cette pureté qui le rend
# importable par une route Flask, par un script et par un test sans
# identifiants. Chaque verbe est en LECTURE SEULE — il n'existe volontairement
# aucun `apply()` dans ce lot : cette moitié-là ne peut pas être éprouvée sans
# dépenser un projet jetable, et la leçon du 2026-09-11 est qu'une commande
# écrite mais jamais exécutée est confiante et fausse.
#
# `symptom` est le champ qui rend la liste VÉRIFIABLE. Sans lui on ne peut ni
# retirer une ligne en confiance, ni diagnostiquer son absence — et plusieurs
# de ces absences échouent EN SILENCE, ce qui est précisément pourquoi elles
# méritaient une table plutôt qu'un paragraphe.

# Le suffixe du seau de quarantaine. `client/config.py` le dérive de
# l'identifiant de projet ; un test épingle les deux ensemble, pour qu'un
# adoptant n'ait pas à choisir un nom.
PORTAIL_BUCKET_SUFFIX = "-portail-quarantaine"

# La file Cloud Tasks et la base nommée, telles que `client/config.py` les
# nomme. Même épingle, même raison.
PORTAIL_QUEUE_NAME = "portail"
PORTAIL_DB_NAME = "portail"

# L'adresse interne d'où App Engine expédie cron et Cloud Tasks. Elle n'est
# PAS une plage Cloudflare, et §7 de DEPLOYMENT.md a dit « les plages
# Cloudflare seulement » jusqu'au 2026-09-12 — suivi à la lettre, cela coupe
# les trois tâches cron et la file.
APPENGINE_INTERNAL_CIDR = "0.1.0.2/32"

# Le cycle de vie du seau de quarantaine, EN DONNÉES. Relevé sur le
# déploiement vivant le 2026-09-13 ; c'est la première fois qu'il vit dans un
# fichier versionné.
QUARANTINE_LIFECYCLE: tuple[dict, ...] = (
    {"prefix": "submissions/", "age_days": 90},
    {"prefix": "archive/", "age_days": 365},
)

# Le seau CANONIQUE (celui de FIREBASE_STORAGE_BUCKET, qui porte les
# documents des clients) : le nom par défaut que la console Firebase donne à
# un projet créé depuis octobre 2024. Un projet plus ancien a
# `{projet}.appspot.com` — l'inspection le rapporterait alors « absent »
# (jamais « en place »), ce qui est le bon côté. Un test épingle ce suffixe
# contre le seau du déploiement d'origine.
CANONICAL_BUCKET_SUFFIX = ".firebasestorage.app"

# Son cycle de vie, EN DONNÉES : `staging/` balayé à 7 jours. C'est la SEULE
# suppression automatique que le code attend sur ce seau — un téléversement
# direct jamais finalisé (formulaire web, ticket du connecteur), une archive
# zip dont la signature a échoué. Le ticket de téléversement du connecteur
# (lot 2A) en DÉPEND : un envoi arrivé après son heure n'est jamais versé, et
# c'est cette règle qui l'efface. Écrite ici le 2026-09-27 à partir de ce que
# DEPLOYMENT.md et le code affirment — pas encore relevée sur le déploiement
# vivant (la sonde s'en charge).
CANONICAL_STAGING_LIFECYCLE: tuple[dict, ...] = (
    {"prefix": "staging/", "age_days": 7},
)

PHASE_SOCLE = "socle"
PHASE_DONNEES = "données"
PHASE_SECRETS = "secrets"
PHASE_PORTAIL = "portail"
PHASE_BORDURE = "bordure"

# L'ordre dans lequel un adoptant doit franchir les phases. Cinq des huit
# défaillances connues d'un clone neuf sont des défaillances d'ORDRE, pas de
# commande : c'est la raison d'être de ce tuple.
PHASE_ORDER: tuple[str, ...] = (
    PHASE_SOCLE,
    PHASE_DONNEES,
    PHASE_SECRETS,
    PHASE_PORTAIL,
    PHASE_BORDURE,
)


@dataclass(frozen=True)
class Resource:
    """Une ressource à provisionner, et la commande qui la CONSTATE."""

    key: str
    label: str
    phase: str
    detect: tuple = ()
    expect: str = ""
    symptom: str = ""
    irreversible: bool = False
    manual: bool = False

    def argv(self, project: str, region: str) -> tuple:
        """Le gabarit, rempli. Aucun secret n'entre jamais ici — un test le
        balaie, parce qu'un argument atterrit dans `ps` et dans l'historique."""
        bucket = project + PORTAIL_BUCKET_SUFFIX
        canonical = project + CANONICAL_BUCKET_SUFFIX
        return tuple(
            a.replace("{project}", project)
             .replace("{region}", region)
             .replace("{bucket}", bucket)
             .replace("{canonical_bucket}", canonical)
            for a in self.detect
        )


# ── La surveillance de sécurité : compteurs log-based et alertes ─────────
#
# Les événements de sécurité de SECURITY_METRICS (OBSERVABILITY.md,
# `log_security_event`) alimentent des COMPTEURS log-based — un compteur
# peut en compter plusieurs —, et chaque compteur qui nomme une politique,
# une alerte Cloud Monitoring qui écrit au juriste. Rien de tout cela ne se
# voit sur une page — c'est le signal qui a manqué pendant les mois où
# `cf-origin-secret` n'existait pas.
#
# LA SOURCE, ce sont les FICHIERS du dossier `monitoring/`, à la racine du
# dépôt : un par compteur (`monitoring/metrics/<compteur>.json`) et un par
# politique (`monitoring/policies/<compteur>.json`, nommé par le compteur
# qu'elle surveille). Relus de la production et trouvés égaux à leur
# écriture (novembre 2026), ils lui restent égaux tant qu'une ressource ne
# change que DEPUIS son fichier (§6.10, étape 5) : la sonde compare le
# filtre d'un compteur à son fichier ; d'une politique, elle ne vérifie
# qu'une chose — qu'UNE seule surveille le compteur, activée, avec un canal —
# et rien ne compare sa condition ni son texte au fichier. Ce texte — celui
# des courriels, la documentation française des politiques — ne vit que dans
# les fichiers. Ce module ne les LIT pas : il
# est importé à l'exécution sur App Engine (routes/settings.py,
# utils/config_checks.py), où la racine du dépôt
# n'existe pas. Il les NOMME, et ne garde que ce dont l'inventaire, la sonde
# et les documents ont besoin ; chaque valeur qu'il redit d'un fichier — le
# nom du compteur, ses événements, le nom affiché de la politique, la
# fenêtre, la fermeture, le type de ressource — est épinglée contre les
# fichiers par tests/test_monitoring_definitions.py et
# tests/test_deployment_inventory.py. Les lignes de RESOURCES, les commandes
# de DEPLOYMENT.md §6.10 et les tables d'OBSERVABILITY.md en DÉRIVENT.

# Le nom de journal de `CloudLoggingHandler` : la copie, pour les documents,
# de `utils/logging_setup.APP_LOG_NAME` (ce module n'importe pas Flask), à
# laquelle un test l'épingle. Les deux services le partagent : un compteur
# filtré sur lui compte les lignes du portail aussi, que
# `resource.labels.module_id` distingue.
APP_LOG_NAME = "pallas-athena"

# Le dossier des définitions, RELATIF à la racine du dépôt — comme les
# chemins de SCAN_FILES, l'appelant les joint.
MONITORING_DIR = "monitoring"

# Ce que les documents disent de chaque politique : « plus de zéro en cinq
# minutes », close d'elle-même après un jour sans nouvelle ligne. Épinglé
# contre l'`alignmentPeriod` et l'`autoClose` de chaque fichier
# (tests/test_monitoring_definitions.py) ; la prose de §6.10 et
# d'OBSERVABILITY.md qui redit l'une et l'autre est épinglée contre ces
# deux constantes (tests/test_deployment_inventory.py).
ALERT_WINDOW = "300s"
ALERT_AUTO_CLOSE = "86400s"
# Le type de ressource que chaque condition restreint : celle que
# `CloudLoggingHandler` détecte sur App Engine (l'API de Monitoring exige
# qu'un filtre de seuil restreigne `resource.type`). §6.10 le fait vérifier.
ALERT_RESOURCE_TYPE = "gae_app"


def metric_path(metric: str) -> str:
    """Le fichier d'un compteur, relatif à la racine du dépôt."""
    return MONITORING_DIR + "/metrics/" + metric + ".json"


def policy_path(metric: str) -> str:
    """Le fichier de la politique qui surveille ce compteur."""
    return MONITORING_DIR + "/policies/" + metric + ".json"


@dataclass(frozen=True)
class SecurityMetric:
    """Un compteur log-based et son alerte, NOMMÉS : leur définition est le
    fichier de `monitoring/`.

    ``events`` redit les `jsonPayload.event` que compte le filtre du fichier
    (un test les compare) ; ``policy`` est le `displayName` de la politique ;
    vide, le compteur n'en porte pas, et ``no_alert_reason`` dit pourquoi.
    """

    metric: str
    events: tuple
    key: str
    metric_symptom: str
    policy: str = ""
    policy_symptom: str = ""
    no_alert_reason: str = ""

    @property
    def metric_key(self) -> str:
        return "metrique-" + self.key

    @property
    def policy_key(self) -> str:
        return "alerte-" + self.key

    @property
    def metric_file(self) -> str:
        return metric_path(self.metric)

    @property
    def policy_file(self) -> str:
        """Vide quand le compteur ne porte pas d'alerte."""
        return policy_path(self.metric) if self.policy else ""

    def event_clause(self) -> str:
        """`jsonPayload.event="x"`, ou `=("x" OR "y")` pour plusieurs — la
        forme du filtre du fichier, pour le rendu de l'inventaire."""
        valeurs = ['"' + e + '"' for e in self.events]
        if len(valeurs) == 1:
            return "jsonPayload.event=" + valeurs[0]
        return "jsonPayload.event=(" + " OR ".join(valeurs) + ")"


SECURITY_METRICS: tuple[SecurityMetric, ...] = (
    SecurityMetric(
        metric="security_redirect_rejected",
        events=("redirect_rejected",),
        key="redirection-refusee",
        metric_symptom=(
            "l'alerte de la redirection refusée n'a rien à compter : un lien "
            "fabriqué ouvert dans le navigateur connecté du juriste ne laisse "
            "qu'une ligne de journal que personne ne lit"
        ),
        policy="Sécurité — redirection refusée",
        policy_symptom=(
            "une redirection refusée n'avertit pas le juriste comme prévu : "
            "le compteur compte, mais le courriel ne part pas, ou part en "
            "double"
        ),
    ),
    SecurityMetric(
        metric="security_control_disabled",
        events=("appcheck_disabled", "origin_secret_disabled"),
        key="controle-desactive",
        metric_symptom=(
            "App Check ou le secret d'origine peuvent se couper EN SILENCE — "
            "c'est ainsi que cf-origin-secret a manqué des mois sans qu'aucun "
            "signal ne parte"
        ),
        policy="Sécurité — une protection est désactivée",
        policy_symptom=(
            "une protection de la bordure désactivée en production n'avertit "
            "pas comme prévu (aucun courriel, ou en double) — le défaut exact "
            "qui a laissé cf-origin-secret absent pendant des mois"
        ),
    ),
    SecurityMetric(
        metric="security_appspot_blocked",
        events=("appspot_blocked",),
        key="appspot-bloque",
        metric_symptom=(
            "le fil-piège de la couche 3 ne compte rien : un accès direct à "
            "l'hôte appspot passe inaperçu"
        ),
        policy="Sécurité — accès direct à appspot.com",
        policy_symptom=(
            "un accès à l'hôte appspot.com — relayé par une zone Cloudflare, "
            "que le pare-feu admet — n'avertit pas comme prévu (aucun "
            "courriel, ou en double)"
        ),
    ),
    SecurityMetric(
        metric="security_request_too_large",
        events=("request_too_large",),
        key="requete-trop-volumineuse",
        metric_symptom=(
            "le volume des refus 413 reste inconnu, donc l'alerte qu'on "
            "voudrait y poser ne peut pas être calibrée"
        ),
        no_alert_reason=(
            "son volume est inconnu : un robot peut poster de gros corps, "
            "une ligne par tentative. Mesurer d'abord, puis choisir un seuil"
        ),
    ),
)


# ── Les commandes de §6.10, ENGENDRÉES ───────────────────────────────────
# Chacune se lance de la RACINE du dépôt (les chemins sont relatifs) et
# prend le fichier pour source : jamais un texte recopié dans la commande.


def _projet(project: str) -> str:
    return project or "$PROJECT"


def metric_create_command(metric: str, project: str = "") -> str:
    """Le compteur, créé DEPUIS son fichier — une fois.

    Un compteur simple (pas de `--bucket-name`) : il compte au niveau du
    PROJET, donc le seau régional de §6.8 n'y change rien. Relancée, elle
    répond `ALREADY_EXISTS` et ne change rien.
    """
    return ("gcloud logging metrics create " + metric
            + " --config-from-file=" + metric_path(metric)
            + " --project=" + _projet(project))


def metric_update_command(metric: str, project: str = "") -> str:
    """Le compteur, mis à jour DEPUIS son fichier : seuls les champs que le
    fichier nomme changent."""
    return ("gcloud logging metrics update " + metric
            + " --config-from-file=" + metric_path(metric)
            + " --project=" + _projet(project))


# Le canal du juriste, tel que les commandes le passent : GARDÉ. Un `$CHANNEL`
# nu, vide — un terminal neuf, où l'étape 2 n'a pas tourné —, devient
# `--notification-channels=` : gcloud le lit comme une liste VIDE, sans
# erreur (SDK 580.0.0, `arg_parsers.ArgList` sur la chaîne vide), et la
# politique créée ou remplacée ne prévient personne. `${CHANNEL:?…}` fait
# refuser la commande par le shell avant qu'elle parte — le garde même de
# `FIREBASE_STORAGE_BUCKET` en §15. Un garde de BASH (et de tout shell
# POSIX) : PowerShell lit `"${CHANNEL:?…}"` comme une variable inexistante
# et passe une valeur VIDE sans erreur, même `$CHANNEL` posé — d'où
# l'avertissement de §6.10 (ces blocs se lancent dans bash).
_CHANNEL_ARG = '"${CHANNEL:?step 2 sets CHANNEL}"'


def policy_create_command(metric: str, project: str = "") -> str:
    """La politique, créée DEPUIS son fichier, avec le canal du juriste.

    Une fois : `policies create` ne dédoublonne pas — deux exécutions font
    deux politiques et deux courriels par déclenchement. Le canal se passe
    ici, jamais dans le fichier versionné (le dépôt est public), et gardé
    (``_CHANNEL_ARG``).
    """
    return ("gcloud monitoring policies create --policy-from-file="
            + policy_path(metric) + " --notification-channels=" + _CHANNEL_ARG
            + " --project=" + _projet(project))


def policy_name_command(metric: str, project: str = "") -> str:
    """Le nom complet de la politique qui surveille ce compteur — le filtre
    même de la sonde (RESOURCES), réduit au nom."""
    return ("gcloud monitoring policies list --project=" + _projet(project)
            + ' --filter="conditions.conditionThreshold.filter~' + metric
            + '" --format="value(name)"')


def policy_update_command(metric: str, project: str = "") -> str:
    """La politique `$POLICY`, mise à jour DEPUIS son fichier.

    ⚠ Sans `--fields`, `policies update --policy-from-file` REMPLACE la
    politique entière par le fichier, qui ne nomme aucun canal : sans
    `--set-notification-channels` — ou avec un `$CHANNEL` vide, que le garde
    ``_CHANNEL_ARG`` refuse —, l'alerte resterait activée et ne
    préviendrait plus personne (la sonde le lirait ensuite en dérive).
    `$POLICY` est gardé aussi : vide, la liste n'a rien trouvé.
    """
    return ('gcloud monitoring policies update "${POLICY:?no policy found}"'
            + " --policy-from-file=" + policy_path(metric)
            + " --set-notification-channels=" + _CHANNEL_ARG
            + " --project=" + _projet(project))


def resource_type_check_command(project: str = "") -> str:
    """Lecture seule : le type de ressource des lignes de l'application, que
    chaque condition restreint (ALERT_RESOURCE_TYPE).

    `--freshness=30d` : le défaut d'un jour ne rend RIEN un jour sans ligne,
    ce qui se lirait comme un mauvais type.
    """
    projet = _projet(project)
    return ('gcloud logging read "logName=' + chr(92) + '"projects/' + projet
            + "/logs/" + APP_LOG_NAME + chr(92) + '"" --project=' + projet
            + ' --freshness=30d --limit=1 --format="value(resource.type)"')


# Les caractères qu'un shell interpréterait dans la valeur d'une option.
_SHELL_SPECIAL = frozenset("()~,;&|<>*?[]{}!#'` ")


def detect_command(resource: "Resource", project: str = "") -> str:
    """La commande de constat d'une ressource, telle qu'on la tape.

    Une valeur d'option qui porte un caractère spécial du shell (parenthèse,
    `~`, virgule…) passe entre guillemets doubles ; `$PROJECT` reste nu, pour
    se développer.
    """
    parties = []
    for a in resource.argv(_projet(project), "$REGION"):
        if a.startswith("--") and "=" in a:
            option, valeur = a.split("=", 1)
            if any(c in _SHELL_SPECIAL for c in valeur):
                a = option + '="' + valeur + '"'
        parties.append(a)
    return " ".join(parties)


def _metric_resource(m: SecurityMetric) -> "Resource":
    # Le FILTRE et l'état avec le nom : exister ne suffit pas. Un compteur
    # créé d'un fichier dont le `logName` nomme un autre projet ne compte
    # rien ; resté en arrière de son fichier, il compte autre chose, ou rien ;
    # DÉSACTIVÉ (`disabled`, un champ que le fichier ne porte pas), il ne
    # produit aucun point. La sonde compare donc ce filtre à celui du
    # fichier, au projet sondé près, et lit l'état (deployment_probe).
    return Resource(
        key=m.metric_key,
        label="Compteur log-based " + m.metric,
        phase=PHASE_BORDURE,
        detect=("gcloud", "logging", "metrics", "describe", m.metric,
                "--project={project}", "--format=value(name,filter,disabled)"),
        expect=("il existe, activé, et son filtre est celui de "
                + m.metric_file + " (" + m.event_clause()
                + "), dans le journal de CE projet"),
        symptom=m.metric_symptom,
    )


def _policy_resource(m: SecurityMetric) -> "Resource":
    # Le filtre vise le COMPTEUR que la condition lit, par son nom COMPLET —
    # en ASCII, donc sûr dans un argv Windows, contrairement au nom affiché
    # accentué. `~` est une RE que gcloud applique côté client, non ancrée :
    # le passage de `:` à la correspondance de mots entiers, que gcloud
    # annonce (un préfixe comme `security_` cessera d'y répondre), ne la
    # touche pas. La sortie dit si la politique est ACTIVÉE et à QUI elle
    # écrit.
    return Resource(
        key=m.policy_key,
        label="Alerte « " + m.policy + " »",
        phase=PHASE_BORDURE,
        detect=("gcloud", "monitoring", "policies", "list",
                "--project={project}",
                "--filter=conditions.conditionThreshold.filter~" + m.metric,
                "--format=value(name,enabled,notificationChannels)"),
        expect="UNE politique, activée, qui écrit à au moins un canal",
        symptom=m.policy_symptom,
    )


RESOURCES: tuple[Resource, ...] = (
    Resource(
        key="app-engine",
        label="Application App Engine",
        phase=PHASE_SOCLE,
        detect=("gcloud", "app", "describe", "--project={project}",
                "--format=value(locationId)"),
        expect="la région, qui doit être celle de tout le reste",
        symptom="rien ne se déploie ; et la région choisie ici est DÉFINITIVE",
        irreversible=True,
    ),
    Resource(
        key="firestore-defaut",
        label="Base Firestore par défaut (mode natif)",
        phase=PHASE_DONNEES,
        detect=("gcloud", "firestore", "databases", "describe",
                "--database=(default)", "--project={project}",
                "--format=value(locationId,type)"),
        expect="FIRESTORE_NATIVE, dans la région App Engine",
        symptom="l'application démarre puis échoue à la première lecture",
        irreversible=True,
    ),
    Resource(
        key="firestore-portail",
        label="Base Firestore NOMMÉE « portail »",
        phase=PHASE_DONNEES,
        detect=("gcloud", "firestore", "databases", "describe",
                "--database=" + PORTAIL_DB_NAME, "--project={project}",
                "--format=value(locationId,type)"),
        expect="FIRESTORE_NATIVE, même région",
        symptom=(
            "aucune invitation ne se lit ni ne s'écrit ; c'est une base "
            "SÉPARÉE, pas une collection de la base par défaut"
        ),
        irreversible=True,
    ),
    Resource(
        key="index-composites",
        label="Index composites Firestore",
        phase=PHASE_DONNEES,
        detect=("gcloud", "firestore", "indexes", "composite", "list",
                "--project={project}", "--format=value(state)"),
        expect=(
            "autant d'index READY que firestore.indexes.json en déclare — le "
            "compte se DÉRIVE du fichier, il ne se recopie pas"
        ),
        symptom=(
            "tant qu'un index construit, la requête qu'il sert échoue et la "
            "vue se dégrade en LISTE VIDE — jamais en erreur"
        ),
    ),
    Resource(
        key="ttl-firestore",
        label=("Politiques TTL (oauth_codes, oauth_tokens, mcp_idempotency, "
               "mcp_upload_tickets)"),
        phase=PHASE_DONNEES,
        detect=("gcloud", "firestore", "fields", "ttls", "list",
                "--collection-group=oauth_codes", "--project={project}",
                "--format=value(ttlConfig.state)"),
        expect="ACTIVE",
        symptom=(
            "aucune conséquence de SÉCURITÉ — l'expiration est appliquée dans "
            "le code à chaque lecture ; le TTL n'est que du ramassage. Les "
            "quatre sont d'ailleurs déclarés en `fieldOverrides` dans "
            "firestore.indexes.json, donc le déploiement des index les pose "
            "déjà : les commandes `ttls update` que DEPLOYMENT.md §11 énumère sont "
            "redondantes (constaté 2026-09-13)"
        ),
    ),
    Resource(
        key="sa-portail",
        label="Compte de service portail-svc",
        phase=PHASE_PORTAIL,
        detect=("gcloud", "iam", "service-accounts", "describe",
                "portail-svc@{project}.iam.gserviceaccount.com",
                "--project={project}", "--format=value(email)"),
        expect="il existe",
        symptom="l'étape 2b de cloudbuild.yaml échoue et la construction s'arrête",
    ),
    Resource(
        key="seau-quarantaine",
        label="Seau de quarantaine du portail",
        phase=PHASE_PORTAIL,
        detect=("gcloud", "storage", "buckets", "describe", "gs://{bucket}",
                "--project={project}",
                # ⚠ `uniform_bucket_level_access` est un BOOLÉEN nu.
                # Projeter `.enabled` — la forme qu'on écrit
                # naturellement — rend une colonne VIDE, et un
                # découpage naïf décale alors tout ce qui suit
                # (mesuré le 2026-09-13 : « accès uniforme DÉSACTIVÉ »
                # rapporté à tort sur un seau conforme).
                "--format=value(location,uniform_bucket_level_access,"
                "public_access_prevention)"),
        expect=(
            "la région App Engine, accès uniforme ACTIVÉ, accès public "
            "« enforced »"
        ),
        symptom=(
            "le client ne peut rien téléverser ; et un seau dans la mauvaise "
            "région fait payer de l'egress inter-régional sur chaque octet"
        ),
    ),
    Resource(
        key="cycle-de-vie-quarantaine",
        label="Cycle de vie du seau (rétention)",
        phase=PHASE_PORTAIL,
        detect=("gcloud", "storage", "buckets", "describe", "gs://{bucket}",
                "--project={project}", "--format=json(lifecycle_config)"),
        expect="submissions/ à 90 jours, archive/ à 365 (QUARANTINE_LIFECYCLE)",
        symptom=(
            "le matériel privilégié d'un client reste en quarantaine POUR "
            "TOUJOURS. C'est la règle qui ne vivait dans aucun fichier "
            "versionné jusqu'au 2026-09-13"
        ),
    ),
    Resource(
        key="file-portail",
        label="File Cloud Tasks « portail »",
        phase=PHASE_PORTAIL,
        detect=("gcloud", "tasks", "queues", "describe", PORTAIL_QUEUE_NAME,
                "--location={region}", "--project={project}",
                "--format=value(state,rateLimits.maxConcurrentDispatches,"
                "rateLimits.maxDispatchesPerSecond,retryConfig.maxAttempts)"),
        expect="RUNNING, 3 dispatches simultanés, 5/s, 10 tentatives",
        symptom=(
            "une soumission n'est jamais traitée EN DIRECT ; le cron de "
            "réconciliation la rattrape un quart d'heure plus tard, donc "
            "l'absence se lit comme de la lenteur, pas comme une panne"
        ),
    ),
    Resource(
        key="pare-feu-interne",
        label="Pare-feu : " + APPENGINE_INTERNAL_CIDR + " autorisé",
        phase=PHASE_BORDURE,
        detect=("gcloud", "app", "firewall-rules", "list", "--project={project}",
                "--format=value(priority,action,sourceRange)"),
        expect=(
            APPENGINE_INTERNAL_CIDR + " en ALLOW, AU-DESSUS des plages "
            "Cloudflare, et le DENY par défaut en dernier"
        ),
        symptom=(
            "les trois tâches cron et toute la file Cloud Tasks sont bloquées "
            "à la couche 1, en silence — App Engine les expédie depuis cette "
            "adresse interne, qui n'est pas une plage Cloudflare"
        ),
    ),
    # ── Ce qu'aucune commande ne peut constater ──────────────────────────
    Resource(
        key="firebase-auth",
        label="Firebase Auth : fournisseur mot de passe + lien courriel",
        phase=PHASE_SOCLE,
        manual=True,
        expect="activés dans la console, domaine du portail autorisé",
        symptom="personne ne peut se connecter, ni le juriste ni un client invité",
    ),
    Resource(
        key="utilisateur-unique",
        label="L'utilisateur unique, et son second facteur",
        phase=PHASE_SOCLE,
        manual=True,
        expect="un seul compte, l'adresse d'AUTHORIZED_USER_EMAIL, MFA inscrite",
        symptom=(
            "avec REQUIRE_MFA=true, une connexion réussit chez Firebase puis "
            "la session est REFUSÉE — et l'application ne peut pas réparer son "
            "propre verrouillage, la page de sécurité étant derrière la "
            "connexion"
        ),
    ),
    Resource(
        key="seau-firebase-storage",
        label="Seau Firebase Storage par défaut",
        phase=PHASE_DONNEES,
        manual=True,
        expect="initialisé dans la console, nom == FIREBASE_STORAGE_BUCKET",
        symptom=(
            "aucun document ne se téléverse ; et ses règles ne se déploient "
            "qu'APRÈS sa création"
        ),
    ),
    # Constatable, mais déclarée ICI, juste après le seau qu'elle vise : le
    # plan trie par phase en gardant l'ordre de la table, et la règle ne peut
    # se poser qu'une fois le seau créé à la console.
    Resource(
        key="cycle-de-vie-staging",
        label="Cycle de vie du seau canonique (staging/)",
        phase=PHASE_DONNEES,
        detect=("gcloud", "storage", "buckets", "describe",
                "gs://{canonical_bucket}", "--project={project}",
                "--format=json(lifecycle_config)"),
        expect=(
            "staging/ à 7 jours (CANONICAL_STAGING_LIFECYCLE), et AUCUNE "
            "autre règle de suppression d'objets vivants"
        ),
        symptom=(
            "les envois directs jamais finalisés — et les fichiers qu'un "
            "ticket de téléversement du connecteur a reçus trop tard — "
            "restent en staging/ POUR TOUJOURS ; à l'inverse, une règle de "
            "suppression sans préfixe effacerait les documents des clients"
        ),
    ),
    Resource(
        key="app-check",
        label="App Check + clé reCAPTCHA Enterprise",
        phase=PHASE_BORDURE,
        manual=True,
        expect="application web enregistrée, clé du domaine du portail incluse",
        symptom=(
            "App Check échoue OUVERT — l'application fonctionne, la protection "
            "n'existe pas, et seul un avertissement en production le dit"
        ),
    ),
    Resource(
        key="cloudflare",
        label="Cloudflare : DNS, Full (Strict), Transform Rule, Configuration Rule",
        phase=PHASE_BORDURE,
        manual=True,
        expect=(
            "la Transform Rule zone-wide PROUVÉE par le traceur de requêtes "
            "AVANT que cf-origin-secret n'existe"
        ),
        symptom=(
            "dans le mauvais ordre, le site répond 403 PARTOUT sans qu'aucun "
            "déploiement ne l'explique — les instances se recyclent seules"
        ),
    ),
    Resource(
        key="entra",
        label="Entra ID : inscription d'application + permissions Graph",
        phase=PHASE_BORDURE,
        manual=True,
        expect="Mail.Send et Calendars.ReadWrite, consentement administrateur",
        symptom=(
            "le courriel sortant et la synchro Bookings sont éteints ; et un "
            "changement de permission met 30 min à 2 h à sortir du cache "
            "Exchange, donc « ça marche encore » ne prouve rien"
        ),
    ),
    Resource(
        key="declencheur-cloud-build",
        label="Déclencheur Cloud Build sur push vers main",
        phase=PHASE_BORDURE,
        manual=True,
        expect="connecté au dépôt, exécutant cloudbuild.yaml",
        symptom=(
            "rien ne se déploie tout seul ; et le brancher AVANT que "
            "portail-svc n'existe fait échouer la première construction à "
            "l'étape 2b"
        ),
    ),
) + tuple(
    # ── La surveillance de sécurité (DEPLOYMENT.md §6.10), DÉRIVÉE de
    # SECURITY_METRICS : les compteurs d'abord — une politique ne se crée
    # que sur un compteur qui existe —, en DERNIER dans la bordure, une fois
    # la bordure armée : avant, `security_control_disabled` se déclencherait
    # à chaque instance neuve, le secret d'origine n'étant posé qu'après la
    # preuve de la Transform Rule (§5).
    _metric_resource(m) for m in SECURITY_METRICS
) + tuple(
    _policy_resource(m) for m in SECURITY_METRICS if m.policy
)

RESOURCE_KEYS: tuple[str, ...] = tuple(r.key for r in RESOURCES)


def resource_by_key(key: str):
    for r in RESOURCES:
        if r.key == key:
            return r
    return None


def provisioning_plan() -> tuple:
    """Les ressources dans l'ordre des phases, l'ordre stable à l'intérieur.

    C'est l'ORDRE qui a de la valeur, pas les commandes : cinq des huit
    défaillances connues d'un clone neuf sont des défaillances d'ordre.
    """
    rang = {p: i for i, p in enumerate(PHASE_ORDER)}
    return tuple(sorted(RESOURCES, key=lambda r: rang[r.phase]))


# ── Runtime environment ──────────────────────────────────────────────────

# Read with bracket ``os.environ[...]`` in the class body of ``config.py``, so
# the module raises at IMPORT without them. FIREBASE_PROJECT_ID is the root of
# the three: ``_from_secret_manager`` uses it to build the resource path, so
# nothing — not one secret — resolves without it.
REQUIRED_ENV: tuple[str, ...] = (
    "FIREBASE_PROJECT_ID",
    "FIREBASE_STORAGE_BUCKET",
    "AUTHORIZED_USER_EMAIL",
)

# Settings whose absence degrades a control. The two edge controls DISABLE
# THEMSELVES when unset, and now say so in production — ``appcheck_disabled``,
# and since 2026-09-07 ``origin_secret_disabled``, both feeding the alert
# « Sécurité — une protection est désactivée » (DEPLOYMENT.md §6.10) since
# 2026-11-08 — but until 2026-09-07 the origin-secret check said nothing at
# all (``if not secret: return None``, no log, no metric). That silence is
# why ``cf-origin-secret`` never existed in Secret Manager for months with
# nothing signalling it. ``DAV_PASSWORD_HASH`` is the other kind: unset, DAV
# fails CLOSED — every login is refused (dav/dav_auth.py) — and nothing is
# logged for it.
FAIL_OPEN_ENV: dict[str, str] = {
    "RECAPTCHA_ENTERPRISE_SITE_KEY": (
        "Firebase App Check est désactivé (les requêtes HTMX ne sont pas "
        "vérifiées)."
    ),
    "CF_ORIGIN_SECRET": (
        "Le contrôle du secret d'origine Cloudflare est désactivé (l'accès "
        "direct à App Engine n'est pas bloqué)."
    ),
    "DAV_PASSWORD_HASH": (
        "L'authentification DAV ne peut pas réussir — la synchro DavX5 est "
        "indisponible."
    ),
}

# Of those, the ones production resolves from SECRET MANAGER rather than the
# environment. Reporting them "unset in production" from ``os.environ`` is a
# FALSE NEGATIVE by construction: ``config._secret()`` never consults the env
# var when ENV=production. The secret pass is the authority for these.
SECRET_BACKED_ENV: dict[str, str] = {
    "CF_ORIGIN_SECRET": "cf-origin-secret",
    "DAV_PASSWORD_HASH": "dav-password-hash",
}


# ── Fork hygiene ─────────────────────────────────────────────────────────
# Values hardcoded to the original deployment. An adopter must replace every
# one before going live — this is the "somebody cloned the repository" check.

OWNER_LITERALS: dict[str, str] = {
    "GCP project id": "athena-pallas",
    "Storage bucket": "athena-pallas.firebasestorage.app",
    "Firebase app id": "1:1073430388324:web:6d60d0d67d74ce1b730971",
    "Authorized user email": "jason@poirierlavoie.ca",
    "Firm name": "Me Jason Poirier Lavoie",
    "reCAPTCHA site key": "6LeR9Y0sAAAAAJhkKWa_wYdbproZUQFjEO7FkfEb",
    "MCP canonical origin": "https://athena.poirierlavoie.ca",
    "Domain": "athena.poirierlavoie.ca",
    "TWA package": "ca.poirierlavoie.athena",
    # Added with the inventory extraction: these live in portail.yaml,
    # client/config.py and dispatch.yaml, none of which the scan covered.
    "Portal host": "portail.poirierlavoie.ca",
    "Portal service account": "portail-svc@athena-pallas.iam.gserviceaccount.com",
    "Firm phone": "(514) 737-2525",
    "Firm email": "reception@poirierlavoie.ca",
    "Graph tenant id": "4c5c39a5-2e63-4b04-8408-973c58cd88c7",
    "Graph client id": "988bf117-3aef-463b-8062-7dac226e50d9",
    # La RÉGION App Engine. `client/config.py` la code en dur comme défaut de
    # `TASKS_LOCATION`, et ce fichier EST balayé — mais le balayage ne peut
    # signaler que ce qu'on lui donne à chercher. Un adoptant qui déploie
    # ailleurs obtient donc une file Cloud Tasks que le code ne trouve pas,
    # et `signaler()` lève `NOT_FOUND` à la finalisation — un échec que le
    # portail avale PAR CONCEPTION (l'enveloppe est la vérité durable), donc
    # le seul symptôme est un lot qui arrive en Réception un quart d'heure
    # plus tard, par le cron de réconciliation.
    "App Engine region": "northamerica-northeast1",
}

# The TWA signing fingerprint is a PATTERN, not a literal value.
OWNER_FINGERPRINT_RE = "47:3B:05:FB"

# Files that legitimately carry deployment config. Docs and tests are excluded
# on purpose — they cite the owner's ids as examples.
#
# ``portail.yaml``, ``client/config.py`` and ``dispatch.yaml`` were MISSING
# here until the extraction, so the owner literals they carry went unchecked.
# ``tests/test_deployment_inventory.py`` now derives this list's completeness:
# it walks the repo's config files and fails if any file containing an
# OWNER_LITERALS value is absent — which fixes the class of bug rather than
# this instance of it.
SCAN_FILES: tuple[str, ...] = (
    "athena/app.yaml",
    "athena/portail.yaml",
    # Repo ROOT, not athena/ — the first draft of this list said
    # "athena/dispatch.yaml", which does not exist, and check_config cannot
    # scan a missing path: it only reports it « not found ». The derived
    # completeness test below is what caught it.
    "dispatch.yaml",
    "athena/config.py",
    "athena/main.py",
    "athena/client/config.py",
) + tuple(
    # Les définitions de la surveillance (DEPLOYMENT.md §6.10, 2026-11-08)
    # nomment le projet d'origine : chaque compteur dans le `logName` de son
    # filtre — tests/test_monitoring_definitions.py exige que ce soit LUI —,
    # d'où la dérivation ; et la politique de l'accès direct à appspot dans
    # le texte de son courriel (`--project=…`), d'où la ligne qui suit. Comme
    # dispatch.yaml, elles sont à la RACINE du dépôt : sur App Engine, où
    # seule `athena/` est déployée, le rapport les dit « not found », comme
    # chacune des entrées précédentes.
    m.metric_file for m in SECURITY_METRICS
) + (
    policy_path("security_appspot_blocked"),
)

__all__ = [
    "ALERT_AUTO_CLOSE",
    "ALERT_RESOURCE_TYPE",
    "ALERT_WINDOW",
    "APPENGINE_INTERNAL_CIDR",
    "APP_LOG_NAME",
    "API_SERVICES",
    "CANONICAL_BUCKET_SUFFIX",
    "CANONICAL_STAGING_LIFECYCLE",
    "Api",
    "MONITORING_DIR",
    "PHASE_ORDER",
    "PORTAIL_BUCKET_SUFFIX",
    "PORTAIL_DB_NAME",
    "PORTAIL_QUEUE_NAME",
    "QUARANTINE_LIFECYCLE",
    "RESOURCES",
    "RESOURCE_KEYS",
    "Resource",
    "FAIL_OPEN_ENV",
    "ORIGIN_EXTERNAL",
    "ORIGIN_GENERATED",
    "ORIGIN_PASSWORD",
    "SHAPE_FAIL",
    "SHAPE_WARN",
    "ShapeVerdict",
    "OWNER_FINGERPRINT_RE",
    "OWNER_LITERALS",
    "REQUIRED_APIS",
    "REQUIRED_ENV",
    "SCAN_FILES",
    "SECRETS",
    "SECRET_BACKED_ENV",
    "SECRET_IDS",
    "SECURITY_METRICS",
    "SERVICES",
    "SERVICE_DEFAULT",
    "SERVICE_PORTAIL",
    "Secret",
    "SecurityMetric",
    "Shape",
    "WRITABLE_SECRET_IDS",
    "detect_command",
    "expected_length_fr",
    "metric_create_command",
    "metric_path",
    "metric_update_command",
    "policy_create_command",
    "policy_name_command",
    "policy_path",
    "policy_update_command",
    "resource_type_check_command",
    "services_enable_block",
    "gcloud_recipe",
    "provisioning_plan",
    "resource_by_key",
    "secret_by_id",
    "stray_whitespace",
]
