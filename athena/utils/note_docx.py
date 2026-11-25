"""Note → note-print context builder (gabarit kind « note », Phase H.3).

Pure — no Firestore, no Flask (the caller loads the note and its dossier).
Mirrors ``utils/invoice_docx.py``: the header namespaces (``dossier.*``,
``cabinet.*``, ``date.*`` + flat aliases) resolve through the Phase H
catalog, with a ``note.*`` overlay supplied here. The note's Markdown body
is the single RICH value — filled by the engine's ``rich_values`` hook
(markdown → formatted Word content), never as a scalar.

``_CATEGORY_LABELS`` mirrors ``models.note.CATEGORY_LABELS`` locally —
importing ``models`` constructs the Firestore client at import time, and
this module must stay importable by the test suite without one (the same
mirror-with-pinning-test pattern as ``template_fields._ROLE_LABEL``).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from tz import MTL
from utils.format_en import format_date_en
from utils.format_fr import format_date_fr
from utils.template_fields import (
    CATALOG,
    FLAT_ALIASES,
    classify_placeholders,
    fallback_value,
    manual_value,
    normalize_lang,
    resolve_values,
)

# The one placeholder filled through the engine's rich hook.
RICH_FIELD = "note.contenu"

# Mirror of models.note.CATEGORY_LABELS (pinned equal by test_note_docx).
_CATEGORY_LABELS = {
    "rencontre": "Rencontre",
    "consultation": "Consultation",
    "analyse": "Analyse",
    "recherche": "Recherche",
    "stratégie": "Stratégie",
    "vacation": "Vacation",
    "autre": "Autre",
}
_CATEGORY_LABELS_EN = {
    "rencontre": "Meeting",
    "consultation": "Consultation",
    "analyse": "Analysis",
    "recherche": "Research",
    "stratégie": "Strategy",
    "vacation": "Attendance",
    "autre": "Other",
}


@dataclass
class NoteContext:
    values: dict[str, str] = field(default_factory=dict)
    rich_values: dict[str, str] = field(default_factory=dict)


def _mtl_date_str(value, lang: str = "fr") -> str:
    """A stored UTC instant → its Montréal calendar date, long form.

    NOT the invoice path's UTC-date rule: note timestamps are true instants
    (unlike the invoice's midnight-UTC date-only fields), so the UTC date of
    an evening note is tomorrow — convert to MTL first, exactly as the note
    detail page's ``to_mtl`` filter does.
    """
    if value is None or not hasattr(value, "astimezone"):
        return ""
    d = value.astimezone(MTL).date()
    return format_date_en(d) if normalize_lang(lang) == "en" else format_date_fr(d)


def build_note_context(
    note: dict,
    *,
    dossier: Optional[dict],
    firm: dict,
    today: date,
    lang: str = "fr",
) -> NoteContext:
    """Build the fill context for the note-print gabarit."""
    norm_lang = normalize_lang(lang)
    values = resolve_values(
        list(CATALOG) + list(FLAT_ALIASES),
        dossier=dossier,
        client=None,
        adverse=None,
        destinataire=None,
        firm=firm or {},
        today=today,
        lang=norm_lang,
    )

    file_number = note.get("dossier_file_number", "")
    if file_number:
        dossier_display = f"{file_number} — {note.get('dossier_title', '')}".rstrip(
            " —"
        )
    else:
        dossier_display = "General" if norm_lang == "en" else "Général"

    created = note.get("created_at")
    updated = note.get("updated_at")
    date_maj = _mtl_date_str(updated, lang=norm_lang)
    if date_maj and date_maj == _mtl_date_str(created, lang=norm_lang):
        # Mirrors the detail page: « Modifiée » only when it adds information.
        date_maj = ""

    category = note.get("category", "")
    cat_labels = _CATEGORY_LABELS_EN if norm_lang == "en" else _CATEGORY_LABELS
    values.update(
        {
            "note.titre": note.get("title", ""),
            "note.categorie": cat_labels.get(category, category),
            "note.date": _mtl_date_str(created, lang=norm_lang),
            "note.date_maj": date_maj,
            "note.dossier": dossier_display,
        }
    )
    return NoteContext(
        values=values,
        rich_values={RICH_FIELD: note.get("content", "")},
    )


def assemble_note_print_values(
    template: dict, ctx: NoteContext, *, lang: str = "fr"
) -> dict[str, str]:
    """One value per template placeholder — the route's assembly seam,
    Flask-free so it is testable without an app.

    Per placeholder: ctx.values exact match → ctx.values case-insensitive
    (an ALL-CAPS name uppercases its value, the catalog convention) → auto
    fallback marker → manual default. :data:`RICH_FIELD` is skipped (it
    travels via ``rich_values``); passthrough names are omitted (left
    verbatim in the .docx).
    """
    norm_lang = normalize_lang(lang)
    if norm_lang == "en" and template.get("en_placeholders") is not None:
        placeholders = template.get("en_placeholders") or []
    else:
        placeholders = template.get("placeholders", [])
    classification = classify_placeholders(placeholders)
    lowered = {k.lower(): v for k, v in ctx.values.items()}
    values: dict[str, str] = {}
    for name in placeholders:
        if name.lower() == RICH_FIELD:
            continue
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name.lower() in lowered:
            v = lowered[name.lower()]
            values[name] = v.upper() if name.isupper() else v
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True, lang=norm_lang)
        elif name in classification.manual:
            # See routes/invoices.py — bare indexing KeyErrors on a
            # case-insensitive manual name, here too.
            values[name] = manual_value(name, lang=norm_lang)
        # else: passthrough → omit
    return values
