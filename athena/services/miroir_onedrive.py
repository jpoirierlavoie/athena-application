"""One-way OneDrive mirroring service (Athena -> OneDrive).

Mirrors Athena client documents, 18-folder trees, Admin/Trust receipts,
completed reconciliation statements, and active FR/EN Word templates into
OneDrive using Graph API (via ``utils.graph_courrier``), while keeping
Firestore + Firebase Storage as the sole source of truth.

Collections
-----------
``onedrive_miroir``
    Top-level bookkeeping collection (Architecture Rule 7 exception: no user
    provenance stamps, no ``etag``, no DAV UID) mapping each mirrored Athena
    entity to its OneDrive driveItem id, parent id, filename, and content hash
    so renames, moves, and asynchronous deletions stay idempotent even after
    the source Firestore document is removed.

Security & privacy boundaries
-----------------------------
* Strictly gated behind ``Config.MIROIR_ONEDRIVE_ACTIF`` and
  ``Config.courrier_configured()``.
* Identity-verification files uploaded on contacts (under
  ``users/*/parties/...``) and transient ``staging/...`` blobs are NEVER
  mirrored: every Storage read validates against an explicit allowlist of
  object path prefixes before any byte is fetched.
* Reconciliation bank statements are mirrored ONLY when
  ``status == "complétée"`` — draft reconciliations (``"brouillon"``) are
  never mirrored.
* Note on AST boundaries: this module intentionally reads plain
  reconciliation dictionaries directly and never imports or references
  excluded proof/statement gatekeepers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import quote

from client.config import TASKS_LOCATION
from config import Config
from firebase_admin import storage
from google.cloud.firestore_v1 import FieldFilter
from models import db
from models.document import _zip_component, _zip_entry_basename
from services.courrier import extraire_prefixe_dossier
from utils import graph_courrier
from utils.logging_setup import log_onedrive_mirror_event, sanitize_log_value
from utils.quickxorhash import quickxorhash_b64

logger = logging.getLogger(__name__)

MIROIR_COLLECTION = "onedrive_miroir"

CABINET_ROOT_NAME = "Cabinet"
COMPTABILITE_DIR = "Comptabilité"
GABARITS_DIR = "Gabarits"
TRUST_DIR = "Fidéicommis"
ADMIN_DIR = "Administration"
RECEIPTS_DIR = "Pièces justificatives"
STATEMENTS_DIR = "Relevés bancaires"

TEMPLATE_CATEGORY_DIRS: dict[str, str] = {
    "procédure": "Procédures",
    "correspondance": "Correspondance",
    "autre": "Autre",
}
TEMPLATE_LANG_DIRS: dict[str, str] = {
    "fr": "FR",
    "en": "EN",
}

CRON_BATCH_LIMIT = 50
MAX_MIRROR_BYTES = 100 * 1024 * 1024  # 100 MiB safety ceiling

_MIME_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

_UUID_SHORT_LEN = 8
_SUPPORTED_ACTIONS = frozenset({
    "sync_document",
    "delete_document",
    "sync_folder",
    "sync_dossier_tree",
    "delete_folder",
    "delete_dossier",
    "sync_template",
    "delete_template",
    "sync_receipt",
    "delete_receipt",
    "sync_reconciliation",
})


def is_active() -> bool:
    """True when OneDrive mirroring is enabled and Graph Courrier creds exist."""
    return bool(Config.MIROIR_ONEDRIVE_ACTIF and Config.courrier_configured())


@lru_cache(maxsize=1)
def _tasks_client():
    from google.cloud import tasks_v2

    return tasks_v2.CloudTasksClient()


# ── Post-commit Cloud Tasks dispatch (best-effort, never raises) ─────────────


def enqueue_event(action: str, **payload: Any) -> bool:
    """Enqueue an asynchronous mirror task onto Cloud Tasks.

    Best-effort post-commit hook: returns False and logs only ids/error class
    when disabled or when Cloud Tasks dispatch fails, so user writes never fail.
    """
    if action not in _SUPPORTED_ACTIONS:
        logger.warning("miroir_onedrive: unsupported action %r ignored", action)
        return False
    if not is_active():
        return False

    body: dict[str, Any] = {
        "action": action,
        "emis_at": datetime.now(timezone.utc).isoformat(),
        **{k: v for k, v in payload.items() if v is not None},
    }
    try:
        from google.cloud import tasks_v2

        client = _tasks_client()
        project_id = Config.FIREBASE_PROJECT_ID or os.environ.get("FIREBASE_PROJECT_ID", "")
        if not project_id:
            raise RuntimeError("FIREBASE_PROJECT_ID missing")
        parent = client.queue_path(project_id, TASKS_LOCATION, Config.MIROIR_ONEDRIVE_QUEUE)
        client.create_task(
            request={
                "parent": parent,
                "task": {
                    "app_engine_http_request": {
                        "http_method": tasks_v2.HttpMethod.POST,
                        "relative_uri": "/taches/onedrive/event",
                        "app_engine_routing": {"service": "default"},
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps(body, default=str).encode("utf-8"),
                    }
                },
            }
        )
        return True
    except Exception as exc:
        log_onedrive_mirror_event(
            "miroir_onedrive_dispatch_error",
            outcome="failure",
            action=action,
            error_type=type(exc).__name__,
        )
        logger.warning(
            "miroir_onedrive enqueue_event failed for action=%s: %s",
            action,
            type(exc).__name__,
        )
        return False


# ── Strict Storage Path Allowlist ────────────────────────────────────────────


def _is_allowed_storage_path(path: Any, kind: str) -> bool:
    """Strict allowlist validating GCS object paths before any read.

    Refuses identity uploads (``parties/``), ``staging/``, traversal segments,
    and mismatched prefixes so Loi 25 / Barreau purge guarantees stay single-store.
    """
    if not isinstance(path, str):
        return False
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        return False
    parts = cleaned.split("/")
    if len(parts) < 5 or parts[0] != "users" or not parts[1]:
        return False
    if "parties" in parts or "staging" in parts or "identite" in parts:
        return False

    if kind == "document":
        return len(parts) >= 6 and parts[2] == "dossiers" and parts[4] == "documents"
    if kind == "template":
        return len(parts) >= 5 and parts[2] == "templates"
    if kind == "admin_receipt":
        return len(parts) >= 5 and parts[2] == "administration" and parts[3] != "conciliations"
    if kind == "trust_receipt":
        return len(parts) >= 5 and parts[2] == "fideicommis" and parts[3] != "conciliations"
    if kind == "admin_statement":
        return len(parts) == 6 and parts[2] == "administration" and parts[3] == "conciliations"
    if kind == "trust_statement":
        return len(parts) == 6 and parts[2] == "fideicommis" and parts[3] == "conciliations"
    return False


# ── Pure naming & hash helpers ───────────────────────────────────────────────


def _item_key(kind: str, *parts: str) -> str:
    return ":".join((kind, *(str(p) for p in parts)))


def _disambiguate_filename(filename: str, entity_id: str) -> str:
    short = re.sub(r"[^a-zA-Z0-9_-]", "", entity_id)[:_UUID_SHORT_LEN] or "dup"
    suffix = f" ({short})"
    if "." in filename:
        stem, ext = filename.rsplit(".", 1)
        dot_ext = f".{ext}"
    else:
        stem, dot_ext = filename, ""
    max_stem = max(1, 140 - len(suffix) - len(dot_ext))
    return f"{stem[:max_stem].rstrip(' .')}{suffix}{dot_ext}"


def _safe_od_segment(raw: str, fallback: str = "element") -> str:
    cleaned = _zip_component(raw).strip()
    return cleaned or fallback


def _clean_filename(raw_name: str, *, ext_fallback: str = "", default_stem: str = "fichier") -> str:
    name = (raw_name or "").strip()
    ext = ""
    if "." in name:
        stem_part, ext_part = name.rsplit(".", 1)
        clean_ext = re.sub(r"[^a-zA-Z0-9]", "", ext_part)[:10].lower()
        if clean_ext:
            ext = f".{clean_ext}"
            stem = _safe_od_segment(stem_part, default_stem)
        else:
            stem = _safe_od_segment(name, default_stem)
    else:
        stem = _safe_od_segment(name, default_stem)
    if not ext and ext_fallback:
        ext = ext_fallback if ext_fallback.startswith(".") else f".{ext_fallback}"
    return f"{stem[:140]}{ext}"


def _extension_from(path: str, content_type: str = "") -> str:
    if isinstance(path, str) and "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
            return ext
    return _MIME_EXTENSIONS.get((content_type or "").split(";", 1)[0].strip().lower(), "")


def _utc_date(val: Any) -> Optional[date]:
    if isinstance(val, datetime):
        return (val.astimezone(timezone.utc) if val.tzinfo else val).date()
    if isinstance(val, date):
        return val
    return None


def _item_quickxorhash(item: Optional[dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    file_info = item.get("file")
    if not isinstance(file_info, dict):
        return ""
    hashes = file_info.get("hashes")
    if not isinstance(hashes, dict):
        return ""
    return str(hashes.get("quickXorHash") or "")


def _item_parent_id(item: Optional[dict[str, Any]]) -> str:
    if not isinstance(item, dict):
        return ""
    pref = item.get("parentReference")
    return str(pref.get("id") or "") if isinstance(pref, dict) else ""


def _item_matches_bytes(
    item: Optional[dict[str, Any]],
    *,
    expected_size: int,
    expected_qxh: str,
) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("file"), dict):
        return False
    size = item.get("size")
    if not isinstance(size, int) or size != expected_size:
        return False
    od_qxh = _item_quickxorhash(item)
    if not od_qxh:
        return False
    return od_qxh == expected_qxh


def _same_effective_name(
    session: "_MirrorSession",
    parent_id: str,
    target_name: str,
    current_name: str,
    entity_id: str,
) -> bool:
    """True if *current_name* already satisfies *target_name* (or its still-blocked fallback)."""
    if current_name == target_name:
        return True
    if current_name == _disambiguate_filename(target_name, entity_id):
        return session._get_child_by_path(parent_id, target_name) is not None
    return False


# ── Per-run OneDrive session cache & Graph wrappers ──────────────────────────


class _MirrorSession:
    """In-memory per-invocation cache avoiding redundant OneDrive folder calls."""

    def __init__(self) -> None:
        self.encoded_upn: str = quote(Config.COURRIER_UPN, safe="")
        self._root_cache: dict[str, dict[str, Any]] = {}
        self._children_cache: dict[str, list[dict[str, Any]]] = {}
        self._subfolder_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._dossier_root_cache: dict[str, dict[str, Any]] = {}
        self._dossier_folder_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._account_names: dict[tuple[str, str], str] = {}

    def invalidate_children(self, parent_id: str) -> None:
        self._children_cache.pop(parent_id, None)

    def _get_item_by_id(self, item_id: str) -> Optional[dict[str, Any]]:
        if not item_id:
            return None
        encoded_item = quote(item_id, safe="")
        return graph_courrier.courrier_lire_element_onedrive(
            f"/users/{self.encoded_upn}/drive/items/{encoded_item}",
            params={"$select": "id,name,size,folder,file,parentReference"},
        )

    def _get_child_by_path(self, parent_id: str, name: str) -> Optional[dict[str, Any]]:
        encoded_parent = quote(parent_id, safe="")
        encoded_name = quote(name, safe="")
        return graph_courrier.courrier_lire_element_onedrive(
            f"/users/{self.encoded_upn}/drive/items/{encoded_parent}:/{encoded_name}",
            params={"$select": "id,name,size,folder,file,parentReference"},
        )

    def list_children(self, parent_id: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or parent_id not in self._children_cache:
            self._children_cache[parent_id] = graph_courrier.courrier_lister_enfants_onedrive(
                parent_id,
                encoded_upn=self.encoded_upn,
                select="id,name,size,folder,file,parentReference",
            )
        return self._children_cache[parent_id]

    def ensure_path_root(self, rel_path: str) -> dict[str, Any]:
        """Resolve (or create segment-by-segment) a root-relative OneDrive folder."""
        cleaned = rel_path.strip().strip("/")
        if cleaned in self._root_cache:
            return self._root_cache[cleaned]

        encoded_rel = quote(cleaned, safe="/")
        existing = graph_courrier.courrier_lire_element_onedrive(
            f"/users/{self.encoded_upn}/drive/root:/{encoded_rel}",
            params={"$select": "id,name,folder"},
        )
        if existing is not None and isinstance(existing.get("folder"), dict):
            self._root_cache[cleaned] = existing
            return existing

        root_drive = graph_courrier.courrier_lire_element_onedrive(
            f"/users/{self.encoded_upn}/drive/root",
            params={"$select": "id,name,folder"},
        )
        if not root_drive or not root_drive.get("id"):
            raise graph_courrier.CourrierGraphError("Racine OneDrive introuvable.")

        current = root_drive
        for seg in [s for s in cleaned.split("/") if s]:
            current = self.ensure_child_folder(str(current["id"]), seg)
        self._root_cache[cleaned] = current
        return current

    def ensure_child_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        clean = _safe_od_segment(name, "Dossier")
        cache_key = (parent_id, clean)
        if cache_key in self._subfolder_cache:
            return self._subfolder_cache[cache_key]

        item, _created = graph_courrier.courrier_creer_dossier_onedrive(
            parent_id, clean, encoded_upn=self.encoded_upn
        )
        self._subfolder_cache[cache_key] = item
        self.invalidate_children(parent_id)
        return item

    def ensure_folder_segments(self, base_item: dict[str, Any], segments: list[str]) -> dict[str, Any]:
        current = base_item
        for seg in segments:
            current = self.ensure_child_folder(str(current["id"]), seg)
        return current

    def ensure_cabinet_subpath(self, segments: list[str]) -> dict[str, Any]:
        base = self.ensure_path_root(CABINET_ROOT_NAME)
        return self.ensure_folder_segments(base, segments)

    def ensure_dossier_root(self, dossier: dict[str, Any]) -> dict[str, Any]:
        dossier_id = str(dossier.get("id") or "")
        file_number = str(dossier.get("file_number") or "").strip()
        if not dossier_id or not file_number:
            raise ValueError("Dossier sans identifiant ou numéro.")
        if dossier_id in self._dossier_root_cache:
            return self._dossier_root_cache[dossier_id]

        book_key = _item_key("dossier", dossier_id)
        book = _get_mirror_record(book_key)
        if book and book.get("onedrive_item_id"):
            od_item = self._get_item_by_id(str(book["onedrive_item_id"]))
            if od_item is not None and isinstance(od_item.get("folder"), dict):
                stored_fn = str(book.get("file_number") or "")
                cur_name = str(od_item.get("name") or "")
                if stored_fn != file_number:
                    if stored_fn and cur_name.startswith(f"{stored_fn} — "):
                        desired = f"{file_number}{cur_name[len(stored_fn):]}"
                    elif not stored_fn and extraire_prefixe_dossier(cur_name) == file_number:
                        desired = cur_name
                    elif not stored_fn and extraire_prefixe_dossier(cur_name):
                        old_pref = extraire_prefixe_dossier(cur_name)
                        desired = (
                            f"{file_number}{cur_name[len(old_pref):]}"
                            if cur_name.startswith(f"{old_pref} — ")
                            else file_number
                        )
                    else:
                        desired = file_number
                    clean_desired = _safe_od_segment(desired, dossier_id[:8])
                    parent_od_id = _item_parent_id(od_item) or str(
                        self.ensure_path_root(Config.COURRIER_ONEDRIVE_ROOT)["id"]
                    )
                    if cur_name != clean_desired:
                        renamed = _move_or_rename_item(
                            self,
                            item_id=str(od_item["id"]),
                            target_parent_id=parent_od_id,
                            target_name=clean_desired,
                            fallback_disambig_id=dossier_id,
                        )
                        if renamed is not None and renamed.get("id"):
                            od_item = renamed
                            self.invalidate_children(parent_od_id)
                    _save_mirror_record(
                        book_key,
                        entity_type="dossier",
                        entity_id=dossier_id,
                        file_number=file_number,
                        onedrive_item_id=str(od_item["id"]),
                        parent_id=_item_parent_id(od_item) or parent_od_id,
                        name=str(od_item.get("name") or clean_desired),
                    )
                self._dossier_root_cache[dossier_id] = od_item
                return od_item

        dossiers_root = self.ensure_path_root(Config.COURRIER_ONEDRIVE_ROOT)
        root_id = str(dossiers_root["id"])
        children = self.list_children(root_id)
        matches = [
            it
            for it in children
            if isinstance(it.get("folder"), dict)
            and extraire_prefixe_dossier(str(it.get("name") or "")) == file_number
        ]
        if len(matches) > 1:
            raise graph_courrier.CourrierGraphError(
                "Plusieurs dossiers OneDrive partagent le même numéro de dossier.",
                reason="ambiguous_dossier_folder",
            )
        if len(matches) == 1:
            od_item = matches[0]
        else:
            folder_name = _safe_od_segment(file_number, dossier_id[:8])
            od_item, _ = graph_courrier.courrier_creer_dossier_onedrive(
                root_id, folder_name, encoded_upn=self.encoded_upn
            )
            self.invalidate_children(root_id)

        _save_mirror_record(
            book_key,
            entity_type="dossier",
            entity_id=dossier_id,
            file_number=file_number,
            onedrive_item_id=str(od_item["id"]),
            parent_id=root_id,
            name=str(od_item.get("name") or file_number),
        )
        self._dossier_root_cache[dossier_id] = od_item
        return od_item

    def account_name(self, register: str, account_id: str) -> str:
        key = (register, account_id)
        if key in self._account_names:
            return self._account_names[key]
        col = "trust_accounts" if register == "trust" else "admin_accounts"
        name = account_id
        if account_id:
            snap = db.collection(col).document(account_id).get()
            if snap.exists:
                data = snap.to_dict() or {}
                name = str(data.get("name") or account_id)
        clean = _safe_od_segment(name, account_id or "Compte")
        self._account_names[key] = clean
        return clean


# ── Bookkeeping persistence (``onedrive_miroir``) ─────────────────────────────


def _get_mirror_record(key: str) -> Optional[dict[str, Any]]:
    snap = db.collection(MIROIR_COLLECTION).document(key).get()
    return {**(snap.to_dict() or {}), "id": snap.id} if snap.exists else None


def _save_mirror_record(key: str, **fields: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = {
        "key": key,
        "updated_at": now,
        **fields,
    }
    db.collection(MIROIR_COLLECTION).document(key).set(payload, merge=True)
    return payload


def _delete_mirror_record(key: str) -> None:
    db.collection(MIROIR_COLLECTION).document(key).delete()


def _item_owned_by_other_mirror(
    item_id: str,
    *,
    current_key: str,
    entity_type: str,
) -> bool:
    if not item_id:
        return False
    snaps = (
        db.collection(MIROIR_COLLECTION)
        .where(filter=FieldFilter("onedrive_item_id", "==", item_id))
        .stream()
    )
    for snap in snaps:
        if snap.id == current_key:
            continue
        data = snap.to_dict() or {}
        if data.get("entity_type") == entity_type:
            return True
    return False


# ── Storage download & Graph upload helpers ──────────────────────────────────


def _download_storage_bytes(storage_path: str, *, kind: str) -> bytes:
    if not _is_allowed_storage_path(storage_path, kind):
        raise PermissionError("Storage path excluded by OneDrive mirror allowlist.")
    blob = storage.bucket().blob(storage_path)
    blob.reload()
    if blob.size is not None and int(blob.size) > MAX_MIRROR_BYTES:
        raise ValueError("File exceeds mirror size limit.")
    return blob.download_as_bytes()


def _upload_bytes(
    session: _MirrorSession,
    parent_id: str,
    filename: str,
    data: bytes,
    *,
    conflict_behavior: str = "fail",
) -> Optional[dict[str, Any]]:
    if len(data) <= graph_courrier.ONEDRIVE_SIMPLE_MAX_BYTES:
        return graph_courrier.courrier_deposer_petit_fichier_onedrive(
            parent_id,
            filename,
            data,
            encoded_upn=session.encoded_upn,
            conflict_behavior=conflict_behavior,
        )

    upload_url = graph_courrier.courrier_ouvrir_session_onedrive(
        parent_id,
        filename,
        encoded_upn=session.encoded_upn,
        conflict_behavior=conflict_behavior,
    )
    if upload_url is None:
        return None
    total = len(data)
    chunk_size = graph_courrier.ONEDRIVE_UPLOAD_CHUNK_BYTES
    completed: Optional[dict[str, Any]] = None
    try:
        for start in range(0, total, chunk_size):
            chunk = data[start : start + chunk_size]
            res = graph_courrier.courrier_televerser_tranche_onedrive(
                upload_url, chunk, start=start, total=total
            )
            if res is not None:
                completed = res
    except graph_courrier.CourrierGraphError as exc:
        if conflict_behavior == "fail" and (exc.status_code == 409 or exc.reason == "conflict"):
            return None
        raise
    if completed is None or not completed.get("id"):
        raise graph_courrier.CourrierGraphError(
            "La session de téléversement OneDrive s'est terminée sans confirmation."
        )
    return completed


def _upload_with_dedup_or_disambiguate(
    session: _MirrorSession,
    *,
    mirror_key: str,
    entity_type: str,
    entity_id: str,
    parent_id: str,
    target_name: str,
    data: bytes,
    existing_item: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], str]:
    """Upload *data* idempotently into *parent_id*/*target_name*.

    * If *existing_item* is owned by *mirror_key* at the target parent+name and
      its bytes differ, replaces in place.
    * Otherwise tries ``conflict_behavior="fail"`` first; on 409 conflict checks
      whether the occupant has identical QuickXorHash (and is not owned by
      another Athena mirror entry) to deduplicate without uploading twice, or
      disambiguates the filename using *entity_id* when colliding with another file.
    """
    qxh = quickxorhash_b64(data)
    size = len(data)

    if existing_item is not None and existing_item.get("id"):
        od_id = str(existing_item["id"])
        od_parent = _item_parent_id(existing_item)
        od_name = str(existing_item.get("name") or "")
        if _item_matches_bytes(existing_item, expected_size=size, expected_qxh=qxh):
            if od_parent == parent_id and _same_effective_name(
                session, parent_id, target_name, od_name, entity_id
            ):
                return existing_item, "unchanged"
            moved = _move_or_rename_item(
                session,
                item_id=od_id,
                target_parent_id=parent_id,
                target_name=target_name,
                fallback_disambig_id=entity_id,
            )
            if moved is not None:
                return moved, "updated"
        else:
            if od_parent == parent_id and _same_effective_name(
                session, parent_id, target_name, od_name, entity_id
            ):
                replace_name = od_name or target_name
                replaced = _upload_bytes(
                    session, parent_id, replace_name, data, conflict_behavior="replace"
                )
                if replaced is not None and replaced.get("id"):
                    return replaced, "replaced"
            # Location changed AND bytes changed: remove old copy cleanly then upload new
            graph_courrier.courrier_supprimer_element_onedrive(
                od_id, encoded_upn=session.encoded_upn
            )

    # First-time placement or recreated placement
    existing_at_target = session._get_child_by_path(parent_id, target_name)
    if existing_at_target is not None:
        occ_id = str(existing_at_target.get("id") or "")
        if (
            occ_id
            and _item_matches_bytes(existing_at_target, expected_size=size, expected_qxh=qxh)
            and not _item_owned_by_other_mirror(
                occ_id, current_key=mirror_key, entity_type=entity_type
            )
        ):
            return existing_at_target, "deduplicated"
        candidate_name = _disambiguate_filename(target_name, entity_id)
        created = _upload_bytes(
            session, parent_id, candidate_name, data, conflict_behavior="fail"
        )
        if created is None:
            created = _upload_bytes(
                session, parent_id, candidate_name, data, conflict_behavior="rename"
            )
        if not created or not created.get("id"):
            raise graph_courrier.CourrierGraphError("Échec du dépôt OneDrive (conflit).")
        return created, "uploaded"

    created = _upload_bytes(session, parent_id, target_name, data, conflict_behavior="fail")
    if created is not None and created.get("id"):
        return created, "uploaded"

    # Raced 409 on PUT/session: re-check occupant before falling back to disambiguation
    occupant = session._get_child_by_path(parent_id, target_name)
    if (
        occupant is not None
        and occupant.get("id")
        and _item_matches_bytes(occupant, expected_size=size, expected_qxh=qxh)
        and not _item_owned_by_other_mirror(
            str(occupant["id"]), current_key=mirror_key, entity_type=entity_type
        )
    ):
        return occupant, "deduplicated"
    candidate_name = _disambiguate_filename(target_name, entity_id)
    created = _upload_bytes(session, parent_id, candidate_name, data, conflict_behavior="rename")
    if not created or not created.get("id"):
        raise graph_courrier.CourrierGraphError("Échec du dépôt OneDrive après conflit.")
    return created, "uploaded"


def _move_or_rename_item(
    session: _MirrorSession,
    *,
    item_id: str,
    target_parent_id: str,
    target_name: str,
    fallback_disambig_id: str,
) -> Optional[dict[str, Any]]:
    try:
        return graph_courrier.courrier_mettre_a_jour_element_onedrive(
            item_id,
            encoded_upn=session.encoded_upn,
            name=target_name,
            parent_item_id=target_parent_id,
        )
    except graph_courrier.CourrierGraphError as exc:
        if exc.reason != "conflict":
            raise
        alt_name = _disambiguate_filename(target_name, fallback_disambig_id)
        return graph_courrier.courrier_mettre_a_jour_element_onedrive(
            item_id,
            encoded_upn=session.encoded_upn,
            name=alt_name,
            parent_item_id=target_parent_id,
        )


# ── Pre-placed Courrier / Teams item discovery ───────────────────────────────


def _find_preplaced_dossier_item_id(dossier_id: str, document_id: str) -> str:
    """Return OneDrive item id already placed by Courrier or Teams if present."""
    for snap in (
        db.collection("courrier_messages")
        .where(filter=FieldFilter("dossier_id", "==", dossier_id))
        .stream()
    ):
        data = snap.to_dict() or {}
        for f in data.get("fichiers") or []:
            if isinstance(f, dict) and f.get("document_id") == document_id:
                od_id = str(f.get("onedrive_item_id") or "").strip()
                if od_id:
                    return od_id

    for snap in (
        db.collection("meeting_transcripts")
        .where(filter=FieldFilter("dossier_id", "==", dossier_id))
        .stream()
    ):
        data = snap.to_dict() or {}
        vers = data.get("versement")
        if isinstance(vers, dict) and vers.get("transcript_document_id") == document_id:
            od_id = str(vers.get("onedrive_transcript_item_id") or "").strip()
            if od_id:
                return od_id
    return ""


# ── Dossier folder sync & delete ─────────────────────────────────────────────


def _load_dossier_folders(dossier_id: str) -> dict[str, dict[str, Any]]:
    folders: dict[str, dict[str, Any]] = {}
    for snap in (
        db.collection("folders")
        .where(filter=FieldFilter("dossier_id", "==", dossier_id))
        .stream()
    ):
        data = snap.to_dict() or {}
        fid = str(data.get("id") or snap.id)
        folders[fid] = {**data, "id": fid}
    return folders


def _sync_folder_chain(
    session: _MirrorSession,
    dossier: dict[str, Any],
    folder_id: Optional[str],
    *,
    folders_by_id: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Ensure the dossier root and every ancestor folder exist in OneDrive."""
    dossier_root = session.ensure_dossier_root(dossier)
    if not folder_id:
        return dossier_root

    dossier_id = str(dossier["id"])
    cached = session._dossier_folder_cache.get((dossier_id, str(folder_id)))
    if cached is not None:
        return cached

    if folders_by_id is None:
        folders_by_id = _load_dossier_folders(dossier_id)

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cur_id: Optional[str] = folder_id
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        f = folders_by_id.get(cur_id)
        if f is None:
            break
        chain.append(f)
        cur_id = f.get("parent_folder_id") or None
    chain.reverse()

    current_parent = dossier_root
    for f in chain:
        current_parent, _ = _sync_single_folder_with_outcome(
            session, dossier_id, f, current_parent
        )
    return current_parent


def _sync_single_folder_with_outcome(
    session: _MirrorSession,
    dossier_id: str,
    folder: dict[str, Any],
    parent_od_item: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    fid = str(folder["id"])
    parent_od_id = str(parent_od_item["id"])
    clean_name = _safe_od_segment(str(folder.get("name") or ""), fid[:8])
    cached = session._dossier_folder_cache.get((dossier_id, fid))
    if (
        cached is not None
        and _item_parent_id(cached) == parent_od_id
        and _same_effective_name(
            session, parent_od_id, clean_name, str(cached.get("name") or ""), fid
        )
    ):
        return cached, "unchanged"

    mirror_key = _item_key("folder", fid)
    record = _get_mirror_record(mirror_key)

    if record and record.get("onedrive_item_id"):
        od_id = str(record["onedrive_item_id"])
        od_item = session._get_item_by_id(od_id)
        if od_item is not None and isinstance(od_item.get("folder"), dict):
            cur_parent = _item_parent_id(od_item)
            cur_name = str(od_item.get("name") or "")
            outcome = "unchanged"
            if cur_parent != parent_od_id or not _same_effective_name(
                session, parent_od_id, clean_name, cur_name, fid
            ):
                updated = _move_or_rename_item(
                    session,
                    item_id=od_id,
                    target_parent_id=parent_od_id,
                    target_name=clean_name,
                    fallback_disambig_id=fid,
                )
                if updated is not None:
                    od_item = updated
                    session.invalidate_children(parent_od_id)
                    session.invalidate_children(cur_parent)
                    outcome = "updated"
            if outcome != "unchanged" or record.get("parent_id") != (
                _item_parent_id(od_item) or parent_od_id
            ) or record.get("name") != str(od_item.get("name") or clean_name):
                _save_mirror_record(
                    mirror_key,
                    entity_type="folder",
                    entity_id=fid,
                    dossier_id=dossier_id,
                    onedrive_item_id=str(od_item["id"]),
                    parent_id=_item_parent_id(od_item) or parent_od_id,
                    name=str(od_item.get("name") or clean_name),
                )
            session._dossier_folder_cache[(dossier_id, fid)] = od_item
            return od_item, outcome

    created, created_flag = graph_courrier.courrier_creer_dossier_onedrive(
        parent_od_id, clean_name, encoded_upn=session.encoded_upn
    )
    session.invalidate_children(parent_od_id)
    _save_mirror_record(
        mirror_key,
        entity_type="folder",
        entity_id=fid,
        dossier_id=dossier_id,
        onedrive_item_id=str(created["id"]),
        parent_id=parent_od_id,
        name=str(created.get("name") or clean_name),
    )
    session._dossier_folder_cache[(dossier_id, fid)] = created
    return created, ("created" if created_flag else "updated")


def _sync_single_folder_under_parent(
    session: _MirrorSession,
    dossier_id: str,
    folder: dict[str, Any],
    parent_od_item: dict[str, Any],
) -> dict[str, Any]:
    od_item, _ = _sync_single_folder_with_outcome(
        session, dossier_id, folder, parent_od_item
    )
    return od_item


def _sync_folder_internal(session: _MirrorSession, dossier_id: str, folder_id: str) -> str:
    if not dossier_id or not folder_id:
        return "skipped_invalid"
    d_snap = db.collection("dossiers").document(dossier_id).get()
    if not d_snap.exists:
        return "skipped_missing"
    dossier = {**(d_snap.to_dict() or {}), "id": d_snap.id}
    folders = _load_dossier_folders(dossier_id)
    if folder_id not in folders:
        return "skipped_missing"
    _sync_folder_chain(session, dossier, folder_id, folders_by_id=folders)
    return "synced"


def _sync_dossier_tree_internal(session: _MirrorSession, dossier_id: str) -> str:
    if not dossier_id:
        return "skipped_invalid"
    d_snap = db.collection("dossiers").document(dossier_id).get()
    if not d_snap.exists:
        return "skipped_missing"
    dossier = {**(d_snap.to_dict() or {}), "id": d_snap.id}
    session.ensure_dossier_root(dossier)
    folders = _load_dossier_folders(dossier_id)

    # Topological depth sort so parents always mirror before children
    depths: dict[str, int] = {}

    def _depth(fid: str, seen: set[str]) -> int:
        if fid in depths:
            return depths[fid]
        if fid in seen:
            return 999
        seen.add(fid)
        parent = (folders[fid].get("parent_folder_id") or "") if fid in folders else ""
        d = 0 if not parent or parent not in folders else 1 + _depth(parent, seen)
        depths[fid] = d
        return d

    for fid in sorted(folders, key=lambda x: _depth(x, set())):
        _sync_folder_chain(session, dossier, fid, folders_by_id=folders)
    return "synced"


def _delete_folder_internal(
    session: _MirrorSession,
    *,
    dossier_id: str,
    folder_ids: list[str],
    moved_document_ids: list[str],
    deleted_document_ids: list[str],
) -> str:
    # Step 1: Move surviving documents OUT of the deleted OneDrive subtree first.
    for doc_id in moved_document_ids or []:
        _sync_document_internal(session, str(doc_id))

    # Step 2: Delete removed documents from OneDrive.
    for doc_id in deleted_document_ids or []:
        _delete_document_internal(session, str(doc_id))

    # Step 3: Remove OneDrive subfolders deepest-first.
    for fid in reversed(folder_ids or []):
        session._dossier_folder_cache.pop((dossier_id, str(fid)), None)
        key = _item_key("folder", str(fid))
        rec = _get_mirror_record(key)
        if rec and rec.get("onedrive_item_id"):
            graph_courrier.courrier_supprimer_element_onedrive(
                str(rec["onedrive_item_id"]), encoded_upn=session.encoded_upn
            )
        if rec is not None:
            _delete_mirror_record(key)

    return "deleted"


def _delete_dossier_internal(
    session: _MirrorSession,
    dossier_id: str,
    folder_ids: list[str],
) -> str:
    if not dossier_id:
        return "skipped_invalid"
    session._dossier_root_cache.pop(dossier_id, None)
    for fid in reversed(folder_ids or []):
        session._dossier_folder_cache.pop((dossier_id, str(fid)), None)
        key = _item_key("folder", str(fid))
        rec = _get_mirror_record(key)
        if rec and rec.get("onedrive_item_id"):
            graph_courrier.courrier_supprimer_element_onedrive(
                str(rec["onedrive_item_id"]), encoded_upn=session.encoded_upn
            )
        if rec is not None:
            _delete_mirror_record(key)
    book_key = _item_key("dossier", dossier_id)
    book = _get_mirror_record(book_key)
    deleted_any = bool(folder_ids)
    if book and book.get("onedrive_item_id"):
        graph_courrier.courrier_supprimer_element_onedrive(
            str(book["onedrive_item_id"]), encoded_upn=session.encoded_upn
        )
        deleted_any = True
    if book is not None:
        _delete_mirror_record(book_key)
    return "deleted" if deleted_any else "skipped_unmapped"


# ── Dossier document sync & delete ───────────────────────────────────────────


def _sync_document_internal(session: _MirrorSession, document_id: str) -> str:
    if not document_id:
        return "skipped_invalid"
    doc_snap = db.collection("documents").document(document_id).get()
    if not doc_snap.exists:
        return "skipped_missing"
    doc = {**(doc_snap.to_dict() or {}), "id": doc_snap.id}
    dossier_id = str(doc.get("dossier_id") or "")
    storage_path = str(doc.get("storage_path") or "")
    if not dossier_id or not storage_path:
        return "skipped_missing"

    if not _is_allowed_storage_path(storage_path, "document"):
        log_onedrive_mirror_event(
            "miroir_onedrive_excluded_skipped",
            outcome="refused",
            reason="excluded_path",
            entity_type="document",
            entity_id=sanitize_log_value(document_id),
        )
        return "skipped_excluded"

    d_snap = db.collection("dossiers").document(dossier_id).get()
    if not d_snap.exists:
        return "skipped_missing"
    dossier = {**(d_snap.to_dict() or {}), "id": d_snap.id}

    target_parent_od = _sync_folder_chain(session, dossier, doc.get("folder_id") or None)
    target_parent_id = str(target_parent_od["id"])
    target_name = _zip_entry_basename(doc)
    mirror_key = _item_key("doc", document_id)
    record = _get_mirror_record(mirror_key)

    existing_item: Optional[dict[str, Any]] = None
    preplaced = False
    if record and record.get("onedrive_item_id"):
        existing_item = session._get_item_by_id(str(record["onedrive_item_id"]))
    else:
        preplaced_id = _find_preplaced_dossier_item_id(dossier_id, document_id)
        if preplaced_id:
            existing_item = session._get_item_by_id(preplaced_id)
            if existing_item is not None:
                preplaced = True

    doc_size = doc.get("file_size")
    if (
        existing_item is not None
        and (
            preplaced
            or (
                record
                and record.get("storage_path") == storage_path
                and isinstance(doc_size, int)
                and existing_item.get("size") == doc_size
            )
        )
    ):
        cur_parent = _item_parent_id(existing_item)
        cur_name = str(existing_item.get("name") or "")
        qxh = _item_quickxorhash(existing_item) or str((record or {}).get("quickxorhash") or "")
        if cur_parent == target_parent_id and (
            preplaced
            or _same_effective_name(session, target_parent_id, target_name, cur_name, document_id)
        ):
            _save_mirror_record(
                mirror_key,
                entity_type="document",
                entity_id=document_id,
                dossier_id=dossier_id,
                storage_path=storage_path,
                onedrive_item_id=str(existing_item["id"]),
                parent_id=cur_parent or target_parent_id,
                name=cur_name or target_name,
                quickxorhash=qxh,
            )
            return "deduplicated" if preplaced else "unchanged"

        # Fast rename/move without re-downloading GCS bytes
        moved = _move_or_rename_item(
            session,
            item_id=str(existing_item["id"]),
            target_parent_id=target_parent_id,
            target_name=target_name,
            fallback_disambig_id=document_id,
        )
        if moved is not None and moved.get("id"):
            _save_mirror_record(
                mirror_key,
                entity_type="document",
                entity_id=document_id,
                dossier_id=dossier_id,
                storage_path=storage_path,
                onedrive_item_id=str(moved["id"]),
                parent_id=_item_parent_id(moved) or target_parent_id,
                name=str(moved.get("name") or target_name),
                quickxorhash=_item_quickxorhash(moved) or qxh,
            )
            return "updated"
        existing_item = None

    data = _download_storage_bytes(storage_path, kind="document")
    qxh = quickxorhash_b64(data)
    item, outcome = _upload_with_dedup_or_disambiguate(
        session,
        mirror_key=mirror_key,
        entity_type="document",
        entity_id=document_id,
        parent_id=target_parent_id,
        target_name=target_name,
        data=data,
        existing_item=existing_item,
    )
    _save_mirror_record(
        mirror_key,
        entity_type="document",
        entity_id=document_id,
        dossier_id=dossier_id,
        storage_path=storage_path,
        onedrive_item_id=str(item["id"]),
        parent_id=_item_parent_id(item) or target_parent_id,
        name=str(item.get("name") or target_name),
        quickxorhash=_item_quickxorhash(item) or qxh,
    )
    return outcome


def _delete_document_internal(
    session: _MirrorSession,
    document_id: str,
    *,
    onedrive_item_id: Optional[str] = None,
) -> str:
    if not document_id:
        return "skipped_invalid"
    mirror_key = _item_key("doc", document_id)
    record = _get_mirror_record(mirror_key)
    target_id = str(
        (record or {}).get("onedrive_item_id") or onedrive_item_id or ""
    ).strip()
    if target_id:
        graph_courrier.courrier_supprimer_element_onedrive(
            target_id, encoded_upn=session.encoded_upn
        )
    if record is not None:
        _delete_mirror_record(mirror_key)
    return "deleted" if target_id else "skipped_unmapped"


# ── Templates (``OneDrive/Cabinet/Gabarits/{FR|EN}/{category}/...``) ───────────


def _sync_template_stream(
    session: _MirrorSession,
    tpl: dict[str, Any],
    *,
    lang: str,
) -> str:
    tpl_id = str(tpl["id"])
    storage_key = "en_storage_path" if lang == "en" else "storage_path"
    sha_key = "en_sha256" if lang == "en" else "sha256"
    storage_path = str(tpl.get(storage_key) or "")
    mirror_key = _item_key("template", tpl_id, lang)
    record = _get_mirror_record(mirror_key)

    if not storage_path:
        if record and record.get("onedrive_item_id"):
            graph_courrier.courrier_supprimer_element_onedrive(
                str(record["onedrive_item_id"]), encoded_upn=session.encoded_upn
            )
            _delete_mirror_record(mirror_key)
            return "deleted"
        return "skipped_no_stream"

    if not _is_allowed_storage_path(storage_path, "template"):
        log_onedrive_mirror_event(
            "miroir_onedrive_excluded_skipped",
            outcome="refused",
            reason="excluded_path",
            entity_type="template",
            entity_id=sanitize_log_value(tpl_id),
        )
        return "skipped_excluded"

    lang_dir = TEMPLATE_LANG_DIRS[lang]
    cat_dir = TEMPLATE_CATEGORY_DIRS.get(str(tpl.get("category") or "autre"), "Autre")
    target_folder = session.ensure_cabinet_subpath([GABARITS_DIR, lang_dir, cat_dir])
    target_parent_id = str(target_folder["id"])
    clean_stem = _safe_od_segment(str(tpl.get("name") or ""), tpl_id[:8])
    target_name = f"{clean_stem[:140]}.docx"
    sha256 = str(tpl.get(sha_key) or "")

    existing_item: Optional[dict[str, Any]] = None
    if record and record.get("onedrive_item_id"):
        existing_item = session._get_item_by_id(str(record["onedrive_item_id"]))

    if existing_item is not None and record and sha256 and record.get("sha256") == sha256:
        cur_parent = _item_parent_id(existing_item)
        cur_name = str(existing_item.get("name") or "")
        if cur_parent == target_parent_id and _same_effective_name(
            session, target_parent_id, target_name, cur_name, tpl_id
        ):
            return "unchanged"
        moved = _move_or_rename_item(
            session,
            item_id=str(existing_item["id"]),
            target_parent_id=target_parent_id,
            target_name=target_name,
            fallback_disambig_id=tpl_id,
        )
        if moved is not None and moved.get("id"):
            _save_mirror_record(
                mirror_key,
                entity_type="template",
                entity_id=tpl_id,
                lang=lang,
                storage_path=storage_path,
                sha256=sha256,
                onedrive_item_id=str(moved["id"]),
                parent_id=_item_parent_id(moved) or target_parent_id,
                name=str(moved.get("name") or target_name),
                quickxorhash=_item_quickxorhash(moved) or str(record.get("quickxorhash") or ""),
            )
            return "updated"
        existing_item = None

    data = _download_storage_bytes(storage_path, kind="template")
    computed_sha = sha256 or hashlib.sha256(data).hexdigest()
    qxh = quickxorhash_b64(data)
    item, outcome = _upload_with_dedup_or_disambiguate(
        session,
        mirror_key=mirror_key,
        entity_type="template",
        entity_id=tpl_id,
        parent_id=target_parent_id,
        target_name=target_name,
        data=data,
        existing_item=existing_item,
    )
    _save_mirror_record(
        mirror_key,
        entity_type="template",
        entity_id=tpl_id,
        lang=lang,
        storage_path=storage_path,
        sha256=computed_sha,
        onedrive_item_id=str(item["id"]),
        parent_id=_item_parent_id(item) or target_parent_id,
        name=str(item.get("name") or target_name),
        quickxorhash=_item_quickxorhash(item) or qxh,
    )
    return outcome


def _sync_template_internal(session: _MirrorSession, template_id: str) -> str:
    if not template_id:
        return "skipped_invalid"
    snap = db.collection("doc_templates").document(template_id).get()
    if not snap.exists:
        return "skipped_missing"
    tpl = {**(snap.to_dict() or {}), "id": snap.id}
    outcomes = [_sync_template_stream(session, tpl, lang=lang) for lang in ("fr", "en")]
    return "synced" if any(o != "unchanged" and not o.startswith("skipped") for o in outcomes) else outcomes[0]


def _delete_template_internal(session: _MirrorSession, template_id: str) -> str:
    if not template_id:
        return "skipped_invalid"
    deleted_any = False
    for lang in ("fr", "en"):
        mirror_key = _item_key("template", template_id, lang)
        rec = _get_mirror_record(mirror_key)
        if rec and rec.get("onedrive_item_id"):
            graph_courrier.courrier_supprimer_element_onedrive(
                str(rec["onedrive_item_id"]), encoded_upn=session.encoded_upn
            )
            deleted_any = True
        if rec is not None:
            _delete_mirror_record(mirror_key)
    return "deleted" if deleted_any else "skipped_unmapped"


# ── Accounting receipts & completed reconciliations ──────────────────────────


def _sync_receipt_internal(session: _MirrorSession, register: str, tx_id: str) -> str:
    if register not in ("admin", "trust") or not tx_id:
        return "skipped_invalid"
    col = "admin_transactions" if register == "admin" else "trust_transactions"
    snap = db.collection(col).document(tx_id).get()
    if not snap.exists:
        return "skipped_missing"
    tx = {**(snap.to_dict() or {}), "id": snap.id}
    storage_path = str(tx.get("receipt_storage_path") or "")
    if not storage_path:
        return "skipped_no_receipt"

    kind = f"{register}_receipt"
    if not _is_allowed_storage_path(storage_path, kind):
        log_onedrive_mirror_event(
            "miroir_onedrive_excluded_skipped",
            outcome="refused",
            reason="excluded_path",
            entity_type="receipt",
            entity_id=sanitize_log_value(tx_id),
        )
        return "skipped_excluded"

    reg_dir = ADMIN_DIR if register == "admin" else TRUST_DIR
    account_id = str(tx.get("account_id") or "")
    account_dir = session.account_name(register, account_id)
    tx_day = _utc_date(tx.get("date"))
    month_dir = tx_day.strftime("%Y-%m") if tx_day else "sans-date"

    target_folder = session.ensure_cabinet_subpath(
        [COMPTABILITE_DIR, reg_dir, RECEIPTS_DIR, account_dir, month_dir]
    )
    target_parent_id = str(target_folder["id"])
    seq = tx.get("sequence")
    prefix = f"{int(seq):04d}" if isinstance(seq, int) and seq >= 0 else tx_id[:_UUID_SHORT_LEN]
    ext = _extension_from(storage_path, str(tx.get("receipt_file_type") or ""))
    clean_receipt = _clean_filename(
        str(tx.get("receipt_filename") or "recu"),
        ext_fallback=ext,
        default_stem="recu",
    )
    target_name = f"{prefix}_{clean_receipt}"

    mirror_key = _item_key("receipt", register, tx_id)
    record = _get_mirror_record(mirror_key)
    existing_item: Optional[dict[str, Any]] = None
    if record and record.get("onedrive_item_id"):
        existing_item = session._get_item_by_id(str(record["onedrive_item_id"]))

    if (
        existing_item is not None
        and record
        and record.get("storage_path") == storage_path
        and _item_parent_id(existing_item) == target_parent_id
        and _same_effective_name(
            session,
            target_parent_id,
            target_name,
            str(existing_item.get("name") or ""),
            tx_id,
        )
    ):
        return "unchanged"

    data = _download_storage_bytes(storage_path, kind=kind)
    qxh = quickxorhash_b64(data)
    item, outcome = _upload_with_dedup_or_disambiguate(
        session,
        mirror_key=mirror_key,
        entity_type="receipt",
        entity_id=tx_id,
        parent_id=target_parent_id,
        target_name=target_name,
        data=data,
        existing_item=existing_item,
    )
    _save_mirror_record(
        mirror_key,
        entity_type="receipt",
        entity_id=tx_id,
        register=register,
        storage_path=storage_path,
        onedrive_item_id=str(item["id"]),
        parent_id=_item_parent_id(item) or target_parent_id,
        name=str(item.get("name") or target_name),
        quickxorhash=_item_quickxorhash(item) or qxh,
    )
    return outcome


def _delete_receipt_internal(session: _MirrorSession, register: str, tx_id: str) -> str:
    if register not in ("admin", "trust") or not tx_id:
        return "skipped_invalid"
    mirror_key = _item_key("receipt", register, tx_id)
    record = _get_mirror_record(mirror_key)
    target_id = str((record or {}).get("onedrive_item_id") or "").strip()
    if target_id:
        graph_courrier.courrier_supprimer_element_onedrive(
            target_id, encoded_upn=session.encoded_upn
        )
    if record is not None:
        _delete_mirror_record(mirror_key)
    return "deleted" if target_id else "skipped_unmapped"


def _delete_reconciliation_internal(
    session: _MirrorSession, register: str, rec_id: str
) -> str:
    if register not in ("admin", "trust") or not rec_id:
        return "skipped_invalid"
    mirror_key = _item_key("statement", register, rec_id)
    record = _get_mirror_record(mirror_key)
    target_id = str((record or {}).get("onedrive_item_id") or "").strip()
    if target_id:
        graph_courrier.courrier_supprimer_element_onedrive(
            target_id, encoded_upn=session.encoded_upn
        )
    if record is not None:
        _delete_mirror_record(mirror_key)
    return "deleted" if target_id else "skipped_unmapped"


def _sync_reconciliation_internal(
    session: _MirrorSession, register: str, rec_id: str
) -> str:
    """Mirror a reconciliation bank statement ONLY when status == 'complétée'."""
    if register not in ("admin", "trust") or not rec_id:
        return "skipped_invalid"
    col = "admin_reconciliations" if register == "admin" else "trust_reconciliations"
    snap = db.collection(col).document(rec_id).get()
    if not snap.exists:
        return "skipped_missing"
    rec = {**(snap.to_dict() or {}), "id": snap.id}

    # Fail-closed exclusion: drafts ("brouillon") MUST NEVER be mirrored.
    if rec.get("status") != "complétée":
        return "skipped_not_completed"

    storage_path = str(rec.get("statement_storage_path") or "")
    if not storage_path:
        return "skipped_no_statement"

    kind = f"{register}_statement"
    if not _is_allowed_storage_path(storage_path, kind):
        log_onedrive_mirror_event(
            "miroir_onedrive_excluded_skipped",
            outcome="refused",
            reason="excluded_path",
            entity_type="reconciliation",
            entity_id=sanitize_log_value(rec_id),
        )
        return "skipped_excluded"

    reg_dir = ADMIN_DIR if register == "admin" else TRUST_DIR
    account_id = str(rec.get("account_id") or "")
    account_dir = session.account_name(register, account_id)
    period_day = _utc_date(rec.get("period_end"))
    year_dir = period_day.strftime("%Y") if period_day else "sans-annee"
    day_stamp = period_day.strftime("%Y-%m-%d") if period_day else "sans-date"
    ext = _extension_from(storage_path, str(rec.get("statement_file_type") or "")) or ".pdf"
    target_name = f"releve-{day_stamp}-{rec_id[:_UUID_SHORT_LEN]}{ext}"

    target_folder = session.ensure_cabinet_subpath(
        [COMPTABILITE_DIR, reg_dir, STATEMENTS_DIR, account_dir, year_dir]
    )
    target_parent_id = str(target_folder["id"])

    mirror_key = _item_key("statement", register, rec_id)
    record = _get_mirror_record(mirror_key)
    statement_id = str(rec.get("statement_id") or "")
    existing_item: Optional[dict[str, Any]] = None
    if record and record.get("onedrive_item_id"):
        existing_item = session._get_item_by_id(str(record["onedrive_item_id"]))

    if (
        existing_item is not None
        and record
        and record.get("storage_path") == storage_path
        and record.get("statement_id") == statement_id
        and _item_parent_id(existing_item) == target_parent_id
        and _same_effective_name(
            session,
            target_parent_id,
            target_name,
            str(existing_item.get("name") or ""),
            rec_id,
        )
    ):
        return "unchanged"

    data = _download_storage_bytes(storage_path, kind=kind)
    qxh = quickxorhash_b64(data)
    item, outcome = _upload_with_dedup_or_disambiguate(
        session,
        mirror_key=mirror_key,
        entity_type="reconciliation",
        entity_id=rec_id,
        parent_id=target_parent_id,
        target_name=target_name,
        data=data,
        existing_item=existing_item,
    )
    _save_mirror_record(
        mirror_key,
        entity_type="reconciliation",
        entity_id=rec_id,
        register=register,
        storage_path=storage_path,
        statement_id=statement_id,
        onedrive_item_id=str(item["id"]),
        parent_id=_item_parent_id(item) or target_parent_id,
        name=str(item.get("name") or target_name),
        quickxorhash=_item_quickxorhash(item) or qxh,
    )
    return outcome


# ── Event dispatcher & cron catch-up sweep ───────────────────────────────────


_ACTION_LOG_EVENTS: dict[str, str] = {
    "sync_document": "miroir_onedrive_doc_sync",
    "delete_document": "miroir_onedrive_doc_delete",
    "sync_folder": "miroir_onedrive_folder_sync",
    "sync_dossier_tree": "miroir_onedrive_folder_sync",
    "delete_folder": "miroir_onedrive_folder_delete",
    "delete_dossier": "miroir_onedrive_folder_delete",
    "sync_template": "miroir_onedrive_template_sync",
    "delete_template": "miroir_onedrive_template_delete",
    "sync_receipt": "miroir_onedrive_receipt_sync",
    "delete_receipt": "miroir_onedrive_receipt_sync",
    "sync_reconciliation": "miroir_onedrive_statement_sync",
}


def _doc_sync_log_action(outcome: str) -> str:
    if outcome in ("uploaded", "replaced"):
        return "uploaded"
    if outcome == "deduplicated":
        return "dedup_hash"
    if outcome == "updated":
        return "moved_or_renamed"
    return "noop"


def _folder_sync_log_action(outcome: str) -> str:
    if outcome == "created":
        return "created"
    if outcome in ("updated", "synced"):
        return "updated"
    return "noop"


def handle_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one asynchronous Cloud Tasks mirror event."""
    action = str((payload or {}).get("action") or "")
    if action not in _SUPPORTED_ACTIONS:
        log_onedrive_mirror_event(
            "miroir_onedrive_dispatch_error",
            outcome="refused",
            reason="unknown_action",
            action=sanitize_log_value(action or "unknown"),
        )
        return {"ok": False, "outcome": "refused", "reason": "unknown_action"}
    if not is_active():
        return {"ok": True, "outcome": "disabled"}

    session = _MirrorSession()
    log_fields: dict[str, Any] = {}
    if action == "sync_document":
        doc_id = str(payload.get("document_id") or "")
        outcome = _sync_document_internal(session, doc_id)
        rec = _get_mirror_record(_item_key("doc", doc_id)) if doc_id else None
        log_fields = {
            "document_id": sanitize_log_value(doc_id),
            "dossier_id": sanitize_log_value(str((rec or {}).get("dossier_id") or "")),
            "action": _doc_sync_log_action(outcome),
        }
    elif action == "delete_document":
        doc_id = str(payload.get("document_id") or "")
        rec = _get_mirror_record(_item_key("doc", doc_id)) if doc_id else None
        outcome = _delete_document_internal(
            session,
            doc_id,
            onedrive_item_id=payload.get("onedrive_item_id"),
        )
        log_fields = {
            "document_id": sanitize_log_value(doc_id),
            "dossier_id": sanitize_log_value(str((rec or {}).get("dossier_id") or "")),
            "deleted": outcome == "deleted",
        }
    elif action == "sync_folder":
        dossier_id = str(payload.get("dossier_id") or "")
        folder_id = str(payload.get("folder_id") or "")
        outcome = _sync_folder_internal(session, dossier_id, folder_id)
        log_fields = {
            "folder_id": sanitize_log_value(folder_id),
            "dossier_id": sanitize_log_value(dossier_id),
            "action": _folder_sync_log_action(outcome),
        }
    elif action == "sync_dossier_tree":
        dossier_id = str(payload.get("dossier_id") or "")
        outcome = _sync_dossier_tree_internal(session, dossier_id)
        log_fields = {
            "folder_id": "",
            "dossier_id": sanitize_log_value(dossier_id),
            "action": _folder_sync_log_action(outcome),
        }
    elif action == "delete_folder":
        dossier_id = str(payload.get("dossier_id") or "")
        fids = [str(x) for x in (payload.get("folder_ids") or [])]
        moved = [str(x) for x in (payload.get("moved_document_ids") or [])]
        deleted = [str(x) for x in (payload.get("deleted_document_ids") or [])]
        outcome = _delete_folder_internal(
            session,
            dossier_id=dossier_id,
            folder_ids=fids,
            moved_document_ids=moved,
            deleted_document_ids=deleted,
        )
        log_fields = {
            "dossier_id": sanitize_log_value(dossier_id),
            "folder_count": len(fids),
            "doc_count": len(moved) + len(deleted),
        }
    elif action == "delete_dossier":
        dossier_id = str(payload.get("dossier_id") or "")
        fids = [str(x) for x in (payload.get("folder_ids") or [])]
        outcome = _delete_dossier_internal(session, dossier_id, fids)
        log_fields = {
            "dossier_id": sanitize_log_value(dossier_id),
            "folder_count": len(fids),
            "doc_count": 0,
        }
    elif action == "sync_template":
        tpl_id = str(payload.get("template_id") or "")
        outcome = _sync_template_internal(session, tpl_id)
        log_fields = {
            "template_id": sanitize_log_value(tpl_id),
            "lang": "fr+en",
            "action": outcome,
        }
    elif action == "delete_template":
        tpl_id = str(payload.get("template_id") or "")
        outcome = _delete_template_internal(session, tpl_id)
        log_fields = {
            "template_id": sanitize_log_value(tpl_id),
            "deleted_count": 1 if outcome == "deleted" else 0,
        }
    elif action == "sync_receipt":
        reg = str(payload.get("register") or "")
        tx_id = str(payload.get("tx_id") or "")
        outcome = _sync_receipt_internal(session, reg, tx_id)
        log_fields = {
            "register": sanitize_log_value(reg),
            "transaction_id": sanitize_log_value(tx_id),
            "action": outcome,
        }
    elif action == "delete_receipt":
        reg = str(payload.get("register") or "")
        tx_id = str(payload.get("tx_id") or "")
        outcome = _delete_receipt_internal(session, reg, tx_id)
        log_fields = {
            "register": sanitize_log_value(reg),
            "transaction_id": sanitize_log_value(tx_id),
            "action": outcome,
        }
    elif action == "sync_reconciliation":
        reg = str(payload.get("register") or "")
        rec_id = str(payload.get("reconciliation_id") or "")
        outcome = _sync_reconciliation_internal(session, reg, rec_id)
        log_fields = {
            "register": sanitize_log_value(reg),
            "reconciliation_id": sanitize_log_value(rec_id),
            "action": outcome,
        }
    else:  # pragma: no cover - guarded by _SUPPORTED_ACTIONS
        outcome = "refused"

    # Excluded items already emit miroir_onedrive_excluded_skipped inside the guard.
    if outcome != "skipped_excluded":
        log_event = _ACTION_LOG_EVENTS[action]
        reason = "not_completed" if outcome == "skipped_not_completed" else outcome
        log_onedrive_mirror_event(
            log_event,
            outcome="success",
            reason=reason,
            **log_fields,
        )
    return {"ok": True, "action": action, "outcome": outcome}


def executer_rattrapage_cron(*, limit: int = CRON_BATCH_LIMIT) -> dict[str, Any]:
    """Bounded periodic catch-up sweep aligning OneDrive with Athena state."""
    if not is_active():
        return {"actif": False, "synced": 0, "deleted": 0, "errors": 0}

    session = _MirrorSession()
    cap = max(1, int(limit or CRON_BATCH_LIMIT))
    stats = {
        "actif": True,
        "scanned": 0,
        "synced": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": 0,
        "truncated": False,
        "dossiers": 0,
        "docs": 0,
        "templates": 0,
        "receipts": 0,
        "statements": 0,
    }

    def _hit_cap() -> bool:
        return (stats["synced"] + stats["deleted"]) >= cap

    def _tally(outcome: str, *, bucket: Optional[str] = None) -> None:
        stats["scanned"] += 1
        if outcome in ("uploaded", "replaced", "updated", "synced", "deduplicated", "created"):
            stats["synced"] += 1
            if bucket:
                stats[bucket] += 1
        elif outcome == "deleted":
            stats["deleted"] += 1
        elif outcome.startswith("skipped") or outcome == "unchanged":
            stats["skipped"] += 1

    # 1. Dossiers, folders, and client documents
    existing_dossier_ids: set[str] = set()
    existing_doc_ids: set[str] = set()
    existing_folder_ids: set[str] = set()
    for d_snap in db.collection("dossiers").stream():
        dossier = {**(d_snap.to_dict() or {}), "id": d_snap.id}
        did = str(dossier["id"])
        existing_dossier_ids.add(did)
        folders = _load_dossier_folders(did)
        existing_folder_ids.update(folders.keys())
        if _hit_cap():
            stats["truncated"] = True
            break
        stats["dossiers"] += 1
        try:
            session.ensure_dossier_root(dossier)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning(
                "cron miroir_onedrive dossier %s failed: %s",
                sanitize_log_value(did),
                type(exc).__name__,
            )
            continue

        depths: dict[str, int] = {}

        def _folder_depth(fid: str, seen: set[str]) -> int:
            if fid in depths:
                return depths[fid]
            if fid in seen:
                return 999
            seen.add(fid)
            p = (folders[fid].get("parent_folder_id") or "") if fid in folders else ""
            d = 0 if not p or p not in folders else 1 + _folder_depth(p, seen)
            depths[fid] = d
            return d

        for fid in sorted(folders, key=lambda x: _folder_depth(x, set())):
            if _hit_cap():
                stats["truncated"] = True
                break
            try:
                dossier_root = session.ensure_dossier_root(dossier)
                parent_fid = str(folders[fid].get("parent_folder_id") or "")
                if parent_fid:
                    parent_od = session._dossier_folder_cache.get((did, parent_fid))
                    if parent_od is None:
                        parent_od = _sync_folder_chain(
                            session, dossier, parent_fid, folders_by_id=folders
                        )
                else:
                    parent_od = dossier_root
                _item, f_out = _sync_single_folder_with_outcome(
                    session, did, folders[fid], parent_od
                )
                if f_out in ("created", "updated"):
                    _tally(f_out)
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "cron miroir_onedrive folder %s failed: %s",
                    sanitize_log_value(fid),
                    type(exc).__name__,
                )
        for doc_snap in (
            db.collection("documents")
            .where(filter=FieldFilter("dossier_id", "==", did))
            .stream()
        ):
            existing_doc_ids.add(doc_snap.id)
            if _hit_cap():
                stats["truncated"] = True
                break
            try:
                _tally(_sync_document_internal(session, doc_snap.id), bucket="docs")
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "cron miroir_onedrive document %s failed: %s",
                    sanitize_log_value(doc_snap.id),
                    type(exc).__name__,
                )
        if _hit_cap():
            stats["truncated"] = True
            break

    # 2. Word templates (FR and EN current versions)
    existing_tpl_ids: set[str] = set()
    for t_snap in db.collection("doc_templates").stream():
        existing_tpl_ids.add(t_snap.id)
        if _hit_cap():
            stats["truncated"] = True
            break
        try:
            _tally(_sync_template_internal(session, t_snap.id), bucket="templates")
        except Exception as exc:
            stats["errors"] += 1
            logger.warning(
                "cron miroir_onedrive template %s failed: %s",
                sanitize_log_value(t_snap.id),
                type(exc).__name__,
            )

    # 3. Admin and Trust receipts
    existing_receipt_keys: set[tuple[str, str]] = set()
    for reg, col in (("admin", "admin_transactions"), ("trust", "trust_transactions")):
        for tx_snap in db.collection(col).stream():
            tx = tx_snap.to_dict() or {}
            if not tx.get("receipt_storage_path"):
                continue
            existing_receipt_keys.add((reg, tx_snap.id))
            if _hit_cap():
                stats["truncated"] = True
                break
            try:
                _tally(_sync_receipt_internal(session, reg, tx_snap.id), bucket="receipts")
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "cron miroir_onedrive receipt %s:%s failed: %s",
                    reg,
                    sanitize_log_value(tx_snap.id),
                    type(exc).__name__,
                )

    # 4. Completed reconciliation statements ONLY (never draft reconciliations)
    existing_statement_keys: set[tuple[str, str]] = set()
    for reg, col in (("admin", "admin_reconciliations"), ("trust", "trust_reconciliations")):
        for rec_snap in (
            db.collection(col)
            .where(filter=FieldFilter("status", "==", "complétée"))
            .stream()
        ):
            rec = rec_snap.to_dict() or {}
            if rec.get("status") != "complétée" or not rec.get("statement_storage_path"):
                continue
            existing_statement_keys.add((reg, rec_snap.id))
            if _hit_cap():
                stats["truncated"] = True
                break
            try:
                _tally(
                    _sync_reconciliation_internal(session, reg, rec_snap.id),
                    bucket="statements",
                )
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "cron miroir_onedrive reconciliation %s:%s failed: %s",
                    reg,
                    sanitize_log_value(rec_snap.id),
                    type(exc).__name__,
                )

    # 5. Remove OneDrive orphans when source Athena entities were deleted
    if not _hit_cap() and not stats["truncated"]:
        for m_snap in db.collection(MIROIR_COLLECTION).stream():
            if _hit_cap():
                stats["truncated"] = True
                break
            m = m_snap.to_dict() or {}
            etype = str(m.get("entity_type") or "")
            eid = str(m.get("entity_id") or "")
            reg = str(m.get("register") or "")
            try:
                if etype == "document" and eid and eid not in existing_doc_ids:
                    if not db.collection("documents").document(eid).get().exists:
                        _tally(_delete_document_internal(session, eid))
                elif etype == "template" and eid and eid not in existing_tpl_ids:
                    if not db.collection("doc_templates").document(eid).get().exists:
                        _tally(_delete_template_internal(session, eid))
                elif etype == "folder" and eid and eid not in existing_folder_ids:
                    if not db.collection("folders").document(eid).get().exists:
                        if m.get("onedrive_item_id"):
                            graph_courrier.courrier_supprimer_element_onedrive(
                                str(m["onedrive_item_id"]), encoded_upn=session.encoded_upn
                            )
                        _delete_mirror_record(m_snap.id)
                        _tally("deleted")
                elif etype == "receipt" and eid and reg in ("admin", "trust"):
                    if (reg, eid) not in existing_receipt_keys:
                        col = "admin_transactions" if reg == "admin" else "trust_transactions"
                        t_snap = db.collection(col).document(eid).get()
                        if not t_snap.exists or not (t_snap.to_dict() or {}).get("receipt_storage_path"):
                            _tally(_delete_receipt_internal(session, reg, eid))
                elif etype == "reconciliation" and eid and reg in ("admin", "trust"):
                    if (reg, eid) not in existing_statement_keys:
                        col = "admin_reconciliations" if reg == "admin" else "trust_reconciliations"
                        r_snap = db.collection(col).document(eid).get()
                        r_data = r_snap.to_dict() or {} if r_snap.exists else {}
                        if (
                            not r_snap.exists
                            or r_data.get("status") != "complétée"
                            or not r_data.get("statement_storage_path")
                        ):
                            _tally(_delete_reconciliation_internal(session, reg, eid))
                elif etype == "dossier" and eid and eid not in existing_dossier_ids:
                    if not db.collection("dossiers").document(eid).get().exists:
                        _tally(_delete_dossier_internal(session, eid, []))
            except Exception as exc:
                stats["errors"] += 1
                logger.warning(
                    "cron miroir_onedrive orphan cleanup %s failed: %s",
                    sanitize_log_value(m_snap.id),
                    type(exc).__name__,
                )

    log_onedrive_mirror_event(
        "miroir_onedrive_sweep",
        outcome="failure" if stats["errors"] else "success",
        dossiers=stats["dossiers"],
        docs=stats["docs"],
        templates=stats["templates"],
        receipts=stats["receipts"],
        statements=stats["statements"],
        errors=stats["errors"],
        scanned=stats["scanned"],
        synced=stats["synced"],
        deleted=stats["deleted"],
        truncated=stats["truncated"],
    )
    return stats
