"""Courrier orchestration service (Phase 2 inventory, Phase 3 save & Phase 4 move).

Coordinates Microsoft Graph reads/uploads/classification (``utils.graph_courrier``),
attachment preview triage (``utils.courrier_apercu``), temporary GCS staging +
Firestore registry state (``models.courrier``), folders (``models.folder``),
and document ingestion (``models.document``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

from config import Config
from models import concurrency
from models import courrier as courrier_model
from models import document as document_model
from models import dossier as dossier_model
from models import folder as folder_model
from tz import to_mtl
from utils import courrier_apercu, email_text, graph_courrier, storage_identity
from utils.logging_setup import log_courrier_event
from utils.quickxorhash import QuickXorHash

DEFAULT_NOMBRE = 5
MAX_NOMBRE = 25
MAX_PREMIER = 10_000
TEXTE_MAX_CHARS = 1500
BUDGET_SECONDES = 25.0

MAX_MESSAGES_SAVE = 25
MAX_MESSAGES_MOVE = 25
VALID_MOVE_ACTIONS: tuple[str, ...] = ("classer", "laisser")
EML_MAX_CHARS = 80
PIECE_NOM_MAX_CHARS = 100
_CONFLICT_RETRY_MAX = 50
_LEGACY_EMAILS_SUBFOLDER = "Correspondance\\Emails"
_CANONICAL_COURRIELS_SUBFOLDER = "Correspondance\\Courriels"

# Test-pinned mapping from OneDrive plan sous_dossier -> (DEFAULT_TREE key, presumed Athena category).
CORRESPONDANCE_SOUS_DOSSIERS: dict[str, tuple[Optional[str], str]] = {
    "": (None, "autre"),
    "Correspondance": ("correspondance", "correspondance"),
    _CANONICAL_COURRIELS_SUBFOLDER: ("courriels", "correspondance"),
    _LEGACY_EMAILS_SUBFOLDER: ("courriels", "correspondance"),
    "Procédures": ("procedures", "procédure"),
    "Jugements": ("jugements", "jugement"),
    "Pièces": ("pieces", "pièce"),
    "Documents du client": ("documents_client", "autre"),
}
VALID_SOUS_DOSSIERS: tuple[str, ...] = tuple(CORRESPONDANCE_SOUS_DOSSIERS)

OUTLOOK_STATUTS: tuple[str, ...] = (
    "Actifs",
    "Suspendu",
    "Services-conseils",
)

# Linear CWE-1333 regexes: explicit ASCII character classes, zero nested quantifiers.
_DOSSIER_PREFIX_RE = re.compile(r"\A([0-9]{4}-[0-9]{3})(?:\Z|[\s\-–—_])")
_DOSSIER_STRICT_RE = re.compile(r"\A[0-9]{4}-[0-9]{3}\Z")
_EXT_CLEAN_RE = re.compile(r"\A\.[a-z0-9]{1,16}\Z")
_SUFFIX_LOOKS_LIKE_EXT_RE = re.compile(r"\A.*\.[a-zA-Z0-9]{1,10}\Z")

_ILLEGAL_FILENAME_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_CIVILITE_TOKENS = frozenset({
    "maitre", "maître", "monsieur", "madame", "mademoiselle",
    "docteur", "docteure", "professeur", "professeure",
    "me", "m", "mme", "mlle", "dr", "dre", "pr", "mr", "mrs", "ms",
})

_MESSAGE_SELECT = ",".join((
    "id",
    "internetMessageId",
    "conversationId",
    "receivedDateTime",
    "from",
    "toRecipients",
    "ccRecipients",
    "subject",
    "isRead",
    "flag",
    "categories",
    "webLink",
    "uniqueBody",
    "hasAttachments",
))

__all__ = [
    "BUDGET_SECONDES",
    "CORRESPONDANCE_SOUS_DOSSIERS",
    "DEFAULT_NOMBRE",
    "EML_MAX_CHARS",
    "MAX_MESSAGES_MOVE",
    "MAX_MESSAGES_SAVE",
    "MAX_NOMBRE",
    "MAX_PREMIER",
    "OUTLOOK_STATUTS",
    "PIECE_NOM_MAX_CHARS",
    "TEXTE_MAX_CHARS",
    "VALID_MOVE_ACTIONS",
    "VALID_SOUS_DOSSIERS",
    "construire_nom_eml",
    "deplacer_courriels",
    "enregistrer_courriels",
    "extraire_prefixe_dossier",
    "inscrire_temps_courriels",
    "inventorier_courriels",
    "nettoyer_expediteur_sans_civilite",
    "nettoyer_nom_piece",
    "suffixer_nom_conflit",
]


def extraire_prefixe_dossier(nom: str) -> str:
    """Extract a leading ``AAAA-NNN`` dossier number from a folder display name."""
    match = _DOSSIER_PREFIX_RE.match(str(nom or "").strip())
    return match.group(1) if match else ""


def _parse_recu_mtl(raw_iso: Any) -> str:
    raw = str(raw_iso or "").strip()
    if not raw:
        return ""
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mtl = to_mtl(dt)
    return mtl.strftime("%Y-%m-%d %H:%M") if mtl is not None else ""


def _extract_expediteur(msg: dict[str, Any]) -> dict[str, str]:
    raw_from = msg.get("from")
    email_obj = raw_from.get("emailAddress") if isinstance(raw_from, dict) else None
    if not isinstance(email_obj, dict):
        return {"nom": "", "adresse": ""}
    return {
        "nom": str(email_obj.get("name") or "").strip(),
        "adresse": str(email_obj.get("address") or "").strip(),
    }


def _extract_addresses(raw_recipients: Any) -> list[str]:
    if not isinstance(raw_recipients, list):
        return []
    out: list[str] = []
    for entry in raw_recipients:
        if not isinstance(entry, dict):
            continue
        email_obj = entry.get("emailAddress")
        if not isinstance(email_obj, dict):
            continue
        addr = str(email_obj.get("address") or "").strip()
        if addr:
            out.append(addr)
    return out


def _extract_body_text(msg: dict[str, Any]) -> str:
    body = msg.get("uniqueBody")
    if not isinstance(body, dict):
        return ""
    raw_content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "text").strip().lower()
    if content_type == "html":
        cleaned = email_text.html_to_text(raw_content)
    else:
        cleaned = email_text.tidy_text(raw_content)
    return cleaned[:TEXTE_MAX_CHARS]


def _find_child_by_name(
    items: list[dict[str, Any]],
    target: str,
) -> Optional[dict[str, Any]]:
    needle = target.strip().casefold()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("displayName") or "").strip().casefold() == needle:
            return item
    return None


def _lire_carte_outlook(
    encoded_upn: str,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Read ``Archive > Dossiers > {Actifs, Suspendu, Services-conseils}``."""
    statuts_payload: dict[str, dict[str, Any]] = {
        statut: {"id": "", "dossiers": []} for statut in OUTLOOK_STATUTS
    }
    folder_map: dict[str, str] = {}
    warnings: list[str] = []

    top_res = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders",
        params={"$select": "id,displayName", "$top": 100},
        immutable_id=True,
    )
    archive_node = _find_child_by_name(top_res.get("value") or [], "Archive")
    if archive_node is None:
        warnings.append("Dossier Outlook « Archive » introuvable.")
        return (
            {"archive_id": "", "dossiers_id": "", "statuts": statuts_payload},
            folder_map,
            warnings,
        )
    archive_id = str(archive_node.get("id") or "")

    archive_children = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders/{quote(archive_id, safe='')}/childFolders",
        params={"$select": "id,displayName", "$top": 100},
        immutable_id=True,
    )
    dossiers_node = _find_child_by_name(archive_children.get("value") or [], "Dossiers")
    if dossiers_node is None:
        warnings.append("Dossier Outlook « Archive > Dossiers » introuvable.")
        return (
            {"archive_id": archive_id, "dossiers_id": "", "statuts": statuts_payload},
            folder_map,
            warnings,
        )
    dossiers_id = str(dossiers_node.get("id") or "")

    status_children = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders/{quote(dossiers_id, safe='')}/childFolders",
        params={"$select": "id,displayName", "$top": 100},
        immutable_id=True,
    )
    status_items = status_children.get("value") or []

    for statut in OUTLOOK_STATUTS:
        node = _find_child_by_name(status_items, statut)
        if node is None:
            warnings.append(
                f"Dossier Outlook « Archive > Dossiers > {statut} » introuvable."
            )
            continue
        statut_id = str(node.get("id") or "")
        folder_map[statut_id] = f"statut:{statut}"

        dossier_children = graph_courrier.courrier_get(
            f"/users/{encoded_upn}/mailFolders/{quote(statut_id, safe='')}/childFolders",
            params={"$select": "id,displayName", "$top": 250},
            immutable_id=True,
        )
        entries: list[dict[str, str]] = []
        for sub in dossier_children.get("value") or []:
            if not isinstance(sub, dict):
                continue
            sub_id = str(sub.get("id") or "")
            sub_name = str(sub.get("displayName") or "").strip()
            numero = extraire_prefixe_dossier(sub_name)
            if not numero or not sub_id:
                continue
            folder_map[sub_id] = numero
            entries.append({"numero": numero, "nom": sub_name, "id": sub_id})
        entries.sort(key=lambda item: (item["numero"], item["nom"]))
        statuts_payload[statut] = {"id": statut_id, "dossiers": entries}

    return (
        {
            "archive_id": archive_id,
            "dossiers_id": dossiers_id,
            "statuts": statuts_payload,
        },
        folder_map,
        warnings,
    )


def _lire_carte_onedrive(encoded_upn: str, racine_onedrive: str) -> dict[str, Any]:
    """Read immediate dossier folders under ``COURRIER_ONEDRIVE_ROOT``."""
    encoded_root = quote(racine_onedrive, safe="/")
    root_meta = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/drive/root:/{encoded_root}",
        params={"$select": "id,folder"},
        immutable_id=False,
    )
    if not isinstance(root_meta.get("folder"), dict):
        raise graph_courrier.CourrierGraphError(
            "Le chemin racine OneDrive n'est pas un dossier."
        )
    racine_id = str(root_meta.get("id") or "")

    children_res = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/drive/root:/{encoded_root}:/children",
        params={"$select": "id,name,folder", "$top": 200},
        immutable_id=False,
    )
    dossiers: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for item in children_res.get("value") or []:
        if not isinstance(item, dict) or not isinstance(item.get("folder"), dict):
            continue
        name = str(item.get("name") or "").strip()
        item_id = str(item.get("id") or "")
        numero = extraire_prefixe_dossier(name)
        if not numero or not item_id:
            continue
        counts[numero] = counts.get(numero, 0) + 1
        dossiers.append({"numero": numero, "nom": name, "id": item_id})

    dossiers.sort(key=lambda item: (item["numero"], item["nom"]))
    doublons = sorted(num for num, count in counts.items() if count > 1)
    return {
        "racine_id": racine_id,
        "dossiers": dossiers,
        "doublons": doublons,
    }


def _lire_pieces(
    encoded_upn: str,
    msg_id: str,
    has_attachments: Any,
) -> list[dict[str, Any]]:
    if has_attachments is False:
        return []
    encoded_msg_id = quote(msg_id, safe="")
    res = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/messages/{encoded_msg_id}/attachments",
        params={"$select": "id,name,contentType,size,isInline"},
        immutable_id=True,
    )
    pieces: list[dict[str, Any]] = []
    for rang, att in enumerate(res.get("value") or [], start=1):
        if not isinstance(att, dict):
            continue
        att_id = str(att.get("id") or "")
        nom = str(att.get("name") or "")
        ctype = str(att.get("contentType") or "")
        odata_type = str(att.get("@odata.type") or "")
        raw_size = att.get("size")
        taille = (
            raw_size
            if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0
            else 0
        )
        ignore = courrier_apercu.motif_ignore_piece(att)
        if ignore is not None:
            apercu = ""
            apercu_omis: Optional[str] = "ignoree"
        elif not courrier_apercu.peut_extraire_apercu(nom, ctype, odata_type):
            apercu = ""
            apercu_omis = "format_non_texte"
        elif taille > courrier_apercu.APERCU_MAX_BYTES:
            apercu = ""
            apercu_omis = "taille_depassee"
        else:
            try:
                raw_bytes = graph_courrier.courrier_get_bytes(
                    f"/users/{encoded_upn}/messages/{encoded_msg_id}/attachments/{quote(att_id, safe='')}/$value",
                    max_bytes=courrier_apercu.APERCU_MAX_BYTES,
                    immutable_id=True,
                )
            except graph_courrier.CourrierGraphError as exc:
                if exc.reason == "too_large":
                    apercu = ""
                    apercu_omis = "taille_depassee"
                else:
                    raise
            else:
                apercu, apercu_omis = courrier_apercu.extraire_apercu(
                    raw_bytes, nom, ctype
                )

        pieces.append({
            "rang": rang,
            "id": att_id,
            "nom": nom,
            "type": ctype,
            "taille": taille,
            "ignore": ignore,
            "apercu": apercu,
            "apercu_omis": apercu_omis,
        })
    return pieces


def _lire_fil_conversation(
    encoded_upn: str,
    *,
    msg_id: str,
    conversation_id: str,
    inbox_id: str,
    folder_map: dict[str, str],
) -> dict[str, int]:
    if not conversation_id:
        return {}
    escaped_cid = conversation_id.replace("'", "''")
    res = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/messages",
        params={
            "$filter": f"conversationId eq '{escaped_cid}'",
            "$select": "id,parentFolderId",
            "$top": 50,
        },
        immutable_id=True,
        follow_next=False,
    )
    counts: dict[str, int] = {}
    for sibling in res.get("value") or []:
        if not isinstance(sibling, dict):
            continue
        if str(sibling.get("id") or "") == msg_id:
            continue
        parent_id = str(sibling.get("parentFolderId") or "")
        if inbox_id and parent_id == inbox_id:
            label = "boite"
        elif parent_id in folder_map:
            label = folder_map[parent_id]
        else:
            label = "autre"
        counts[label] = counts.get(label, 0) + 1
    return counts


def inventorier_courriels(
    *,
    premier: int = 0,
    nombre: int = DEFAULT_NOMBRE,
    budget_secondes: float = BUDGET_SECONDES,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Inventory Inbox oldest-first with previews, threads, and registry state."""
    if not Config.courrier_configured():
        raise graph_courrier.CourrierNotConfigured(
            "Le module Courrier n'est pas configuré."
        )

    started = clock()
    encoded_upn = quote(Config.COURRIER_UPN, safe="")
    racine_onedrive = Config.COURRIER_ONEDRIVE_ROOT or "4 - Dossiers"

    inbox_meta = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders/Inbox",
        params={"$select": "id,totalItemCount"},
        immutable_id=True,
    )
    inbox_id = str(inbox_meta.get("id") or "")
    raw_total = inbox_meta.get("totalItemCount")
    inbox_total = (
        raw_total
        if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0
        else 0
    )

    page_res = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders/Inbox/messages",
        params={
            "$orderby": "receivedDateTime asc",
            "$skip": premier,
            "$top": nombre,
            "$select": _MESSAGE_SELECT,
        },
        immutable_id=True,
        prefer=('outlook.body-content-type="text"',),
        follow_next=False,
    )
    raw_messages = [
        m for m in (page_res.get("value") or []) if isinstance(m, dict)
    ]

    outlook_payload: Optional[dict[str, Any]] = None
    onedrive_payload: Optional[dict[str, Any]] = None
    folder_map: dict[str, str] = {}
    warnings: list[str] = []

    if premier == 0 or raw_messages:
        outlook_map, folder_map, outlook_warnings = _lire_carte_outlook(encoded_upn)
        warnings.extend(outlook_warnings)
        if premier == 0:
            outlook_payload = outlook_map
    if premier == 0:
        onedrive_payload = _lire_carte_onedrive(encoded_upn, racine_onedrive)

    message_cles: list[str] = []
    for msg in raw_messages:
        msg_id = str(msg.get("id") or "").strip()
        raw_imid = str(msg.get("internetMessageId") or "").strip()
        message_cles.append(courrier_model.compute_cle(raw_imid or msg_id))

    reg_map = courrier_model.get_courrier_messages_map_strict(message_cles)

    messages: list[dict[str, Any]] = []
    budget_atteint = False
    for idx, (msg, cle) in enumerate(zip(raw_messages, message_cles)):
        if idx >= 1 and (clock() - started) >= budget_secondes:
            budget_atteint = True
            break

        msg_id = str(msg.get("id") or "").strip()
        raw_categories = msg.get("categories")
        categories = (
            [str(c) for c in raw_categories if isinstance(c, str)]
            if isinstance(raw_categories, list)
            else []
        )
        flag_obj = msg.get("flag")
        drapeau = (
            str(flag_obj.get("flagStatus") or "notFlagged")
            if isinstance(flag_obj, dict)
            else "notFlagged"
        )
        conversation_id = str(msg.get("conversationId") or "").strip()

        pieces = _lire_pieces(encoded_upn, msg_id, msg.get("hasAttachments"))
        fil = _lire_fil_conversation(
            encoded_upn,
            msg_id=msg_id,
            conversation_id=conversation_id,
            inbox_id=inbox_id,
            folder_map=folder_map,
        )
        reg_doc = reg_map.get(cle)
        etat = (
            reg_doc.get("etat", courrier_model.ETAT_INVENTORIE)
            if reg_doc is not None
            else courrier_model.ETAT_INVENTORIE
        )
        legacy_ref = (
            str(reg_doc.get("legacy_ref") or "")
            if reg_doc is not None
            else ""
        )

        messages.append({
            "cle": cle,
            "id": msg_id,
            "recu": _parse_recu_mtl(msg.get("receivedDateTime")),
            "expediteur": _extract_expediteur(msg),
            "a": _extract_addresses(msg.get("toRecipients")),
            "cc": _extract_addresses(msg.get("ccRecipients")),
            "objet": str(msg.get("subject") or ""),
            "lu": bool(msg.get("isRead")),
            "drapeau": drapeau,
            "categories": categories,
            "lien": str(msg.get("webLink") or ""),
            "texte": _extract_body_text(msg),
            "pieces": pieces,
            "fil": fil,
            "etat": etat,
            "legacy_ref": legacy_ref,
        })

    prochain = premier + len(messages)
    if not raw_messages:
        boite_total = inbox_total
        suite = False
    elif not budget_atteint and len(raw_messages) < nombre:
        boite_total = prochain
        suite = False
    else:
        boite_total = max(inbox_total, prochain)
        suite = (prochain < boite_total) or budget_atteint

    log_courrier_event(
        "courrier_inventaire",
        "success",
        premier=premier,
        nombre_retourne=len(messages),
        boite_total=boite_total,
        suite=suite,
        budget_atteint=budget_atteint,
    )
    return {
        "messages": messages,
        "nombre_retourne": len(messages),
        "boite_total": boite_total,
        "premier": premier,
        "prochain": prochain,
        "suite": suite,
        "budget_atteint": budget_atteint,
        "outlook": outlook_payload,
        "onedrive": onedrive_payload,
        "avertissements": warnings,
    }


# ── Phase 3 : save_courriels (OneDrive + Athéna + registre) ─────────────


class _SaveMessageError(RuntimeError):
    """Per-message validation or transfer failure with fixed French reason."""


def _sanitize_component_text(raw: str) -> str:
    """Replace illegal filesystem/control characters and collapse whitespace."""
    out: list[str] = []
    for ch in str(raw or ""):
        if ch in _ILLEGAL_FILENAME_CHARS or unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append(ch)
    return " ".join("".join(out).split()).strip(" .")


def nettoyer_expediteur_sans_civilite(nom: str, adresse: str = "") -> str:
    """Clean sender display name and strip leading titles/honorifics (Decision #6)."""
    text = _sanitize_component_text(nom).strip("\"'` ")
    lower = text.casefold()
    if lower.startswith("courriel de "):
        text = text[len("courriel de ") :].strip()
    elif lower.startswith("de "):
        text = text[len("de ") :].strip()

    while text:
        parts = text.split(None, 1)
        if len(parts) < 2:
            break
        first_norm = unicodedata.normalize("NFC", parts[0]).rstrip(".,:").casefold()
        if first_norm in _CIVILITE_TOKENS:
            text = parts[1].strip()
        else:
            break

    text = _sanitize_component_text(text)
    if text:
        return text
    addr_local = str(adresse or "").strip().split("@", 1)[0]
    fallback = _sanitize_component_text(addr_local)
    return fallback or "Expediteur"


def _parse_utc_datetime(raw_iso: Any) -> Optional[datetime]:
    raw = str(raw_iso or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def construire_nom_eml(
    dossier_numero: str,
    recu_dt_utc: datetime,
    expediteur_nom: str,
    expediteur_adresse: str = "",
) -> str:
    """Build ``AAAA-NNN - AAAA-MM-JJ HHhMM - {expéditeur sans civilité}.eml`` <= 80 chars."""
    if not _DOSSIER_STRICT_RE.match(str(dossier_numero or "")):
        raise ValueError("Numéro de dossier invalide pour nommer l'.eml.")
    mtl = to_mtl(recu_dt_utc)
    if mtl is None:
        raise ValueError("Horodatage de réception invalide.")
    stamp = mtl.strftime("%Y-%m-%d %Hh%M")
    prefix = f"{dossier_numero} - {stamp} - "
    ext = ".eml"
    max_sender = EML_MAX_CHARS - len(prefix) - len(ext)
    sender = nettoyer_expediteur_sans_civilite(expediteur_nom, expediteur_adresse)
    if len(sender) > max_sender:
        sender = sender[:max_sender].rstrip(" .")
    if not sender:
        sender = "Expediteur"[:max_sender]
    stem = f"{prefix}{sender}".rstrip(" .")
    if stem.upper() in _WINDOWS_RESERVED_STEMS:
        stem = f"{stem}_msg"
    return f"{stem}{ext}"


def nettoyer_nom_piece(
    nom_demande: str,
    nom_origine_graph: str,
) -> tuple[Optional[str], Optional[str]]:
    """Sanitize attachment filename while preserving Graph's real lowercase extension."""
    orig_raw = str(nom_origine_graph or "").strip()
    if "." not in orig_raw:
        return None, "La pièce jointe d'origine ne porte aucune extension."
    orig_ext = "." + orig_raw.rsplit(".", 1)[1].strip().lower()
    if not _EXT_CLEAN_RE.match(orig_ext):
        return None, "Extension de pièce jointe d'origine non prise en charge."

    cleaned_req = _sanitize_component_text(nom_demande)
    if not cleaned_req:
        cleaned_req = _sanitize_component_text(orig_raw.rsplit(".", 1)[0])
    if cleaned_req.lower().endswith(orig_ext):
        stem = cleaned_req[: -len(orig_ext)]
    elif _SUFFIX_LOOKS_LIKE_EXT_RE.match(cleaned_req):
        return (
            None,
            f"Le nom demandé doit conserver l'extension d'origine ({orig_ext}).",
        )
    else:
        stem = cleaned_req

    stem = _sanitize_component_text(stem)
    if not stem or stem.upper() in _WINDOWS_RESERVED_STEMS:
        return None, "Nom de pièce jointe vide ou réservé."
    max_stem = PIECE_NOM_MAX_CHARS - len(orig_ext)
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED_STEMS:
        return None, "Nom de pièce jointe invalide après troncature."
    return f"{stem}{orig_ext}", None


def suffixer_nom_conflit(nom_propre: str, index: int, max_chars: int) -> str:
    """Append `` (N)`` before extension while keeping length <= ``max_chars``."""
    if index <= 1:
        return nom_propre
    suffix = f" ({index})"
    if "." in nom_propre:
        stem, dot_ext = nom_propre.rsplit(".", 1)
        ext = f".{dot_ext}"
    else:
        stem, ext = nom_propre, ""
    max_stem = max_chars - len(suffix) - len(ext)
    if max_stem < 1:
        raise ValueError("Nom de fichier trop long pour le suffixe de conflit.")
    trimmed_stem = stem[:max_stem].rstrip(" .") or "fichier"
    return f"{trimmed_stem}{suffix}{ext}"


def _clean_dossier_onedrive_name(nom: str, numero: str) -> Optional[str]:
    if not isinstance(nom, str):
        return None
    raw = nom.strip()
    if not raw or len(raw) > 150 or "/" in raw or "\\" in raw or ".." in raw:
        return None
    if any(ch in _ILLEGAL_FILENAME_CHARS or unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in raw):
        return None
    if raw.upper() in _WINDOWS_RESERVED_STEMS:
        return None
    if not raw.startswith(f"{numero} "):
        return None
    return raw


def _slot_token(slot: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", slot.lower())[:32] or "file"


def _resoudre_sous_dossier_courriel_onedrive(
    encoded_upn: str,
    od_dossier_id: Optional[str],
    cache: dict[tuple[str, str], Optional[str]],
) -> str:
    """Standardize on Courriels, reusing legacy Emails only if already populated."""
    if not od_dossier_id:
        return _CANONICAL_COURRIELS_SUBFOLDER
    courriels_id = _obtenir_sous_dossier_onedrive(
        encoded_upn, od_dossier_id, _CANONICAL_COURRIELS_SUBFOLDER, cache=cache, creer=False
    )
    if courriels_id:
        return _CANONICAL_COURRIELS_SUBFOLDER
    emails_id = _obtenir_sous_dossier_onedrive(
        encoded_upn, od_dossier_id, _LEGACY_EMAILS_SUBFOLDER, cache=cache, creer=False
    )
    if emails_id:
        return _LEGACY_EMAILS_SUBFOLDER
    return _CANONICAL_COURRIELS_SUBFOLDER


def _obtenir_sous_dossier_onedrive(
    encoded_upn: str,
    od_dossier_id: str,
    sous_dossier: str,
    *,
    cache: dict[tuple[str, str], Optional[str]],
    creer: bool,
) -> Optional[str]:
    if not sous_dossier:
        return od_dossier_id
    cache_key = (od_dossier_id, sous_dossier)
    if cache_key in cache and cache[cache_key] is not None:
        return cache[cache_key]
    segments = [seg for seg in sous_dossier.split("\\") if seg]
    current_id = od_dossier_id
    for seg in segments:
        if creer:
            item, _ = graph_courrier.courrier_creer_dossier_onedrive(
                current_id, seg, encoded_upn=encoded_upn
            )
            current_id = str(item.get("id") or "")
        else:
            existing = graph_courrier.courrier_lire_element_onedrive(
                f"/users/{encoded_upn}/drive/items/{quote(current_id, safe='')}:/{quote(seg, safe='')}",
                params={"$select": "id,name,folder"},
            )
            if existing is None:
                return None
            if not isinstance(existing.get("folder"), dict):
                raise _SaveMessageError(
                    "Un fichier OneDrive bloque le sous-dossier de classement."
                )
            current_id = str(existing.get("id") or "")
        if not current_id:
            raise _SaveMessageError("Identifiant de sous-dossier OneDrive manquant.")
    cache[cache_key] = current_id
    return current_id


def _extraire_quickxor_item(item: dict[str, Any]) -> str:
    file_obj = item.get("file")
    if not isinstance(file_obj, dict):
        return ""
    hashes = file_obj.get("hashes")
    if not isinstance(hashes, dict):
        return ""
    return str(hashes.get("quickXorHash") or "").strip()


def _item_correspond_empreinte(
    item: dict[str, Any],
    *,
    expected_size: int,
    expected_qxh: str,
) -> bool:
    raw_size = item.get("size")
    if not isinstance(raw_size, int) or isinstance(raw_size, bool):
        return False
    if raw_size != expected_size:
        return False
    actual_qxh = _extraire_quickxor_item(item)
    if not actual_qxh:
        return False
    return hmac.compare_digest(actual_qxh, expected_qxh)


def _verifier_onedrive_essai(
    encoded_upn: str,
    od_target_folder_id: Optional[str],
    nom_propre: str,
    *,
    max_chars: int,
    expected_size: int,
    expected_qxh: str,
) -> tuple[str, Optional[int], Optional[str]]:
    if not od_target_folder_id:
        return "prevu_cree", None, None
    encoded_parent = quote(od_target_folder_id, safe="")
    for idx in range(1, _CONFLICT_RETRY_MAX + 1):
        candidate = (
            nom_propre
            if idx == 1
            else suffixer_nom_conflit(nom_propre, idx, max_chars)
        )
        encoded_name = quote(candidate, safe="")
        existing = graph_courrier.courrier_lire_element_onedrive(
            f"/users/{encoded_upn}/drive/items/{encoded_parent}:/{encoded_name}",
            params={"$select": "id,name,size,file,folder"},
        )
        if existing is None:
            statut = "prevu_cree" if idx == 1 else "prevu_renomme"
            return statut, (idx if idx > 1 else None), None
        if isinstance(existing.get("folder"), dict):
            continue
        if _item_correspond_empreinte(
            existing, expected_size=expected_size, expected_qxh=expected_qxh
        ):
            return (
                "prevu_deja_present",
                (idx if idx > 1 else None),
                str(existing.get("id") or "") or None,
            )
    raise _SaveMessageError("Trop de conflits de nom consécutifs dans OneDrive.")


def _televerser_sur_onedrive(
    encoded_upn: str,
    od_target_folder_id: str,
    nom_propre: str,
    *,
    max_chars: int,
    staged: courrier_model.StagedCourrierBlob,
) -> tuple[str, Optional[int], str, str]:
    encoded_parent = quote(od_target_folder_id, safe="")
    for idx in range(1, _CONFLICT_RETRY_MAX + 1):
        candidate = (
            nom_propre
            if idx == 1
            else suffixer_nom_conflit(nom_propre, idx, max_chars)
        )
        encoded_name = quote(candidate, safe="")
        created: Optional[dict[str, Any]] = None

        if staged.size <= graph_courrier.ONEDRIVE_SIMPLE_MAX_BYTES:
            payload_bytes = courrier_model.read_staged_courrier_slice(
                staged, 0, staged.size
            )
            created = graph_courrier.courrier_deposer_petit_fichier_onedrive(
                od_target_folder_id,
                candidate,
                payload_bytes,
                encoded_upn=encoded_upn,
            )
        else:
            upload_url = graph_courrier.courrier_ouvrir_session_onedrive(
                od_target_folder_id,
                candidate,
                encoded_upn=encoded_upn,
            )
            if upload_url is not None:
                chunk_size = graph_courrier.ONEDRIVE_UPLOAD_CHUNK_BYTES
                offset = 0
                try:
                    while offset < staged.size:
                        step = min(chunk_size, staged.size - offset)
                        slice_bytes = courrier_model.read_staged_courrier_slice(
                            staged, offset, step
                        )
                        result = graph_courrier.courrier_televerser_tranche_onedrive(
                            upload_url,
                            slice_bytes,
                            start=offset,
                            total=staged.size,
                        )
                        offset += step
                        if offset >= staged.size:
                            if result is None:
                                raise _SaveMessageError(
                                    "Réponse finale manquante après téléversement OneDrive."
                                )
                            created = result
                except graph_courrier.CourrierGraphError as exc:
                    if exc.status_code == 409:
                        created = None
                    else:
                        raise

        if created is None:
            existing = graph_courrier.courrier_lire_element_onedrive(
                f"/users/{encoded_upn}/drive/items/{encoded_parent}:/{encoded_name}",
                params={"$select": "id,name,size,file,folder"},
            )
            if existing is None or isinstance(existing.get("folder"), dict):
                continue
            if _item_correspond_empreinte(
                existing,
                expected_size=staged.size,
                expected_qxh=staged.quickxorhash,
            ):
                item_id = str(existing.get("id") or "")
                if not item_id:
                    raise _SaveMessageError("Identifiant OneDrive existant manquant.")
                return "deja_present", (idx if idx > 1 else None), item_id, candidate
            continue

        item_id = str(created.get("id") or "")
        if not item_id or not _item_correspond_empreinte(
            created,
            expected_size=staged.size,
            expected_qxh=staged.quickxorhash,
        ):
            raise _SaveMessageError(
                "Contrôle d'intégrité OneDrive échoué (taille ou QuickXorHash différent)."
            )
        statut = "cree" if idx == 1 else "renomme"
        return statut, (idx if idx > 1 else None), item_id, candidate

    raise _SaveMessageError("Trop de conflits de nom consécutifs dans OneDrive.")


def _enregistrer_un_fichier(
    *,
    user_id: Optional[str],
    encoded_upn: str,
    graph_path: str,
    cle: str,
    slot: str,
    nom_propre: str,
    max_chars: int,
    dossier_id: str,
    dossier_numero: str,
    athena_folder_id: Optional[str],
    presumed_category: str,
    od_target_folder_id: Optional[str],
    essai: bool,
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    ext = (
        "." + nom_propre.rsplit(".", 1)[1].lower()
        if "." in nom_propre
        else ""
    )
    reserved_id = courrier_model.reserved_courrier_document_id(
        dossier_id, cle, slot
    )
    supported_athena = ext in document_model.ALLOWED_EXTENSIONS

    if essai:
        sha = hashlib.sha256()
        qxh = QuickXorHash()
        total = 0
        with graph_courrier.courrier_ouvrir_flux(
            graph_path,
            max_bytes=document_model.MAX_FILE_SIZE,
            immutable_id=True,
        ) as chunks:
            for chunk in chunks:
                sha.update(chunk)
                qxh.update(chunk)
                total += len(chunk)
        if total <= 0:
            raise _SaveMessageError("Le fichier reçu de Graph est vide.")
        sha_hex = sha.hexdigest()
        qxh_b64 = qxh.b64digest()
        statut_od, renomme_idx, existing_od_id = _verifier_onedrive_essai(
            encoded_upn,
            od_target_folder_id,
            nom_propre,
            max_chars=max_chars,
            expected_size=total,
            expected_qxh=qxh_b64,
        )
        if not supported_athena:
            statut_athena = "prevu_non_pris_en_charge"
            doc_id_out: Optional[str] = None
        else:
            existing_doc = document_model.get_document_strict(reserved_id)
            if (
                existing_doc is not None
                and existing_doc.get("dossier_id") == dossier_id
            ):
                statut_athena = "prevu_deja_present"
                doc_id_out = reserved_id
            else:
                statut_athena = "prevu_cree"
                doc_id_out = None
        result_entry = {
            "slot": slot,
            "sha256": sha_hex,
            "quickxorhash": qxh_b64,
            "taille": total,
            "statut_onedrive": statut_od,
            "renomme_index": renomme_idx,
            "onedrive_item_id": existing_od_id,
            "statut_athena": statut_athena,
            "document_id": doc_id_out,
        }
        return result_entry, None

    uid = storage_identity.require_uid(user_id)
    if not od_target_folder_id:
        raise _SaveMessageError("Dossier cible OneDrive introuvable.")

    staged: Optional[courrier_model.StagedCourrierBlob] = None
    try:
        with graph_courrier.courrier_ouvrir_flux(
            graph_path,
            max_bytes=document_model.MAX_FILE_SIZE,
            immutable_id=True,
        ) as chunks:
            staged = courrier_model.stage_courrier_bytes(
                chunks,
                user_id=uid,
                cle=cle,
                slot_token=_slot_token(slot),
                ext=ext or ".bin",
            )

        statut_od, renomme_idx, od_item_id, final_name = _televerser_sur_onedrive(
            encoded_upn,
            od_target_folder_id,
            nom_propre,
            max_chars=max_chars,
            staged=staged,
        )

        if not supported_athena:
            statut_athena = "non_pris_en_charge"
            doc_id_out = None
        else:
            existing_doc = document_model.get_document_strict(reserved_id)
            display_stem = final_name.rsplit(".", 1)[0] if "." in final_name else final_name
            doc_meta: dict[str, Any] = {
                "category": presumed_category,
                "display_name": display_stem,
            }
            if athena_folder_id:
                doc_meta["folder_id"] = athena_folder_id
            ingested, ingest_errors = document_model.ingest_blob_as_document(
                staged.blob,
                dossier_id,
                dossier_numero,
                final_name,
                doc_meta,
                uid,
                document_id=reserved_id,
                category_source="mcp",
            )
            if ingest_errors or ingested is None:
                msg = "; ".join(ingest_errors) if ingest_errors else "Échec du versement dans Athéna."
                raise _SaveMessageError(msg)
            statut_athena = "deja_present" if existing_doc is not None else "cree"
            doc_id_out = str(ingested.get("id") or reserved_id)

        result_entry = {
            "slot": slot,
            "sha256": staged.sha256,
            "quickxorhash": staged.quickxorhash,
            "taille": staged.size,
            "statut_onedrive": statut_od,
            "renomme_index": renomme_idx,
            "onedrive_item_id": od_item_id,
            "statut_athena": statut_athena,
            "document_id": doc_id_out,
        }
        registry_entry = {
            "slot": slot,
            "sha256": staged.sha256,
            "taille": staged.size,
            "onedrive_item_id": od_item_id,
            "document_id": doc_id_out or "",
            "statut_onedrive": statut_od,
            "statut_athena": statut_athena,
        }
        return result_entry, registry_entry
    finally:
        courrier_model.discard_staged_courrier_blob(staged)


def _enregistrer_un_courriel(
    item: dict[str, Any],
    *,
    user_id: Optional[str],
    encoded_upn: str,
    racine_id: str,
    od_by_numero: dict[str, list[dict[str, str]]],
    od_subfolder_cache: dict[tuple[str, str], Optional[str]],
    essai: bool,
) -> dict[str, Any]:
    cle = str(item.get("cle") or "").strip()
    msg_id = str(item.get("id") or "").strip()
    dossier_num = str(item.get("dossier") or "").strip()
    creer_od = bool(item.get("creer_dossier_onedrive", False))
    dossier_od_req = str(item.get("dossier_onedrive") or "").strip()
    eml_spec = item.get("eml")
    raw_pieces = item.get("pieces")

    if not courrier_model.is_valid_cle(cle):
        raise _SaveMessageError("Clé de courriel invalide (16 hexadécimaux attendus).")
    if not msg_id:
        raise _SaveMessageError("Identifiant Graph du courriel manquant.")
    if not _DOSSIER_STRICT_RE.match(dossier_num):
        raise _SaveMessageError("Numéro de dossier Athéna invalide (AAAA-NNN attendu).")
    cleaned_od_name = _clean_dossier_onedrive_name(dossier_od_req, dossier_num)
    if cleaned_od_name is None:
        raise _SaveMessageError("Nom du dossier OneDrive invalide pour ce numéro de dossier.")

    if eml_spec is None:
        eml_sous_dossier = _CANONICAL_COURRIELS_SUBFOLDER
    elif isinstance(eml_spec, dict):
        eml_sous_dossier = str(
            eml_spec.get("sous_dossier") or _CANONICAL_COURRIELS_SUBFOLDER
        ).strip()
        if eml_sous_dossier not in (_CANONICAL_COURRIELS_SUBFOLDER, _LEGACY_EMAILS_SUBFOLDER):
            raise _SaveMessageError("Sous-dossier de l'.eml non autorisé.")
    else:
        raise _SaveMessageError("Bloc `eml` invalide.")

    if raw_pieces is None:
        pieces_plan: list[dict[str, Any]] = []
    elif isinstance(raw_pieces, list):
        pieces_plan = [p for p in raw_pieces if isinstance(p, dict)]
        if len(pieces_plan) != len(raw_pieces):
            raise _SaveMessageError("Entrée de pièce jointe invalide.")
    else:
        raise _SaveMessageError("`pieces` doit être une liste.")

    seen_att_ids: set[str] = set()
    for p in pieces_plan:
        att_id = str(p.get("id") or "").strip()
        sous = str(p.get("sous_dossier") if p.get("sous_dossier") is not None else "")
        if not att_id:
            raise _SaveMessageError("Identifiant Graph de pièce jointe manquant.")
        if att_id in seen_att_ids:
            raise _SaveMessageError("Identifiant de pièce jointe en double dans le plan.")
        seen_att_ids.add(att_id)
        if sous not in CORRESPONDANCE_SOUS_DOSSIERS:
            raise _SaveMessageError("Sous-dossier de pièce jointe non autorisé.")

    dossier_doc = dossier_model.get_dossier_by_file_number(dossier_num)
    if dossier_doc is None or not str(dossier_doc.get("id") or ""):
        raise _SaveMessageError(f"Le dossier Athéna {dossier_num} est introuvable.")
    dossier_id = str(dossier_doc["id"])

    encoded_msg_id = quote(msg_id, safe="")
    try:
        msg_meta = graph_courrier.courrier_get(
            f"/users/{encoded_upn}/messages/{encoded_msg_id}",
            params={"$select": "id,internetMessageId,receivedDateTime,from,hasAttachments"},
            immutable_id=True,
        )
    except graph_courrier.CourrierGraphError as exc:
        if exc.status_code == 404:
            raise _SaveMessageError("Courriel introuvable dans Microsoft Graph.") from exc
        raise

    raw_imid = str(msg_meta.get("internetMessageId") or "").strip()
    if not raw_imid:
        raise _SaveMessageError("Identifiant internetMessageId absent de la réponse Graph.")
    if courrier_model.compute_cle(raw_imid) != cle:
        raise _SaveMessageError(
            "La clé ne correspond pas à l'identifiant du message (refus)."
        )

    recu_dt = _parse_utc_datetime(msg_meta.get("receivedDateTime"))
    if recu_dt is None:
        raise _SaveMessageError("Horodatage receivedDateTime Graph manquant ou invalide.")
    exp = _extract_expediteur(msg_meta)
    eml_filename = construire_nom_eml(
        dossier_num, recu_dt, exp["nom"], exp["adresse"]
    )

    graph_att_by_id: dict[str, dict[str, Any]] = {}
    if pieces_plan:
        att_res = graph_courrier.courrier_get(
            f"/users/{encoded_upn}/messages/{encoded_msg_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
            immutable_id=True,
        )
        for att in att_res.get("value") or []:
            if isinstance(att, dict) and att.get("id"):
                graph_att_by_id[str(att["id"])] = att

    resolved_pieces: list[dict[str, Any]] = []
    for idx_piece, p in enumerate(pieces_plan, start=1):
        att_id = str(p.get("id") or "").strip()
        graph_att = graph_att_by_id.get(att_id)
        if graph_att is None:
            raise _SaveMessageError("Pièce jointe introuvable sur ce courriel dans Graph.")
        odata_type = str(graph_att.get("@odata.type") or "")
        if odata_type in (
            "#microsoft.graph.referenceAttachment",
            "#microsoft.graph.itemAttachment",
        ):
            raise _SaveMessageError(
                "Ce type de pièce jointe ne peut pas être versé comme fichier."
            )
        rang = p.get("rang")
        rang_num = (
            rang
            if isinstance(rang, int) and not isinstance(rang, bool) and rang >= 1
            else idx_piece
        )
        clean_att_name, name_err = nettoyer_nom_piece(
            str(p.get("nom") or ""), str(graph_att.get("name") or "")
        )
        if name_err or not clean_att_name:
            raise _SaveMessageError(name_err or "Nom de pièce jointe invalide.")
        resolved_pieces.append({
            "slot": f"piece:{rang_num}",
            "att_id": att_id,
            "sous_dossier": str(p.get("sous_dossier") or ""),
            "nom": clean_att_name,
        })

    # Verify / create OneDrive root dossier folder
    existing_od_list = od_by_numero.get(dossier_num, [])
    if len(existing_od_list) > 1:
        raise _SaveMessageError(
            f"Plusieurs dossiers OneDrive portent déjà le numéro {dossier_num}."
        )
    dossier_od_cree = False
    od_dossier_id: Optional[str] = None
    if len(existing_od_list) == 1:
        existing_entry = existing_od_list[0]
        if (
            unicodedata.normalize("NFC", existing_entry["nom"]).strip().casefold()
            != unicodedata.normalize("NFC", cleaned_od_name).strip().casefold()
        ):
            raise _SaveMessageError(
                f"Le dossier OneDrive existant pour {dossier_num} ne correspond pas au nom du plan."
            )
        od_dossier_id = existing_entry["id"]
    else:
        if not creer_od:
            raise _SaveMessageError(
                f"Dossier OneDrive absent pour {dossier_num} (creer_dossier_onedrive est faux)."
            )
        dossier_od_cree = True
        if not essai:
            created_od, _ = graph_courrier.courrier_creer_dossier_onedrive(
                racine_id, cleaned_od_name, encoded_upn=encoded_upn
            )
            od_dossier_id = str(created_od.get("id") or "")
            if not od_dossier_id:
                raise _SaveMessageError("Identifiant du dossier OneDrive créé manquant.")
            od_by_numero.setdefault(dossier_num, []).append({
                "numero": dossier_num,
                "nom": str(created_od.get("name") or cleaned_od_name),
                "id": od_dossier_id,
            })

    effective_mail_od_sub = _resoudre_sous_dossier_courriel_onedrive(
        encoded_upn, od_dossier_id, od_subfolder_cache
    )

    # Ensure / check Athena target folders upfront
    needed_tree_keys: list[str] = ["courriels"]
    for rp in resolved_pieces:
        ext = "." + rp["nom"].rsplit(".", 1)[1].lower()
        if ext in document_model.ALLOWED_EXTENSIONS:
            tree_key, _ = CORRESPONDANCE_SOUS_DOSSIERS[rp["sous_dossier"]]
            if tree_key and tree_key not in needed_tree_keys:
                needed_tree_keys.append(tree_key)

    athena_folder_ids: dict[str, str] = {}
    if essai:
        plan_report, plan_errors = folder_model.plan_default_tree(dossier_id)
        if plan_report is None:
            raise _SaveMessageError(
                "; ".join(plan_errors) or "Lecture des dossiers Athéna impossible."
            )
        blocked_keys = {k for k, _reason in plan_report.blocked}
        for key in needed_tree_keys:
            if key in blocked_keys:
                raise _SaveMessageError(
                    f"Le dossier de classement Athéna « {key} » est bloqué."
                )
    else:
        for key in needed_tree_keys:
            folder_doc, folder_errors = folder_model.ensure_tree_folder(dossier_id, key)
            if folder_errors or folder_doc is None:
                raise _SaveMessageError(
                    "; ".join(folder_errors) or "Préparation du dossier Athéna impossible."
                )
            athena_folder_ids[key] = str(folder_doc["id"])

    # Resolve OneDrive subfolder IDs and queue files (.eml first, then pieces)
    file_jobs: list[dict[str, Any]] = [
        {
            "slot": "eml",
            "graph_path": f"/users/{encoded_upn}/messages/{encoded_msg_id}/$value",
            "nom_propre": eml_filename,
            "max_chars": EML_MAX_CHARS,
            "od_sub": effective_mail_od_sub,
            "athena_folder_id": athena_folder_ids.get("courriels"),
            "presumed_category": "correspondance",
        }
    ]
    for rp in resolved_pieces:
        raw_sub = rp["sous_dossier"]
        tree_key, presumed_cat = CORRESPONDANCE_SOUS_DOSSIERS[raw_sub]
        od_sub = (
            effective_mail_od_sub
            if raw_sub in (_CANONICAL_COURRIELS_SUBFOLDER, _LEGACY_EMAILS_SUBFOLDER)
            else raw_sub
        )
        file_jobs.append({
            "slot": rp["slot"],
            "graph_path": (
                f"/users/{encoded_upn}/messages/{encoded_msg_id}/attachments/"
                f"{quote(rp['att_id'], safe='')}/$value"
            ),
            "nom_propre": rp["nom"],
            "max_chars": PIECE_NOM_MAX_CHARS,
            "od_sub": od_sub,
            "athena_folder_id": (
                athena_folder_ids.get(tree_key) if tree_key else None
            ),
            "presumed_category": presumed_cat,
        })

    fichiers_out: list[dict[str, Any]] = []
    registry_rows: list[dict[str, Any]] = []
    for job in file_jobs:
        od_target_id: Optional[str] = None
        if od_dossier_id:
            od_target_id = _obtenir_sous_dossier_onedrive(
                encoded_upn,
                od_dossier_id,
                job["od_sub"],
                cache=od_subfolder_cache,
                creer=not essai,
            )
        f_out, reg_row = _enregistrer_un_fichier(
            user_id=user_id,
            encoded_upn=encoded_upn,
            graph_path=job["graph_path"],
            cle=cle,
            slot=job["slot"],
            nom_propre=job["nom_propre"],
            max_chars=job["max_chars"],
            dossier_id=dossier_id,
            dossier_numero=dossier_num,
            athena_folder_id=job["athena_folder_id"],
            presumed_category=job["presumed_category"],
            od_target_folder_id=od_target_id,
            essai=essai,
        )
        fichiers_out.append(f_out)
        if reg_row is not None:
            registry_rows.append(reg_row)

    if not essai:
        reg_doc, reg_errors = courrier_model.record_enregistre(
            cle,
            msg_id,
            dossier_id=dossier_id,
            dossier_numero=dossier_num,
            fichiers=registry_rows,
        )
        if reg_errors or reg_doc is None:
            raise _SaveMessageError(
                "; ".join(reg_errors) or "Échec d'inscription au registre Courrier."
            )

    return {
        "cle": cle,
        "statut": "ok",
        "dossier": dossier_num,
        "dossier_onedrive_cree": dossier_od_cree,
        "fichiers": fichiers_out,
        "erreur": None,
    }


def enregistrer_courriels(
    messages: list[dict[str, Any]],
    *,
    essai: bool = False,
    user_id: Optional[str] = None,
    clock: Callable[[], float] = time.monotonic,
    budget_secondes: float = BUDGET_SECONDES,
) -> dict[str, Any]:
    """Save approved Outlook emails & attachments into OneDrive, Athena, and registry."""
    if not Config.COURRIER_ACTIF:
        log_courrier_event(
            "courrier_enregistrement",
            "refused",
            essai=bool(essai),
            reason="switch_off",
        )
        raise PermissionError("Le module Courrier est désactivé (COURRIER_ACTIF=false).")
    if not Config.courrier_configured():
        raise graph_courrier.CourrierNotConfigured(
            "Le module Courrier n'est pas configuré."
        )
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES_SAVE:
        raise ValueError(
            f"`messages` doit contenir entre 1 et {MAX_MESSAGES_SAVE} éléments."
        )

    uid = storage_identity.require_uid(user_id) if not essai else user_id
    started = clock()
    encoded_upn = quote(Config.COURRIER_UPN, safe="")
    racine_onedrive = Config.COURRIER_ONEDRIVE_ROOT or "4 - Dossiers"

    od_carte = _lire_carte_onedrive(encoded_upn, racine_onedrive)
    racine_id = str(od_carte.get("racine_id") or "")
    if not racine_id:
        raise graph_courrier.CourrierGraphError(
            "Identifiant racine OneDrive introuvable."
        )
    od_by_numero: dict[str, list[dict[str, str]]] = {}
    for entry in od_carte.get("dossiers") or []:
        od_by_numero.setdefault(entry["numero"], []).append(dict(entry))
    od_subfolder_cache: dict[tuple[str, str], Optional[str]] = {}

    results: list[dict[str, Any]] = []
    traites = 0
    echecs = 0
    differes = 0

    for idx, item in enumerate(messages):
        if idx > 0 and (clock() - started) >= budget_secondes:
            for rem in messages[idx:]:
                rem_cle = str(rem.get("cle") or "").strip() if isinstance(rem, dict) else ""
                rem_dos = (
                    str(rem.get("dossier") or "").strip()
                    if isinstance(rem, dict)
                    else ""
                )
                results.append({
                    "cle": rem_cle,
                    "statut": "differe",
                    "dossier": rem_dos or None,
                    "dossier_onedrive_cree": False,
                    "fichiers": [],
                    "erreur": "Budget temps d'appel atteint : reprendre cet envoi.",
                })
                differes += 1
            break

        if not isinstance(item, dict):
            results.append({
                "cle": "",
                "statut": "echec",
                "dossier": None,
                "dossier_onedrive_cree": False,
                "fichiers": [],
                "erreur": "Élément de plan invalide.",
            })
            echecs += 1
            continue

        msg_cle = str(item.get("cle") or "").strip()
        msg_dos = str(item.get("dossier") or "").strip() or None
        try:
            res = _enregistrer_un_courriel(
                item,
                user_id=uid,
                encoded_upn=encoded_upn,
                racine_id=racine_id,
                od_by_numero=od_by_numero,
                od_subfolder_cache=od_subfolder_cache,
                essai=bool(essai),
            )
            results.append(res)
            traites += 1
        except _SaveMessageError as exc:
            results.append({
                "cle": msg_cle,
                "statut": "echec",
                "dossier": msg_dos,
                "dossier_onedrive_cree": False,
                "fichiers": [],
                "erreur": str(exc),
            })
            echecs += 1
        except graph_courrier.CourrierGraphError as exc:
            results.append({
                "cle": msg_cle,
                "statut": "echec",
                "dossier": msg_dos,
                "dossier_onedrive_cree": False,
                "fichiers": [],
                "erreur": graph_courrier.format_graph_error_fr(exc),
            })
            echecs += 1

    log_courrier_event(
        "courrier_enregistrement",
        "success" if echecs == 0 else "failure",
        essai=bool(essai),
        traites=traites,
        echecs=echecs,
        differes=differes,
    )
    return {
        "essai": bool(essai),
        "traites": traites,
        "echecs": echecs,
        "differes": differes,
        "messages": results,
    }


# ── Phase 4 : move_courriels (catégories + déplacement Outlook) ─────────


class _MoveMessageError(RuntimeError):
    """Per-message validation or Outlook classification failure (fixed French text)."""


def _clean_dossier_outlook_name(nom: Any, numero: str) -> Optional[str]:
    if not isinstance(nom, str):
        return None
    raw = nom.strip()
    if not raw or len(raw) > 150 or "/" in raw or "\\" in raw or ".." in raw:
        return None
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in raw):
        return None
    if not raw.startswith(f"{numero} "):
        return None
    return raw


def _compute_target_categories(
    raw_categories: Any,
    action: str,
) -> tuple[list[str], list[str]]:
    current = (
        [str(c) for c in raw_categories if isinstance(c, str)]
        if isinstance(raw_categories, list)
        else []
    )
    if action == "classer":
        wanted = graph_courrier.CATEGORIE_CLASSE
        removed_cf = graph_courrier.CATEGORIE_A_CLASSER.casefold()
    else:
        wanted = graph_courrier.CATEGORIE_A_CLASSER
        removed_cf = graph_courrier.CATEGORIE_CLASSE.casefold()
    wanted_cf = wanted.casefold()

    target: list[str] = []
    has_wanted = False
    for cat in current:
        cf = cat.strip().casefold()
        if cf == removed_cf:
            continue
        if cf == wanted_cf:
            has_wanted = True
        target.append(cat)
    if not has_wanted:
        target.append(wanted)
    return current, target


def _deplacer_un_courriel(
    item: dict[str, Any],
    *,
    encoded_upn: str,
    inbox_id: str,
    by_outlook_id: dict[str, dict[str, str]],
    by_numero: dict[str, list[dict[str, str]]],
    statut_folder_ids: dict[str, str],
    batch_state: dict[str, Any],
    avertissements: list[str],
    essai: bool,
) -> dict[str, Any]:
    cle = str(item.get("cle") or "").strip()
    msg_id = str(item.get("id") or "").strip()
    action = str(item.get("action") or "").strip()
    raw_dossier = item.get("dossier")
    dossier_num = str(raw_dossier).strip() if raw_dossier is not None else ""
    raw_outlook_id = item.get("outlook_id")
    outlook_id_req = str(raw_outlook_id).strip() if raw_outlook_id is not None else ""
    outlook_creer_raw = item.get("outlook_creer")

    if not courrier_model.is_valid_cle(cle):
        raise _MoveMessageError("Clé de courriel invalide (16 hexadécimaux attendus).")
    if not msg_id:
        raise _MoveMessageError("Identifiant Graph du courriel manquant.")
    if action not in VALID_MOVE_ACTIONS:
        raise _MoveMessageError("Action Outlook invalide (`classer` ou `laisser` attendu).")

    creer_spec: Optional[tuple[str, str]] = None
    if action == "laisser":
        if dossier_num or outlook_id_req or outlook_creer_raw is not None:
            raise _MoveMessageError(
                "L'action `laisser` n'accepte ni `dossier`, ni `outlook_id`, ni `outlook_creer`."
            )
    else:
        if not _DOSSIER_STRICT_RE.match(dossier_num):
            raise _MoveMessageError(
                "Numéro de dossier Athéna invalide (AAAA-NNN attendu)."
            )
        if outlook_id_req and outlook_creer_raw is not None:
            raise _MoveMessageError(
                "Ne fournissez pas simultanément `outlook_id` et `outlook_creer`."
            )
        if outlook_creer_raw is not None:
            if not isinstance(outlook_creer_raw, dict):
                raise _MoveMessageError("Bloc `outlook_creer` invalide.")
            statut_req = str(outlook_creer_raw.get("statut") or "").strip()
            if statut_req not in OUTLOOK_STATUTS:
                raise _MoveMessageError(
                    "Statut Outlook cible invalide (Actifs, Suspendu ou Services-conseils)."
                )
            cleaned_nom = _clean_dossier_outlook_name(
                outlook_creer_raw.get("nom"), dossier_num
            )
            if cleaned_nom is None:
                raise _MoveMessageError(
                    "Nom du dossier Outlook à créer invalide pour ce numéro de dossier."
                )
            creer_spec = (statut_req, cleaned_nom)

    msg_meta = graph_courrier.courrier_lire_courriel(msg_id, encoded_upn=encoded_upn)
    if msg_meta is None:
        return {
            "cle": cle,
            "action": action,
            "statut": "introuvable",
            "dossier": dossier_num or None,
            "statut_outlook_cible": None,
            "outlook_folder_id": None,
            "dossier_outlook_cree": False,
            "id_immuable_verifie": False,
            "erreur": None,
        }

    raw_imid = str(msg_meta.get("internetMessageId") or "").strip()
    if not raw_imid:
        raise _MoveMessageError("Identifiant internetMessageId absent de la réponse Graph.")
    if courrier_model.compute_cle(raw_imid) != cle:
        raise _MoveMessageError(
            "La clé ne correspond pas à l'identifiant du message (refus)."
        )

    reg_doc = courrier_model.get_courrier_message_strict(cle)
    if action == "classer":
        if (
            reg_doc is None
            or reg_doc.get("etat")
            not in (courrier_model.ETAT_ENREGISTRE, courrier_model.ETAT_CLASSE)
        ):
            raise _MoveMessageError("Enregistrement non constaté : déplacement refusé.")
        reg_num = str(reg_doc.get("dossier_numero") or "").strip()
        if reg_num and reg_num != dossier_num:
            raise _MoveMessageError(
                f"Le courriel a été enregistré sous le dossier {reg_num}, pas {dossier_num} : déplacement refusé."
            )
    else:
        if reg_doc is not None and reg_doc.get("etat") == courrier_model.ETAT_CLASSE:
            raise _MoveMessageError(
                "Ce courriel est déjà classé : action `laisser` refusée."
            )

    parent_fid = str(msg_meta.get("parentFolderId") or "").strip()
    if parent_fid != inbox_id:
        already_classified_here = (
            action == "classer"
            and reg_doc is not None
            and reg_doc.get("etat") == courrier_model.ETAT_CLASSE
            and bool(parent_fid)
            and parent_fid == str(reg_doc.get("outlook_folder_id") or "").strip()
            and parent_fid in by_outlook_id
        )
        if not already_classified_here:
            return {
                "cle": cle,
                "action": action,
                "statut": "introuvable",
                "dossier": dossier_num or None,
                "statut_outlook_cible": None,
                "outlook_folder_id": None,
                "dossier_outlook_cree": False,
                "id_immuable_verifie": False,
                "erreur": None,
            }

    target_folder_id: Optional[str] = None
    statut_cible: Optional[str] = None
    dossier_outlook_cree = False

    if action == "classer":
        if outlook_id_req:
            entry = by_outlook_id.get(outlook_id_req)
            if entry is None:
                raise _MoveMessageError(
                    "Dossier cible Outlook introuvable sous Archive > Dossiers."
                )
            if entry["numero"] != dossier_num:
                raise _MoveMessageError(
                    f"Le dossier Outlook cible ne porte pas le numéro {dossier_num}."
                )
            target_folder_id = entry["id"]
            statut_cible = entry["statut"]
        else:
            matches = by_numero.get(dossier_num, [])
            if len(matches) > 1:
                raise _MoveMessageError(
                    f"Plusieurs dossiers Outlook portent le numéro {dossier_num} ; précisez `outlook_id`."
                )
            if len(matches) == 1:
                existing_entry = matches[0]
                if creer_spec is not None:
                    statut_req, cleaned_nom = creer_spec
                    if (
                        existing_entry["statut"] != statut_req
                        or unicodedata.normalize("NFC", existing_entry["nom"]).strip().casefold()
                        != unicodedata.normalize("NFC", cleaned_nom).strip().casefold()
                    ):
                        raise _MoveMessageError(
                            f"Un dossier Outlook existe déjà pour {dossier_num} sous un autre nom ou statut."
                        )
                target_folder_id = existing_entry["id"]
                statut_cible = existing_entry["statut"]
            else:
                if creer_spec is None:
                    raise _MoveMessageError(
                        f"Aucun dossier Outlook trouvé pour {dossier_num} (fournissez `outlook_creer`)."
                    )
                statut_req, cleaned_nom = creer_spec
                statut_parent_id = statut_folder_ids.get(statut_req) or ""
                if not statut_parent_id:
                    raise _MoveMessageError(
                        f"Dossier parent Outlook « Archive > Dossiers > {statut_req} » introuvable."
                    )
                if essai:
                    target_folder_id = None
                    statut_cible = statut_req
                    dossier_outlook_cree = True
                else:
                    created_folder, was_created = graph_courrier.courrier_creer_dossier_outlook(
                        statut_parent_id, cleaned_nom, encoded_upn=encoded_upn
                    )
                    new_id = str(created_folder.get("id") or "").strip()
                    new_name = str(created_folder.get("displayName") or cleaned_nom).strip()
                    if not new_id or extraire_prefixe_dossier(new_name) != dossier_num:
                        raise _MoveMessageError(
                            "Réponse invalide à la création du dossier Outlook."
                        )
                    new_entry = {
                        "id": new_id,
                        "nom": new_name,
                        "numero": dossier_num,
                        "statut": statut_req,
                    }
                    by_outlook_id[new_id] = new_entry
                    by_numero.setdefault(dossier_num, []).append(new_entry)
                    target_folder_id = new_id
                    statut_cible = statut_req
                    dossier_outlook_cree = was_created

    current_cats, target_cats = _compute_target_categories(
        msg_meta.get("categories"), action
    )

    if essai:
        return {
            "cle": cle,
            "action": action,
            "statut": "ok",
            "dossier": dossier_num or None,
            "statut_outlook_cible": statut_cible,
            "outlook_folder_id": target_folder_id,
            "dossier_outlook_cree": dossier_outlook_cree,
            "id_immuable_verifie": False,
            "erreur": None,
        }

    if not batch_state["master_checked"]:
        batch_state["master_checked"] = True
        if not graph_courrier.courrier_assurer_categories_maitres(encoded_upn=encoded_upn):
            avertissements.append(
                "Catégories maîtres Outlook non vérifiables sans MailboxSettings.ReadWrite (HTTP 403) ; étiquettes appliquées directement aux courriels."
            )

    if target_cats != current_cats:
        graph_courrier.courrier_modifier_categories_courriel(
            msg_id, target_cats, encoded_upn=encoded_upn
        )

    if action == "classer":
        assert target_folder_id is not None
        if str(msg_meta.get("parentFolderId") or "") != target_folder_id:
            moved = graph_courrier.courrier_deplacer_courriel(
                msg_id, target_folder_id, encoded_upn=encoded_upn
            )
            if str(moved.get("id") or "") != msg_id:
                raise _MoveMessageError(
                    "L'identifiant Graph du courriel a changé après déplacement (ImmutableId non respecté)."
                )
        reg_written, reg_errors = courrier_model.record_classe(
            cle, msg_id, outlook_folder_id=target_folder_id
        )
        if reg_errors or reg_written is None:
            raise _MoveMessageError(
                "; ".join(reg_errors)
                or "Échec d'inscription du classement dans le registre Courrier."
            )
        return {
            "cle": cle,
            "action": action,
            "statut": "ok",
            "dossier": dossier_num,
            "statut_outlook_cible": statut_cible,
            "outlook_folder_id": target_folder_id,
            "dossier_outlook_cree": dossier_outlook_cree,
            "id_immuable_verifie": True,
            "erreur": None,
        }

    if reg_doc is None or reg_doc.get("etat") == courrier_model.ETAT_INVENTORIE:
        reg_written, reg_errors = courrier_model.record_inventorie(cle, msg_id)
        if reg_errors or reg_written is None:
            raise _MoveMessageError(
                "; ".join(reg_errors)
                or "Échec d'inscription du marquage dans le registre Courrier."
            )
    return {
        "cle": cle,
        "action": action,
        "statut": "ok",
        "dossier": None,
        "statut_outlook_cible": None,
        "outlook_folder_id": None,
        "dossier_outlook_cree": False,
        "id_immuable_verifie": False,
        "erreur": None,
    }


def deplacer_courriels(
    messages: list[dict[str, Any]],
    *,
    essai: bool = False,
    clock: Callable[[], float] = time.monotonic,
    budget_secondes: float = BUDGET_SECONDES,
) -> dict[str, Any]:
    """Classify saved Outlook emails under Archive > Dossiers or tag them for triage."""
    if not Config.COURRIER_ACTIF:
        log_courrier_event(
            "courrier_classement",
            "refused",
            essai=bool(essai),
            reason="switch_off",
        )
        raise PermissionError("Le module Courrier est désactivé (COURRIER_ACTIF=false).")
    if not Config.courrier_configured():
        raise graph_courrier.CourrierNotConfigured(
            "Le module Courrier n'est pas configuré."
        )
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES_MOVE:
        raise ValueError(
            f"`messages` doit contenir entre 1 et {MAX_MESSAGES_MOVE} éléments."
        )

    started = clock()
    encoded_upn = quote(Config.COURRIER_UPN, safe="")
    inbox_meta = graph_courrier.courrier_get(
        f"/users/{encoded_upn}/mailFolders/Inbox",
        params={"$select": "id"},
        immutable_id=True,
    )
    inbox_id = str(inbox_meta.get("id") or "").strip()
    if not inbox_id:
        raise graph_courrier.CourrierGraphError(
            "Identifiant de la boîte de réception Outlook introuvable."
        )
    needs_map = any(
        isinstance(m, dict) and str(m.get("action") or "").strip() == "classer"
        for m in messages
    )

    by_outlook_id: dict[str, dict[str, str]] = {}
    by_numero: dict[str, list[dict[str, str]]] = {}
    statut_folder_ids: dict[str, str] = {}
    avertissements: list[str] = []

    if needs_map:
        outlook_map, _folder_map, outlook_warnings = _lire_carte_outlook(encoded_upn)
        avertissements.extend(outlook_warnings)
        for statut, info in (outlook_map.get("statuts") or {}).items():
            statut_folder_ids[statut] = str(info.get("id") or "")
            for entry in info.get("dossiers") or []:
                fid = str(entry.get("id") or "")
                num = str(entry.get("numero") or "")
                nom = str(entry.get("nom") or "")
                if fid and num:
                    enriched = {
                        "id": fid,
                        "nom": nom,
                        "numero": num,
                        "statut": statut,
                    }
                    by_outlook_id[fid] = enriched
                    by_numero.setdefault(num, []).append(enriched)

    batch_state: dict[str, Any] = {"master_checked": False}
    results: list[dict[str, Any]] = []
    classes = 0
    laisses = 0
    introuvables = 0
    echecs = 0
    differes = 0

    for idx, item in enumerate(messages):
        if idx > 0 and (clock() - started) >= budget_secondes:
            for rem in messages[idx:]:
                rem_cle = str(rem.get("cle") or "").strip() if isinstance(rem, dict) else ""
                rem_action = str(rem.get("action") or "").strip() if isinstance(rem, dict) else ""
                rem_dos = (
                    str(rem.get("dossier") or "").strip()
                    if isinstance(rem, dict)
                    else ""
                )
                results.append({
                    "cle": rem_cle,
                    "action": rem_action,
                    "statut": "differe",
                    "dossier": rem_dos or None,
                    "statut_outlook_cible": None,
                    "outlook_folder_id": None,
                    "dossier_outlook_cree": False,
                    "id_immuable_verifie": False,
                    "erreur": "Budget temps d'appel atteint : reprendre cet envoi.",
                })
                differes += 1
            break

        if not isinstance(item, dict):
            results.append({
                "cle": "",
                "action": "",
                "statut": "echec",
                "dossier": None,
                "statut_outlook_cible": None,
                "outlook_folder_id": None,
                "dossier_outlook_cree": False,
                "id_immuable_verifie": False,
                "erreur": "Élément de plan invalide.",
            })
            echecs += 1
            continue

        msg_cle = str(item.get("cle") or "").strip()
        msg_action = str(item.get("action") or "").strip()
        msg_dos = str(item.get("dossier") or "").strip() or None
        try:
            res = _deplacer_un_courriel(
                item,
                encoded_upn=encoded_upn,
                inbox_id=inbox_id,
                by_outlook_id=by_outlook_id,
                by_numero=by_numero,
                statut_folder_ids=statut_folder_ids,
                batch_state=batch_state,
                avertissements=avertissements,
                essai=bool(essai),
            )
            results.append(res)
            if res["statut"] == "introuvable":
                introuvables += 1
            elif res["action"] == "classer":
                classes += 1
            else:
                laisses += 1
        except _MoveMessageError as exc:
            results.append({
                "cle": msg_cle,
                "action": msg_action,
                "statut": "echec",
                "dossier": msg_dos,
                "statut_outlook_cible": None,
                "outlook_folder_id": None,
                "dossier_outlook_cree": False,
                "id_immuable_verifie": False,
                "erreur": str(exc),
            })
            echecs += 1
        except graph_courrier.CourrierGraphError as exc:
            results.append({
                "cle": msg_cle,
                "action": msg_action,
                "statut": "echec",
                "dossier": msg_dos,
                "statut_outlook_cible": None,
                "outlook_folder_id": None,
                "dossier_outlook_cree": False,
                "id_immuable_verifie": False,
                "erreur": graph_courrier.format_graph_error_fr(exc),
            })
            echecs += 1

    log_courrier_event(
        "courrier_classement",
        "success" if echecs == 0 else "failure",
        essai=bool(essai),
        classes=classes,
        laisses=laisses,
        introuvables=introuvables,
        echecs=echecs,
        differes=differes,
    )
    return {
        "essai": bool(essai),
        "classes": classes,
        "laisses": laisses,
        "introuvables": introuvables,
        "echecs": echecs,
        "differes": differes,
        "messages": results,
        "avertissements": avertissements,
    }


# ── Phase 5 : record_courriels_temps (entrées de temps + lien atomique) ─


def inscrire_temps_courriels(
    entrees: list[dict[str, Any]],
    *,
    essai: bool = False,
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Validate and atomically record time entries linked to saved Courrier emails."""
    if not Config.COURRIER_ACTIF:
        log_courrier_event(
            "courrier_temps",
            "refused",
            essai=bool(essai),
            reason="switch_off",
        )
        raise PermissionError("Le module Courrier est désactivé (COURRIER_ACTIF=false).")

    rows, errors = courrier_model.record_courriels_temps(entrees, essai=bool(essai))
    if errors or rows is None:
        uncertain = concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR in errors
        log_courrier_event(
            "courrier_temps",
            "failure" if uncertain else "refused",
            essai=bool(essai),
            reason="uncertain" if uncertain else "validation_or_collision",
            demandees=len(entrees) if isinstance(entrees, list) else 0,
        )
        return None, errors

    courriels_lies = sum(len(r["cles"]) for r in rows)
    log_courrier_event(
        "courrier_temps",
        "success",
        essai=bool(essai),
        creees=len(rows),
        courriels_lies=courriels_lies,
    )
    return {
        "essai": bool(essai),
        "creees": len(rows),
        "courriels_lies": courriels_lies,
        "entrees": rows,
    }, []
