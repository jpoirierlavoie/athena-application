"""Every banner that prints request text wraps a long unbroken token.

``?erreur=`` and ``?message=`` are this codebase's banner convention: a route
reads the text from the query — sanitized or not, always autoescaped — and
the template prints it: ``{{ erreur }}``, ``{{ message }}``, or, on the two
trust pages, ``{{ request.args.get('erreur')[:500] }}``. The text is the
client's: a hand-made link holding one long unbroken token (300 characters,
no space) scrolled the phone sideways at 375 px until 2026-11-08 (CLAUDE.md
Rule 9). Each such element carries ``wrap-anywhere`` — ``overflow-wrap:
anywhere``, which, unlike ``break-words`` (``overflow-wrap: break-word``),
also lowers the element's min-content width, so the token breaks inside a
flex row as well as in a block.

The sweep reads EVERY template of both services, and every name a printed
expression reads — bare, filtered (``erreur|trim``), with a fallback
(``message or "…"``), sliced, concatenated or under Jinja's whitespace
control (``{{- erreur }}``) — so a new banner on these names that forgets
the class fails here; on the old code, every one did. Some elements print
the same names with a sentence the server wrote, never request text; they
are named below with the reason, and the list holds only live entries.
``tests/test_post_refusals.py`` renders the pages it can reach through their
real routes; ``tests/test_compiled_classes.py`` proves the class compiles.
Linear ``str.find`` loops and one-pass scans, no regex (CWE-1333).
"""

import pathlib

ATHENA = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_ROOTS = (ATHENA / "templates", ATHENA / "client" / "templates")
UTILITY = "wrap-anywhere"

# (template, printed expression) → why it is not request text. Live entries
# only: test_the_fixed_sentence_list_holds_only_live_entries.
FIXED_SENTENCES = {
    ("templates/settings/affichage.html", "erreur"): (
        "the refusal routes/settings._affichage_page writes itself"),
    ("templates/parties/_mandataire_search_results.html", "message"): (
        "the search route's own sentence (« Sélectionnez d'abord un rôle. »)"),
    ("templates/components/empty_state.html",
     "message|default('Aucun élément.')"): (
        "its one includer, parties/_partie_rows.html, renders under "
        "parties.partie_list, which passes no message: the default prints"),
    ("client/templates/erreur.html", 'message or "Réessayez plus tard."'): (
        "client/app.py's error handlers and client/routes.py pass it fixed "
        "French sentences"),
}

_BANNER_NAMES = ("erreur", "message")


def _printed(text: str):
    """Yield (offset, expression) for every ``{{ … }}`` of *text*."""
    i = 0
    while True:
        i = text.find("{{", i)
        if i < 0:
            return
        j = text.find("}}", i + 2)
        if j < 0:
            return
        yield i, text[i + 2:j].strip()
        i = j + 2


def _names(expression: str) -> list:
    """The dotted names *expression* reads, string literals skipped, in one
    pass: ``erreur|trim``, ``message or "…"``, ``erreur[:500]``,
    ``"x" ~ erreur`` and ``- erreur -`` all read ``erreur``."""
    names, current, quote, escaped = [], "", "", False
    for ch in expression:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch.isalnum() or ch in "_.":
            current += ch
            continue
        elif ch in "'\"":
            quote = ch
        if current:
            names.append(current)
            current = ""
    if current:
        names.append(current)
    return names


def _is_request_text(expression: str) -> bool:
    """Whether the expression reads a banner name or the query, in ANY form.
    Comparing the whole expression would let a filter, a fallback or
    whitespace control slip past the sweep unchecked."""
    return any(name.split(".", 1)[0] in _BANNER_NAMES
               or name.startswith("request.args")
               for name in _names(expression))


def _sites(roots=TEMPLATE_ROOTS, base=ATHENA) -> list:
    """(template, expression, the opening tag of the element printing it)."""
    sites = []
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(base).as_posix()
            for offset, expression in _printed(text):
                if not _is_request_text(expression):
                    continue
                lt, gt = text.rfind("<", 0, offset), text.rfind(">", 0, offset)
                if lt > gt:
                    continue  # inside a tag: an attribute value, not text
                sites.append((rel, expression, text[lt:gt + 1]))
    return sites


def _classes(tag: str) -> list:
    if 'class="' not in tag:
        return []
    return tag.split('class="', 1)[1].split('"', 1)[0].split()


def test_the_sweep_finds_the_banners():
    """Not vacuous: the 2026-11-08 count, as a floor."""
    sites = [s for s in _sites() if (s[0], s[1]) not in FIXED_SENTENCES]
    assert len(sites) >= 25, len(sites)
    assert {s[0] for s in sites} >= {
        "templates/tasks/_task_rows.html", "templates/tasks/detail.html",
        "templates/time_expenses/list.html", "templates/invoices/list.html",
        "templates/invoices/detail.html", "templates/trust/detail.html",
        "templates/reception/index.html"}


def _unwrapped(sites: list) -> list:
    unwrapped = []
    for rel, expression, tag in sites:
        if (rel, expression) in FIXED_SENTENCES:
            continue
        # The text sits directly in its element: a closing tag here would
        # mean the sweep read the wrong element — say so, never pass.
        assert not tag.startswith("</"), (rel, expression, tag)
        if UTILITY not in _classes(tag):
            unwrapped.append((rel, expression, tag[:90]))
    return unwrapped


def test_every_element_printing_request_text_wraps_anywhere():
    unwrapped = _unwrapped(_sites())  # reads the templates: not in the assert
    assert unwrapped == []


# Each prints request text without the class; comparing the whole
# expression would catch only the first and the last (the bare name and
# `request.args`).
_PLANTED_FORMS = (
    "{{ erreur }}", "{{ erreur|trim }}", "{{- erreur }}", "{{ erreur -}}",
    '{{ message or "" }}', "{{ erreur[:500] }}", '{{ "Erreur : " ~ erreur }}',
    "{{ (message) }}", "{{ request.args.get('message', '') }}",
)


def test_the_sweep_catches_every_form_of_a_planted_unwrapped_banner(
        tmp_path):
    """The guard proves it catches something: one planted template per form,
    each a banner without the class, and each must be flagged. A string that
    merely spells a banner name prints no request text and is not."""
    root = tmp_path / "templates"
    root.mkdir()
    for i, form in enumerate(_PLANTED_FORMS):
        (root / ("banner%d.html" % i)).write_text(
            '<div role="alert" class="mb-4 text-red-700">%s</div>\n' % form,
            encoding="utf-8")
    (root / "fixed.html").write_text(
        "<p class=\"text-sm\">{{ 'erreur' }} {{ messages }} "
        "{{ erreur_code }}</p>\n", encoding="utf-8")
    flagged = {rel for rel, _expression, _tag in _unwrapped(
        _sites(roots=(root,), base=tmp_path))}
    assert flagged == {"templates/banner%d.html" % i
                       for i in range(len(_PLANTED_FORMS))}


def test_the_fixed_sentence_list_holds_only_live_entries():
    live = {(rel, expression) for rel, expression, _tag in _sites()}
    assert set(FIXED_SENTENCES) <= live, set(FIXED_SENTENCES) - live
