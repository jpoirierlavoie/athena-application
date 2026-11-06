"""Compliance statuses (2026-11, D3-D4) — a one-shot migration that writes
OUTSIDE the model.

What it does
------------
* D3 — a RETIRED status (``utils.kyc.RETIRED_STATUSES``: the identity
  check's « exempté ») is rewritten as its successor (« identifié »). Only
  the status key moves: the check's ``*_date``, ``*_source``,
  ``*_confirmed_at`` and ``*_confirmed_by`` are not in the payload, so the
  lawyer's decision stays his, a presumed Claude inscription stays
  presumed and a confirmed one stays confirmed. The report sorts the
  conversions into these three classes.
* D4 — a DECIDED check with no date gets ``{field}_date = created_at``.
  A client created through the form with a decided status before the
  2026-09-29 push, when ``create_partie`` stamped nothing, shows no « le …
  par Me … », and re-saving the same status never stamps. Every condition
  must hold: the status, read as every reader reads it
  (``kyc.stored_status``), is decided; the date is absent or ``None``; the
  source is absent or ``""``, which already reads as the lawyer, so the
  script never writes « juriste »; ``created_at`` is a datetime; and the
  record carries no ``created_via``. That key reached production in the
  same push as the creation stamp, so a dateless decision on a record that
  carries it is not legacy data: it is reported, never dated.
* It REPORTS, and never writes (« À examiner »): a dateless decision that
  has a source, or whose record has no readable ``created_at`` or carries
  ``created_via``; a status outside the vocabulary (its value is never
  printed); a date that is not a datetime; an undecided check that still
  carries a date, a source or a confirmation, the inverse of the PA-D07
  invariant « date present ⇔ status decided ».

D3 and D4 on one contact form ONE payload: one update, one new etag.

Why it writes outside the model
-------------------------------
« One door per rule » (CLAUDE.md) sends a contact write through
``models.partie``. That door cannot make D4 at all: ``_normalize`` pops
every provenance key from a payload, and ``kyc.apply_status_transition``
stamps « now » and the writer's source on a status transition — never a
past date, and nothing on an unchanged status. D3 it makes only as a side
effect of a full-document re-save (``_migrate_kyc`` hands
``_update_partie`` the successor, provenance kept), and ``update_partie``
re-validates the whole record — a legacy contact holding an unreadable
phone number would refuse a change it is not about — and never bumps the
CTag. So the script writes only the status and date keys above plus the
house stamps (``provenance.update_fields`` under ``writing_via("script")``:
a new ``etag``, ``updated_at``, ``updated_via = "script"``), each contact
one partial ``update()`` guarded by the ``update_time`` it was read at, and
stages ``dav.sync.bump_ctag_in_batch(…, "parties")`` in every batch
(CLAUDE.md item 2: every write to a DAV-exposed record bumps its CTag).
The written contacts keep their relative order in the « recently
modified » list: each gets ``now`` plus its rank in microseconds, oldest
first.

Running it
----------
ONLY once the release reading « identifié » serves 100 % of the traffic:
the older code reads « identifié » as undecided, and its form erases it.

    python -m scripts.migrate_compliance_statuses               # à blanc
    python -m scripts.migrate_compliance_statuses --appliquer

Dry run by default; ``--appliquer`` writes. Idempotent: once applied, a
dry run reads « Rien à faire. » and still lists what is « À examiner », for
the lawyer to settle on the fiche. A read failure stops the run before
anything is written. The output names contacts by id — never a person's
name, never an offending value.

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

from dav.sync import bump_ctag_in_batch  # noqa: E402
from models import db, provenance  # noqa: E402
from models.partie import COLLECTION  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils import kyc  # noqa: E402

DAV_COLLECTION = "parties"
# Contacts per batch (the precedent's size, under Firestore's former
# 500-write cap); each batch adds its own CTag bump.
CHUNK = 400

# The report's name for each check: one per kyc.FIELDS (pinned by the test).
CHECK_LABELS: dict[str, str] = {
    kyc.FIELD_IDENTITY: "identité",
    kyc.FIELD_CONFLICT: "conflits",
}

# D3's classes, in report order: who inscribed the retired status.
LAWYER = "décidé par le juriste"
PRESUMED = "inscrit par Claude, présumé — il le reste"
CONFIRMED = "inscrit par Claude, confirmé — il le reste"
CLASSES: tuple[str, ...] = (LAWYER, PRESUMED, CONFIRMED)

# « À examiner »: reported, never written.
WITH_SOURCE = "décidé sans date, mais avec une source"
NO_CREATION = "décidé sans date, sur une fiche sans date de création lisible"
AFTER_FIX = "décidé sans date sur une fiche créée après le correctif"
OUT_OF_VOCABULARY = "statut hors vocabulaire"
NOT_A_TIMESTAMP = "date qui n'est pas un horodatage"


@dataclass
class Plan:
    """What one contact needs, read from its snapshot."""

    snap: object
    changes: dict = field(default_factory=dict)
    retired: list = field(default_factory=list)   # (check, retired key, class)
    dated: list = field(default_factory=list)     # (check, created_at)
    review: list = field(default_factory=list)    # (check, reason)


def _class(doc: dict, check: str) -> str:
    """Who inscribed the check — the readings of ``kyc.is_presumed``."""
    if kyc.source_of(doc, check) != kyc.SOURCE_MCP:
        return LAWYER
    return CONFIRMED if doc.get(kyc.confirmed_at_key(check)) else PRESUMED


def _undatable(doc: dict, check: str) -> list[str]:
    """Why a dateless decision may NOT take ``created_at`` (D4); empty when
    it may."""
    reasons = []
    if doc.get(kyc.source_key(check)):
        reasons.append(WITH_SOURCE)
    if not isinstance(doc.get("created_at"), datetime):
        reasons.append(NO_CREATION)
    if doc.get("created_via"):
        reasons.append(AFTER_FIX)
    return reasons


def _traces(doc: dict, check: str) -> list[str]:
    """What an undecided check still carries (PA-D07's invariant, inverted)."""
    traces = []
    if doc.get(kyc.date_key(check)) is not None:
        traces.append("une date")
    if doc.get(kyc.source_key(check)):
        traces.append("une source")
    if doc.get(kyc.confirmed_at_key(check)) or doc.get(kyc.confirmed_by_key(check)):
        traces.append("une confirmation")
    return traces


def _plan(snap) -> Plan:
    doc = snap.to_dict() or {}
    plan = Plan(snap=snap)
    for check in kyc.FIELDS:
        raw = doc.get(check)
        retired = kyc.RETIRED_STATUSES[check]
        if isinstance(raw, str) and raw in retired:
            plan.changes[check] = retired[raw]
            plan.retired.append((check, raw, _class(doc, check)))
        date = doc.get(kyc.date_key(check))
        if date is not None and not isinstance(date, datetime):
            plan.review.append((check, NOT_A_TIMESTAMP))
        status = kyc.stored_status(doc, check)
        if status not in kyc.STATUSES[check]:
            plan.review.append((check, OUT_OF_VOCABULARY))
        elif status in kyc.DECIDED[check]:
            if date is None:
                reasons = _undatable(doc, check)
                if reasons:
                    plan.review.extend((check, reason) for reason in reasons)
                else:
                    plan.changes[kyc.date_key(check)] = doc["created_at"]
                    plan.dated.append((check, doc["created_at"]))
        else:
            traces = _traces(doc, check)
            if traces:
                label = kyc.STATUS_LABELS[status]
                plan.review.append(
                    (check, f"« {label} » qui porte encore {', '.join(traces)}"))
    return plan


def _read_all() -> list:
    """Every contact, oldest ``updated_at`` first. Raises on a failed read."""
    snaps = list(db.collection(COLLECTION).stream())
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def _key(snap):
        value = (snap.to_dict() or {}).get("updated_at")
        return value if isinstance(value, datetime) else floor

    return sorted(snaps, key=_key)


def _day(instant: datetime) -> str:
    """The Montréal day of an instant (Rule 5): the day the fiche shows."""
    return to_mtl(instant).strftime("%Y-%m-%d")


def describe(plans: list[Plan]) -> int:
    """Print the plan; return how many contacts would be written."""
    for check in kyc.FIELDS:
        for old, new in kyc.RETIRED_STATUSES[check].items():
            rows = [(p.snap.id, cls) for p in plans
                    for c, key, cls in p.retired if c == check and key == old]
            print(f"\n« {old.capitalize()} » → « {kyc.STATUS_LABELS[new]} » "
                  f"({CHECK_LABELS[check]}) : {len(rows)}")
            for cls in CLASSES:
                ids = [pid for pid, c in rows if c == cls]
                print(f"  {cls} : {len(ids)}")
                for pid in ids:
                    print(f"    {pid}")

    dated = [(p.snap.id, c, created) for p in plans for c, created in p.dated]
    print(f"\nDécision sans date → date de création de la fiche : {len(dated)}")
    for check in kyc.FIELDS:
        rows = [(pid, created) for pid, c, created in dated if c == check]
        print(f"  {CHECK_LABELS[check]} : {len(rows)}")
        for pid, created in rows:
            print(f"    {pid} : {_day(created)}")

    review = [(p.snap.id, c, reason) for p in plans for c, reason in p.review]
    print(f"\nÀ examiner — rien n'est écrit : {len(review)}")
    for pid, check, reason in review:
        print(f"  {pid} ({CHECK_LABELS[check]}) : {reason}")

    writes = sum(1 for p in plans if p.changes)
    print(f"\nContacts à corriger : {writes} sur {len(plans)}.")
    return writes


def _landed(ref, etag: str) -> Optional[bool]:
    try:
        return (ref.get().to_dict() or {}).get("etag") == etag
    except Exception:
        return None


def apply(plans: list[Plan]) -> int:
    targets = [p for p in plans if p.changes]
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
          f"Relancez à blanc : il doit lire « Rien à faire. »")
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
    parser = argparse.ArgumentParser(prog="python -m scripts.migrate_compliance_statuses")
    parser.add_argument("--appliquer", action="store_true",
                        help="écrire (par défaut : afficher seulement)")
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
    if not describe(plans):
        print("Rien à faire.")
        return 0
    if not args.appliquer:
        print("N'appliquez qu'une fois la nouvelle version servie à 100 % — "
              "l'ancienne efface « Identifié ».")
        print("À blanc : rien n'a été écrit. Relancez avec --appliquer.")
        return 0
    return apply(plans)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
