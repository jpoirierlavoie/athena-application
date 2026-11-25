"""Printable PDF *Bordereau d'accusé de réception* for portal lots and openings.

Uses ReportLab with ``NotoSerif`` / ``NotoSerif-Bold`` registered via
``utils.export_pdf`` (never Helvetica) and formats all instants in Montreal
local time (``utils.tz.to_mtl``). Pure builder — imports neither Flask nor
Firestore models.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tz import to_mtl
from utils.export_pdf import FONT, FONT_BOLD
from utils.format_fr import format_date_fr


def _format_size(size_bytes: int) -> str:
    n = max(0, int(size_bytes or 0))
    if n >= 1048576:
        human = f"{n / 1048576:.1f}".replace(".", ",") + " Mo"
    elif n >= 1024:
        human = f"{round(n / 1024)} ko"
    else:
        human = f"{n} o"
    return f"{human} ({n} octets)"


def _format_iso_mtl(raw: object) -> str:
    """Parse an ISO UTC instant string (or datetime) into Montreal readable form."""
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return raw.strip()
    else:
        return "—"
    local = to_mtl(dt)
    return f"{format_date_fr(local.date())} à {local.strftime('%H h %M')}"


def _file_status_label(entree: dict) -> str:
    etat = (entree.get("etat") or "reçu").strip().lower()
    if etat == "versé":
        dossier_ref = (entree.get("verse_dossier") or "").strip()
        return f"Versé ({dossier_ref})" if dossier_ref else "Versé"
    if etat == "refusé":
        return "Refusé"
    if etat == "manquant":
        return "Manquant"
    return "En attente"


def build_accuse_reception_pdf(
    invitation: dict,
    lots: list[dict],
    *,
    cabinet: dict,
    client: dict,
    dossier: dict | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """Render a printable PDF acknowledgment sheet (*Bordereau d'accusé de réception*)."""
    now_dt = generated_at or datetime.now(timezone.utc)
    now_local = to_mtl(now_dt if now_dt.tzinfo else now_dt.replace(tzinfo=timezone.utc))
    generated_line = f"{format_date_fr(now_local.date())} à {now_local.strftime('%H h %M')}"

    buffer = io.BytesIO()
    usable_width = LETTER[0] - 30 * mm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        title="Bordereau d'accusé de réception",
        initialFontName=FONT,
    )

    base = getSampleStyleSheet()
    s_firm = ParagraphStyle(
        "AccuseFirm", parent=base["Normal"], fontName=FONT_BOLD,
        fontSize=12, leading=15, textColor=colors.HexColor("#111827"),
    )
    s_firm_sub = ParagraphStyle(
        "AccuseFirmSub", parent=base["Normal"], fontName=FONT,
        fontSize=8.5, leading=12, textColor=colors.HexColor("#4B5563"),
    )
    s_title = ParagraphStyle(
        "AccuseTitle", parent=base["Normal"], fontName=FONT_BOLD,
        fontSize=14, leading=18, textColor=colors.HexColor("#111827"),
        spaceAfter=2,
    )
    s_sub = ParagraphStyle(
        "AccuseSub", parent=base["Normal"], fontName=FONT,
        fontSize=8.5, leading=12, textColor=colors.HexColor("#4B5563"),
        spaceAfter=8,
    )
    s_section = ParagraphStyle(
        "AccuseSection", parent=base["Normal"], fontName=FONT_BOLD,
        fontSize=9.5, leading=13, textColor=colors.HexColor("#111827"),
        spaceBefore=10, spaceAfter=4,
    )
    s_cell_bold = ParagraphStyle(
        "AccuseCellBold", parent=base["Normal"], fontName=FONT_BOLD,
        fontSize=8, leading=11, textColor=colors.HexColor("#111827"),
    )
    s_cell = ParagraphStyle(
        "AccuseCell", parent=base["Normal"], fontName=FONT,
        fontSize=8, leading=11, textColor=colors.HexColor("#1F2937"),
    )
    s_hash = ParagraphStyle(
        "AccuseHash", parent=base["Normal"], fontName=FONT,
        fontSize=6.8, leading=9.5, textColor=colors.HexColor("#374151"),
        wordWrap="CJK",
    )
    s_notice = ParagraphStyle(
        "AccuseNotice", parent=base["Normal"], fontName=FONT,
        fontSize=7.5, leading=10.5, textColor=colors.HexColor("#4B5563"),
        spaceBefore=10,
    )

    elements = []

    # ── En-tête cabinet ──────────────────────────────────────────────
    firm_parts = [p for p in (cabinet.get("organisation"), cabinet.get("nom")) if p]
    firm_title = " — ".join(firm_parts) if len(firm_parts) == 2 and firm_parts[0] != firm_parts[1] else (firm_parts[0] if firm_parts else "Cabinet")
    elements.append(Paragraph(escape(firm_title), s_firm))
    sub_lines = [escape(l) for l in (cabinet.get("adresse_lignes") or []) if l]
    contacts = []
    if cabinet.get("telephone"):
        contacts.append(f"Tél. : {escape(cabinet['telephone'])}")
    if cabinet.get("telecopieur"):
        contacts.append(f"Téléc. : {escape(cabinet['telecopieur'])}")
    if cabinet.get("courriel"):
        contacts.append(escape(cabinet["courriel"]))
    if contacts:
        sub_lines.append(" · ".join(contacts))
    if sub_lines:
        elements.append(Paragraph("<br/>".join(sub_lines), s_firm_sub))
    elements.append(Spacer(1, 8))

    elements.append(Paragraph("BORDEREAU D'ACCUSÉ DE RÉCEPTION", s_title))
    elements.append(
        Paragraph(
            f"Portail client sécurisé · Édité le {escape(generated_line)} (heure de Montréal)",
            s_sub,
        )
    )

    # ── Expéditeur & Invitation ──────────────────────────────────────
    client_lines = [escape(client.get("nom") or invitation.get("client_name") or invitation.get("email") or "—")]
    if client.get("organisation"):
        client_lines.append(escape(str(client["organisation"])))
    for adr in client.get("adresse_lignes") or []:
        client_lines.append(escape(str(adr)))
    if client.get("telephone"):
        client_lines.append(f"Tél. : {escape(str(client['telephone']))}")
    if client.get("courriel"):
        client_lines.append(f"Courriel : {escape(str(client['courriel']))}")

    inv_type = "Ouverture de dossier" if invitation.get("type") == "intake" else "Transmission de documents"
    inv_lines = [
        f"Désignation : {escape(str(invitation.get('display_label') or '—'))}",
        f"Type : {escape(inv_type)}",
        f"Statut : {escape(str(invitation.get('statut') or '—'))}",
        f"Réf. invitation : {escape(str(invitation.get('id') or '—'))}",
    ]
    if dossier:
        if dossier.get("file_number"):
            inv_lines.append(f"Dossier cabinet : {escape(str(dossier['file_number']))}")
        if dossier.get("court_file_number"):
            inv_lines.append(f"Dossier cour : {escape(str(dossier['court_file_number']))}")
        if dossier.get("title"):
            inv_lines.append(f"Intitulé : {escape(str(dossier['title']))}")

    meta_table = Table(
        [
            [Paragraph("EXPÉDITEUR / CLIENT", s_cell_bold), Paragraph("INVITATION ET DOSSIER", s_cell_bold)],
            [Paragraph("<br/>".join(client_lines), s_cell), Paragraph("<br/>".join(inv_lines), s_cell)],
        ],
        colWidths=[usable_width * 0.48, usable_width * 0.52],
    )
    meta_table.setStyle(
        TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTNAME", (0, 1), (-1, -1), FONT),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
            ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#D1D5DB")),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ])
    )
    elements.append(meta_table)
    elements.append(Spacer(1, 6))

    # ── Lots ─────────────────────────────────────────────────────────
    if not lots:
        elements.append(Paragraph("Aucun lot transmis pour cette invitation.", s_cell))
    for lot in lots:
        batch = str(lot.get("batch") or "—")
        soumis_le = _format_iso_mtl(lot.get("submitted_at") or lot.get("soumis_le"))
        http_info = lot.get("http") or {}
        ip = str(http_info.get("ip") or "").strip()
        header_txt = f"Lot {escape(batch)} · Transmis le {escape(soumis_le)}"
        if ip:
            header_txt += f" · IP {escape(ip)}"
        elements.append(Paragraph(header_txt, s_section))

        if invitation.get("type") == "intake":
            consent = lot.get("consentement") or {}
            consent_date = _format_iso_mtl(consent.get("horodatage")) if consent.get("horodatage") else "—"
            rows = [
                [Paragraph("Élément", s_cell_bold), Paragraph("Détail consigné", s_cell_bold)],
                [Paragraph("État du traitement", s_cell), Paragraph(escape(str(lot.get("statut_affiche") or invitation.get("statut") or "soumise")), s_cell)],
                [Paragraph("Horodatage de transmission", s_cell), Paragraph(escape(soumis_le), s_cell)],
                [Paragraph("Consentement", s_cell), Paragraph(escape(f"Version {consent.get('version_texte') or '1'} · {consent_date}"), s_cell)],
            ]
            for ligne in lot.get("lignes") or []:
                label = str(ligne.get("libelle") or "")
                val = str(ligne.get("soumis_affiche") or "—")
                if label:
                    rows.append([Paragraph(escape(label), s_cell), Paragraph(escape(val), s_cell)])
            for adv in lot.get("adverses") or []:
                desc = str(adv.get("nom") or "")
                if adv.get("precision"):
                    desc += f" — {adv['precision']}"
                rows.append([Paragraph("Partie adverse déclarée", s_cell), Paragraph(escape(desc), s_cell)])

            intake_tbl = Table(rows, colWidths=[usable_width * 0.35, usable_width * 0.65], repeatRows=1)
            intake_tbl.setStyle(
                TableStyle([
                    ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                    ("FONTNAME", (0, 1), (-1, -1), FONT),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E5E7EB")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ])
            )
            elements.append(intake_tbl)
            continue

        fichiers = lot.get("fichiers") or []
        if not fichiers:
            elements.append(Paragraph("Aucun fichier consigné dans ce lot.", s_cell))
            continue

        tbl_rows = [[
            Paragraph("Fichier", s_cell_bold),
            Paragraph("Taille", s_cell_bold),
            Paragraph("Empreinte SHA-512", s_cell_bold),
            Paragraph("Statut", s_cell_bold),
        ]]
        for f in fichiers:
            fname = escape(str(f.get("name") or "—"))
            verse_nom = (f.get("verse_nom") or "").strip()
            if verse_nom and verse_nom != f.get("name"):
                fname += f"<br/><font size='7' color='#4B5563'>Classé sous : {escape(verse_nom)}</font>"
            size_txt = escape(_format_size(int(f.get("size_gcs") or 0)))
            sha_txt = escape((f.get("sha512") or "—").upper())
            status_txt = escape(_file_status_label(f))
            tbl_rows.append([
                Paragraph(fname, s_cell),
                Paragraph(size_txt, s_cell),
                Paragraph(sha_txt, s_hash),
                Paragraph(status_txt, s_cell),
            ])

        file_tbl = Table(
            tbl_rows,
            colWidths=[
                usable_width * 0.28,
                usable_width * 0.16,
                usable_width * 0.38,
                usable_width * 0.18,
            ],
            repeatRows=1,
        )
        file_tbl.setStyle(
            TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("FONTNAME", (0, 1), (-1, -1), FONT),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ])
        )
        elements.append(file_tbl)

    # ── Avis de confidentialité ──────────────────────────────────────
    elements.append(
        Paragraph(
            "<b>AVIS DE CONFIDENTIALITÉ :</b> Le présent bordereau atteste la réception "
            "technique via le portail sécurisé et les empreintes ou décisions consignées au "
            "dossier. Document destiné à l'usage exclusif du cabinet et de son client.",
            s_notice,
        )
    )

    def _draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        footer_title = (
            firm_title[:62] + "…" if len(firm_title) > 65 else firm_title
        )
        canvas.drawString(15 * mm, 11 * mm, f"{footer_title} — Accusé de réception")
        canvas.drawRightString(LETTER[0] - 15 * mm, 11 * mm, f"Page {doc_obj.page}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    out = buffer.getvalue()
    buffer.close()
    return out
