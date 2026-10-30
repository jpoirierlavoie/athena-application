"""Contacts: titles, « Fonction » and the vCard (2026-10-30) — a one-shot
migration that writes OUTSIDE the model.

What it does
------------
* folds a stored ``job_role`` (« Fonction », gone from the schema) into
  ``job_title`` with ``models.partie.fold_job_role`` — the function every
  save uses — and deletes the key;
* stores a title in its canonical form (``utils.civilite.canonical_prefix``:
  « Dr. » → « Dr », a list → its joined text). A title the vocabulary does
  not know is kept as it is and REPORTED, for the lawyer to correct on the
  fiche;
* joins a name component stored as a list (a multi-valued vCard component
  a phone sent before the model joined them);
* with ``--resynchroniser``, restamps EVERY contact, organizations included.
  The vCard changed for nearly all of them (FN without the title, no ADR for
  an empty address, PRONOUNS, no ROLE), and DavX5 downloads a card again
  only when its ETag — the record's ``etag`` — changes.

Why it writes outside the model
-------------------------------
``update_partie`` re-validates the whole record, so a legacy contact holding
an unreadable phone number would refuse a change it is not about; and it
never bumps the CTag. The script writes only the keys above plus the house
stamps (``provenance.update_fields`` under ``writing_via("script")``: a new
``etag``, ``updated_at``, ``updated_via = "script"``), each document guarded
by the ``update_time`` it was read at, and stages
``dav.sync.bump_ctag_in_batch(…, "parties")`` in every batch — a contact
written without the bump would never reach the phone. The restamped
contacts keep their relative order in the « recently modified » list: each
gets ``now`` plus its rank in microseconds, oldest first.

Running it
----------
After the deploy (the new serialization must be live when DavX5 downloads):

    python -m scripts.migrate_contact_titles                          # à blanc
    python -m scripts.migrate_contact_titles --appliquer --resynchroniser

Idempotent: once applied, a dry run reads « rien à faire » (a second
``--resynchroniser`` only makes the phones download every contact again).
Read failures stop the run before anything is written. The output names
contacts by id — never a person's name; the « Fonction » values it shows
for review stay in the terminal.

Exit codes: 0 done or nothing to do, 1 a read or a write failed (or its
outcome is uncertain), 2 a usage error.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        # python-dotenv is a local-dev convenience: without it the environment is read as is.
        pass


_load_env()

from google.cloud import firestore  # noqa: E402

from dav.sync import bump_ctag_in_batch  # noqa: E402
from models import db, provenance  # noqa: E402
from models.partie import COLLECTION, fold_job_role  # noqa: E402
from utils import civilite  # noqa: E402

DAV_COLLECTION = "parties"
# Firestore caps a batch at 500 operations; each chunk adds the CTag bump.
CHUNK = 400
_NAME_FIELDS = ("first_name", "last_name", "middle_name", "suffix")


@dataclass
class Plan:
    """What one contact needs, read from its snapshot."""

    snap: object
    changes: dict = field(default_factory=dict)
    role_pair: Optional[tuple[str, str, str]] = None   # (title, role, folded)
    prefix_pair: Optional[tuple[str, str]] = None      # (stored, canonical)
    unknown_prefix: str = ""


def _plan(snap) -> Plan:
    doc = snap.to_dict() or {}
    plan = Plan(snap=snap)
    if "job_role" in doc:
        title = civilite.as_text(doc.get("job_title", ""))
        role = civilite.as_text(doc.get("job_role", ""))
        folded = fold_job_role(title, role)
        plan.changes["job_role"] = firestore.DELETE_FIELD
        if folded != doc.get("job_title", ""):
            plan.changes["job_title"] = folded
        if role:
            plan.role_pair = (title, role, folded)
    stored = doc.get("prefix", "")
    canonical = civilite.canonical_prefix(stored)
    if canonical != stored:
        plan.changes["prefix"] = canonical
        plan.prefix_pair = (civilite.as_text(stored), canonical)
    if not civilite.is_known(canonical):
        plan.unknown_prefix = canonical
    for key in _NAME_FIELDS:
        if key in doc and not isinstance(doc[key], str):
            plan.changes[key] = civilite.as_text(doc[key])
    return plan


def _read_all() -> list:
    """Every contact, oldest ``updated_at`` first. Raises on a failed read."""
    snaps = list(db.collection(COLLECTION).stream())
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def _key(snap):
        value = (snap.to_dict() or {}).get("updated_at")
        return value if isinstance(value, datetime) else floor

    return sorted(snaps, key=_key)


def describe(plans: list[Plan], resync: bool) -> int:
    """Print the plan; return how many contacts would be written."""
    roles = [p for p in plans if p.role_pair]
    prefixes = [p for p in plans if p.prefix_pair]
    unknown = [p for p in plans if p.unknown_prefix]
    lists = [p for p in plans if any(k in p.changes for k in _NAME_FIELDS)]
    no_uid = [p for p in plans if not (p.snap.to_dict() or {}).get("vcard_uid")]

    print(f"\n« Fonction » à replier dans « Poste ou profession » : {len(roles)}")
    for p in roles:
        title, role, folded = p.role_pair
        print(f"  {p.snap.id} : « {title} » + « {role} » → « {folded} »")
    print(f"Titres à normaliser : {len(prefixes)}")
    for p in prefixes:
        print(f"  {p.snap.id} : « {p.prefix_pair[0]} » → « {p.prefix_pair[1]} »")
    print(f"Titres inconnus, gardés tels quels (à corriger sur la fiche) : {len(unknown)}")
    for p in unknown:
        print(f"  {p.snap.id} : « {p.unknown_prefix} »")
    print(f"Composants de nom stockés en liste : {len(lists)}")
    print(f"Contacts sans UID vCard (attendu : 0 — jamais créé ici) : {len(no_uid)}")
    for p in no_uid:
        print(f"  {p.snap.id}")

    writes = len(plans) if resync else sum(1 for p in plans if p.changes)
    label = "à réestampiller ou corriger" if resync else "à corriger"
    print(f"\nContacts {label} : {writes} sur {len(plans)}.")
    return writes


def _landed(ref, etag: str) -> Optional[bool]:
    try:
        return (ref.get().to_dict() or {}).get("etag") == etag
    except Exception:
        return None


def apply(plans: list[Plan], resync: bool) -> int:
    targets = [p for p in plans if resync or p.changes]
    if not targets:
        print("Rien à faire.")
        return 0
    base = datetime.now(timezone.utc)
    written = 0
    for start in range(0, len(targets), CHUNK):
        chunk = targets[start:start + CHUNK]
        batch = db.batch()
        staged = []
        for rank, plan in enumerate(chunk, start=start):
            payload = dict(plan.changes)
            with provenance.writing_via("script"):
                payload.update(
                    provenance.update_fields(base + timedelta(microseconds=rank)))
            ref = db.collection(COLLECTION).document(plan.snap.id)
            batch.update(ref, payload, option=db.write_option(
                last_update_time=plan.snap.update_time))
            staged.append((ref, payload["etag"]))
        bump_ctag_in_batch(batch, DAV_COLLECTION)
        try:
            batch.commit()
        except Exception as exc:
            landed = [_landed(ref, etag) for ref, etag in staged]
            if all(v is True for v in landed):
                written += len(chunk)
                print(f"Lot {start // CHUNK + 1} écrit — la réponse du serveur "
                      f"s'était perdue ({type(exc).__name__}), la relecture le confirme.")
                continue
            if all(v is False for v in landed):
                print(f"Lot {start // CHUNK + 1} : rien n'a été écrit "
                      f"({type(exc).__name__}) — un contact a changé depuis la "
                      f"lecture, ou le serveur a refusé. {written} contact(s) "
                      f"écrit(s) avant. Relancez à blanc.")
                return 1
            print(f"Lot {start // CHUNK + 1} : issue INCERTAINE "
                  f"({type(exc).__name__}). {written} contact(s) écrit(s) avant. "
                  f"Relancez à blanc avant toute autre chose.")
            return 1
        written += len(chunk)
    print(f"Écrit : {written} contact(s) ; CTag « {DAV_COLLECTION} » renouvelé. "
          f"Relancez à blanc : il doit lire « rien à faire ».")
    return 0


def _safe_console() -> None:
    """Never crash on the console's encoding (the lawyer's Windows console
    encodes cp1252, which has no « → »): a character it cannot encode is
    written as a replacement — never an exception halfway through the
    report, or between two batches of ``--appliquer``."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        # A stream that cannot be reconfigured keeps its encoding — nothing to repair.
        pass


def main(argv: Optional[list[str]] = None) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(prog="python -m scripts.migrate_contact_titles")
    parser.add_argument("--appliquer", action="store_true",
                        help="écrire (par défaut : afficher seulement)")
    parser.add_argument("--resynchroniser", action="store_true",
                        help="réestampiller TOUS les contacts pour que DavX5 "
                             "les retélécharge (à blanc : les compter)")
    args = parser.parse_args(argv if argv is not None else [])

    print(f"Projet : {db.project}")
    try:
        snaps = _read_all()
    except Exception as exc:
        print(f"Lecture des contacts impossible ({type(exc).__name__}) — rien n'a été écrit.")
        return 1
    if not snaps:
        print("Aucun contact lu — mauvais projet ? Rien n'a été écrit.")
        return 1
    plans = [_plan(snap) for snap in snaps]
    if not describe(plans, args.resynchroniser):
        print("Rien à faire.")
        return 0
    if not args.appliquer:
        print("À blanc : rien n'a été écrit. Relancez avec --appliquer.")
        return 0
    return apply(plans, args.resynchroniser)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
