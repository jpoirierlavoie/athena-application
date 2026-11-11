"""Partie (contact/party) management routes — list, detail, create, edit,
delete, and a contact's proofs of identity (the « Preuves d'identité »
block of the fiche's Conformité card)."""

import json
from datetime import datetime
from typing import Any, Optional

from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from config import Config
from utils.cabinet import cabinet_dict
from services.carnet import apres_ecriture_contact, apres_suppression_contact
from models import concurrency
from models import piece_identite
from models.audit_event import record_deletion
from models.document import format_file_size, get_file_icon
from tz import to_mtl
from utils import civilite, kyc, occupations, storage_identity
from utils.format_fr import format_date_fr
from utils.logging_setup import (
    log_partie_event,
    log_unexpected,
    sanitize_log_value,
)
from utils.template_fields import selected_address, selected_email, selected_phone
from pagination import (
    PAGE_SIZE,
    cursor_pagination,
    paginate,
    parse_trail,
    resolve_page,
    total_pages_of,
)
from security import sanitize
from models.partie import (
    MANDATAIRE_KIND_LABELS,
    PARTIE_NOT_FOUND,
    ROLE_LABELS,
    VALID_CONTACT_ROLES,
    count_parties_page,
    confirm_kyc_status,
    create_partie,
    delete_partie,
    display_name,
    fold_job_role,
    formal_name,
    get_partie,
    list_parties,
    list_parties_page,
    update_partie,
)
from models.dossier import (
    STATUS_LABELS as DOSSIER_STATUS_LABELS,
    DOMAINE_LABELS as DOSSIER_DOMAINE_LABELS,
    list_dossiers_for_partie,
)
from routes import edit_conflict
from routes._helpers import is_htmx, redirect_after_post

parties_bp = Blueprint(
    "parties", __name__, url_prefix="/parties"
)


_is_htmx = is_htmx


def _form_data() -> dict:
    """Extract partie fields from the submitted form."""
    f = request.form
    data = {
        "type": f.get("type", "individual"),
        # Individual
        "prefix": f.get("prefix", ""),
        "first_name": f.get("first_name", "").strip(),
        "middle_name": f.get("middle_name", "").strip(),
        "last_name": f.get("last_name", "").strip(),
        "suffix": f.get("suffix", "").strip(),
        # <input type="date"> → « AAAA-MM-JJ » ; le modèle la fixe à minuit
        # UTC, et une chaîne vide efface la date (_validate la remet à None).
        "birth_date": f.get("birth_date", "").strip(),
        # Organization (personne morale)
        "organization_name": f.get("organization_name", "").strip(),
        "trade_name": f.get("trade_name", "").strip(),
        "governing_law": f.get("governing_law", "").strip(),
        # Demographics
        "language": f.get("language", ""),
        "gender": f.get("gender", ""),
        "pronouns": f.get("pronouns", ""),
        # Occupation and professional coordinates
        "occupation": f.get("occupation", ""),
        "job_title": f.get("job_title", "").strip(),
        "organization": f.get("organization", "").strip(),
        # Personal contact
        "email": f.get("email", "").strip(),
        "phone_home": f.get("phone_home", "").strip(),
        "phone_cell": f.get("phone_cell", "").strip(),
        # Professional contact
        "email_work": f.get("email_work", "").strip(),
        "phone_work": f.get("phone_work", "").strip(),
        "fax": f.get("fax", "").strip(),
        # Personal address
        "address_street": f.get("address_street", "").strip(),
        "address_unit": f.get("address_unit", "").strip(),
        "address_city": f.get("address_city", "").strip(),
        "address_province": f.get("address_province", "Québec").strip(),
        "address_postal_code": f.get("address_postal_code", "").strip().upper(),
        "address_country": f.get("address_country", "Canada").strip(),
        # Work address
        "work_address_street": f.get("work_address_street", "").strip(),
        "work_address_unit": f.get("work_address_unit", "").strip(),
        "work_address_city": f.get("work_address_city", "").strip(),
        "work_address_province": f.get("work_address_province", "").strip(),
        "work_address_postal_code": f.get("work_address_postal_code", "").strip().upper(),
        "work_address_country": f.get("work_address_country", "Canada").strip(),
        # Legal identifiers
        "bar_number": f.get("bar_number", "").strip(),
        "company_neq": f.get("company_neq", "").strip(),
        # Compliance (only when contact_role == client)
        "identity_verified": f.get("identity_verified", kyc.NON_VERIFIE),
        "identity_verified_notes": f.get("identity_verified_notes", "").strip(),
        "conflict_check": f.get("conflict_check", kyc.NON_VERIFIE),
        "conflict_check_notes": f.get("conflict_check_notes", "").strip(),
        # Mandataires (parsed from a JSON-encoded list submitted by the form)
        "mandataires": _parse_mandataires_json(f.get("mandataires_json", "")),
        # Notes
        "notes": f.get("notes", "").strip(),
    }
    # The role travels only when posted (merge by presence): an edit posted
    # without it keeps the stored role. Defaulting it to « client » turned
    # any contact saved from a page whose role menu did not post — a
    # supplier included — into a client. A create without it still gets
    # the model's default, « client ».
    if "contact_role" in f:
        data["contact_role"] = f["contact_role"]
    return data


def _form_context(partie: Optional[dict]) -> dict[str, Any]:
    """The vocabularies every render of ``parties/form.html`` needs.

    ``prefix_extra`` is a stored title the vocabulary does not know — one a
    phone sent, kept by the model — offered as its own selected option, so
    re-saving the form re-posts it unchanged instead of erasing it.

    The compliance selects follow the same rule, for a sharper reason: the
    identity list OPENS on « Identifié », a decided status (D1's order), so
    a select whose value matched no option would post it as the lawyer's
    attestation. Each current value is therefore marked ``selected`` on the
    server (``kyc_selected``) — a page whose Alpine never ran still posts
    the stored status — and a stored value outside the vocabulary is
    offered as its own option (``kyc_extra``), so a save re-posts it
    unchanged (``kyc.apply_status_transition`` keeps it).
    """
    prefix = civilite.as_text((partie or {}).get("prefix", ""))
    kyc_selected = {f: kyc.stored_status(partie, f) for f in kyc.FIELDS}
    return {
        # A legacy « Fonction » shows already folded into the title — what
        # the save will store (models.partie._retire_job_role).
        "job_title_value": fold_job_role(
            civilite.as_text((partie or {}).get("job_title", "")),
            civilite.as_text((partie or {}).get("job_role", ""))),
        "prefix_labels": civilite.PREFIX_LABELS,
        "prefix_value": prefix,
        "prefix_extra": "" if civilite.is_known(prefix) else prefix,
        "occupation_labels": occupations.OCCUPATION_LABELS,
        "organization_labels": occupations.ORGANIZATION_LABELS,
        "organization_label_default": occupations.ORGANIZATION_LABEL_DEFAULT,
        "kyc_choices": {f: kyc.choices(f) for f in kyc.FIELDS},
        "kyc_selected": kyc_selected,
        "kyc_extra": {
            f: value for f, value in kyc_selected.items()
            if value not in kyc.STATUSES[f]
        },
        "kyc_todo": kyc.NON_VERIFIE,
    }


def _parse_mandataires_json(raw: str) -> list[dict[str, Any]]:
    """Parse the form's `mandataires_json` field into a list of dicts.

    Returns [] on any parse failure or if the value isn't a list.
    Per-entry sanitation (dedup, type coercion) happens in the model's
    `_normalize`; here we just surface what the form sent.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _is_dossier_client(partie_id: str) -> bool:
    """Is this contact a CLIENT of at least one dossier (whatever its
    role)? The coverage report checks every dossier client, so the fiche
    and the form show their Conformité. Display only: the lister fails open
    (an outage hides the card, never writes anything)."""
    if not partie_id:
        return False
    return any(
        partie_id in (d.get("client_ids") or [])
        for d in list_dossiers_for_partie(partie_id)
    )


def _kyc_presumed(partie: Optional[dict]) -> dict[str, bool]:
    """``{field: presumed}`` from the STORED record — the edit form's note
    beside each select (a re-rendered form holds the submitted values, which
    carry no provenance, so the caller passes the stored record)."""
    return {field: kyc.is_presumed(partie, field) for field in kyc.FIELDS}


def _hydrate_mandataires(entries: Optional[list]) -> list[dict[str, Any]]:
    """Resolve each `{id, kind, notes}` entry to include the partie's display name.

    Used to enrich the data passed to detail/form templates so they can render
    each linked partie's name without doing the lookup themselves.
    """
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        target = get_partie(mid)
        out.append({
            "id": mid,
            "name": display_name(target) if target else "(introuvable)",
            "kind": str(entry.get("kind") or "mandataire"),
            "notes": str(entry.get("notes") or ""),
            "found": target is not None,
        })
    return out


# ── List ──────────────────────────────────────────────────────────────────

# The list's « every role » value of ?role=.
ALL_ROLES = "tous"


def _role_filter() -> str:
    """The ``?role=`` filter, checked against its vocabulary (Security
    Rules): a role, « tous », or « client » — the default view — for
    anything else. Unchecked, an unknown role reached the model, which
    reads it as NO filter: every contact listed under a menu reading
    « Client »."""
    role = request.args.get("role", "client")
    return role if role == ALL_ROLES or role in VALID_CONTACT_ROLES else "client"


@parties_bp.route("/")
@login_required
def partie_list() -> str:
    """Render the partie list with optional filters."""
    role_filter = _role_filter()
    search = request.args.get("q", "").strip()

    if search:
        # Search fallback: Firestore has no full-text search, so searching
        # still streams the (role-filtered) collection, filters in Python
        # and slices via the legacy paginate(). Search is occasional; the
        # default browse path below costs ~PAGE_SIZE reads per page.
        page = request.args.get("page", 1, type=int)
        parties = list_parties(
            role_filter=role_filter if role_filter != ALL_ROLES else None,
            search=search,
        )
        parties, pagination = paginate(parties, page)
        pagination["url"] = url_for("parties.partie_list")
        pagination["target"] = "#partie-rows"
    else:
        cursor = request.args.get("cursor", "") or None
        trail = parse_trail(request.args.get("trail", ""))
        effective_role = role_filter if role_filter != ALL_ROLES else None
        # The count is issued on the CURSOR branch only. It rides the same
        # query builder as the page read, so the same index serves both,
        # and it fails to None (never 0) — which HIDES the leap controls
        # rather than asserting a total the code does not have.
        total = count_parties_page(role_filter=effective_role)
        page_no, page_offset = resolve_page(
            request.args.get("page", type=int),
            total_pages_of(total),
            has_cursor=bool(cursor),
        )
        if page_offset:
            cursor, trail = None, []
        parties, next_cursor = list_parties_page(
            role_filter=effective_role,
            limit=PAGE_SIZE,
            cursor=cursor,
            offset=page_offset,
        )
        # No extra_vals needed: the pagination links hx-include
        # "#filters input, #filters select", which carries the active
        # role tab (and the empty q) on every next/prev click.
        pagination = cursor_pagination(
            cursor=cursor,
            trail=trail,
            next_cursor=next_cursor,
            url=url_for("parties.partie_list"),
            target="#partie-rows",
            page=page_no,
            total=total,
        )

    # Attach display names (page rows only) and the identity dot: a client
    # whose identity is not DECIDED — a presumed Claude inscription counts
    # as open (D7), which the template alone could not know.
    for p in parties:
        p["_display_name"] = display_name(p)
        # The row's email and phone follow the documents' choice of block.
        p["_courriel"] = selected_email(p)
        p["_telephone"] = selected_phone(p)
        p["_kyc_open"] = (
            p.get("contact_role") == "client"
            and not kyc.is_decided(p, kyc.FIELD_IDENTITY)
        )
        p["_kyc_presumed"] = kyc.is_presumed(p, kyc.FIELD_IDENTITY)

    if _is_htmx():
        return render_template(
            "parties/_partie_rows.html",
            parties=parties,
            role_labels=ROLE_LABELS,
            pagination=pagination,
            role_filter=role_filter,
            search=search,
        )

    return render_template(
        "parties/list.html",
        parties=parties,
        role_filter=role_filter,
        search=search,
        role_labels=ROLE_LABELS,
        valid_roles=VALID_CONTACT_ROLES,
        pagination=pagination,
    )


# ── Search (HTMX autocomplete for other modules) ─────────────────────────


@parties_bp.route("/search")
@login_required
def partie_search() -> str:
    """Return a small HTML fragment of matching parties (for autocomplete).

    ``?boost=<contact_role>`` reorders (never filters) the results so that
    role comes first — the dossier form's avocat picker boosts
    ``avocat_adverse`` while still letting any contact be chosen (a lawyer
    filed under « notaire » or « autre » stays reachable).
    """
    q = request.args.get("q", "").strip()
    boost = request.args.get("boost", "").strip()
    results = list_parties(search=q) if q else []
    if boost:
        # sort() is stable: within each half the search order is preserved.
        results.sort(key=lambda p: p.get("contact_role") != boost)
    for p in results:
        p["_display_name"] = display_name(p)
    return render_template("parties/_search_results.html", parties=results[:10])


@parties_bp.route("/mandataire-search")
@login_required
def mandataire_search() -> str:
    """Search for a mandataire candidate.

    Restricted to personnes physiques (type=individual) of the same
    contact_role as the partie being represented. The current partie is
    excluded from results (no self-reference).
    """
    q = request.args.get("mandataire_query", "").strip() or request.args.get("q", "").strip()
    role = request.args.get("contact_role", "").strip()
    exclude_raw = request.args.get("exclude", "") or ""
    exclude_ids = {eid.strip() for eid in exclude_raw.split(",") if eid.strip()}

    if role not in VALID_CONTACT_ROLES:
        return render_template(
            "parties/_mandataire_search_results.html",
            parties=[],
            message="Sélectionnez d'abord un rôle.",
        )

    results = list_parties(
        type_filter="individual",
        role_filter=role,
        search=q or None,
    )
    if exclude_ids:
        results = [p for p in results if p.get("id") not in exclude_ids]
    for p in results:
        p["_display_name"] = display_name(p)
    return render_template(
        "parties/_mandataire_search_results.html",
        parties=results[:10],
    )


# ── Detail ────────────────────────────────────────────────────────────────


def _compliance_signer() -> str:
    """« Me X » — le nom du juriste, sans dupliquer une civilité présente."""
    nom = (cabinet_dict().get("nom") or "").strip()
    titre, _, reste = nom.partition(" ")
    if not nom or (reste and civilite.canonical_prefix(titre) == "Me"):
        return nom
    return f"Me {nom}"


# The two checks, by their URL slug (the « Confirmer » route) and field.
_KYC_CHECKS = (("identite", kyc.FIELD_IDENTITY),
               ("conflit", kyc.FIELD_CONFLICT))
_KYC_FIELD_BY_SLUG = dict(_KYC_CHECKS)
# One badge per status of kyc.STATUS_LABELS (pinned): « Identifié » takes
# the blue « Exempté » had — decided, yet distinct from Vérifié's green.
_KYC_BADGES = {
    "identifié": "bg-blue-100 text-blue-700",
    "vérifié": "bg-green-100 text-green-700",
    "non_vérifié": "bg-orange-100 text-orange-700",
    "conflit_détecté": "bg-red-100 text-red-700",
}
# A presumed status is NOT decided: amber, never the green of a verified
# identity — at a glance the fiche must not read as verified (D7).
_KYC_PRESUMED_BADGE = "bg-amber-100 text-amber-700"
# …EXCEPT the one presumed outcome that is an ALARM, not an all-clear: a
# conflict Claude detected keeps the red of a detected conflict (label
# « (présumé) » all the same). Amber is the colour of « Vérifié (présumé) »
# too, so softening it would make « Claude says conflict » and « Claude says
# verified » look alike at a glance — under-warning on a deontological bar
# to acting is the failure that costs; over-warning merely costs a look.
_KYC_PRESUMED_KEEPS_ITS_BADGE = frozenset({"conflit_détecté"})


def _kyc_day(value) -> str:
    """« 25 septembre 2026 » in Montréal — ``""`` when there is no date
    (a contact created with a decided status before lot 4a has none)."""
    local = to_mtl(value) if value else None
    return format_date_fr(local.date()) if local else ""


def _kyc_view(partie: dict) -> dict:
    """Per check: the badge, its label and the ATTRIBUTION line — composed
    here, so the signer's name is never attached to a Claude inscription.

    * presumed (inscribed by Claude, not confirmed): amber « … (présumé) »
      — a presumed « Conflit détecté » keeps its red, an alarm is never
      softened —, « inscrit par Claude le … — à confirmer », and the
      « Confirmer » button;
    * Claude's, confirmed: « confirmé le … par Me … (inscrit par Claude le
      …) »;
    * the lawyer's: « le … par Me … » (the text before lot 4a).
    A missing date drops the date, never raises.
    """
    signer = _compliance_signer()
    by = f" par {signer}" if signer else ""
    view: dict = {}
    for slug, field in _KYC_CHECKS:
        status = kyc.stored_status(partie, field)
        presumed = kyc.is_presumed(partie, field)
        label = kyc.STATUS_LABELS.get(status, status)
        badge = _KYC_BADGES.get(status, "bg-gray-100 text-gray-600")
        if presumed:
            label = f"{label} (présumé)"
            if status not in _KYC_PRESUMED_KEEPS_ITS_BADGE:
                badge = _KYC_PRESUMED_BADGE
        inscribed = _kyc_day(partie.get(kyc.date_key(field)))
        attribution = ""
        if status in kyc.DECIDED[field]:
            if presumed:
                attribution = (
                    f"inscrit par Claude le {inscribed} — à confirmer"
                    if inscribed else "inscrit par Claude — à confirmer"
                )
            elif kyc.source_of(partie, field) == kyc.SOURCE_MCP:
                confirmed = _kyc_day(partie.get(kyc.confirmed_at_key(field)))
                attribution = (
                    f"confirmé le {confirmed}{by}" if confirmed
                    else f"confirmé{by}"
                )
                if inscribed:
                    attribution += f" (inscrit par Claude le {inscribed})"
            elif inscribed:
                attribution = f"le {inscribed}{by}"
        view[slug] = {
            "slug": slug,
            "field": field,
            "status": status,
            "label": label,
            "badge": badge,
            "presumed": presumed,
            "attribution": attribution,
            "notes": str(partie.get(kyc.notes_key(field)) or ""),
        }
    return view


@parties_bp.route("/<partie_id>")
@login_required
def partie_detail(partie_id: str) -> str:
    """Render the partie detail page."""
    partie = get_partie(partie_id)
    if not partie:
        return render_template(
            "parties/list.html",
            parties=[],
            role_filter="",
            search="",
            role_labels=ROLE_LABELS,
            valid_roles=VALID_CONTACT_ROLES,
            error="Contact introuvable.",
        )

    partie["_display_name"] = display_name(partie)
    # The heading carries the honorific suffix (« …, j.c.s. »), and the
    # occupation and organization rows their labels: composed here, the
    # template owns no vocabulary.
    partie["_nom_entete"] = formal_name(partie)
    partie["_occupation_label"] = occupations.occupation_label(
        partie.get("occupation", ""))
    partie["_organization_label"] = occupations.organization_label(
        partie.get("occupation", ""))
    # « Appeler » / « Courriel » reach the person where the documents write
    # to them — personal first for a natural person (template_fields).
    partie["_quick_phone"] = selected_phone(partie)
    partie["_quick_email"] = selected_email(partie)
    # A « Fonction » the migration has not folded yet still shows, folded.
    partie["_poste"] = fold_job_role(
        civilite.as_text(partie.get("job_title", "")),
        civilite.as_text(partie.get("job_role", "")))
    dossiers = list_dossiers_for_partie(partie_id)
    mandataires = _hydrate_mandataires(partie.get("mandataires"))
    # Conformité is shown for a contact whose role is « client » — and for
    # any contact that is a CLIENT of a dossier whatever its role: the
    # coverage report checks every dossier client, so the fiche must show
    # what it checks. Computed ONCE, here: the card's condition, the checks'
    # grid and the proofs block all read this one flag (one authority per
    # derived value; the template recomputes nothing).
    conformite_kyc = partie.get("contact_role") == "client" or any(
        partie_id in (d.get("client_ids") or []) for d in dossiers
    )
    preuves = _id_proofs_view(partie_id, conformite_kyc)
    # ?gerer=1 reopens the « Gérer » dialog — after a rename, a deletion, a
    # consult that failed, or the uploads. Only « 1 » opens it (a query
    # filter is checked against its vocabulary), and only where the block
    # shows at all.
    preuves["ouvert"] = preuves["visible"] and request.args.get("gerer") == "1"
    return render_template(
        "parties/detail.html",
        partie=partie,
        conformite_kyc=conformite_kyc,
        preuves=preuves,
        preuves_config=_id_proofs_config(partie_id, preuves["ouvert"]),
        kyc_view=_kyc_view(partie),
        # Signataire des vérifications de conformité. Le gabarit lisait
        # `config.FIRM_NAME` directement ; la valeur vit maintenant dans
        # settings/cabinet, et le libellé se compose ici (« le gabarit ne
        # possède que le libellé, jamais une transformation »).
        #
        # Il lit le nom du JURISTE, jamais celui du cabinet : une attestation
        # d'identité est signée par une personne. La mention est DÉRIVÉE À LA
        # LECTURE, sans instantané au dossier — renommer le champ
        # réattribuerait donc toutes les attestations passées, ce qui est
        # précisément pourquoi il ne doit jamais pointer sur `organisation`.
        # Bandeau d'arrivée : Réception redirige ICI après avoir créé ou mis à
        # jour une fiche depuis une ouverture du portail, et doit pouvoir dire
        # ce qui vient de se passer (« vérifiez et complétez », combien de
        # champs appliqués, combien de contacts adverses créés). Sans lui, le
        # message était construit puis silencieusement perdu.
        message=sanitize(request.args.get("message", ""), max_length=300),
        # Bandeau d'erreur : un « Supprimer » REFUSÉ revient ici avec la
        # raison (contact lié à un dossier, mandataire d'un autre contact).
        # 600 et non 300 : la raison nomme jusqu'à trois contacts.
        erreur=sanitize(request.args.get("erreur", ""), max_length=600),
        role_labels=ROLE_LABELS,
        mandataires=mandataires,
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
        dossiers=dossiers,
        dossier_status_labels=DOSSIER_STATUS_LABELS,
        dossier_domaine_labels=DOSSIER_DOMAINE_LABELS,
    )


# ── Create ────────────────────────────────────────────────────────────────


@parties_bp.route("/new")
@login_required
def partie_new() -> str:
    """Render the empty partie form."""
    return render_template(
        "parties/form.html",
        partie=None,
        errors=[],
        role_labels=ROLE_LABELS,
        mandataires=[],
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
        **_form_context(None),
    )


@parties_bp.route("/", methods=["POST"])
@login_required
def partie_create() -> str:
    """Handle new partie form submission."""
    data = _form_data()
    # The lawyer's form: a decided status set here is HIS attestation (D7).
    partie, errors = create_partie(data, kyc_source=kyc.SOURCE_JURISTE)

    if errors:
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            role_labels=ROLE_LABELS,
            mandataires=_hydrate_mandataires(data.get("mandataires")),
            mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
            kyc_presumed=_kyc_presumed(None),
            **_form_context(data),
        )

    # The phone's address book: the card, or its tombstone for a role it
    # never carries (services/carnet.py, Change Impact item 2).
    apres_ecriture_contact(partie)

    return redirect_after_post(
        url_for("parties.partie_detail", partie_id=partie["id"]))


# ── Edit ──────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/edit")
@login_required
def partie_edit(partie_id: str) -> str:
    """Render the edit form pre-filled with partie data."""
    partie = get_partie(partie_id)
    if not partie:
        return redirect(url_for("parties.partie_list"))

    return render_template(
        "parties/form.html",
        partie=partie,
        errors=[],
        role_labels=ROLE_LABELS,
        mandataires=_hydrate_mandataires(partie.get("mandataires")),
        mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
        kyc_presumed=_kyc_presumed(partie),
        is_dossier_client=_is_dossier_client(partie_id),
        **_form_context(partie),
    )


@parties_bp.route("/<partie_id>", methods=["POST"])
@login_required
def partie_update(partie_id: str) -> str:
    """Handle edit form submission."""
    expected = edit_conflict.submitted_etag()
    data = _form_data()
    # The lawyer's form (D7): a CHANGED status becomes his decision; an
    # unchanged one — a presumed Claude inscription re-submitted by an
    # unrelated save — keeps its provenance. Only « Confirmer » confirms.
    partie, errors = update_partie(
        partie_id, data, expected_etag=expected,
        kyc_source=kyc.SOURCE_JURISTE)

    if errors:
        errors, conflict, data["etag"] = edit_conflict.resolve_refusal(
            errors,
            submitted=expected,
            reread=lambda: get_partie(partie_id),
            compare_url=url_for("parties.partie_detail", partie_id=partie_id),
        )
        data["id"] = partie_id
        stored = get_partie(partie_id)
        if "contact_role" not in data:
            # Not posted, so not changed: the page shows the stored role —
            # never « client », which a resubmission would then save.
            data["contact_role"] = (stored or {}).get("contact_role", "")
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            conflict=conflict,
            role_labels=ROLE_LABELS,
            mandataires=_hydrate_mandataires(data.get("mandataires")),
            mandataire_kind_labels=MANDATAIRE_KIND_LABELS,
            kyc_presumed=_kyc_presumed(stored),
            is_dossier_client=_is_dossier_client(partie_id),
            **_form_context(data),
        )

    # A role change in or out of the phone's address book moves the card
    # (services/carnet.py, Change Impact item 2).
    apres_ecriture_contact(partie)

    return redirect_after_post(
        url_for("parties.partie_detail", partie_id=partie_id))


# ── Conformité : « Confirmer » une inscription présumée (D7) ─────────────


@parties_bp.route("/<partie_id>/conformite/<check>/confirmer", methods=["POST"])
@login_required
def kyc_confirm(partie_id: str, check: str) -> Response:
    """The lawyer confirms a check Claude INSCRIBED (presumed until now).

    A plain form POST (CSRF enforced by the blueprint) carrying the etag the
    fiche was rendered from — a confirmation says « I read THIS version »,
    so an inscription that changed since is refused, never confirmed
    unseen. Answered by a redirect to the fiche, always in 2xx after it:
    ``?message=`` on success, ``?erreur=`` (the model's French refusal) on
    refusal. The contact's etag moves, so the ``parties`` CTag is bumped
    (DavX5's next If-Match PUT would 412 otherwise).
    """
    field = _KYC_FIELD_BY_SLUG.get(check)
    if field is None or not partie_id.strip():
        return redirect(url_for("parties.partie_list"))
    expected = edit_conflict.submitted_etag()
    _doc, errors = confirm_kyc_status(
        partie_id, field, par=kyc.SOURCE_JURISTE, expected_etag=expected)
    if errors:
        if errors == [PARTIE_NOT_FOUND]:
            return redirect_after_post(url_for("parties.partie_list"))
        erreur = (
            "La fiche a changé depuis son affichage — rien n'a été confirmé. "
            "Relisez-la, puis confirmez de nouveau."
            if concurrency.is_stale(errors) else " ".join(errors)
        )
        target = url_for("parties.partie_detail", partie_id=partie_id,
                         erreur=erreur)
    else:
        apres_ecriture_contact(_doc)
        log_partie_event("kyc_confirmed", partie_id, field=field)
        target = url_for("parties.partie_detail", partie_id=partie_id,
                         message="Vérification confirmée.")
    return redirect_after_post(target)


# ── Delete ────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/delete", methods=["POST"])
@login_required
def partie_delete(partie_id: str) -> str:
    """Delete a partie and redirect to the list."""
    existing = get_partie(partie_id)
    success, error = delete_partie(partie_id)

    if success:
        apres_suppression_contact(partie_id)
        # Append-only deletion trail (PA-G06).
        record_deletion(
            "partie", partie_id,
            title=display_name(existing) if existing else "",
            status=(existing or {}).get("contact_role", ""),
        )

    # A REFUSED deletion lands back on the contact's own page with the
    # French reason in a red banner — it used to redirect to the LIST like a
    # success, so a contact still linked to a dossier, or still the
    # mandataire of another contact, silently stayed while the lawyer
    # believed it gone (lot 0b review). Always a redirect: htmx swaps 2xx
    # only, and the confirmation dialog is a plain form POST anyway.
    if success or error == PARTIE_NOT_FOUND:
        target = url_for("parties.partie_list")
    else:
        target = url_for(
            "parties.partie_detail", partie_id=partie_id, erreur=error
        )
    return redirect_after_post(target)


# ── Export ───────────────────────────────────────────────────────────────


# « _courriel » / « _ville » sont DÉRIVÉS (motif de _display_name) : lire
# email/address_city bruts exportait toute personne morale sans courriel ni
# ville — son adresse et son courriel vivent dans le bloc professionnel, le
# formulaire masquant le bloc personnel pour une entreprise.
_EXPORT_COLUMNS_CSV = [
    ("_display_name", "Nom"),
    ("contact_role", "Rôle"),
    ("_courriel", "Courriel"),
    ("phone_cell", "Cellulaire"),
    ("phone_work", "Tél. professionnel"),
    ("organization", "Organisation"),
    ("_ville", "Ville"),
]

_EXPORT_COLUMNS_PDF = [
    ("_display_name", "Nom", 2.0),
    ("contact_role", "Rôle", 1.0),
    ("_courriel", "Courriel", 1.5),
    ("phone_cell", "Cellulaire", 1.0),
    ("phone_work", "Tél. professionnel", 1.0),
    ("organization", "Organisation", 1.5),
    ("_ville", "Ville", 1.0),
]


def _get_export_parties() -> list[dict]:
    """Fetch and pre-process parties for export, respecting current filters."""
    role_filter = _role_filter()
    search = request.args.get("q", "").strip()

    parties = list_parties(
        role_filter=role_filter if role_filter != ALL_ROLES else None,
        search=search or None,
    )
    for p in parties:
        p["_display_name"] = display_name(p)
        # L'adresse et le courriel RETENUS — la même autorité que les
        # gabarits, l'accusé du portail et le connecteur MCP.
        p["_ville"] = selected_address(p).get("city", "")
        p["_courriel"] = selected_email(p)
        p["contact_role"] = ROLE_LABELS.get(p.get("contact_role", ""), p.get("contact_role", ""))
    return parties


@parties_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export parties as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"impliques_{date_str}.csv",
    )


@parties_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export parties as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Impliqués",
        filename=f"impliques_{date_str}.pdf",
    )


# ── Preuves d'identité (lot 2 of the compliance work, 2026-11) ───────────
#
# A contact's proofs of identity (art. 22, RLRQ c. B-1, r. 5): the fiche's
# « Preuves d'identité » block, inside its Conformité card, and the
# « Gérer » dialog. Every rule and every Storage verb belongs to the model
# (``models/piece_identite.py``); these routes map its closed outcome codes
# to HTTP or to a banner, and journal what the model says happened:
#
# * the two JSON endpoints the dialog's script fetches — the upload
#   session, the filing — keep their 4xx/5xx, like ``documents.api_*``:
#   fetch reads them, htmx never does;
# * consult, rename and delete answer with a REDIRECT to the fiche, the
#   dialog reopened (``?gerer=1``) with its banner (``?message=`` /
#   ``?erreur=``) — a 200 carrying ``HX-Redirect`` under htmx
#   (``routes/_helpers.redirect_after_post``): Rule 8, never a 422 (the
#   ``kyc_confirm`` pattern).
#
# The uid comes only from ``utils.storage_identity``. Logs carry ids only
# (« Do not log PII »): never a display name, a file name, a storage path
# or a signed URL — the session URL and the signed URL are capabilities,
# handed to the uploading script or to the browser's redirect, never
# logged nor rendered in a page.

ID_PROOF_RENAMED = "Nom du document modifié."
ID_PROOF_DELETED = "Preuve d'identité supprimée."
# Never shown in practice: the model gives every refusal its French
# sentence. A banner is still never empty.
_ID_PROOF_FALLBACK = "L'opération n'a pas abouti — rechargez la page."

# The HTTP status of every NON-success code the two JSON endpoints answer:
# each code of SESSION_CODES and FILING_CODES but ok / verse / rejoue
# (pinned by the tests). « incertain » is a 503 with Retry-After: the
# script re-posts the finalization once, which settles it only when the
# store answers (rejoue, or verse). A re-post that meets the same outage
# answers like any filing whose first read failed (« indisponible », « Rien
# n'a été enregistré »): the page then keeps the uncertain sentence.
_ID_PROOF_JSON_STATUS = {
    piece_identite.CODE_FORME: 400,
    piece_identite.CODE_REFUS: 422,
    piece_identite.CODE_INDISPONIBLE: 503,
    piece_identite.CODE_INCERTAIN: 503,
}
_ID_PROOF_RETRY_SECONDS = 2


def _id_proof_day(value: object) -> str:
    """« 2 novembre 2026 » — a proof's filing day in Montréal (Rule 5);
    ``""`` when the stored value is not an instant (a hand edit)."""
    if not isinstance(value, datetime):
        return ""
    return format_date_fr(to_mtl(value).date())


def _id_proof_row(proof: dict) -> dict:
    """One proof as the card and the dialog show it."""
    name = proof.get("display_name")
    size = proof.get("file_size")
    return {
        "id": proof.get("id") or "",
        # A stored name that is no text (a hand edit) shows the neutral
        # name rather than an empty row.
        "display_name": (name if isinstance(name, str) and name.strip()
                         else piece_identite.NEUTRAL_NAME),
        # The PROOF's own etag: the rename and delete forms commit only
        # against the version the dialog showed.
        "etag": concurrency.etag_of(proof),
        # _file_icon's `kind` (pdf | image | file → its glyph).
        "icone": get_file_icon(proof.get("file_type") or ""),
        "taille": (format_file_size(size)
                   if isinstance(size, int) and not isinstance(size, bool)
                   else ""),
        "jour": _id_proof_day(proof.get("created_at")),
    }


def _id_proofs_view(partie_id: str, conformite_kyc: bool) -> dict:
    """The proofs block of the fiche — composed HERE, read alike by the
    card (``parties/_pieces_identite.html``) and the « Gérer » dialog
    (``parties/_pieces_identite_gestion.html``).

    ``liste`` (not ``items``: Jinja would read ``preuves.items`` as the
    dict's method) is empty both when the contact holds no proof and when
    the read FAILED; ``indisponible`` tells the two apart, so a failed read
    never shows « aucune » (``list_proofs`` is the fail-open reader:
    display only). ``tronquee``: the read met more than the cap.

    ``visible`` — the Conformité rule (*conformite_kyc*) OR at least one
    proof held: a role change must never strand confidential files out of
    reach. A failed read on a contact that is not a client shows nothing:
    unknown is not claimed. ``detenues`` adds a sentence to the contact's
    delete dialog (the model refuses that deletion; the sentence informs).
    ``ouvert`` is set by the caller (``?gerer=1``).
    """
    listing = piece_identite.list_proofs(partie_id)
    liste = ([_id_proof_row(proof) for proof in listing.proofs]
             if listing is not None else [])
    return {
        "liste": liste,
        "indisponible": listing is None,
        "tronquee": bool(listing is not None and listing.truncated),
        "tronquee_texte": piece_identite.TRUNCATED_NOTICE,
        "detenues": bool(liste),
        "visible": bool(conformite_kyc or liste),
        "ouvert": False,
        "accept": piece_identite.ACCEPT,
        "consignes": piece_identite.UPLOAD_HINT,
        "nom_max": piece_identite.DISPLAY_NAME_MAX,
    }


def _id_proofs_config(partie_id: str, ouvert: bool) -> dict:
    """What the dialog's script reads from the page — every URL built by
    the server (the script composes none), in a ``<script
    type="application/json">`` (server data stays out of the script, CSP).
    ``suivant``: the fiche with the dialog reopened, once every file of a
    batch is filed; ``fiche``: the fiche alone, when the dialog closes
    after a file was filed or its filing left unconfirmed (the list then
    shows whether the proof is there). ``incertain``: the model's sentence
    for a filing whose outcome stayed unknown (its single source,
    ``FILING_UNCERTAIN``), which the page shows when a finalization's
    answer was lost on the way and the one re-post did not settle it."""
    return {
        "ouvert": bool(ouvert),
        "televersement": url_for("parties.id_proof_session",
                                 partie_id=partie_id),
        "finaliser": url_for("parties.id_proof_finalize",
                             partie_id=partie_id),
        "suivant": url_for("parties.partie_detail", partie_id=partie_id,
                           gerer="1", _anchor="preuves-identite"),
        "fiche": url_for("parties.partie_detail", partie_id=partie_id,
                         _anchor="preuves-identite"),
        "incertain": piece_identite.FILING_UNCERTAIN,
    }


def _back_to_proofs(partie_id: str, *, message: str = "",
                    erreur: str = "") -> Response:
    """Redirect to the fiche, the « Gérer » dialog reopened with its
    banner — a 200 carrying ``HX-Redirect`` under htmx (Rule 8: always a
    2xx after the redirect, never a 422)."""
    params = {"gerer": "1"}
    if message:
        params["message"] = message
    if erreur:
        params["erreur"] = erreur
    return redirect_after_post(url_for(
        "parties.partie_detail", partie_id=partie_id,
        _anchor="preuves-identite", **params))


def _id_proof_ids(partie_id: object, proof_id: object = None) -> dict:
    """The log fields of a proof — ids from the URL, CR/LF neutralized."""
    ids = {"partie_id": sanitize_log_value(partie_id)}
    if proof_id is not None:
        ids["proof_id"] = sanitize_log_value(proof_id)
    return ids


def _json_payload() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _text(value: object) -> str:
    """A JSON field as the model expects it: a string, or ``""`` (which
    the model refuses in French) — never coerced, never trimmed: the model
    judges the exact string."""
    return value if isinstance(value, str) else ""


def _json_refusal(outcome) -> Response:
    """A JSON endpoint's answer to a non-success code — the model's French
    sentence, the code's status."""
    body = {"erreur": " ".join(outcome.errors) or _ID_PROOF_FALLBACK}
    if outcome.code == piece_identite.CODE_INCERTAIN:
        # Both objects were KEPT (the model's declared exception): one more
        # finalization — idempotent through the reserved id and the
        # create-only copy — settles it only when the store answers
        # (rejoue, or verse). Any other answer to it leaves the outcome
        # unknown: the page keeps THIS sentence, never the re-post's.
        body["incertain"] = True
    resp = jsonify(body)
    resp.status_code = _ID_PROOF_JSON_STATUS.get(outcome.code, 500)
    if outcome.code == piece_identite.CODE_INCERTAIN:
        resp.headers["Retry-After"] = str(_ID_PROOF_RETRY_SECONDS)
    return resp


@parties_bp.route("/<partie_id>/pieces-identite/televersement",
                  methods=["POST"])
@login_required
def id_proof_session(partie_id: str) -> Response:
    """Open the browser's DIRECT upload of one proof (JSON ``{name,
    size}``; CSRF in the ``X-CSRFToken`` header).

    The model judges the name, its extension, the declared size, the
    contact and the cap BEFORE a byte moves, reserves the proof's id and
    opens a create-only resumable session on its staging name. 200
    ``{url, objet}`` — ``url`` is a write capability, handed to the
    uploading script only, never logged —; 422 a refusal; 503 a store
    failure or a uid that cannot name a Storage prefix.
    """
    payload = _json_payload()
    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": storage_identity.public_message(exc)}), 503
    # The session's CORS origin is the PAGE's. In production TLS ends
    # upstream of gunicorn and there is no ProxyFix: request.scheme would
    # read « http » and the browser would refuse the PUTs — https is
    # FORCED there (routes/documents.api_televersement's rule).
    scheme = "https" if Config.ENV == "production" else (request.scheme or "http")
    outcome = piece_identite.open_proof_upload(
        partie_id, _text(payload.get("name")), payload.get("size"), user_id,
        origin=f"{scheme}://{request.host}",
    )
    if outcome.code == piece_identite.CODE_OK:
        return jsonify({"url": outcome.doc["url"],
                        "objet": outcome.doc["objet"]})
    return _json_refusal(outcome)


@parties_bp.route("/<partie_id>/pieces-identite/finaliser", methods=["POST"])
@login_required
def id_proof_finalize(partie_id: str) -> Response:
    """File an uploaded staging object as a proof of the contact (JSON
    ``{objet, name}``).

    200 ``{ok, proof_id, suivant}`` when the proof is filed — by this call
    (``verse``: the one ``id_proof_filed`` event) or already (``rejoue``: a
    replay, nothing new, no event); 400 « Requête invalide. » for a staging
    name that is not this module's shape (nothing touched); 422 a refusal;
    503 a store failure, or ``incertain`` (``"incertain": true`` and
    ``Retry-After``) — the script then re-posts this finalization ONCE.
    """
    payload = _json_payload()
    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": storage_identity.public_message(exc)}), 503
    outcome = piece_identite.file_staged_proof(
        _text(payload.get("objet")), partie_id, _text(payload.get("name")),
        user_id,
    )
    if outcome.code not in (piece_identite.CODE_VERSE,
                            piece_identite.CODE_REJOUE):
        return _json_refusal(outcome)
    proof = outcome.doc or {}
    if outcome.code == piece_identite.CODE_VERSE:
        log_partie_event(
            "id_proof_filed", sanitize_log_value(partie_id),
            proof_id=proof.get("id"), file_type=proof.get("file_type"),
            size=proof.get("file_size"),
        )
    return jsonify({
        "ok": True,
        "proof_id": proof.get("id"),
        "suivant": url_for("parties.partie_detail", partie_id=partie_id,
                           gerer="1", _anchor="preuves-identite"),
    })


@parties_bp.route("/<partie_id>/pieces-identite/<proof_id>")
@login_required
def id_proof_consult(partie_id: str, proof_id: str) -> Response:
    """« Consulter » — a 302 to a 15-minute signed URL that SHOWS the file
    (inline, under its display name). The URL is minted on the click and
    never embedded in a page.

    Every failure lands back on the fiche, the dialog open, with its own
    French sentence — logged, never swallowed: the record unreadable, the
    proof gone, its stored path not this module's shape, its file missing,
    the reload or the signing failed.
    """
    ids = _id_proof_ids(partie_id, proof_id)
    try:
        proof = piece_identite.get_proof_strict(partie_id, proof_id)
    except Exception as exc:
        log_unexpected("id proof: read failed", exc_info=False,
                       error_type=type(exc).__name__, **ids)
        return _back_to_proofs(partie_id,
                               erreur=concurrency.READ_UNAVAILABLE_ERROR)
    if proof is None:
        return _back_to_proofs(partie_id, erreur=piece_identite.PROOF_NOT_FOUND)
    try:
        url = piece_identite.consult_url(partie_id, proof)
    except piece_identite.StoredPathRefused:
        log_unexpected("id proof: stored path refused", exc_info=False, **ids)
        return _back_to_proofs(partie_id, erreur=piece_identite.PATH_REFUSED)
    except piece_identite.ObjectMissing:
        log_unexpected("id proof: object missing", exc_info=False, **ids)
        return _back_to_proofs(partie_id, erreur=piece_identite.OBJECT_MISSING)
    except Exception as exc:
        # The reload or the signing: their text can name the object, so
        # the class only, never a traceback.
        log_unexpected("id proof: signing failed", exc_info=False,
                       error_type=type(exc).__name__, **ids)
        return _back_to_proofs(partie_id, erreur=piece_identite.CONSULT_FAILED)
    return redirect(url)


@parties_bp.route("/<partie_id>/pieces-identite/<proof_id>/renommer",
                  methods=["POST"])
@login_required
def id_proof_rename(partie_id: str, proof_id: str) -> Response:
    """Rename one proof — compare-and-set on the etag its row was rendered
    with (``expected_etag``, required by the model). The typed name is
    trimmed here, as every form field is; the model judges the exact
    string it would store, and refuses rather than alters it. An unchanged
    name writes nothing and says nothing."""
    expected = edit_conflict.submitted_etag()
    outcome = piece_identite.rename_proof(
        partie_id, proof_id, request.form.get("display_name", "").strip(),
        expected_etag=expected,
    )
    if outcome.code == piece_identite.CODE_MODIFIE:
        log_partie_event("id_proof_renamed", sanitize_log_value(partie_id),
                         proof_id=sanitize_log_value(proof_id))
        return _back_to_proofs(partie_id, message=ID_PROOF_RENAMED)
    if outcome.code == piece_identite.CODE_INCHANGE:
        return _back_to_proofs(partie_id)
    return _back_to_proofs(
        partie_id, erreur=" ".join(outcome.errors) or _ID_PROOF_FALLBACK)


@parties_bp.route("/<partie_id>/pieces-identite/<proof_id>/supprimer",
                  methods=["POST"])
@login_required
def id_proof_delete(partie_id: str, proof_id: str) -> Response:
    """Delete one proof for good (D6): the model erases its file by
    generation, then deletes its record EXACTLY ONCE.

    Only the model's ``supprime`` means THIS call removed the record: then,
    and only then, ONE neutral row joins the deletion trail — the title is
    « Preuve d'identité », never the display name (``audit_events`` is
    write-once, and a name typed by hand must not outlive the erased file)
    — and the one ``id_proof_deleted`` event is logged. ``deja`` (a double
    submission's other half found the record gone) writes nothing and
    says so, without alarm: the proof is deleted."""
    expected = edit_conflict.submitted_etag()
    outcome = piece_identite.delete_proof(partie_id, proof_id,
                                          expected_etag=expected)
    if outcome.code == piece_identite.CODE_SUPPRIME:
        record_deletion(
            "piece_identite", partie_id,
            title=piece_identite.NEUTRAL_NAME,
            status=(outcome.doc or {}).get("file_type") or "",
        )
        log_partie_event("id_proof_deleted", sanitize_log_value(partie_id),
                         proof_id=sanitize_log_value(proof_id))
        return _back_to_proofs(partie_id, message=ID_PROOF_DELETED)
    if outcome.code == piece_identite.CODE_DEJA:
        return _back_to_proofs(
            partie_id, message=" ".join(outcome.errors) or ID_PROOF_DELETED)
    return _back_to_proofs(
        partie_id, erreur=" ".join(outcome.errors) or _ID_PROOF_FALLBACK)
