"""Phase 5 (Lot E — 1k Case Protocols Rework) end-to-end test suite.

Exercises the real models, services, Flask routes, Jinja templates, and MCP
handlers over ``tests._fake_firestore``:

1. Statutory 8-step ``cq_simplifié`` (arts. 145 & 535.4–535.13 C.p.c.):
   - J+15 Réponse key milestone, 130–160 day CRA window, and 6-calendar-month
     Inscription via ``recours.add_period`` + ``PROCEDURAL`` prorogation.
   - Start-date recompute moves both day-offset and month-offset steps.
2. Dynamic role-aware actor badges across ``cq_simplifié`` and ``cs_ordinaire``,
   plus graceful degradation on unannotated legacy steps.
3. Key milestones (``reponse``, ``inscription``) & all four *Inscription*
   variations (``defaut``, ``commune``, ``unilaterale`` + 15 days, ``greffier``)
   guarded by ``protocol_expected_etag``.
4. ``prejudiciaire`` draft protocol (seeded ``brouillon`` with 4 editable
   headings, regime coherence, draft editing, activation before completion) &
   Art. 174 C.p.c. pre-trial readiness editable headings block.
5. Dossier-level ``conflict_check`` (lawyer confirmation vs ``mcp`` ``présumé``
   guard & disclosure ban) + per-track auto-triggered ``ouverture`` protocol
   across Web, Réception, and MCP (skipping ``fermé``/``archivé``, untouched
   deletion cleanup vs touched block, coverage report ``PROTO_ABSENT``).
"""

import html as html_lib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from flask import Flask

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.disclosure as disclosure
    import mcp.handlers as mcp_handlers
    import mcp.output_schemas as output_schemas
    import mcp.tools as mcp_tools
    import mcp.write_support as write_support
    from models import dossier as dossier_model
    from models import folder as folder_model
    from models import partie as partie_model
    from models import protocol as protocol_model
    import routes.doc_templates as doc_templates_routes
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.hearings as hearings_routes
    import routes.invoices as invoices_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.reception as reception_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    from utils import kyc
    from utils.format_fr import format_cents_fr
    from utils.html_attr import jsattr
    from utils.icons import ms
    from utils.markdown_docx import markdown_to_safe_html
    from utils.validators import format_phone_display
    from tz import to_mtl

from tests._fake_firestore import install  # noqa: E402

_ = (dav_sync, folder_model, output_schemas, write_support)

UTC = timezone.utc


def _fake_modules() -> list:
    return [
        m for n, m in sorted(sys.modules.items())
        if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
        and getattr(m, "db", None) is not None
    ]


@pytest.fixture
def fake(monkeypatch):
    store = install(monkeypatch, *_fake_modules())
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    store.seed("parties/p-cli", {
        **partie_model._default_doc(), "id": "p-cli", "type": "individual",
        "contact_role": "client", "first_name": "Jean", "last_name": "Tremblay",
        "etag": "ep-cli", "created_at": now, "updated_at": now,
    })
    store.seed("parties/p-adv", {
        **partie_model._default_doc(), "id": "p-adv", "type": "individual",
        "contact_role": "partie_adverse", "first_name": "Paul", "last_name": "Roy",
        "etag": "ep-adv", "created_at": now, "updated_at": now,
    })
    return store


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce="n",
    )
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=jsattr,
        markdown=markdown_to_safe_html,
    )
    for bp in (
        parties_routes.parties_bp,
        dossiers_routes.dossiers_bp,
        documents_routes.documents_bp,
        notes_routes.notes_bp,
        tasks_routes.tasks_bp,
        protocols_routes.protocols_bp,
        hearings_routes.hearings_bp,
        doc_templates_routes.doc_templates_bp,
        invoices_routes.invoices_bp,
        time_expenses_routes.time_expenses_bp,
        reception_routes.reception_bp,
    ):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _seed_dossier(
    fake,
    did: str = "d1",
    *,
    tribunal: str = "Cour du Québec",
    forum_type: str = "judiciaire",
    court_file_number: str = "500-02-012345-261",
    client_roles: list[str] | None = None,
    role: str = "demandeur",
) -> dict:
    roles = ["demandeur"] if client_roles is None else client_roles
    doc = {
        **dossier_model._default_doc(),
        "id": did,
        "file_number": f"2026-{did}",
        "title": "Tremblay c. Roy",
        "status": "actif",
        "tribunal": tribunal,
        "forum_type": forum_type,
        "court_file_number": court_file_number,
        "role": role,
        "clients": [{
            "id": "p-cli", "name": "Jean Tremblay",
            "roles": roles, "avocat_id": "", "avocat_name": "",
        }],
        "client_ids": ["p-cli"],
        "opposing_parties": [{
            "id": "p-adv", "name": "Paul Roy",
            "roles": ["défendeur"], "avocat_id": "", "avocat_name": "",
        }],
        "opposing_party_ids": ["p-adv"],
        "etag": f"e-{did}",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    fake.seed(f"dossiers/{did}", doc)
    fake.seed(f"dav_sync/dossier:{did}", {
        "ctag": "c0", "sync_token": "c0",
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
    })
    return doc


# ── 1. Statutory 8-step CQ simplifié & 6-calendar-month arithmetic ───────


def test_cq_template_has_8_statutory_steps_and_6_month_inscription(fake):
    _seed_dossier(fake, "d1")
    # 2026-08-31 + 6 calendar months clamps to 2027-02-28 (Sun) -> 2027-03-01 (Mon)
    start = datetime(2026, 8, 31, tzinfo=UTC)
    proto, errors = protocol_model.create_protocol("d1", "cq_simplifié", start, {})
    assert errors == []
    steps = sorted(proto["steps"], key=lambda s: s["order"])
    assert len(steps) == 8

    assert steps[0]["cpc_reference"] == "art. 145 al. 1 C.p.c."
    assert steps[0]["actor"] == "demandeur"

    # Step 2: Réponse à l'assignation (J+15, key milestone, défendeur)
    assert steps[1]["title"] == "Réponse à l'assignation"
    assert steps[1]["cpc_reference"] == "art. 145 al. 2 C.p.c."
    assert steps[1]["deadline_offset_days"] == 15
    assert steps[1]["actor"] == "défendeur"
    assert steps[1]["key_milestone"] == "reponse"
    assert steps[1]["deadline_date"] == datetime(2026, 9, 15, tzinfo=UTC)

    assert steps[2]["cpc_reference"] == "art. 535.4 C.p.c."
    assert steps[2]["deadline_offset_days"] == 20
    assert steps[3]["cpc_reference"] == "art. 535.5 C.p.c."
    assert steps[3]["actor"] == "commun"
    assert steps[4]["cpc_reference"] == "art. 535.6 C.p.c."
    assert steps[4]["actor"] == "défendeur"
    assert steps[5]["cpc_reference"] == "art. 535.8 C.p.c."
    assert steps[5]["actor"] == "greffe"

    # Step 7: CRA / préparatoire window 130–160 days
    assert steps[6]["cpc_reference"] == "art. 535.12 C.p.c."
    assert steps[6]["window_min_days"] == 130
    assert steps[6]["window_max_days"] == 160
    assert steps[6]["actor"] == "greffe"

    # Step 8: 6 calendar months (NOT hardcoded 180 days: 2026-08-31 + 180d would be 2027-02-27)
    assert steps[7]["cpc_reference"] == "art. 535.13 C.p.c."
    assert steps[7]["deadline_offset_months"] == 6
    assert steps[7]["deadline_offset_days"] is None
    assert steps[7]["actor"] == "greffe"
    assert steps[7]["key_milestone"] == "inscription"
    assert steps[7]["deadline_date"] == datetime(2027, 3, 1, tzinfo=UTC)
    assert proto["inscription_variation"] == "greffier"

    # Dec 26 (art. 82 non-juridical day) prorogation check:
    # 2025-06-26 + 6m -> 2025-12-26 (Friday, art. 82) -> 2025-12-27 (Sat) -> 2025-12-29 (Mon)
    _seed_dossier(fake, "d2")
    p2, err2 = protocol_model.create_protocol(
        "d2",
        "cq_simplifié",
        datetime(2025, 6, 26, tzinfo=UTC),
        {},
    )
    assert err2 == []
    s8 = sorted(p2["steps"], key=lambda s: s["order"])[-1]
    assert s8["deadline_date"] == datetime(2025, 12, 29, tzinfo=UTC)


# ── 2. Role-aware dynamic actor badges & graceful degradation ────────────


def test_role_aware_actor_badges_across_cq_cs_and_legacy(client, fake):
    # Plaintiff dossier
    d_dem = _seed_dossier(fake, "d-dem", role="demandeur", client_roles=["demandeur"])
    # Defendant dossier
    d_def = _seed_dossier(
        fake, "d-def", tribunal="Cour supérieure",
        court_file_number="500-17-012345-261",
        role="défendeur", client_roles=["défendeur"],
    )

    p_cq, err = protocol_model.create_protocol(
        "d-dem", "cq_simplifié", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert err == []
    cq_steps = sorted(p_cq["steps"], key=lambda s: s["order"])
    assert protocol_model.resolve_step_actor_badge(cq_steps[0], d_dem)["label"] == "Notre délai"
    assert protocol_model.resolve_step_actor_badge(cq_steps[1], d_dem)["label"] == "Délai adverse"
    assert protocol_model.resolve_step_actor_badge(cq_steps[3], d_dem)["label"] == "Commun"
    assert protocol_model.resolve_step_actor_badge(cq_steps[5], d_dem)["label"] == "Délai du greffe / tribunal"

    # Defendant flips who owns Step 1 vs Step 2 on CS Ordinaire
    p_cs, err2 = protocol_model.create_protocol(
        "d-def", "cs_ordinaire", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert err2 == []
    cs_steps = sorted(p_cs["steps"], key=lambda s: s["order"])
    assert cs_steps[1]["key_milestone"] == "reponse"
    assert cs_steps[7]["key_milestone"] == "inscription"
    assert protocol_model.resolve_step_actor_badge(cs_steps[0], d_def)["label"] == "Délai adverse"
    assert protocol_model.resolve_step_actor_badge(cs_steps[1], d_def)["label"] == "Notre délai"

    # Legacy step missing `actor` degrades cleanly
    assert protocol_model.resolve_step_actor_badge({"id": "legacy", "title": "Vieille étape"}, d_def) is None

    # Web rendering shows key milestones & dynamic actor labels
    html = client.get(f"/protocoles/{p_cs['id']}").get_data(as_text=True)
    assert "Jalon clé" in html
    assert "border-l-4 border-indigo-500" in html
    assert "Notre délai" in html
    assert "Délai adverse" in html

    # MCP list_protocol_steps emits role-aware actor_label & key_milestone
    listed = mcp_handlers.list_protocol_steps({"dossier_id": "d-def"})
    assert listed["protocols"][0]["steps"][1]["actor"] == "défendeur"
    assert listed["protocols"][0]["steps"][1]["actor_label"] == "Notre délai"
    assert listed["protocols"][0]["steps"][1]["key_milestone"] == "reponse"


# ── 3. Setting-down variations + Art. 174 readiness headings ───────────────


def test_inscription_variations_and_art174_readiness_with_etag_guard(client, fake):
    _seed_dossier(
        fake, "d1", tribunal="Cour supérieure",
        court_file_number="500-17-012345-261",
    )
    proto, errors = protocol_model.create_protocol(
        "d1", "cs_ordinaire", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert errors == []
    pid = proto["id"]

    # Default Art. 174 headings seeded
    assert len(proto["pretrial_headings"]) == 5
    assert proto["pretrial_headings"][0]["title"] == "Faits admis"

    # Reject stale inscription update (protocol_expected_etag mismatch)
    resp_stale = client.post(
        f"/protocoles/{pid}/inscription",
        data={"inscription_variation": "unilaterale", "protocol_expected_etag": "wrong"},
    )
    assert resp_stale.status_code == 302
    assert "modifi" in resp_stale.headers["Location"]
    assert fake.peek(f"protocols/{pid}")["inscription_variation"] == ""

    # Fresh update selects unilateral variation -> +15d reply deadline appears
    etag = fake.peek(f"protocols/{pid}")["etag"]
    resp_ok = client.post(
        f"/protocoles/{pid}/inscription",
        data={
            "inscription_variation": "unilaterale",
            "protocol_expected_etag": etag,
            "return_to": f"/dossiers/d1?tab=protocole",
        },
    )
    assert resp_ok.status_code == 302
    assert "/dossiers/d1?tab=protocole" in resp_ok.headers["Location"]
    assert fake.peek(f"protocols/{pid}")["inscription_variation"] == "unilaterale"

    detail_html = client.get(f"/protocoles/{pid}").get_data(as_text=True)
    assert "+ 15 jours pour ajouts ou retraits au dossier (art. 174 al. 2 C.p.c.)" in detail_html

    # Update Art. 174 pre-trial readiness headings & add custom heading
    etag2 = fake.peek(f"protocols/{pid}")["etag"]
    headings = fake.peek(f"protocols/{pid}")["pretrial_headings"]
    form_data = {
        "protocol_expected_etag": etag2,
        "return_to": f"/protocoles/{pid}",
        "heading_key": [h["key"] for h in headings],
        "heading_title": [h["title"] for h in headings],
        "heading_notes": ["Document P-1 admis" if idx == 0 else "" for idx, _h in enumerate(headings)],
        "heading_completed": ["0"],
        "new_heading_title": "Séance en visioconférence",
        "new_heading_notes": "Demander salle virtuelle",
    }
    resp_prep = client.post(f"/protocoles/{pid}/preparation", data=form_data)
    assert resp_prep.status_code == 302

    stored = fake.peek(f"protocols/{pid}")
    assert len(stored["pretrial_headings"]) == 6
    assert stored["pretrial_headings"][0]["completed"] is True
    assert stored["pretrial_headings"][0]["notes"] == "Document P-1 admis"
    assert stored["pretrial_headings"][-1]["title"] == "Séance en visioconférence"

    tab_html = client.get("/dossiers/d1/tab/protocole").get_data(as_text=True)
    assert "Conférence préparatoire / Déclaration de mise en état (art. 174 C.p.c.)" in tab_html
    assert "Document P-1 admis" in tab_html


# ── 4. Préjudiciaire draft protocol ───────────────────────────────────────


def test_prejudiciaire_draft_lifecycle_and_regime_coherence(client, fake):
    _seed_dossier(
        fake, "d-pre", tribunal="", forum_type="prejudiciaire",
        court_file_number="Préjudiciaire",
    )
    _seed_dossier(
        fake, "d-jud", tribunal="Cour du Québec", forum_type="judiciaire",
        court_file_number="500-02-012345-261",
    )

    # Refused on numbered judicial dossier
    assert protocol_model.regime_mismatch("prejudiciaire", fake.peek("dossiers/d-jud")) is True
    _bad, err_bad = protocol_model.create_protocol(
        "d-jud", "prejudiciaire", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert err_bad

    # Seeded in brouillon with 4 editable draft steps on pre-suit dossier
    proto, errs = protocol_model.create_protocol(
        "d-pre", "prejudiciaire", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert errs == []
    assert proto["status"] == "brouillon"
    assert proto["track"] == protocol_model.TRACK_INSTANCE
    assert len(proto["steps"]) == 4

    pid = proto["id"]
    first_step = proto["steps"][0]

    # Step title editable while in brouillon
    upd_step, upd_err = protocol_model.update_step(
        pid, first_step["id"], {"title": "Prescription et mise en demeure urgente"},
    )
    assert upd_err == []
    assert upd_step["title"] == "Prescription et mise en demeure urgente"

    # Completion refused while brouillon
    _c, comp_err, _o = protocol_model.set_step_status(pid, first_step["id"], "complété")
    assert comp_err

    # Draft activation via web button (protocol_expected_etag)
    detail_html = client.get(f"/protocoles/{pid}").get_data(as_text=True)
    assert "Activer le protocole" in detail_html
    cur_etag = fake.peek(f"protocols/{pid}")["etag"]
    resp_act = client.post(
        f"/protocoles/{pid}",
        data={
            "title": proto["title"],
            "notes": "",
            "status": "actif",
            "protocol_expected_etag": cur_etag,
        },
    )
    assert resp_act.status_code == 302
    assert fake.peek(f"protocols/{pid}")["status"] == "actif"

    # Now completion succeeds
    done_step, done_err, _o2 = protocol_model.set_step_status(pid, first_step["id"], "complété")
    assert done_err == [] and done_step["status"] == "complété"


# ── 5. Dossier conflict_check, ouverture auto-trigger & per-track rules ──


def test_dossier_conflict_check_and_ouverture_web_mcp_reception(client, fake, monkeypatch):
    # 5a. Web dossier creation auto-seeds ouverture-{did}
    resp = client.post(
        "/dossiers/",
        data={
            "file_number": "2026-WEB-01",
            "title": "Tremblay c. Roy",
            "tribunal": "Cour du Québec",
            "forum_type": "judiciaire",
            "court_file_number": "500-02-012345-261",
            "status": "actif",
            "clients_json": json.dumps([
                {"id": "p-cli", "name": "Jean Tremblay", "roles": ["demandeur"]},
            ]),
            "opposing_parties_json": json.dumps([
                {"id": "p-adv", "name": "Paul Roy", "roles": ["défendeur"]},
            ]),
            "conflict_check": "non_vérifié",
        },
    )
    assert resp.status_code == 302
    web_dossier = next(
        d for d in fake.peek_collection("dossiers").values()
        if d["file_number"] == "2026-WEB-01"
    )
    did_web = web_dossier["id"]
    open_proto = fake.peek(f"protocols/ouverture-{did_web}")
    assert open_proto is not None
    assert open_proto["track"] == "ouverture"
    assert open_proto["protocol_type"] == "ouverture"
    assert open_proto["status"] == "actif"
    assert len(fake.peek_collection(f"protocols/ouverture-{did_web}/steps")) == 4

    # Idempotent retry never duplicates ouverture
    again, errs_again = protocol_model.ensure_ouverture_protocol(did_web)
    assert errs_again == [] and again["id"] == f"ouverture-{did_web}"

    # Per-track concurrency: active CQ protocol allowed simultaneously alongside active ouverture!
    cq_proto, cq_err = protocol_model.create_protocol(
        did_web, "cq_simplifié", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert cq_err == [] and cq_proto["track"] == "instance"
    # Second active instance protocol is blocked, while active ouverture stayed untouched
    dup_inst, dup_err = protocol_model.create_protocol(
        did_web, "conventionnel", datetime(2026, 9, 1, tzinfo=UTC), {},
    )
    assert dup_inst is None and protocol_model.ONE_ACTIVE_ERROR in dup_err

    # 5b. Lawyer confirmation of conflict_check syncs ouverture step 1
    step1_before = next(
        s for s in fake.peek_collection(f"protocols/ouverture-{did_web}/steps").values()
        if s.get("ouverture_key") == "conflit"
    )
    assert step1_before["status"] == "à_venir"

    # Simulated MCP path cannot confirm conflict check (stays présumé + cannot mark step 1)
    kyc_never = next(n for n in disclosure.NEVERS if n.key == "kyc")
    assert ("*", "conflict_check") in kyc_never.forbidden_inputs
    upd_mcp, _e = dossier_model.update_dossier(
        did_web,
        {"conflict_check": "vérifié"},
        kyc_source=kyc.SOURCE_MCP,
    )
    assert kyc.is_presumed(upd_mcp, kyc.FIELD_CONFLICT) is True
    step1_after_mcp = fake.peek(f"protocols/ouverture-{did_web}/steps/{step1_before['id']}")
    assert step1_after_mcp["status"] == "à_venir"

    # Lawyer confirms via Web UI POST /dossiers/<id>/conflit/confirmer
    resp_conf = client.post(
        f"/dossiers/{did_web}/conflit/confirmer",
        data={"expected_etag": upd_mcp["etag"]},
    )
    assert resp_conf.status_code == 302
    assert kyc.is_decided(fake.peek(f"dossiers/{did_web}"), kyc.FIELD_CONFLICT) is True
    step1_confirmed = fake.peek(f"protocols/ouverture-{did_web}/steps/{step1_before['id']}")
    assert step1_confirmed["status"] == "complété"

    # Cannot downgrade lawyer-confirmed conflict via MCP
    _refused, mcp_err = dossier_model.update_dossier(
        did_web, {"conflict_check": "non_vérifié"}, kyc_source=kyc.SOURCE_MCP,
    )
    assert mcp_err

    # 5c. MCP create_dossier: auto-seeds ouverture on active dossier, skips closed
    mcp_open = mcp_handlers.create_dossier({
        "file_number": "2026-MCP-01",
        "title": "Dossier MCP Actif",
        "tribunal": "Cour du Québec",
        "court_file_number": "500-02-099999-261",
        "clients": [{"partie_id": "p-cli", "roles": ["demandeur"]}],
    })
    did_mcp = mcp_open["entity"]["id"]
    assert fake.peek(f"protocols/ouverture-{did_mcp}") is not None
    # Active ouverture must NEVER suppress PROTO_ABSENT in compliance coverage
    cov = mcp_handlers.get_coverage_report({})
    mcp_item = next(item for item in cov["items"] if item["dossier_id"] == did_mcp)
    codes = {f["code"] for f in mcp_item["findings"]}
    assert "PROTO_ABSENT" in codes

    # Closed backfill dossier skips ouverture auto-seed
    mcp_closed = mcp_handlers.create_dossier({
        "file_number": "2020-HIST-01",
        "title": "Archive",
        "status": "fermé",
        "clients": [{"partie_id": "p-cli", "roles": ["demandeur"]}],
    })
    assert fake.peek(f"protocols/ouverture-{mcp_closed['entity']['id']}") is None

    # 5d. Untouched ouverture allows delete_dossier; touched ouverture blocks it
    ok_del, err_del, rep_del = dossier_model.delete_dossier(did_mcp)
    assert ok_del is True and err_del == "" and rep_del["code"] == ""
    assert fake.peek(f"protocols/ouverture-{did_mcp}") is None

    # A dossier with ONLY an untouched ouverture deleted after step completion is blocked
    d_touch = mcp_handlers.create_dossier({
        "file_number": "2026-MCP-02",
        "title": "Dossier Ouverture Touchée",
        "clients": [{"partie_id": "p-cli", "roles": ["demandeur"]}],
    })["entity"]["id"]
    touch_step = next(iter(fake.peek_collection(f"protocols/ouverture-{d_touch}/steps").values()))
    protocol_model.set_step_status(f"ouverture-{d_touch}", touch_step["id"], "complété")
    ok_t, err_t, rep_t = dossier_model.delete_dossier(d_touch)
    assert ok_t is False and rep_t["code"] == "contenu" and "protocoles" in err_t

    ok_blocked, err_blocked, rep_blocked = dossier_model.delete_dossier(did_web)
    assert ok_blocked is False and rep_blocked["code"] == "contenu" and "protocoles" in err_blocked

    # 5e. Réception intake approval with ouvrir_dossier=on creates dossier + ouverture
    now = datetime(2026, 9, 1, tzinfo=UTC)
    monkeypatch.setattr(reception_routes.pi, "lire_invitation", lambda i: {
        "id": "inv1", "type": "intake", "email": "client@exemple.com",
        "statut": "soumise", "display_label": "Ouverture",
        "partie_id": None, "dossier_id": None, "created_at": now,
        "expires_at": now, "soumissions": [{"batch": "b1", "files_count": 0, "total_bytes": 0}],
        "accuses": {"b1": True},
    })
    monkeypatch.setattr(reception_routes.pi, "maj_statut", lambda i, s: True)
    monkeypatch.setattr(reception_routes, "_lire_enveloppe", lambda i, b: {
        "type": "intake", "invitation_id": "inv1", "batch": "b1",
        "donnees": {"nature": "physique", "prenom": "Luc", "nom": "Gagnon",
                    "courriel": "luc@exemple.com", "langue": "fr"},
        "parties_adverses": [],
        "consentement": {"accepte": True},
        "pieces_identite": None,
    })
    monkeypatch.setattr(reception_routes, "_archiver_enveloppe", lambda i, b: None)
    monkeypatch.setattr(reception_routes, "apres_ecriture_contact", lambda *a: None)
    monkeypatch.setattr(reception_routes, "log_portail_event", lambda *a, **k: None)

    rep_rec = client.post(
        "/reception/ouvertures/inv1/b1/creer",
        data={
            "ouvrir_dossier": "on",
            "dossier_file_number": "2026-REC-01",
            "dossier_title": "Gagnon — Préjudiciaire",
        },
    )
    assert rep_rec.status_code == 302
    rec_dossier = next(
        d for d in fake.peek_collection("dossiers").values()
        if d["file_number"] == "2026-REC-01"
    )
    assert fake.peek(f"protocols/ouverture-{rec_dossier['id']}") is not None

    # Render both dossier Protocole tab (instance + ouverture + can_create_prejudiciaire) and Aperçu tab
    tab_web = client.get(f"/dossiers/{did_web}/tab/protocole")
    assert tab_web.status_code == 200
    assert "Protocole d'ouverture du dossier" in html_lib.unescape(tab_web.get_data(as_text=True))

    tab_pre = client.get(f"/dossiers/{rec_dossier['id']}/tab/protocole")
    assert tab_pre.status_code == 200
    assert "Brouillon préjudiciaire" in html_lib.unescape(tab_pre.get_data(as_text=True))

    apercu_html = html_lib.unescape(client.get(f"/dossiers/{did_web}/tab/apercu").get_data(as_text=True))
    assert "Vérifié" in apercu_html


def test_mcp_protocol_tracks_drafts_and_schema_conformance(fake):
    def _conforms(tool: str, payload: dict) -> None:
        clean = mcp_tools._jsonable(payload)
        errs = mcp_tools.validate_args(output_schemas.OUTPUT_SCHEMAS[tool], clean)
        assert errs == [], f"{tool}: {errs}"

    # Create pre-suit dossier via MCP -> seeds active ouverture protocol automatically
    d_pre = mcp_handlers.create_dossier({
        "file_number": "2026-MCP-PRE",
        "title": "Dossier Préjudiciaire",
        "forum_type": "prejudiciaire",
        "district_judiciaire": "Montréal",
        "clients": [{"partie_id": "p-cli", "roles": ["demandeur"]}],
    })
    _conforms("create_dossier", d_pre)
    did = d_pre["entity"]["id"]

    # get_dossier exposes conflict_check fields and conforms to schema
    got = mcp_handlers.get_dossier({"dossier_id": did})
    _conforms("get_dossier", got)
    assert got["dossier"]["conflict_check"] == kyc.NON_VERIFIE
    assert got["dossier"]["conflict_check_presumed"] is False

    # Create prejudiciaire draft protocol via MCP even while ouverture is active
    p_pre = mcp_handlers.create_protocol({
        "dossier_id": did,
        "protocol_type": "prejudiciaire",
        "start_date": "2026-09-01",
    })
    _conforms("create_protocol", p_pre)
    assert p_pre["entity"]["status"] == "brouillon"
    assert len(p_pre["steps"]) == 4

    # Edit a draft step title while in brouillon, then refuse completion until actif
    step0 = p_pre["steps"][0]
    edited = mcp_handlers.update_protocol_step({
        "protocol_id": p_pre["entity"]["id"],
        "step_id": step0["id"],
        "title": "Prescription et mise en demeure",
        "expected_etag": step0["etag"],
    })
    _conforms("update_protocol_step", edited)

    with pytest.raises(mcp_tools.ToolArgumentError):
        mcp_handlers.update_protocol_step({
            "protocol_id": p_pre["entity"]["id"],
            "step_id": step0["id"],
            "status": "complété",
        })

    # Activate brouillon on the instance track alongside active ouverture
    act = mcp_handlers.update_protocol({
        "protocol_id": p_pre["entity"]["id"],
        "status": "actif",
        "expected_etag": edited["protocol_etag"],
    })
    _conforms("update_protocol", act)
    assert act["entity"]["status"] == "actif"

    # list_protocol_steps(include_history=True) returns both tracks with schema conformance
    listed = mcp_handlers.list_protocol_steps({"dossier_id": did, "include_history": True})
    _conforms("list_protocol_steps", listed)
    assert {p["track"] for p in listed["protocols"]} == {"ouverture", "instance"}

    # get_agenda validates against schema too
    agenda = mcp_handlers.get_agenda({})
    _conforms("get_agenda", agenda)

