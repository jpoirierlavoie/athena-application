"""The one-shot ``scripts/migrate_compliance_statuses`` (2026-11, D3-D4).

Against the REAL Firestore client (only the server is fake), as
``test_migrate_contact_titles.py``: a dry run writes nothing and names no
one; ``--appliquer`` rewrites a retired « exempté » as « identifié » with
its provenance kept, dates a legacy decision with its ``created_at``,
stamps, and bumps the CTag in every batch; everything else is only
reported; a contact written between the read and the write fails its
batch and is never overwritten.

The seeds come from ``models.partie._default_doc()``. Their dates are in a
fictional year (public repository).
"""

import io
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    from models import partie as pm
    import scripts.migrate_compliance_statuses as script

from tests._fake_firestore import install  # noqa: E402
from utils import kyc  # noqa: E402

UTC = timezone.utc
IDENTITY = kyc.FIELD_IDENTITY
# 02:30 UTC is still the previous day in Montréal: the report must print
# the day the fiche will show, never the UTC one.
CREATED = datetime(2031, 3, 2, 2, 30, tzinfo=UTC)
CREATED_DAY = "2031-03-01"
DECIDED = datetime(2031, 4, 7, 15, 0, tzinfo=UTC)
CONFIRMED = datetime(2031, 4, 9, 13, 0, tzinfo=UTC)
UNREADABLE_DATE = "2031-04-07"
UNKNOWN_STATUS = "presque-fait"
MISSING = object()

WRITTEN = ("a-exempte-juriste", "b-exempte-presume", "c-exempte-confirme",
           "d-sans-date", "e-exempte-sans-date")
STAMPS = {"updated_at", "etag", "updated_via"}

RETIRED_BLOCK = (
    "« Exempté » → « Identifié » (identité) : 4",
    "  décidé par le juriste : 2",
    "    e-exempte-sans-date",
    "    a-exempte-juriste",
    "  inscrit par Claude, présumé — il le reste : 1",
    "    b-exempte-presume",
    "  inscrit par Claude, confirmé — il le reste : 1",
    "    c-exempte-confirme",
)
DATED_BLOCK = (
    "Décision sans date → date de création de la fiche : 3",
    "  identité : 2",
    f"    d-sans-date : {CREATED_DAY}",
    f"    e-exempte-sans-date : {CREATED_DAY}",
    "  conflits : 1",
    f"    d-sans-date : {CREATED_DAY}",
)
REVIEW_BLOCK = (
    "À examiner — rien n'est écrit : 9",
    "  f-source-sans-date (identité) : décidé sans date, mais avec une source",
    "  g-sans-creation (identité) : décidé sans date, sur une fiche sans date de création lisible",
    "  i-hors-vocabulaire (identité) : statut hors vocabulaire",
    "  i-hors-vocabulaire (conflits) : statut hors vocabulaire",
    "  j-apres-correctif (identité) : décidé sans date sur une fiche créée après le correctif",
    "  k-a-faire-date (identité) : « À faire » qui porte encore une date",
    "  l-date-illisible (identité) : date qui n'est pas un horodatage",
    "  m-a-faire-source (conflits) : « À faire » qui porte encore une source",
    "  n-a-faire-confirme (identité) : « À faire » qui porte encore une confirmation",
)


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch, dav_sync, script)


def _seed(fake, pid, minutes, *, legacy=False, **over):
    """A contact from the model's own defaults, last updated *minutes* after
    its creation. ``legacy`` models a record written before the 2026-09-29
    push: no ``*_source`` key and no ``created_via``. A key given MISSING is
    left out."""
    doc = {**pm._default_doc(), "id": pid, "type": "individual",
           "contact_role": "client", "first_name": "Ginette",
           "last_name": "Labrecque", "vcard_uid": f"u-{pid}", "etag": f"e-{pid}",
           "created_at": CREATED, "updated_at": CREATED + timedelta(minutes=minutes)}
    if legacy:
        for check in kyc.FIELDS:
            doc.pop(kyc.source_key(check))
        doc.pop("created_via", None)
    doc.update(over)
    fake.seed(f"parties/{pid}", {k: v for k, v in doc.items() if v is not MISSING})


def _contacts(fake):
    """One contact per case of the plan, last updated in an order that is
    NOT their id order (the order the script must keep)."""
    # D3: a retired « exempté » by each hand; each keeps its provenance.
    _seed(fake, "a-exempte-juriste", 50, identity_verified="exempté",
          identity_verified_date=DECIDED, identity_verified_source="juriste")
    _seed(fake, "b-exempte-presume", 10, identity_verified="exempté",
          identity_verified_date=DECIDED, identity_verified_source="mcp")
    _seed(fake, "c-exempte-confirme", 40, identity_verified="exempté",
          identity_verified_date=DECIDED, identity_verified_source="mcp",
          identity_verified_confirmed_at=CONFIRMED,
          identity_verified_confirmed_by="Me Exemple")
    # D4: dateless, sourceless decisions on a pre-fix record.
    _seed(fake, "d-sans-date", 20, legacy=True, identity_verified="vérifié",
          conflict_check="conflit_détecté")
    _seed(fake, "e-exempte-sans-date", 30, legacy=True, identity_verified="exempté")
    # Reported, never written.
    _seed(fake, "f-source-sans-date", 60, legacy=True, identity_verified="vérifié",
          identity_verified_source="juriste")
    _seed(fake, "g-sans-creation", 70, legacy=True, identity_verified="vérifié",
          created_at=MISSING)
    _seed(fake, "h-a-faire", 80)
    _seed(fake, "i-hors-vocabulaire", 90, identity_verified=UNKNOWN_STATUS,
          conflict_check=["vérifié"])
    _seed(fake, "j-apres-correctif", 100, identity_verified="vérifié",
          created_via="web")
    _seed(fake, "k-a-faire-date", 110, identity_verified_date=DECIDED)
    _seed(fake, "l-date-illisible", 120, identity_verified="vérifié",
          identity_verified_date=UNREADABLE_DATE)
    # The inverse PA-D07 anomaly in its two other forms: an « À faire »
    # check carrying a source, and one carrying a confirmation.
    _seed(fake, "m-a-faire-source", 130, conflict_check_source="mcp")
    _seed(fake, "n-a-faire-confirme", 140,
          identity_verified_confirmed_at=CONFIRMED,
          identity_verified_confirmed_by="Me Exemple")


def _changed(before: dict, after: dict) -> set:
    return {key for key in set(before) | set(after)
            if before.get(key, MISSING) != after.get(key, MISSING)}


def _boom(*_args, **_kwargs):
    raise gexc.ServiceUnavailable("firestore indisponible")


def test_the_report_names_every_check():
    assert set(script.CHECK_LABELS) == set(kyc.FIELDS)


def test_a_dry_run_writes_nothing_and_names_no_one(fake, capsys):
    _contacts(fake)
    before = fake.peek_collection("parties")
    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert fake.peek_collection("parties") == before
    assert fake.commits == []
    assert "Ginette" not in out and "Labrecque" not in out
    assert "Me Exemple" not in out
    # The offending values themselves are never printed.
    assert UNKNOWN_STATUS not in out and "['vérifié']" not in out
    assert UNREADABLE_DATE not in out
    for block in (RETIRED_BLOCK, DATED_BLOCK, REVIEW_BLOCK):
        assert "\n".join(block) in out
    assert "h-a-faire" not in out          # nothing to write, nothing to review
    assert out.rstrip().endswith("\n".join((
        "Contacts à corriger : 5 sur 14.",
        ("N'appliquez qu'une fois la nouvelle version servie à 100 % — "
         "l'ancienne efface « Identifié »."),
        "À blanc : rien n'a été écrit. Relancez avec --appliquer.",
    )))


def test_appliquer_converts_dates_stamps_and_bumps_the_ctag(fake, capsys):
    _contacts(fake)
    fake.seed("dav_sync/parties", {"ctag": "c0", "sync_token": "c0"})
    before = fake.peek_collection("parties")
    assert script.main(["--appliquer"]) == 0
    after = fake.peek_collection("parties")

    # D3: the successor, with the check's date, source and confirmation
    # byte-identical: each class stays what it was.
    for pid in ("a-exempte-juriste", "b-exempte-presume", "c-exempte-confirme"):
        assert after[pid][IDENTITY] == "identifié"
        assert _changed(before[pid], after[pid]) == {IDENTITY} | STAMPS
    assert kyc.is_decided(after["a-exempte-juriste"], IDENTITY)
    assert kyc.is_presumed(after["b-exempte-presume"], IDENTITY)
    assert kyc.is_decided(after["c-exempte-confirme"], IDENTITY)

    # D4: the creation instant, the source left empty (never « juriste »).
    d = after["d-sans-date"]
    assert d["identity_verified_date"] == CREATED
    assert d["conflict_check_date"] == CREATED
    assert _changed(before["d-sans-date"], d) == (
        {"identity_verified_date", "conflict_check_date"} | STAMPS)
    e = after["e-exempte-sans-date"]
    assert e[IDENTITY] == "identifié" and e["identity_verified_date"] == CREATED
    assert _changed(before["e-exempte-sans-date"], e) == (
        {IDENTITY, "identity_verified_date"} | STAMPS)
    for doc in (d, e):
        assert kyc.source_key(IDENTITY) not in doc
        assert kyc.is_decided(doc, IDENTITY)

    # One batch: one update per written contact, (e)'s D3 and D4 in ONE,
    # and the CTag bump in the same commit.
    (commit,) = fake.commits
    assert sorted(commit.ops) == sorted(
        [("update", f"parties/{pid}") for pid in WRITTEN]
        + [("set", "dav_sync/parties")])
    assert commit.ops.count(("update", "parties/e-exempte-sans-date")) == 1
    for pid in WRITTEN:
        assert after[pid]["etag"] != before[pid]["etag"]
        assert after[pid]["updated_via"] == "script"
    for pid in set(after) - set(WRITTEN):
        assert after[pid] == before[pid], pid
    assert dav_sync.get_ctag("parties") != "c0"
    assert capsys.readouterr().out.rstrip().endswith(
        "Écrit : 5 contact(s) ; CTag « parties » renouvelé. "
        "Relancez à blanc : il doit lire « Rien à faire. »")


def test_a_second_pass_has_nothing_to_do_and_still_lists_what_to_review(fake, capsys):
    _contacts(fake)
    assert script.main(["--appliquer"]) == 0
    commits = len(fake.commits)
    capsys.readouterr()
    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert "« Exempté » → « Identifié » (identité) : 0" in out
    assert "Décision sans date → date de création de la fiche : 0" in out
    assert "\n".join(REVIEW_BLOCK) in out
    assert "N'appliquez" not in out
    assert out.rstrip().endswith("Contacts à corriger : 0 sur 14.\nRien à faire.")
    assert len(fake.commits) == commits


def test_every_batch_bumps_the_ctag_and_the_written_keep_their_order(fake, monkeypatch):
    monkeypatch.setattr(script, "CHUNK", 2)
    _contacts(fake)
    before = fake.peek_collection("parties")
    assert script.main(["--appliquer"]) == 0
    after = fake.peek_collection("parties")
    assert len(fake.commits) == 3
    for commit in fake.commits:
        assert commit.ops.count(("set", "dav_sync/parties")) == 1
    assert sorted(op for commit in fake.commits for op in commit.ops
                  if op[0] == "update") == sorted(
        ("update", f"parties/{pid}") for pid in WRITTEN)
    oldest_first = sorted(WRITTEN, key=lambda pid: before[pid]["updated_at"])
    assert oldest_first != sorted(WRITTEN)   # the seeds do test the sort
    assert sorted(WRITTEN, key=lambda pid: after[pid]["updated_at"]) == oldest_first
    assert len({after[pid]["updated_at"] for pid in WRITTEN}) == len(WRITTEN)


def test_a_contact_written_during_the_run_is_not_overwritten(fake, monkeypatch, capsys):
    _contacts(fake)
    fake.seed("dav_sync/parties", {"ctag": "c0", "sync_token": "c0"})
    describe = script.describe

    def _then_someone_edits(plans):
        n = describe(plans)
        fake.external_write("parties/b-exempte-presume", {
            **fake.peek("parties/b-exempte-presume"), "first_name": "Ginou"})
        return n

    monkeypatch.setattr(script, "describe", _then_someone_edits)
    assert script.main(["--appliquer"]) == 1
    b = fake.peek("parties/b-exempte-presume")
    assert b["first_name"] == "Ginou" and b[IDENTITY] == "exempté"
    # The same batch: nothing of it, the CTag bump included.
    assert fake.peek("parties/a-exempte-juriste")[IDENTITY] == "exempté"
    assert fake.peek("parties/d-sans-date")["identity_verified_date"] is None
    assert fake.peek("dav_sync/parties")["ctag"] == "c0"
    assert "Lot 1 : rien n'a été écrit (FailedPrecondition)" in capsys.readouterr().out


@pytest.mark.parametrize("created", [MISSING, None, "2031-03-02"],
                         ids=["absent", "none", "text"])
def test_a_dateless_decision_needs_a_readable_creation_instant(fake, capsys, created):
    _seed(fake, "sans-creation", 1, legacy=True, identity_verified="vérifié",
          created_at=created)
    assert script.main(["--appliquer"]) == 0
    out = capsys.readouterr().out
    assert ("  sans-creation (identité) : décidé sans date, sur une fiche "
            "sans date de création lisible") in out
    assert "2031-03-02" not in out
    assert out.rstrip().endswith("Rien à faire.")
    assert fake.peek("parties/sans-creation")["identity_verified_date"] is None
    assert fake.commits == []


def test_a_lost_commit_answer_is_settled_by_rereading(fake, monkeypatch, capsys):
    _contacts(fake)
    commit = fake._fake_server.commit

    def _commit_then_lose_the_answer(*args, **kwargs):
        commit(*args, **kwargs)
        raise gexc.ServiceUnavailable("réponse perdue")

    monkeypatch.setattr(fake._fake_server, "commit", _commit_then_lose_the_answer)
    assert script.main(["--appliquer"]) == 0
    out = capsys.readouterr().out
    assert ("Lot 1 écrit — la réponse du serveur s'était perdue "
            "(ServiceUnavailable), la relecture le confirme.") in out
    assert "Écrit : 5 contact(s)" in out
    assert fake.peek("parties/e-exempte-sans-date")[IDENTITY] == "identifié"


def test_an_unsettled_outcome_is_reported_as_uncertain(fake, monkeypatch, capsys):
    _contacts(fake)
    before = fake.peek_collection("parties")
    monkeypatch.setattr(fake._fake_server, "commit", _boom)
    monkeypatch.setattr(fake._fake_server, "batch_get_documents", _boom)
    assert script.main(["--appliquer"]) == 1
    assert "Lot 1 : issue INCERTAINE (ServiceUnavailable)" in capsys.readouterr().out
    assert fake.peek_collection("parties") == before


def test_a_failed_read_writes_nothing(fake, monkeypatch, capsys):
    _contacts(fake)
    before = fake.peek_collection("parties")
    monkeypatch.setattr(fake._fake_server, "run_query", _boom)
    assert script.main(["--appliquer"]) == 1
    assert ("Lecture des contacts impossible (ServiceUnavailable) — rien n'a "
            "été écrit.") in capsys.readouterr().out
    assert fake.commits == []
    assert fake.peek_collection("parties") == before


def test_no_contact_read_is_a_failure(fake, capsys):
    assert script.main(["--appliquer"]) == 1
    assert "Aucun contact lu — mauvais projet ?" in capsys.readouterr().out
    assert fake.commits == []


def test_a_usage_error_exits_with_code_2(fake):
    with pytest.raises(SystemExit) as exit_:
        script.main(["--inconnu"])
    assert exit_.value.code == 2
    assert fake.commits == []


def test_a_cp1252_console_does_not_crash_the_report(fake, monkeypatch):
    """The lawyer's Windows console encodes cp1252, which has no « → »:
    the report's first header would raise UnicodeEncodeError."""
    _contacts(fake)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert script.main([]) == 0
    sys.stdout.flush()
    assert "« Exempté » ? « Identifié »".encode("cp1252") in raw.getvalue()
    assert "Contacts à corriger : 5 sur 14.".encode("cp1252") in raw.getvalue()
