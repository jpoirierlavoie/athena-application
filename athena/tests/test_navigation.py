"""The main menu — base.html's two hand-written lists (2026-11-10).

The desktop sidebar and the phone's « Plus » menu repeat the same entries
in two lists of literal links (``base.html`` calls ``url_for`` only for
static files). These tests read the template's source, as the menu's other
pins do (``tests/test_comptabilite_parity.py``,
``tests/test_settings_routes.py``), and hold the lawyer's decisions of
2026-11-10: the firm's money — « Honoraires » (the invoices page, still at
``/factures``), « Comptabilité », « Finances » — sits together, in that
order, set off from the rest in BOTH menus; and the invoices entry is
called « Honoraires », never « Factures ».
"""

import pathlib
import re

_TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "templates"
BASE = (_TEMPLATES / "base.html").read_text(encoding="utf-8")

#: The money entries, in their menu order.
MONEY = ["/factures", "/comptabilite", "/finances/"]

_ANCHOR = re.compile(r'<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _nav(marker: str) -> str:
    start = BASE.index(marker)
    return BASE[start:BASE.index("</nav>", start)]


DESKTOP = _nav('<nav class="hidden md:flex')
PHONE = _nav('<nav class="md:hidden')
MENUS = {"desktop": DESKTOP, "phone": PHONE}


def _label(body: str) -> str:
    """The text a link shows: no Jinja block (the Réception badge), no
    expression (the icon), no tag."""
    body = re.sub(r"\{%\s*if.*?\{%\s*endif\s*%\}", "", body, flags=re.S)
    body = re.sub(r"\{\{.*?\}\}", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return " ".join(body.split())


def _entries(nav: str) -> list:
    return [(href, _label(body)) for href, body in _ANCHOR.findall(nav)]


def test_the_money_entries_sit_together_in_both_menus():
    for name, nav in MENUS.items():
        hrefs = [href for href, _ in _entries(nav)]
        assert hrefs.count("/factures") == 1, name
        start = hrefs.index("/factures")
        assert hrefs[start:start + len(MONEY)] == MONEY, (name, hrefs)


def test_the_invoices_entry_is_called_honoraires():
    for name, nav in MENUS.items():
        entries = _entries(nav)
        assert dict(entries)["/factures"] == "Honoraires", name
        assert "Factures" not in [label for _, label in entries], name


def test_the_group_is_set_off_from_the_rest():
    # The sidebar: a block of its own, ruled like the overflow block.
    block = '<div class="mt-6 pt-6 border-t border-gray-200 flex flex-col gap-1">'
    first = DESKTOP.index('href="/factures"')
    start = DESKTOP.rindex(block, 0, first)
    group = DESKTOP[start:DESKTOP.index("</div>", first)]
    assert [href for href, _ in _entries(group)] == MONEY
    # The phone: between two rules.
    first = PHONE.index('href="/factures"')
    start = PHONE.rindex("<hr", 0, first)
    group = PHONE[start:PHONE.index("<hr", first)]
    assert [href for href, _ in _entries(group)] == MONEY


def test_the_invoice_pages_back_link_names_the_list_honoraires():
    """A back link names the list it returns to — « Documents »,
    « Calendrier », « Tâches » — so the invoice pages' says « Honoraires »."""
    for page in ("create.html", "detail.html"):
        src = (_TEMPLATES / "invoices" / page).read_text(encoding="utf-8")
        assert "'Retour' if return_to else 'Honoraires'" in src, page
        assert "'Factures'" not in src, page


# ── The Bibliothèque (2026-11-11) ──────────────────────────────────────
#: « Bibliothèque » replaces « Documents » and « Gabarits », where
#: « Documents » stood, and opens its first tab (the lawyer's order:
#: Gabarits · Modèles · Documents — tests/test_bibliotheque.py).
LIBRARY = ("/gabarits/", "Bibliothèque")


def test_the_library_entry_replaces_documents_and_gabarits():
    for name, nav in MENUS.items():
        entries = _entries(nav)
        assert entries.count(LIBRARY) == 1, name
        hrefs = [href for href, _ in entries]
        labels = [label for _, label in entries]
        for gone in ("/documents", "/documents/", "/gabarits", "/modeles/"):
            assert gone not in hrefs, (name, gone)
        assert "Documents" not in labels and "Gabarits" not in labels, name
        at = hrefs.index("/gabarits/")
        assert hrefs[at - 1:at + 2] == ["/taches", "/gabarits/", "/reception"], (
            name, hrefs)


def test_the_library_entry_draws_its_own_icon():
    for name, nav in MENUS.items():
        body = dict(_ANCHOR.findall(nav))["/gabarits/"]
        assert "ms('local_library', 20" in body, name
