"""Tests Lot 2 pour le moteur mathématique et le service de disponibilités."""

import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.taches_disponibilites as td
    from models import portail_disponibilites, portail_invitation
    from services import disponibilites

from config import Config  # noqa: E402
from tests._fake_firestore import FakeFirestore, install  # noqa: E402
from tz import MTL, mtl_to_utc  # noqa: E402
from utils import disponibilites_math as dm  # noqa: E402
from utils import graph, graph_teams  # noqa: E402
from utils.graph import GraphError, GraphNotConfigured  # noqa: E402
from utils.rendez_vous_defaults import default_reglages  # noqa: E402

UTC = timezone.utc


@pytest.fixture()
def stores(monkeypatch):
    for modname in (
        "models.hearing",
        "models.rendez_vous_reglages",
        "models.portail_invitation",
        "models.portail_disponibilites",
    ):
        importlib.import_module(modname)
    modules = [
        m
        for name, m in sorted(sys.modules.items())
        if name.startswith("models.") and getattr(m, "db", None) is not None
    ]
    main_db = install(monkeypatch, *modules)
    portail_db = FakeFirestore()
    monkeypatch.setattr(portail_invitation, "_pdb", lambda: portail_db)
    return main_db, portail_db


# ── Pure math: utils/disponibilites_math.py ───────────────────────────────


def test_calculer_creneaux_ouverts_respects_windows_buffers_lead_and_holidays():
    reglages = default_reglages()
    reglages["horizon_jours"] = 7
    reglages["delai_minimum_heures"] = 24
    reglages["battement_minutes"] = 15
    reglages["plages"]["mercredi"]["matin_debut"] = "09:00"

    # Mardi 22 décembre 2026 08:00 Montréal (EST = UTC-5 → 13:00 UTC).
    # +24h lead = mercredi 23 décembre 08:00.
    # Vendredi 25 décembre 2026 est férié (Noël, deadlines.PROCEDURAL).
    now_utc = mtl_to_utc(datetime(2026, 12, 22, 8, 0))
    busy = [
        # Mercredi 23 déc. 10:00–11:00 Montréal (avec battement 15 min → 09:45–11:15).
        (mtl_to_utc(datetime(2026, 12, 23, 10, 0)), mtl_to_utc(datetime(2026, 12, 23, 11, 0))),
    ]
    out = dm.calculer_creneaux_ouverts(
        reglages=reglages,
        now_utc=now_utc,
        plages_occupees_utc=busy,
        durees_minutes=[30, 60],
    )
    slots_30 = set(out["30"])
    slots_60 = set(out["60"])

    # Mardi 22 (< 24h) absent
    assert mtl_to_utc(datetime(2026, 12, 22, 14, 0)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_30

    # Mercredi 23 déc. : 09:00 libre (09:00–09:30), 09:30 bloqué par battement (chevauche 09:45),
    # 11:00 bloqué (chevauche 11:15), 11:30 libre pour 30 min mais pas pour 60 min (fenêtre finit 12:00).
    assert mtl_to_utc(datetime(2026, 12, 23, 9, 0)).strftime("%Y-%m-%dT%H:%M:%SZ") in slots_30
    assert mtl_to_utc(datetime(2026, 12, 23, 9, 30)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_30
    assert mtl_to_utc(datetime(2026, 12, 23, 11, 0)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_30
    assert mtl_to_utc(datetime(2026, 12, 23, 11, 30)).strftime("%Y-%m-%dT%H:%M:%SZ") in slots_30
    assert mtl_to_utc(datetime(2026, 12, 23, 11, 30)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_60

    # Vendredi 25 déc. (Noël férié) et samedi 26 déc. (week-end) → zéro créneau
    for day in (25, 26):
        assert mtl_to_utc(datetime(2026, 12, day, 9, 0)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_30

    # Hors horizon (mercredi 30 déc. = J+8 > 7) → absent
    assert mtl_to_utc(datetime(2026, 12, 30, 9, 0)).strftime("%Y-%m-%dT%H:%M:%SZ") not in slots_30


def test_est_creneau_disponible_honors_verifier_delai_minimum_flag():
    reglages = default_reglages()
    reglages["delai_minimum_heures"] = 24
    now_utc = mtl_to_utc(datetime(2026, 11, 24, 9, 0))  # mardi
    soon = mtl_to_utc(datetime(2026, 11, 24, 14, 0))    # +5h (même jour)
    assert not dm.est_creneau_disponible(
        debut_utc=soon, duree_minutes=30, reglages=reglages, now_utc=now_utc, plages_occupees_utc=[]
    )
    assert dm.est_creneau_disponible(
        debut_utc=soon,
        duree_minutes=30,
        reglages=reglages,
        now_utc=now_utc,
        plages_occupees_utc=[],
        verifier_delai_minimum=False,
    )


def test_hearing_busy_interval_filters_and_all_day_conversion():
    start = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)
    end = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)

    assert dm.hearing_busy_interval({"start_datetime": start, "end_datetime": end, "confirmation": ""}) == (start, end)
    assert dm.hearing_busy_interval({"start_datetime": start, "end_datetime": end, "confirmation": "à_confirmer"}) == (start, end)
    assert dm.hearing_busy_interval({"start_datetime": start, "status": "annulée"}) is None
    assert dm.hearing_busy_interval({"start_datetime": start, "confirmation": "refusée"}) is None
    assert dm.hearing_busy_interval({"start_datetime": start, "confirmation": "annulée_client"}) is None

    # Journée entière : 25 nov. 00:00 → 26 nov. 00:00 Montréal (EST UTC-5 = 05:00Z → 05:00Z)
    all_day_iv = dm.hearing_busy_interval({
        "start_datetime": datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
        "all_day": True,
        "confirmation": "",
    })
    assert all_day_iv == (mtl_to_utc(datetime(2026, 11, 25, 0, 0)), mtl_to_utc(datetime(2026, 11, 26, 0, 0)))

    # Sans fin explicite → repli 1 h
    assert dm.hearing_busy_interval({"start_datetime": start}) == (start, start + timedelta(hours=1))


# ── Graph free/busy: utils/graph_teams.lire_plages_occupees ───────────────


def test_lire_plages_occupees_chunks_over_60_days_parses_tz_and_skips_free(monkeypatch):
    for key, val in (
        ("GRAPH_TENANT_ID", "t1"),
        ("GRAPH_CLIENT_ID", "c1"),
        ("GRAPH_CLIENT_SECRET", "s1"),
        ("BOOKINGS_JURISTE_UPN", "Juriste@Cabinet.CA"),
        ("BOOKINGS_JURISTE_OBJECT_ID", "uid-1"),
        ("GRAPH_SENDER_UPN", "juriste@cabinet.ca"),
    ):
        monkeypatch.setattr(Config, key, val)

    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {
            "value": [{
                "scheduleId": "juriste@cabinet.ca",
                "scheduleItems": [
                    {
                        "status": "free",
                        "start": {"dateTime": "2026-12-01T14:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-12-01T15:00:00", "timeZone": "UTC"},
                    },
                    {
                        "status": "busy",
                        "start": {"dateTime": "2026-12-01T09:00:00", "timeZone": "Eastern Standard Time"},
                        "end": {"dateTime": "2026-12-01T10:30:00", "timeZone": "Eastern Standard Time"},
                    },
                    {
                        "status": "tentative",
                        "start": {"dateTime": "2026-12-02T15:00:00Z", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-12-02T16:00:00Z", "timeZone": "UTC"},
                    },
                ],
            }]
        }

    monkeypatch.setattr(graph, "graph_post", fake_post)
    debut = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    fin = debut + timedelta(days=75)
    res = graph_teams.lire_plages_occupees(debut, fin)

    assert len(calls) == 2
    assert calls[0][0] == "/users/Juriste%40Cabinet.CA/calendar/getSchedule"
    assert (datetime(2026, 12, 1, 14, 0, tzinfo=UTC), datetime(2026, 12, 1, 15, 30, tzinfo=UTC)) in res
    assert (datetime(2026, 12, 2, 15, 0, tzinfo=UTC), datetime(2026, 12, 2, 16, 0, tzinfo=UTC)) in res
    # L'entrée status=free ne figure pas (les 2 tranches busy/tentative sont répétées par les 2 appels factices → triées)
    assert all(s != datetime(2026, 12, 1, 14, 0, tzinfo=UTC) or e == datetime(2026, 12, 1, 15, 30, tzinfo=UTC) for s, e in res)


def test_lire_plages_occupees_fails_closed_on_schedule_error_or_unconfigured(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    with pytest.raises(GraphNotConfigured):
        graph_teams.lire_plages_occupees(
            datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 12, 2, 0, 0, tzinfo=UTC),
        )

    for key, val in (
        ("GRAPH_TENANT_ID", "t1"),
        ("GRAPH_CLIENT_ID", "c1"),
        ("GRAPH_CLIENT_SECRET", "s1"),
        ("BOOKINGS_JURISTE_UPN", "j@c.ca"),
        ("BOOKINGS_JURISTE_OBJECT_ID", "uid-1"),
    ):
        monkeypatch.setattr(Config, key, val)

    monkeypatch.setattr(
        graph,
        "graph_post",
        lambda path, payload: {"value": [{"scheduleId": "j@c.ca", "error": {"message": "Denied"}}]},
    )
    with pytest.raises(GraphError):
        graph_teams.lire_plages_occupees(
            datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 12, 2, 0, 0, tzinfo=UTC),
        )


# ── Service disponibilites + route cron ───────────────────────────────────


def test_synchroniser_disponibilites_writes_zero_pii_projection_and_custom_durations(
    stores, monkeypatch
):
    main_db, _portail_db = stores
    monkeypatch.setattr(Config, "PORTAIL_RDV_ACTIF", True)
    monkeypatch.setattr(Config, "teams_configured", lambda: False)
    monkeypatch.setattr(Config, "ENV", "development")

    # Invitation active avec durée 45 min non standard
    monkeypatch.setattr(portail_invitation, "lister_durees_rdv_actives", lambda: {45})

    start_h = datetime.now(UTC) + timedelta(days=3)
    main_db.seed(
        "hearings/h-secret",
        {
            "id": "h-secret",
            "title": "Audience confidentielle Me Untel c. Banque",
            "start_datetime": start_h,
            "end_datetime": start_h + timedelta(hours=1),
            "confirmation": "",
            "status": "confirmée",
        },
    )

    summary = disponibilites.synchroniser_disponibilites()
    assert summary["creneaux_total"] >= 0

    stored = portail_disponibilites.lire_disponibilites_strict()
    assert stored is not None
    assert "45" in stored["creneaux_par_duree"]
    assert "30" in stored["creneaux_par_duree"]
    # Zéro PII ou titre interne dans disponibilites/courant
    raw_dump = str(stored)
    assert "Banque" not in raw_dump
    assert "ordre_du_jour" not in raw_dump
    assert "Me Untel" not in raw_dump


def test_verifier_creneau_libre_strict_excludes_self_on_reschedule_and_fails_closed(
    stores, monkeypatch
):
    main_db, _portail_db = stores
    monkeypatch.setattr(Config, "teams_configured", lambda: True)

    # Choisir un futur mardi à 10h00 Montréal
    now_local = datetime.now(MTL)
    target_day = (now_local + timedelta(days=7)).date()
    while target_day.weekday() >= 5:
        target_day += timedelta(days=1)
    slot_a = mtl_to_utc(datetime(target_day.year, target_day.month, target_day.day, 10, 0))
    slot_b = mtl_to_utc(datetime(target_day.year, target_day.month, target_day.day, 10, 30))

    main_db.seed(
        "hearings/h-self",
        {
            "id": "h-self",
            "title": "Rencontre",
            "start_datetime": slot_a,
            "end_datetime": slot_a + timedelta(minutes=60),
            "confirmation": "",
            "status": "confirmée",
        },
    )
    monkeypatch.setattr(
        graph_teams,
        "lire_plages_occupees",
        lambda s, e: [(slot_a, slot_a + timedelta(minutes=60))],
    )

    # Sans exclusion : 10h30 chevauche l'ancien 10h00-11h00 → indisponible
    ok, reason = disponibilites.verifier_creneau_libre_strict(slot_b, 60)
    assert (ok, reason) == (False, "creneau_indisponible")

    # Avec exclusion de sa propre audience + plage Outlook : 10h30 libre !
    ok2, reason2 = disponibilites.verifier_creneau_libre_strict(
        slot_b,
        60,
        exclure_hearing_id="h-self",
        exclure_graph_slot=(slot_a, slot_a + timedelta(minutes=60)),
    )
    assert (ok2, reason2) == (True, "")

    # Erreur Graph → échec fermé "erreur_lecture"
    def boom(_s, _e):
        raise GraphError("Graph 503")

    monkeypatch.setattr(graph_teams, "lire_plages_occupees", boom)
    assert disponibilites.verifier_creneau_libre_strict(slot_b, 60) == (False, "erreur_lecture")


def test_cron_route_requires_appengine_cron_header_and_handles_disabled(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(td.taches_disponibilites_bp)
    client = app.test_client()

    assert client.get("/taches/disponibilites/sync").status_code == 403

    monkeypatch.setattr(Config, "PORTAIL_RDV_ACTIF", False)
    r_off = client.get("/taches/disponibilites/sync", headers={"X-Appengine-Cron": "true"})
    assert r_off.status_code == 200
    assert r_off.get_json() == {"actif": False}

    monkeypatch.setattr(Config, "PORTAIL_RDV_ACTIF", True)
    monkeypatch.setattr(
        disponibilites,
        "synchroniser_disponibilites",
        lambda: {"horizon_jours": 60, "plages_occupees": 1, "creneaux_total": 12},
    )
    r_ok = client.get("/taches/disponibilites/sync", headers={"X-Appengine-Cron": "true"})
    assert r_ok.status_code == 200
    assert r_ok.get_json() == {
        "actif": True,
        "horizon_jours": 60,
        "plages_occupees": 1,
        "creneaux_total": 12,
    }
