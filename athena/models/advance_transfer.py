"""Operations -> Trust client-advance bridge -- ONE transaction, both legs
born ``status="compensée"`` immediately (Phase 3 / Lot C).

Moves client advances received in an active firm ``opérations`` account into
an active ``trust`` account for a named ``dossier_id`` and ``client_id``,
atomically committing in ONE Firestore transaction:

1. Administration leg: ``virement_interne_sortant`` with
   ``transfer_purpose="avances"``, ``dossier_id=dossier_id``,
   ``status="compensée"``, ``cleared_date=date_value``, linked via
   ``trust_transaction_id``. Preserves ``models/financial_statements.py``
   ``avance_honoraires`` liability settlement with zero unexplained equity
   variance.
2. Trust leg: ``avance_honoraires`` (``direction="recette"``),
   ``dossier_id=dossier_id``, ``client_id=client_id``,
   ``status="compensée"``, ``cleared_date=date_value``, linked via
   ``admin_transaction_id``. Updates ``book_balance``, ``bank_balance``,
   ``trust_balance_by_client``, ``trust_cleared_by_client``, and
   ``trust_balance``.

Both registers' lock floors (and the trust register's backdating guard) are
verified inside the transaction before staging either write. Reversals use
:func:`reverse_advance_transfer` to reverse both legs atomically while
enforcing Barreau art. 59 cleared-funds protection on the trust client balance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import admin_ledger as al
from models import db, provenance, trust
from security import sanitize
from utils.deadlines import today_mtl
from utils.logging_setup import log_trust_event, log_unexpected
from utils.tracing_setup import span

CREATE_OUTCOME_UNCERTAIN = (
    "L'enregistrement du virement d'avances vers le fidéicommis a échoué "
    "d'une façon qui ne permet pas de savoir s'il a été inscrit : vérifiez "
    "les journaux du fidéicommis et d'administration avant de réessayer."
)
REVERSE_OUTCOME_UNCERTAIN = (
    "La contre-passation du virement d'avances a échoué d'une façon qui ne "
    "permet pas de savoir si elle a été inscrite : vérifiez les journaux "
    "avant de réessayer."
)
OUTCOME_UNCERTAIN_REASON = trust.OUTCOME_UNCERTAIN_REASON
_NOTHING_WRITTEN = (
    "Rien n'a été inscrit — ni au compte d'opérations, ni au fidéicommis."
)
_NOTHING_REVERSED = (
    "Rien n'a été contre-passé — ni au fidéicommis, ni au compte "
    "d'opérations."
)

_MESSAGES = {
    "compte_administration_requis": (
        "Sélectionnez le compte d'opérations d'où provient le virement."
    ),
    "compte_administration_invalide": (
        "Un virement d'avances au fidéicommis part d'un compte d'opérations — "
        "jamais d'une carte de crédit. " + _NOTHING_WRITTEN
    ),
    "compte_administration_introuvable": (
        "Compte d'administration introuvable. " + _NOTHING_WRITTEN
    ),
    "compte_administration_fermé": (
        "Ce compte d'opérations est fermé. " + _NOTHING_WRITTEN
    ),
    "compte_fideicommis_requis": (
        "Sélectionnez le compte en fidéicommis destinataire."
    ),
    "mode_invalide": (
        "Choisissez un mode de virement accepté par les deux registres "
        "(ex. : Virement ou Chèque). " + _NOTHING_WRITTEN
    ),
    "date_administration_verrouillée": (
        "La date du virement tombe dans une période déjà conciliée du compte "
        "d'opérations (conciliation au {detail}) : inscrivez une date "
        "postérieure à la conciliation. " + _NOTHING_WRITTEN
    ),
    "date_administration_verrouillée_jour": (
        "Une conciliation complétée du compte d'opérations couvre déjà "
        "aujourd'hui (conciliation au {detail}) : aucune date permise "
        "aujourd'hui. Réessayez demain. " + _NOTHING_WRITTEN
    ),
    "pas_un_virement_avances": (
        "Cette écriture n'est pas un virement d'avances issu du compte "
        "d'opérations."
    ),
    "ecriture_admin_introuvable": (
        "L'écriture d'administration liée à ce virement d'avances n'a pas pu "
        "être lue. " + _NOTHING_REVERSED + " Réessayez dans un instant."
    ),
    "contre_passation_administration_verrouillée": (
        "Une conciliation complétée du compte d'opérations couvre déjà la "
        "date d'aujourd'hui : l'écriture d'administration liée ne peut pas "
        "être contre-passée aujourd'hui, ni donc celle du fidéicommis. "
        "Réessayez demain. " + _NOTHING_REVERSED
    ),
    "solde_compensé_insuffisant": (
        "Impossible de contre-passer ce virement d'avances : les fonds "
        "compensés du client au fidéicommis ne couvrent plus ce montant "
        "(ils ont déjà été utilisés). " + _NOTHING_REVERSED
    ),
    "motif_requis": "Un motif de contre-passation est requis.",
}


class _AdvanceAbort(Exception):
    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _message(reason: str, detail: Optional[str] = None) -> str:
    text = _MESSAGES.get(reason, "Virement d'avances refusé. " + _NOTHING_WRITTEN)
    return text.replace("{detail}", detail or "—")


def _uncertain(
    report: Optional[dict], message: str, log_message: str, **ids,
) -> tuple[None, list[str]]:
    log_unexpected(
        log_message, exc_info=False,
        **{k: v for k, v in ids.items() if v},
    )
    if report is not None:
        report["reason"] = OUTCOME_UNCERTAIN_REASON
        report["side"] = "virement"
    return None, [message]


def _fail(
    report: Optional[dict], reason: str, message: str, *,
    side: str, operation: str, **ids,
) -> tuple[None, list[str]]:
    if report is not None:
        report["reason"] = reason
        report["side"] = side
    log_trust_event(
        "trust_transaction_refused", "refused", reason=reason,
        side=side, operation=operation,
        **{k: v for k, v in ids.items() if v},
    )
    return None, [message]


def create_advance_transfer(
    *,
    admin_account_id: str,
    trust_account_id: str,
    dossier_id: str,
    client_id: str,
    amount: int,
    date_value,
    method: str = "virement",
    reference: str = "",
    description: str = "",
    _report_out: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Move *amount* client advances from an active ``opérations`` account to
    an active ``trust`` account for ``(dossier_id, client_id)`` in ONE
    transaction, both legs born ``compensée`` immediately."""
    admin_account_id = (admin_account_id or "").strip()
    trust_account_id = (trust_account_id or "").strip()
    dossier_id = (dossier_id or "").strip()
    client_id = (client_id or "").strip()
    method = (method or "").strip()

    if not admin_account_id:
        return _fail(
            _report_out, "compte_administration_requis",
            _message("compte_administration_requis"),
            side="administration", operation="advance_transfer",
        )
    if not trust_account_id:
        return _fail(
            _report_out, "compte_fideicommis_requis",
            _message("compte_fideicommis_requis"),
            side="fidéicommis", operation="advance_transfer",
        )
    if method not in trust.VALID_METHODS or method not in al.VALID_METHODS:
        return _fail(
            _report_out, "mode_invalide", _message("mode_invalide"),
            side="virement", operation="advance_transfer",
            account_id=trust_account_id, dossier_id=dossier_id,
        )

    day = trust._midnight_utc(date_value)
    if day is None:
        return _fail(
            _report_out, "date_requise", trust._abort_message("date_requise"),
            side="virement", operation="advance_transfer",
            account_id=trust_account_id, dossier_id=dossier_id,
        )
    if day.date() > today_mtl():
        return _fail(
            _report_out, "date_future", trust._abort_message("date_future"),
            side="virement", operation="advance_transfer",
            account_id=trust_account_id, dossier_id=dossier_id,
        )

    clean_ref = sanitize((reference or "").strip(), max_length=2000)
    clean_desc = sanitize((description or "").strip(), max_length=2000)

    trust_data = {
        "account_id": trust_account_id,
        "direction": "recette",
        "purpose": "avance_honoraires",
        "method": method,
        "counterparty": "Client",
        "dossier_id": dossier_id,
        "client_id": client_id,
        "amount": amount,
        "date": day,
        "reference": clean_ref,
        "description": clean_desc,
    }
    t_ctx, t_reason, t_clean = trust._prepare_create(
        trust_data, cleared_date=day,
    )
    if t_reason:
        return _fail(
            _report_out, t_reason, trust._abort_message(t_reason),
            side="fidéicommis", operation="advance_transfer",
            account_id=t_clean.get("account_id"),
            dossier_id=t_clean.get("dossier_id"),
        )

    admin_data = {
        "account_id": admin_account_id,
        "kind": "virement_interne_sortant",
        "transfer_purpose": "avances",
        "amount": amount,
        "date": day,
        "method": method,
        "counterparty": "Compte en fidéicommis",
        "dossier_id": dossier_id,
        "reference": clean_ref,
        "description": clean_desc,
    }
    a_ctx, a_reason, _ = al._prepare_create(
        admin_data, trust_transaction_id=t_ctx["tx_id"], cleared_date=day,
    )
    if a_reason:
        return _fail(
            _report_out, a_reason,
            al._ABORT_MESSAGES.get(a_reason, "Virement d'avances refusé."),
            side="administration", operation="advance_transfer",
            account_id=admin_account_id, dossier_id=dossier_id,
        )
    t_ctx["admin_transaction_id"] = a_ctx["tx_id"]

    transaction = db.transaction()
    result: dict = {}
    attempted: dict = {"commit": False}

    @firestore.transactional
    def _bridge(txn) -> None:
        result.clear()
        now = datetime.now(timezone.utc)
        t_reads = trust._read_create(txn, t_ctx)
        a_reads = al._read_create(txn, a_ctx, dossier=t_reads["dossier"])

        account = a_reads["account"]
        if account.get("account_type") != "opérations":
            raise _AdvanceAbort("compte_administration_invalide")
        if account.get("status") != "actif":
            raise _AdvanceAbort("compte_administration_fermé")
        floor = a_reads["lock_floor"]
        if floor is not None and day.date() <= floor.date():
            if floor.date() >= today_mtl():
                raise _AdvanceAbort(
                    "date_administration_verrouillée_jour",
                    detail=floor.strftime("%Y-%m-%d"),
                )
            raise _AdvanceAbort(
                "date_administration_verrouillée",
                detail=floor.strftime("%Y-%m-%d"),
            )

        dossier = t_reads["dossier"] or {}
        client_name = ""
        for c in dossier.get("clients") or []:
            if c.get("id") == client_id:
                client_name = c.get("name", "")
                break
        t_ctx["counterparty"] = client_name or "Client"
        if not clean_desc:
            file_no = dossier.get("file_number") or ""
            label = f" — dossier {file_no}" if file_no else ""
            t_ctx["clean"]["description"] = (
                f"Virement d'avances du compte d'opérations{label}"
            )
            admin_desc = f"Virement d'avances au fidéicommis{label}"
        else:
            admin_desc = clean_desc

        t_result = trust._stage_create(txn, t_ctx, t_reads, now)
        trust_acc_name = (
            t_reads["account"].get("name") or "Compte en fidéicommis"
        )
        a_result = al._stage_create(
            txn, a_ctx, a_reads, now,
            overrides={
                "counterparty": trust_acc_name,
                "description": admin_desc,
            },
        )
        result.update(trust=t_result, admin=a_result)
        attempted["commit"] = True

    try:
        with span(
            "trust.transaction", direction="recette",
            purpose="avance_honoraires", dossier_id=dossier_id,
        ):
            _bridge(transaction)
    except (trust._OwnCommitLanded, al._OwnCommitLanded):
        return _uncertain(
            _report_out, CREATE_OUTCOME_UNCERTAIN,
            "advance transfer: an earlier attempt of the transaction landed",
            account_id=trust_account_id, dossier_id=dossier_id,
        )
    except _AdvanceAbort as abort:
        return _fail(
            _report_out, abort.reason, _message(abort.reason, abort.detail),
            side="administration", operation="advance_transfer",
            account_id=admin_account_id, dossier_id=dossier_id,
        )
    except trust._TxnAbort as abort:
        return _fail(
            _report_out, abort.reason,
            trust._abort_message(abort.reason, abort.detail),
            side="fidéicommis", operation="advance_transfer",
            account_id=trust_account_id, dossier_id=dossier_id,
        )
    except al._TxnAbort as abort:
        return _fail(
            _report_out, abort.reason,
            al._abort_message(abort, "Opération refusée."),
            side="administration", operation="advance_transfer",
            account_id=admin_account_id, dossier_id=dossier_id,
        )
    except Exception as exc:
        if attempted["commit"]:
            return _uncertain(
                _report_out, CREATE_OUTCOME_UNCERTAIN,
                "advance transfer: transaction failed after a commit attempt",
                account_id=trust_account_id, dossier_id=dossier_id,
                error_type=type(exc).__name__,
            )
        log_unexpected("advance transfer write failed", error_type=type(exc).__name__)
        if _report_out is not None:
            _report_out["reason"] = "erreur"
            _report_out["side"] = "virement"
        return None, [
            "Erreur lors du virement d'avances vers le fidéicommis. "
            "Rien n'a été inscrit. Veuillez réessayer."
        ]

    t_result = result["trust"]
    a_result = result["admin"]
    t_entry = t_result["entry"]
    a_entry = a_result["entry"]

    provenance.note_commit(trust.TRANSACTIONS_COLLECTION, t_entry["id"])
    log_trust_event(
        "trust_transaction_created", transaction_id=t_entry["id"],
        dossier_id=dossier_id, account_id=trust_account_id,
        direction="recette", purpose="avance_honoraires",
        sequence=t_entry["sequence"],
    )
    log_trust_event(
        "trust_transaction_cleared", transaction_id=t_entry["id"],
        account_id=trust_account_id,
    )
    al._after_create_commit(a_ctx, a_result)
    log_trust_event(
        "trust_advance_transfer_recorded", transaction_id=t_entry["id"],
        dossier_id=dossier_id, account_id=trust_account_id,
        admin_account_id=admin_account_id, admin_transaction_id=a_entry["id"],
    )
    return {
        "trust_entry": t_entry,
        "admin_entry": a_entry,
        "client_balance": t_result.get("client_balance"),
    }, []


def reverse_advance_transfer(
    trust_tx_id: str, reason: str, *, expected_etag: Optional[str] = None,
    _report_out: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Reverse BOTH legs of an Operations -> Trust advance bridge atomically,
    blocking if the client's cleared trust balance would fall below zero."""
    reason = sanitize((reason or "").strip(), max_length=500)
    if not reason:
        return _fail(
            _report_out, "motif_requis", _message("motif_requis"),
            side="virement", operation="reverse_advance_transfer",
            transaction_id=trust_tx_id,
        )

    admin_reason = f"Contre-passation du virement d'avances — {reason}"
    transaction = db.transaction()
    result: dict = {}
    attempted: dict = {"commit": False}

    @firestore.transactional
    def _reverse(txn) -> None:
        result.clear()
        now = datetime.now(timezone.utc)
        today = trust._today_midnight_utc()
        t_ctx = trust._read_reverse(
            txn, trust_tx_id, today, fee_payment_ok=True,
            expected_etag=expected_etag,
        )
        orig = t_ctx["original"]
        admin_tx_id = orig.get("admin_transaction_id") or ""
        if orig.get("purpose") != "avance_honoraires" or not admin_tx_id:
            raise _AdvanceAbort("pas_un_virement_avances")

        try:
            a_rows = al.list_by_trust_transaction(trust_tx_id, txn=txn)
        except Exception:
            raise _AdvanceAbort("ecriture_admin_introuvable")
        standing = [
            r for r in a_rows
            if not (
                r.get("reversed_by_id")
                or r.get("status") == "annulée"
                or r.get("kind") == al.REVERSAL_KIND
            )
        ]
        if not standing:
            snap = db.collection(al.TRANSACTIONS_COLLECTION).document(admin_tx_id).get(transaction=txn)
            if not snap.exists:
                raise _AdvanceAbort("ecriture_admin_introuvable")
            row = snap.to_dict()
            if not (row.get("reversed_by_id") or row.get("status") == "annulée"):
                standing = [row]

        a_ctx = al._read_reverse_legs(txn, standing) if standing else None
        if a_ctx is not None:
            for info in a_ctx["accounts"].values():
                floor = info["floor"]
                if floor is not None and today.date() <= floor.date():
                    raise _AdvanceAbort("contre_passation_administration_verrouillée")

        # Barreau art. 59 protection: reversing a compensée avance_honoraires
        # takes the funds out of the client's trust balance; refuse if already
        # spent/withdrawn.
        client_id = orig.get("client_id")
        dossier = t_ctx["dossier"] or {}
        cleared_map = dict(dossier.get("trust_cleared_by_client") or {})
        current_cleared = int(cleared_map.get(client_id, 0)) if client_id else 0
        if int(orig.get("amount", 0)) > current_cleared:
            raise _AdvanceAbort("solde_compensé_insuffisant")

        t_result = trust._stage_reverse(txn, t_ctx, reason, today, now)
        a_result = (
            al._stage_reverse_legs(txn, a_ctx, admin_reason, today, now)
            if a_ctx is not None else {"reversals": [], "legs": [], "invoices": []}
        )
        result.update(trust=t_result, admin=a_result)
        attempted["commit"] = True

    try:
        with span(
            "trust.transaction", direction="reversal",
            purpose="avance_honoraires", dossier_id=None,
        ):
            _reverse(transaction)
    except _AdvanceAbort as abort:
        if abort.reason == "solde_compensé_insuffisant":
            log_trust_event(
                "trust_overdraft_refused", "refused",
                transaction_id=trust_tx_id,
                reason="insufficient_cleared_balance",
            )
        side = (
            "fidéicommis" if abort.reason == "solde_compensé_insuffisant"
            else "administration"
        )
        return _fail(
            _report_out, abort.reason, _message(abort.reason, abort.detail),
            side=side, operation="reverse_advance_transfer",
            transaction_id=trust_tx_id,
        )
    except trust._TxnAbort as abort:
        if abort.reason == "déjà_contrepassée" and attempted["commit"]:
            return _uncertain(
                _report_out, REVERSE_OUTCOME_UNCERTAIN,
                "advance transfer reversal: reversed after commit attempt",
                transaction_id=trust_tx_id,
            )
        return _fail(
            _report_out, abort.reason,
            trust._abort_message(abort.reason, abort.detail, "Contre-passation refusée."),
            side="fidéicommis", operation="reverse_advance_transfer",
            transaction_id=trust_tx_id,
        )
    except al._TxnAbort as abort:
        return _fail(
            _report_out, abort.reason,
            f"Contre-passation de l'écriture d'administration refusée : "
            f"{al._abort_message(abort, 'contre-passation refusée.')} {_NOTHING_REVERSED}",
            side="administration", operation="reverse_advance_transfer",
            transaction_id=trust_tx_id,
        )
    except Exception as exc:
        if attempted["commit"]:
            return _uncertain(
                _report_out, REVERSE_OUTCOME_UNCERTAIN,
                "advance transfer reversal: transaction failed after commit attempt",
                transaction_id=trust_tx_id, error_type=type(exc).__name__,
            )
        log_unexpected("advance transfer reversal failed", error_type=type(exc).__name__)
        if _report_out is not None:
            _report_out["reason"] = "erreur"
            _report_out["side"] = "virement"
        return None, [
            "Erreur lors de la contre-passation du virement d'avances. "
            "Rien n'a été contre-passé. Veuillez réessayer."
        ]

    t_result = result["trust"]
    a_result = result["admin"]
    trust._after_reverse_commit(t_result)
    if a_result["reversals"]:
        al._after_reverse_commit(a_result, original_id=trust_tx_id)
    admin_reversals = [
        {"admin_transaction_id": rev.get("reverses_id"), "reversal_id": rev["id"]}
        for rev in a_result["reversals"]
    ]
    log_trust_event(
        "trust_advance_transfer_reversed", transaction_id=trust_tx_id,
        account_id=t_result["reversal"].get("account_id"),
        dossier_id=t_result["reversal"].get("dossier_id"),
        reversal_id=t_result["reversal"]["id"],
        admin_reversal_count=len(admin_reversals),
    )
    return {
        "trust_reversal": t_result["reversal"],
        "trust_reversals": list(t_result["reversals"]),
        "original_status_after": t_result.get("original_status_after"),
        "admin_reversals": admin_reversals,
        "client_cleared_after": t_result.get("client_cleared_after"),
    }, []
