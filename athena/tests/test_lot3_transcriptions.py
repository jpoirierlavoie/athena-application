"""Tests unitaires et d'intégration — Lot 3 : transcriptions Teams & Copilot.

Couvre de bout en bout (sans jamais importer ``main`` ni toucher Firestore réel) :
- ``utils/webvtt.py`` : parsing WebVTT multi-locuteurs, fusion de tours, échappement
  de chevrons, mise en forme Markdown des ``aiInsights`` Copilot ;
- ``utils/transcription_docx.py`` : génération OOXML ``.docx`` (transcription
  horodatée par locuteur + note autonome), validation ``defusedxml``,
  ``_structural_errors == []``, signature PK et extraction texte ;
- ``utils/graph_teams.py`` : liste/téléchargement WebVTT borné et repli propre
  HTTP 400/403 sur ``aiInsights`` (``copilot_disponible=False``) ;
- ``services/transcription_reunion.py`` : zéro appel Graph s'il n'y a aucune
  réunion terminée, fenêtre d'attente Copilot 2 h / contournement manuel /
  basculer « aucune » après 24 h, versement sans OneDrive (prod actuelle),
  versement avec OneDrive + reprise idempotente après panne partielle, rejet ;
- Cron ``/taches/transcriptions/sync`` et routes Web ``hearings`` / ``reception`` ;
- Connecteur MCP : ``get_meeting_transcripts``, ``decide_meeting_transcript``
  (``category_source="mcp"``), et alignement portail de ``decide_rendez_vous``
  et du verrou D10 sur ``update_hearing``.
"""

from __future__ import annotations

import io
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from defusedxml import ElementTree as DET
from flask import Flask

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "avocat@cabinet.example")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.write_support as write_support
    from config import Config
    from models import doc_template as doc_template_model
    from models import document as document_model
    from models import dossier as dossier_model
    from models import folder as folder_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import transcription_reunion as tr_model
    from routes import hearings as hearings_route
    from routes import reception as reception_route
    from routes import taches_transcriptions as taches_route
    from services import courrier as courrier_service
    from services import transcription_reunion as tx_service
    from utils import docx_fill, graph_courrier, graph_teams, pdf_text, transcription_docx, webvtt
    from utils.graph import GraphError
    from utils.icons import ms

from tests._fake_firestore import install  # noqa: E402

_LOADED = (
    dav_sync,
    doc_template_model,
    document_model,
    dossier_model,
    folder_model,
    hearing_model,
    note_model,
    tr_model,
    write_support,
)
_ = _LOADED

UTC = timezone.utc


def _fake_modules() -> list:
    return [
        m
        for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
        and getattr(m, "db", None) is not None
    ]


class _FakeBlob:
    def __init__(self, store: dict[str, dict], path: str):
        self._store = store
        self.name = path
        self.size = 0
        self.generation = 1

    def upload_from_string(self, data, content_type: str = "") -> None:
        raw = bytes(data)
        self._store[self.name] = {"bytes": raw, "content_type": content_type}
        self.size = len(raw)

    def download_as_bytes(self) -> bytes:
        if self.name not in self._store:
            raise KeyError(self.name)
        return self._store[self.name]["bytes"]

    def reload(self) -> None:
        if self.name in self._store:
            self.size = len(self._store[self.name]["bytes"])

    def delete(self, if_generation_match=None) -> None:
        self._store.pop(self.name, None)


class _FakeBucket:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def blob(self, path: str) -> _FakeBlob:
        return _FakeBlob(self.objects, path)


@pytest.fixture
def fake_world(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    bucket = _FakeBucket()
    monkeypatch.setattr(tr_model.storage, "bucket", lambda *_a, **_kw: bucket)
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", True)
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_SECRET", "secret-1")
    monkeypatch.setattr(Config, "GRAPH_SENDER_UPN", "reception@cabinet.example")
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_OBJECT_ID", "obj-1")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "")
    monkeypatch.setattr(Config, "COURRIER_UPN", "")

    fake.seed("dossiers/d1", {
        "id": "d1",
        "file_number": "2026-001",
        "title": "Tremblay c. Roy",
        "status": "actif",
        "etag": "etag-d1",
    })
    fake.seed("dav_sync/dossier:d1", {"ctag": "ctag-0"})
    fake.seed("dav_sync/general", {"ctag": "gctag-0"})
    return fake, bucket


def _seed_hearing(
    fake,
    hid: str = "h1",
    *,
    online_meeting_id: str = "om-1",
    transcript_status: str = "en_attente",
    end_dt: datetime | None = None,
    dossier_id: str = "d1",
    confirmation: str = "",
    status: str = "confirmée",
    source: str = "portail",
) -> None:
    end_val = end_dt or datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    fake.seed(f"hearings/{hid}", {
        "id": hid,
        "title": "Consultation initiale",
        "hearing_type": "consultation",
        "start_datetime": end_val - timedelta(hours=1),
        "end_datetime": end_val,
        "all_day": False,
        "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "status": status,
        "source": source,
        "confirmation": confirmation,
        "online_meeting_id": online_meeting_id,
        "transcript_status": transcript_status,
        "client_nom": "Alice Tremblay",
        "client_email": "alice@example.com",
        "etag": f"etag-{hid}",
    })


# ── 1. WebVTT & DOCX pure helpers ────────────────────────────────────────


def test_webvtt_parses_speakers_coalesces_and_sanitizes_copilot():
    raw = (
        "\ufeffWEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "<v Me Dupont>Bonjour &amp; bienvenue.</v>\n\n"
        "00:00:06.000 --> 00:00:09.000\n"
        "<v Me Dupont>Parlons du dossier.</v>\n\n"
        "00:00:20.000 --> 00:00:25.000\n"
        "<v Client>Merci Maître.</v>\n"
    ).encode("utf-8")
    cues = webvtt.parse_webvtt(raw)
    assert len(cues) == 2
    assert cues[0].speaker == "Me Dupont"
    assert cues[0].text == "Bonjour & bienvenue. Parlons du dossier."
    assert cues[1].speaker == "Client"
    assert webvtt.extract_speakers(cues) == ["Me Dupont", "Client"]
    preview = webvtt.format_transcript_preview(cues, max_chars=200)
    assert "[00:00:01] Me Dupont :" in preview

    md = webvtt.format_copilot_insights_markdown({
        "meetingNotes": [{
            "title": "Points clés <confidentiel>",
            "text": "Résumé exécutif.",
            "subpoints": [{"title": "Délai", "text": "30 jours"}],
        }],
        "actionItems": [{
            "ownerDisplayName": "Me Dupont",
            "title": "Mise en demeure",
            "text": "Préparer projet avant vendredi.",
        }],
    })
    assert "‹confidentiel›" in md
    assert "<confidentiel>" not in md
    assert "## Suivis et actions" in md


def test_transcription_docx_builders_produce_valid_ooxml():
    cues = webvtt.parse_webvtt(
        "WEBVTT\n\n00:00:02.000 --> 00:00:06.000\n<v Me Roy>Allô & co.</v>\n"
    )
    tx_bytes = transcription_docx.build_transcript_docx(
        title="Rencontre & suivi",
        dossier_label="2026-001 — Tremblay c. Roy",
        date_label="20 novembre 2026",
        speakers=["Me Roy"],
        cues=cues,
    )
    assert tx_bytes[:4] == b"PK\x03\x04"
    errs_tx, zf_tx = docx_fill._structural_errors(tx_bytes)
    zf_tx.close()
    assert errs_tx == []
    with zipfile.ZipFile(io.BytesIO(tx_bytes)) as zf:
        DET.fromstring(zf.read("word/document.xml"))
    extracted_tx = "\n".join(pdf_text.extract_docx_text(tx_bytes))
    assert "Me Roy" in extracted_tx and "Allô & co." in extracted_tx

    note_bytes = transcription_docx.build_standalone_note_docx(
        title="Notes Consultation",
        dossier_label="2026-001",
        date_label="20 novembre 2026",
        category_label="Consultation",
        markdown_text="## Synthèse\n\n- Élément 1\n- Élément 2",
    )
    assert note_bytes[:4] == b"PK\x03\x04"
    errs_note, zf_note = docx_fill._structural_errors(note_bytes)
    zf_note.close()
    assert errs_note == []
    with zipfile.ZipFile(io.BytesIO(note_bytes)) as zf:
        DET.fromstring(zf.read("word/document.xml"))
    extracted_note = "\n".join(pdf_text.extract_docx_text(note_bytes))
    assert "Élément 1" in extracted_note


# ── 2. Graph Teams transcripts & aiInsights ──────────────────────────────


def test_graph_teams_ai_insights_403_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(Config, "TEAMS_REUNIONS_ACTIF", True)
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "t1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_ID", "c1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_SECRET", "s1")
    monkeypatch.setattr(Config, "GRAPH_SENDER_UPN", "reception@cabinet.example")
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_OBJECT_ID", "obj-1")

    def fake_graph_get(path, **_kw):
        if path.endswith("/aiInsights"):
            raise GraphError("Échec d'un GET Graph (HTTP 403).")
        return {"value": [{"id": "tx-1", "createdDateTime": "2026-11-20T15:05:00Z"}]}

    monkeypatch.setattr("utils.graph.graph_get", fake_graph_get)
    transcripts = graph_teams.lister_transcriptions_reunion("om-1")
    assert len(transcripts) == 1
    insights, copilot_ok = graph_teams.lister_insights_reunion("om-1")
    assert insights == [] and copilot_ok is False


# ── 3. Service capture & idempotent Verser / Ecarter ─────────────────────


def test_cron_makes_zero_graph_calls_when_no_ended_meeting(fake_world, monkeypatch):
    fake, _bucket = fake_world
    now = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
    _seed_hearing(fake, "h-future", end_dt=now + timedelta(minutes=30))

    graph_calls = []
    monkeypatch.setattr(
        graph_teams,
        "lister_transcriptions_reunion",
        lambda mid: graph_calls.append(mid) or [],
    )

    counts = tx_service.verifier_reunions_terminees(now=now)
    assert counts["eligibles"] == 0
    assert graph_calls == []


def test_copilot_2h_wait_vs_manual_override_and_24h_giveup(fake_world, monkeypatch):
    fake, bucket = fake_world
    end_dt = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
    _seed_hearing(fake, "h1", end_dt=end_dt)

    monkeypatch.setattr(
        graph_teams,
        "lister_transcriptions_reunion",
        lambda _mid: [{"id": "t1", "createdDateTime": "2026-11-20T14:05:00Z"}],
    )
    monkeypatch.setattr(graph_teams, "lister_insights_reunion", lambda _mid: ([], True))
    monkeypatch.setattr(
        graph_teams,
        "lire_contenu_transcription_vtt",
        lambda _mid, _tid: b"WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Avocat>Allo</v>\n",
    )

    # 30 minutes après la fin : attend Copilot (ne déclenche rien)
    res_wait = tx_service.verifier_reunions_terminees(now=end_dt + timedelta(minutes=30))
    assert res_wait["attente"] == 1 and res_wait["pretes"] == 0
    assert bucket.objects == {}

    # Bouton manuel (« forcer_sans_attente=True ») : prépare immédiatement
    outcome, errors, doc = tx_service.verifier_une_reunion(
        "h1", forcer_sans_attente=True, now=end_dt + timedelta(minutes=30)
    )
    assert outcome == "a_revoir" and errors == []
    assert doc["has_transcript"] is True and doc["has_insights"] is False
    assert doc["staged_vtt_path"] in bucket.objects
    assert fake.peek("hearings/h1")["transcript_status"] == "a_revoir"

    # Deuxième réunion après 24 h sans rien : marquée « aucune »
    _seed_hearing(fake, "h2", end_dt=end_dt)
    monkeypatch.setattr(graph_teams, "lister_transcriptions_reunion", lambda _mid: [])
    res_old = tx_service.verifier_reunions_terminees(now=end_dt + timedelta(hours=25))
    assert res_old["aucune"] == 1
    assert fake.peek("hearings/h2")["transcript_status"] == "aucune"


def test_verser_without_onedrive_and_with_onedrive_partial_retry_idempotency(
    fake_world, monkeypatch
):
    fake, bucket = fake_world
    _seed_hearing(fake, "h1")
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Me Dupont>Discussion.</v>\n"
    staged_path = tr_model.stage_vtt_bytes(vtt, user_id="u1", hearing_id="h1")
    tr_doc, errs = tr_model.upsert_staged_transcription(
        "h1",
        online_meeting_id="om-1",
        dossier_id="d1",
        transcript_id="t1",
        has_transcript=True,
        transcript_cue_count=1,
        transcript_speakers=["Me Dupont"],
        transcript_preview="[00:00:01] Me Dupont : Discussion.",
        staged_vtt_path=staged_path,
        insight_id="i1",
        has_insights=True,
        copilot_markdown="## Synthèse\n\n- Accord de principe",
    )
    assert not errs

    ingested = []

    def fake_ingest(_blob, did, _fn, fname, meta, _uid, **kw):
        doc_id = kw.get("document_id")
        doc_rec = {"id": doc_id, "dossier_id": did, "etag": f"etag-{doc_id}"}
        fake.seed(f"documents/{doc_id}", doc_rec)
        ingested.append((doc_id, meta.get("folder_id"), fname))
        return doc_rec, []

    monkeypatch.setattr(document_model, "ingest_blob_as_document", fake_ingest)

    # Cas A : OneDrive non configuré (prod actuelle) -> réussit dans Athéna + avertissement
    doc_a, errs_a, warns_a = tx_service.verser_transcription(
        "h1",
        dossier_id="d1",
        note_title="Consultation — Alice Tremblay",
        note_category="consultation",
        markdown_content="## Synthèse\n\n- Accord de principe",
        expected_etag=tr_doc["etag"],
        user_id="avocat@cabinet.example",
    )
    assert errs_a == []
    assert doc_a["status"] == "versee"
    assert doc_a["versement"]["onedrive_statut"] == "non_configure"
    assert any("OneDrive" in w for w in warns_a)
    assert len(ingested) == 1
    assert staged_path not in bucket.objects
    assert fake.peek("dav_sync/dossier:d1")["ctag"] != "ctag-0"

    # Cas B : OneDrive configuré + échec sur la transcription OneDrive au 1er essai -> reprise sans doublon
    _seed_hearing(fake, "h2")
    staged_h2 = tr_model.stage_vtt_bytes(vtt, user_id="u1", hearing_id="h2")
    tr_h2, _ = tr_model.upsert_staged_transcription(
        "h2",
        online_meeting_id="om-2",
        dossier_id="d1",
        has_transcript=True,
        transcript_cue_count=1,
        transcript_speakers=["Me Dupont"],
        staged_vtt_path=staged_h2,
        has_insights=True,
        copilot_markdown="## Synthèse\n\n- Point 1",
    )
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "cg-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "cg-sec")
    monkeypatch.setattr(Config, "COURRIER_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")

    od_calls = []

    def flaky_onedrive(**kw):
        od_calls.append(kw["nom_fichier"])
        if len(od_calls) == 2:
            raise graph_courrier.CourrierGraphError("OneDrive timeout")
        return {"statut": "cree", "item_id": f"od-item-{len(od_calls)}", "nom": kw["nom_fichier"]}

    monkeypatch.setattr(courrier_service, "deposer_docx_interne_onedrive", flaky_onedrive)

    notes_before = len(fake.peek_collection("notes"))
    partial_tr, errs_b1, _ = tx_service.verser_transcription(
        "h2",
        dossier_id="d1",
        note_title="Rencontre Teams",
        note_category="rencontre",
        markdown_content="## Synthèse\n\n- Point 1",
        expected_etag=tr_h2["etag"],
        user_id="avocat@cabinet.example",
    )
    assert errs_b1 and "OneDrive" in errs_b1[0] and "HTTP" not in errs_b1[0]
    assert partial_tr["status"] == "a_revoir"
    assert len(fake.peek_collection("notes")) == notes_before + 1
    assert partial_tr["versement"]["onedrive_note_item_id"] == "od-item-1"
    assert len(od_calls) == 2

    # 2e essai avec le NOUVEL etag : NE recrée ni la note Athéna, ni le doc Athéna, ni la note OneDrive
    final_tr, errs_b2, _ = tx_service.verser_transcription(
        "h2",
        dossier_id="d1",
        note_title="Rencontre Teams",
        note_category="rencontre",
        markdown_content="## Synthèse\n\n- Point 1",
        expected_etag=partial_tr["etag"],
        user_id="avocat@cabinet.example",
    )
    assert errs_b2 == []
    assert final_tr["status"] == "versee"
    assert len(fake.peek_collection("notes")) == notes_before + 1
    assert len(ingested) == 2  # +1 pour h2 uniquement au 1er essai
    assert len(od_calls) == 3  # uniquement le .docx transcription réessayé!
    assert final_tr["versement"]["onedrive_transcript_item_id"] == "od-item-3"


def test_verser_onedrive_error_surfaces_http_status_code_without_pii(
    fake_world, monkeypatch
):
    fake, _bucket = fake_world
    _seed_hearing(fake, "h-404")
    tr_doc, _ = tr_model.upsert_staged_transcription(
        "h-404",
        online_meeting_id="om-404",
        dossier_id="d1",
        has_transcript=False,
        has_insights=True,
        copilot_markdown="## Synthèse\n\n- Point confidentiel",
    )
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_ID", "cg-id")
    monkeypatch.setattr(Config, "COURRIER_GRAPH_CLIENT_SECRET", "cg-sec")
    monkeypatch.setattr(Config, "COURRIER_UPN", "avocat@cabinet.example")
    monkeypatch.setattr(Config, "COURRIER_ONEDRIVE_ROOT", "Dossiers")

    def fail_404(**_kw):
        raise graph_courrier.CourrierGraphError(
            "Échec d'un GET Graph Courrier (HTTP 404).",
            status_code=404,
        )

    monkeypatch.setattr(courrier_service, "deposer_docx_interne_onedrive", fail_404)

    partial_tr, errs, _ = tx_service.verser_transcription(
        "h-404",
        dossier_id="d1",
        note_title="Consultation — Dr. Test",
        note_category="consultation",
        markdown_content="## Synthèse\n\n- Point confidentiel",
        expected_etag=tr_doc["etag"],
        user_id="avocat@cabinet.example",
    )
    assert partial_tr is not None and partial_tr["status"] == "a_revoir"
    assert errs == [
        "Versement Athéna enregistré, mais le dépôt des notes dans OneDrive a échoué (HTTP 404) — réessayez pour compléter OneDrive sans doublon."
    ]


def test_ecarter_purges_staged_vtt_and_refuses_if_already_versee(fake_world):
    fake, bucket = fake_world
    _seed_hearing(fake, "h1")
    staged = tr_model.stage_vtt_bytes(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n", user_id="u1", hearing_id="h1")
    tr_doc, _ = tr_model.upsert_staged_transcription(
        "h1", online_meeting_id="om-1", staged_vtt_path=staged, has_transcript=True
    )
    assert staged in bucket.objects

    doc, errs = tx_service.ecarter_transcription("h1", expected_etag=tr_doc["etag"])
    assert errs == [] and doc["status"] == "ecartee"
    assert staged not in bucket.objects
    assert fake.peek("hearings/h1")["transcript_status"] == "ecartee"


# ── 4. Cron & Web routes (minimal Flask app, never importing main) ───────


def _make_web_app() -> Flask:
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    app.secret_key = "test-secret"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms)
    app.register_blueprint(taches_route.taches_transcriptions_bp)
    app.register_blueprint(hearings_route.hearings_bp)
    app.register_blueprint(reception_route.reception_bp)
    return app


def test_cron_and_web_routes(fake_world, monkeypatch):
    fake, _bucket = fake_world
    app = _make_web_app()
    client = app.test_client()

    # Cron sans header → 403 ; avec header → 200 (0 graph calls)
    assert client.get("/taches/transcriptions/sync").status_code == 403
    ok_resp = client.get(
        "/taches/transcriptions/sync", headers={"X-Appengine-Cron": "true"}
    )
    assert ok_resp.status_code == 200
    assert ok_resp.get_json()["eligibles"] == 0

    # Session authentifiée pour les routes web
    monkeypatch.setattr("auth.is_session_active", lambda: True)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["email"] = "avocat@cabinet.example"
        sess["mfa_verified"] = True

    _seed_hearing(fake, "h-web")
    monkeypatch.setattr(
        tx_service,
        "verifier_une_reunion",
        lambda hid, **_kw: ("a_revoir", [], {"hearing_id": hid}),
    )
    r_verify = client.post(
        "/audiences/h-web/verifier-transcription", data={"return_to": ""}
    )
    assert r_verify.status_code == 302
    assert "reception" not in r_verify.location

    tr_model.upsert_staged_transcription(
        "h-web",
        online_meeting_id="om-w",
        dossier_id="d1",
        copilot_markdown="## Synthèse\n\n- Test Web",
        has_insights=True,
    )
    reception_route._invalidater_pastille()
    monkeypatch.setattr(reception_route.pi, "compter_soumises", lambda: 0)
    monkeypatch.setattr(hearing_model, "list_rendez_vous_strict", lambda **_kw: [])
    assert reception_route.compteur_reception() == 1

    r_tab = client.get("/reception/?onglet=rdv")
    assert r_tab.status_code == 200
    assert "Consultation initiale" in r_tab.get_data(as_text=True)

    # Verser via POST Réception
    stored_tr = tr_model.get_transcription("h-web")
    r_verser = client.post(
        "/reception/transcriptions/h-web/verser",
        data={
            "dossier_id": "d1",
            "note_title": "Note Web",
            "note_category": "consultation",
            "copilot_markdown": "## Synthèse\n\n- Validé",
            "expected_etag": stored_tr["etag"],
        },
    )
    assert r_verser.status_code == 302
    assert tr_model.get_transcription("h-web")["status"] == "versee"


# ── 5. MCP tools (read, write provenance, portal D10) ────────────────────


def test_mcp_transcripts_provenance_and_portal_d10(fake_world, monkeypatch):
    fake, _bucket = fake_world
    _seed_hearing(fake, "h-mcp", source="portail", confirmation="")
    vtt = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Client>Bonjour</v>\n"
    staged = tr_model.stage_vtt_bytes(vtt, user_id="u1", hearing_id="h-mcp")
    tr_doc, _ = tr_model.upsert_staged_transcription(
        "h-mcp",
        online_meeting_id="om-mcp",
        dossier_id="d1",
        has_transcript=True,
        transcript_cue_count=1,
        transcript_speakers=["Client"],
        staged_vtt_path=staged,
        has_insights=True,
        copilot_markdown="## Synthèse\n\n- Élément confidentiel",
    )

    seen_sources = []
    monkeypatch.setattr(
        document_model,
        "ingest_blob_as_document",
        lambda _blob, did, _fn, _fname, _meta, _uid, **kw: (
            seen_sources.append(kw.get("category_source")),
            ({"id": kw.get("document_id"), "dossier_id": did}, []),
        )[1],
    )

    listed = handlers.get_meeting_transcripts({})
    assert listed["count"] == 1
    assert "staged_vtt_path" not in listed["items"][0]

    res = handlers.decide_meeting_transcript({
        "hearing_id": "h-mcp",
        "action": "verser",
        "dossier_id": "d1",
        "note_title": "Consultation MCP",
        "note_category": "consultation",
        "expected_etag": tr_doc["etag"],
        "idempotency_key": "mcp-tx-key-1",
    })
    assert res["decided"] is True and res["entity"]["status"] == "versee"
    assert seen_sources == ["mcp"]
    # Confidential note body must NOT be stored in write idempotency payload
    assert "confidentiel" not in repr(res)
    assert "url" not in repr(res).lower()

    # Confirmé portail : verrou D10 autorise notes/dossier_id, refuse date/title
    with pytest.raises(handlers.ToolArgumentError, match="portail client"):
        handlers.update_hearing({"hearing_id": "h-mcp", "title": "Nouveau titre"})
    ok_note = handlers.update_hearing({"hearing_id": "h-mcp", "notes_append": "Suivi OK."})
    assert "Suivi OK." in fake.peek("hearings/h-mcp")["notes"]
    assert ok_note["outlook_mirror"] == "not_mirrored"
