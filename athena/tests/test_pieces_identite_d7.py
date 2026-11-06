"""D7 — Claude has no access to the proofs of identity.

The connector reads what its tool handlers (``mcp/``), the services they
write through (``services/``) and the DAV layer they share (``dav/``) can
reach, and what runs beside them: the helpers they import (``utils/``)
and the first-party modules at the root of ``athena/`` (``config.py``,
``security.py``…, and ``main.py``, which hosts them). None of those
modules may import ``models/piece_identite`` nor spell its
subcollection's name. Among the models, the one the connector reaches
that knows the proofs, ``models/partie.py``, asks only « does this
contact hold one? », and only inside ``delete_partie``, which no
connector module may call (the disclosure's « delete » never); no other
model reaches the proofs model at all. The web routes read the proofs
(``routes/parties.py``) and are not swept, so no module of the connector
packages or the helpers may import them — nor may the models or any root
module but ``main.py``, which registers the blueprints.

Every sweep is proved non-vacuous: it walks the real tree (and finds what
it should find there), and a planted source in each form it claims to see
is caught, while look-alikes are not.

The sweeps read the source, not what runs, and each sees exactly the
forms its docstring lists: import statements, and the few dynamic
reaches it names, whose module name is written as a constant
(``importlib.import_module``, ``__import__``, ``sys.modules[…]`` and
``.get(…)``). Every other form stays out of their reach: another loader
(``pkgutil.resolve_name``, ``runpy``), another ``sys.modules`` method or
an alias of it, ``exec`` of an import string, a name computed at run
time, and attribute walks such as a function's ``__globals__``. They
catch honest mistakes, not deliberate evasion.

The deletion trail's own rows are hidden elsewhere: ``list_deletions``
(``tests/test_mcp_output_schemas.py``) and its enum
(``tests/test_audit_events.py``).
"""

import ast
import inspect
import os
import pathlib
import re
import sys
from unittest import mock

import pytest

ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers  # noqa: F401 — the handlers get_handler resolves
    from mcp import disclosure, tools
    from models import piece_identite as pi

# The swept boundary, and what it must never name — derived from the model.
CONNECTOR_PACKAGES = ("mcp", "services", "dav")
# Swept beside them by the same two scans (sections 1 and 2:
# _swept_sources): the helpers they import, at any depth, and the root's
# own modules (config.py, security.py…, and main.py, which hosts them).
# models/ has its own, finer sweep (section 3); routes/ is not walked, but
# no connector package or helper may import it (section 4); tests/,
# scripts/, client/ and the static and template trees are not walked.
HELPER_PACKAGES = ("utils",)
MODULE = pi.__name__                      # "models.piece_identite"
PACKAGE, _, LEAF = MODULE.rpartition(".")  # "models", "piece_identite"
SUBCOLLECTION = pi.SUBCOLLECTION          # "pieces_identite"
OWNER = MODULE.replace(".", "/") + ".py"  # "models/piece_identite.py"
ASK = pi.has_proofs_strict.__name__       # "has_proofs_strict": the question


def test_the_names_the_sweeps_look_for_are_the_model_s():
    assert (MODULE, PACKAGE, LEAF) == (
        "models.piece_identite", "models", "piece_identite")
    assert SUBCOLLECTION == "pieces_identite"
    assert (OWNER, ASK) == ("models/piece_identite.py", "has_proofs_strict")


def _read(root: pathlib.Path, paths) -> dict[str, str]:
    """``{"mcp/x.py": source}`` of *paths*, named relative to *root*."""
    return {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(paths)}


def _package_paths(root: pathlib.Path) -> list[pathlib.Path]:
    """Every module of the connector packages and of the helpers under
    *root*, at any depth (section 4 reads these alone)."""
    return [path for package in CONNECTOR_PACKAGES + HELPER_PACKAGES
            for path in (root / package).rglob("*.py")]


def _swept_sources(root: pathlib.Path) -> dict[str, str]:
    """What sections 1 and 2 read under *root*: every module of the
    connector packages and of the helpers, at any depth, and the root's
    own modules (``main.py``, ``config.py``… — discovered, never listed)."""
    return _read(root, [*root.glob("*.py"), *_package_paths(root)])


# ══════════════════════════════════════════════════════════════════════
# 1. No connector-side module imports the proofs model
# ══════════════════════════════════════════════════════════════════════


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _strings_in(nodes) -> list[str]:
    return [sub.value for node in nodes for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]


def _sys_modules_keys(node: ast.AST) -> list[str]:
    """The string constants *node* looks a module up by in ``sys.modules``:
    ``sys.modules[…]``, ``sys.modules.get(…)``, or ``modules[…]`` after
    ``from sys import modules`` — ``[]`` for any other node."""
    if isinstance(node, ast.Subscript):
        owner, keys = node.value, [node.slice]
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
          and node.func.attr == "get"):
        owner, keys = node.func.value, node.args[:1]
    else:
        return []
    if getattr(owner, "attr", getattr(owner, "id", None)) != "modules":
        return []
    return _strings_in(keys)


def _sys_modules_reach(node: ast.AST) -> bool:
    """*node* looks the proofs model up in ``sys.modules`` — loaded there
    under main:app, since main.py imports routes/parties.py, which
    imports it."""
    return any(LEAF in key for key in _sys_modules_keys(node))


def proof_model_imports(source: str) -> list[int]:
    """The lines of *source* that reach the proofs model as a module:

    * ``import models.piece_identite`` (aliased or not, or a submodule);
    * ``from models.piece_identite import X``;
    * ``from models import piece_identite`` (aliased, or among others), and
      a relative import naming it;
    * ``importlib.import_module(…)`` / ``__import__(…)`` with a constant
      naming it (``fromlist`` and a relative name with its package
      included);
    * the package's attribute (``models.piece_identite.X``),
      ``getattr(models, "piece_identite")``, or a ``sys.modules`` lookup
      (``sys.modules[…]``, ``sys.modules.get(…)``, ``modules[…]`` after
      ``from sys import modules``) — a module imported elsewhere is
      reachable that way without an import here.

    String constants elsewhere (prose, docstrings) are not imports."""
    return [node.lineno for node in _proof_model_reaches(ast.parse(source))]


def _proof_model_reaches(tree: ast.AST) -> list[ast.AST]:
    """The nodes of *tree* reaching the proofs model as a module, in the
    forms :func:`proof_model_imports` lists."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if _sys_modules_reach(node):
            found.append(node)
            continue
        if isinstance(node, ast.Import):
            if any(alias.name == MODULE or alias.name.startswith(MODULE + ".")
                   for alias in node.names):
                found.append(node)
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            parts = (node.module or "").split(".") if node.module else []
            if node.level == 0:
                full = node.module or ""
                if (full == MODULE or full.startswith(MODULE + ".")
                        or (full == PACKAGE and LEAF in names)):
                    found.append(node)
            elif LEAF in parts or LEAF in names:
                found.append(node)
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            arguments = [*node.args, *(k.value for k in node.keywords)]
            if name in ("import_module", "__import__") and any(
                    LEAF in text for text in _strings_in(arguments)):
                found.append(node)
            elif name in ("getattr", "hasattr") and LEAF in _strings_in(
                    node.args[1:2]):
                found.append(node)
        elif isinstance(node, ast.Attribute) and node.attr == LEAF:
            found.append(node)
    return found


def test_no_connector_side_module_imports_the_proofs_model():
    sources = _swept_sources(ATHENA)
    # Non-vacuous: the three packages are walked, the registry among them,
    # and so are the helpers the connector imports (utils.kyc; utils.cabinet,
    # whose cabinet_dict imports a model lazily) and the root modules.
    assert {"mcp/handlers.py", "mcp/tools.py", "dav/carddav.py",
            "services/comptabilite.py", "utils/kyc.py", "utils/cabinet.py",
            "main.py", "config.py", "security.py"} <= set(sources)
    offenders = {rel: lines for rel, source in sources.items()
                 if (lines := proof_model_imports(source))}
    assert offenders == {}


def test_the_sweeps_walk_the_helpers_and_the_root_modules(tmp_path):
    """Both scans (this section's and the next) read the connector
    packages, the helpers at any depth and the root's own modules — and
    nothing else: a reach planted in any of them is seen, while the trees
    left out are never read."""
    plant = 'from models import piece_identite\nNAME = "pieces_identite"\n'
    walked = {"mcp/handlers.py", "services/comptabilite.py", "dav/carddav.py",
              "utils/kyc.py", "utils/sous/module.py", "main.py", "config.py"}
    left_out = {"models/dossier.py", "routes/parties.py", "tests/test_x.py",
                "scripts/x.py", "client/main.py", "static/x.py",
                "templates/x.py", "utils/notes.txt"}
    for rel in walked | left_out:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plant, encoding="utf-8")
    sources = _swept_sources(tmp_path)
    assert set(sources) == walked
    assert all(proof_model_imports(source) and subcollection_mentions(source)
               for source in sources.values())


def test_the_import_sweep_sees_the_real_importer():
    """Non-vacuous on the real tree: delete_partie's local import is the
    form the sweep must see — outside the swept packages."""
    source = (ATHENA / "models" / "partie.py").read_text(encoding="utf-8")
    assert proof_model_imports(source)


@pytest.mark.parametrize("snippet", [
    # The four forms the plan names…
    "from models import piece_identite\n",
    "import models.piece_identite\n",
    "from models.piece_identite import list_proofs\n",
    'importlib.import_module("models.piece_identite")\n',
    # …and their variants.
    "from models import piece_identite as pi\n",
    "from models import (partie, piece_identite)\n",
    "import models.piece_identite as pi\n",
    "from models.piece_identite import SUBCOLLECTION as S\n",
    "def f():\n    from models import piece_identite\n    return piece_identite\n",
    "from importlib import import_module\nm = import_module('models.piece_identite')\n",
    'importlib.import_module(".piece_identite", package="models")\n',
    '__import__("models.piece_identite")\n',
    '__import__("models", fromlist=["piece_identite"])\n',
    "import models\nrows = models.piece_identite.list_proofs(pid)\n",
    'import models\nmodule = getattr(models, "piece_identite")\n',
    "from .models import piece_identite\n",
    # Loaded under main:app, the model is reachable through sys.modules.
    "import sys\nrows = sys.modules['models.piece_identite'].list_proofs_strict(p)\n",
    "import sys\nm = sys.modules.get('models.piece_identite')\n",
    "from sys import modules\nm = modules['models.piece_identite']\n",
])
def test_the_import_sweep_catches_every_form(snippet):
    assert proof_model_imports(snippet), snippet


@pytest.mark.parametrize("snippet", [
    "from models import partie\n",
    "import models.partie\n",
    "from models import piece_justificative\n",
    '"""Proofs live in models.piece_identite — never read here."""\n',
    "# from models import piece_identite\n",
    'NOTE = "importlib.import_module(\'models.piece_identite\')"\n',
    'x = CACHE["models.piece_identite"]\n',
    "import sys\nm = sys.modules['models.partie']\n",
])
def test_the_import_sweep_ignores_look_alikes_and_prose(snippet):
    assert proof_model_imports(snippet) == [], snippet


# ══════════════════════════════════════════════════════════════════════
# 2. No registered tool's handler reaches the subcollection by name
# ══════════════════════════════════════════════════════════════════════


def subcollection_mentions(source: str) -> list[int]:
    """The lines of *source* that spell the subcollection's name: the raw
    text (code, comment or docstring alike — the connector side has no
    business naming it), plus a string constant holding it once the parser
    has joined adjacent literals (« "pieces_" "identite" »)."""
    lines = [number for number, line in enumerate(source.splitlines(), 1)
             if SUBCOLLECTION in line]
    lines += [node.lineno for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.Constant) and isinstance(node.value, str)
              and SUBCOLLECTION in node.value]
    return sorted(set(lines))


def test_every_registered_handler_lives_in_a_swept_package():
    """The scan below covers a handler's body only if the handler is
    defined there: every tool the registry dispatches resolves into the
    connector packages (today all of them into mcp/handlers.py)."""
    roots = [(ATHENA / package).resolve() for package in CONNECTOR_PACKAGES]
    assert tools.TOOLS, "the registry is empty"
    for name in tools.TOOLS:
        handler = inspect.unwrap(tools.get_handler(name))
        path = pathlib.Path(inspect.getsourcefile(handler)).resolve()
        assert any(root in path.parents for root in roots), (name, path)


def test_no_connector_side_module_names_the_subcollection():
    sources = _swept_sources(ATHENA)
    # Non-vacuous: the handlers, the helper that documents the Storage
    # prefixes, and the root modules are read.
    assert {"mcp/handlers.py", "utils/storage_identity.py",
            "main.py"} <= set(sources)
    offenders = {rel: lines for rel, source in sources.items()
                 if (lines := subcollection_mentions(source))}
    assert offenders == {}


def test_the_subcollection_scan_sees_the_owner():
    """Non-vacuous on the real tree: the model that owns the store names
    it — outside the swept packages."""
    source = (ATHENA / "models" / "piece_identite.py").read_text(
        encoding="utf-8")
    assert subcollection_mentions(source)


@pytest.mark.parametrize("snippet", [
    'ref = db.collection("parties").document(p).collection("pieces_identite")\n',
    'rows = db.collection_group("pieces_identite").stream()\n',
    'path = f"parties/{pid}/pieces_identite/{proof_id}"\n',
    'NAME = ("pieces_"\n        "identite")\n',
    "# the pieces_identite subcollection\n",
])
def test_the_subcollection_scan_catches_every_spelling(snippet):
    assert subcollection_mentions(snippet), snippet


@pytest.mark.parametrize("snippet", [
    'ref.collection("analyses")\n',
    'kyc = partie.get("kyc_document_ids", [])\n',
    'label = "Preuves d\'identité"\n',
])
def test_the_subcollection_scan_ignores_other_names(snippet):
    assert subcollection_mentions(snippet) == [], snippet


# ══════════════════════════════════════════════════════════════════════
# 3. The one connector-reachable model that knows the proofs asks yes/no
# ══════════════════════════════════════════════════════════════════════


def proof_model_uses(source: str) -> dict[str, set[str]]:
    """``{top-level function (or "<module>"): {names reached on the proofs
    model}}`` — through an alias of ``from models import piece_identite``
    / ``import models.piece_identite as X``, or the names of
    ``from models.piece_identite import …``."""
    tree = ast.parse(source)
    aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == PACKAGE:
                aliases.update(a.asname or a.name for a in node.names
                               if a.name == LEAF)
            elif node.module == MODULE:
                direct.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            aliases.update(a.asname for a in node.names
                           if a.name == MODULE and a.asname)

    def reached(scope: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(scope):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in aliases):
                names.add(node.attr)
            elif isinstance(node, ast.Name) and node.id in direct:
                names.add(node.id)
        return names

    uses: dict[str, set[str]] = {}
    for statement in tree.body:
        scope = (statement.name if isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else "<module>")
        if found := reached(statement):
            uses.setdefault(scope, set()).update(found)
    return uses


ASKER = "models/partie.py"        # the one model that may ask ASK…
ASKER_FUNCTION = "delete_partie"  # …and only there
# A host for planted reaches: a model the connector reaches (mcp/handlers
# imports it).
REACHED_MODEL = "models/dossier.py"


def _model_sources(root: pathlib.Path) -> dict[str, str]:
    """``{"models/x.py": source}`` of every model under *root*."""
    return _read(root, (root / "models").rglob("*.py"))


def _top_level_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name == name]
    assert len(found) == 1, f"{len(found)} top-level {name}(), one expected"
    return found[0]


def _ask_offences(source: str) -> list[int]:
    """The lines inside *source*'s delete_partie that reach the proofs
    model other than to ask ASK — forms the use sweep does not follow, so
    it would still report ASK alone: any import but ``from models import
    piece_identite [as X]`` or ``from models.piece_identite import
    has_proofs_strict [as Y]`` (importlib, an unaliased or relative
    import, the package's attribute, another of the model's names — even
    one imported under ASK's), and ``X`` read as anything but
    ``X.has_proofs_strict``."""
    function = _top_level_function(ast.parse(source), ASKER_FUNCTION)
    allowed: set[int] = set()  # id() of the imports that ask ASK only
    aliases: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module == PACKAGE and LEAF in {a.name for a in node.names}:
            allowed.add(id(node))
            aliases.update(a.asname or a.name for a in node.names
                           if a.name == LEAF)
        elif node.module == MODULE and all(a.name == ASK for a in node.names):
            allowed.add(id(node))
    asking = {id(node.value) for node in ast.walk(function)
              if isinstance(node, ast.Attribute) and node.attr == ASK}
    lines = [node.lineno for node in _proof_model_reaches(function)
             if id(node) not in allowed]
    lines += [node.lineno for node in ast.walk(function)
              if isinstance(node, ast.Name) and node.id in aliases
              and isinstance(node.ctx, ast.Load) and id(node) not in asking]
    return lines


def proofs_reach_offences(sources: dict[str, str]) -> dict[str, list[int]]:
    """``{model: lines}`` of *sources* (``{"models/x.py": source}``) that
    reach the proofs model beyond the one question allowed. In a model
    other than the owner and partie.py: every line proof_model_imports
    flags — any form, not only the aliases the use sweep follows. In
    partie.py: every one outside delete_partie (from its ``def`` line to
    its body's last), and inside it, what :func:`_ask_offences` finds."""
    offences: dict[str, list[int]] = {}
    for rel, source in sorted(sources.items()):
        if rel == OWNER:
            continue
        lines = proof_model_imports(source)
        if rel == ASKER:
            function = _top_level_function(ast.parse(source), ASKER_FUNCTION)
            lines = [line for line in lines
                     if not function.lineno <= line <= function.end_lineno]
            lines += _ask_offences(source)
        if lines:
            offences[rel] = sorted(set(lines))
    return offences


def test_partie_reaches_only_the_yes_no_check_and_only_to_delete():
    """models/partie.py is reached by the connector (mcp/handlers imports
    it), so whatever it reads of the proofs, Claude could. It may ask one
    thing — whether a contact holds a proof — and only in delete_partie;
    no other model reaches the proofs model at all, in any form the
    import sweep lists."""
    sources = _model_sources(ATHENA)
    assert {OWNER, ASKER, REACHED_MODEL} <= set(sources)
    assert proofs_reach_offences(sources) == {}
    # On the aliases it follows, the use sweep sees that question only.
    users = {}
    for rel, source in sources.items():
        if rel == OWNER:
            continue
        if uses := proof_model_uses(source):
            users[rel] = uses
    assert users == {"models/partie.py": {"delete_partie": {"has_proofs_strict"}}}


def _plant(source: str, snippet: str, placement: str) -> tuple[str, int]:
    """*source* with *snippet* inserted at its ``"end"``, or ``"before"``,
    ``"after"`` or ``"inside"`` its top-level delete_partie (inside: as
    the last statements of its body) — and the offset of the snippet's
    line numbers there."""
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    indent = ""
    if placement == "end":
        index = len(lines)
    else:
        function = _top_level_function(ast.parse(source), ASKER_FUNCTION)
        if placement == "before":
            index = min([function.lineno] + [decorator.lineno for decorator
                                             in function.decorator_list]) - 1
        else:
            index = function.end_lineno
            if placement == "inside":
                indent = " " * function.body[-1].col_offset
    body = "".join(indent + line for line in snippet.splitlines(keepends=True))
    return "".join(lines[:index]) + body + "".join(lines[index:]), index


# Reaches the use sweep does not follow (proof_model_uses finds nothing
# in them), each one reading the list of a contact's proofs. (In partie.py
# the use sweep does see the unaliased relative import, but only because
# its name is delete_partie's own alias — hence the aliased one.)
UNFOLLOWED_FORMS = {
    "unaliased import": (
        "import models.piece_identite\n"
        "def planted(pid):\n"
        "    return models.piece_identite.list_proofs_strict(pid)\n"),
    "relative module import": (
        "def planted(pid):\n"
        "    from . import piece_identite\n"
        "    return piece_identite.list_proofs_strict(pid)\n"),
    "relative module import, aliased": (
        "def planted(pid):\n"
        "    from . import piece_identite as proofs\n"
        "    return proofs.list_proofs_strict(pid)\n"),
    "relative name import": (
        "def planted(pid):\n"
        "    from .piece_identite import list_proofs_strict\n"
        "    return list_proofs_strict(pid)\n"),
    "importlib": (
        "def planted(pid):\n"
        "    import importlib\n"
        '    module = importlib.import_module("models.piece_identite")\n'
        "    return module.list_proofs_strict(pid)\n"),
}


@pytest.mark.parametrize("host, placement", [
    (REACHED_MODEL, "end"),
    (ASKER, "before"),  # its last line right above delete_partie's def
    (ASKER, "after"),   # its first line right below the body's last
])
@pytest.mark.parametrize("form", sorted(UNFOLLOWED_FORMS))
def test_a_reach_outside_delete_partie_is_refused_in_any_form(
        form, host, placement):
    """The check judges each model on its own (the rest of the real tree
    is proved clean above): the planted host alone is judged here."""
    snippet = UNFOLLOWED_FORMS[form]
    source, offset = _plant((ATHENA / host).read_text(encoding="utf-8"),
                            snippet, placement)
    planted = sorted({offset + line for line in proof_model_imports(snippet)})
    assert planted
    assert proofs_reach_offences({host: source}) == {host: planted}


# Inside delete_partie, reaches the use sweep does not follow — it would
# still report has_proofs_strict alone — with the snippet's lines that
# must be refused.
UNFOLLOWED_INSIDE = {
    "importlib": (
        "import importlib\n"
        'rows = importlib.import_module("models.piece_identite")'
        ".list_proofs_strict(partie_id)\n",
        [2]),
    "relative import": (
        "from . import piece_identite as proofs\n"
        "rows = proofs.list_proofs_strict(partie_id)\n",
        [1]),
    "unaliased import": (
        "import models.piece_identite\n"
        "rows = models.piece_identite.list_proofs_strict(partie_id)\n",
        [1, 2]),
    "the module handed on": (
        "module = piece_identite\n"
        "rows = module.list_proofs_strict(partie_id)\n",
        [1]),
    "list_proofs_strict renamed has_proofs_strict": (
        "from models.piece_identite import (\n"
        "    list_proofs_strict as has_proofs_strict)\n"
        "rows = has_proofs_strict(partie_id)\n",
        [1]),
}


@pytest.mark.parametrize("form", sorted(UNFOLLOWED_INSIDE))
def test_inside_delete_partie_only_the_question_is_asked(form):
    snippet, refused = UNFOLLOWED_INSIDE[form]
    source, offset = _plant((ATHENA / ASKER).read_text(encoding="utf-8"),
                            snippet, "inside")
    assert proofs_reach_offences({ASKER: source}) == {
        ASKER: [offset + line for line in refused]}


def test_the_connector_may_not_call_delete_partie():
    """The reason the yes/no check is out of Claude's reach: the
    disclosure's « delete » never forbids every ``delete_*`` reference in
    connector code (tests/test_mcp_disclosure.py sweeps it)."""
    patterns = [pattern for never in disclosure.NEVERS
                for pattern in never.forbidden]
    assert any(re.fullmatch(pattern, "delete_partie") for pattern in patterns)


@pytest.mark.parametrize("snippet, expected", [
    ("from models import piece_identite\n"
     "def get_partie(pid):\n    return piece_identite.list_proofs(pid)\n",
     {"get_partie": {"list_proofs"}}),
    ("import models.piece_identite as pi\n"
     "def partie_to_vcard(p):\n    return pi.get_proof_strict(p, 'x')\n",
     {"partie_to_vcard": {"get_proof_strict"}}),
    ("from models.piece_identite import list_proofs_strict as lps\n"
     "ROWS = lps('p1')\n",
     {"<module>": {"lps"}}),
    ("def delete_partie(pid):\n"
     "    from models import piece_identite\n"
     "    def _delete(txn):\n"
     "        return piece_identite.has_proofs_strict(pid, transaction=txn)\n"
     "    return _delete(None)\n",
     {"delete_partie": {"has_proofs_strict"}}),
])
def test_the_use_sweep_catches_what_it_claims(snippet, expected):
    assert proof_model_uses(snippet) == expected


# ══════════════════════════════════════════════════════════════════════
# 4. No connector-side package imports the web routes
# ══════════════════════════════════════════════════════════════════════

ROUTES = "routes"  # the web's package: routes/parties.py reads the proofs


def _names_routes(name: str) -> bool:
    """*name* is the routes package or one of its modules, a relative
    spelling (``.routes``, ``..routes.parties``) included — not a module
    whose name only begins like it (``routes_legacy``)."""
    name = name.lstrip(".")
    return name == ROUTES or name.startswith(ROUTES + ".")


def routes_imports(source: str) -> list[int]:
    """The lines of *source* that reach the web routes as a module:

    * ``import routes…`` (aliased or not, or a submodule);
    * ``from routes… import X``, and a relative import naming them
      (``from ..routes import X``, ``from .. import routes``);
    * ``importlib.import_module(…)`` / ``__import__(…)`` with a constant
      naming them (``package="routes"`` and a relative name included);
    * a ``sys.modules`` lookup of them (``sys.modules[…]``,
      ``sys.modules.get(…)``, ``modules[…]`` after ``from sys import
      modules``) — main.py loads them under main:app.

    String constants elsewhere (prose, docstrings) are not imports."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = ([node.module] if node.module
                     else [alias.name for alias in node.names])
        elif (isinstance(node, ast.Call)
              and _call_name(node) in ("import_module", "__import__")):
            names = _strings_in([*node.args,
                                 *(k.value for k in node.keywords)])
        else:
            names = _sys_modules_keys(node)
        if any(_names_routes(name) for name in names):
            found.append(node.lineno)
    return found


def test_no_connector_side_package_imports_the_routes():
    """The sweeps above do not walk routes/, and routes/parties.py reads
    the proofs for the web: a connector-side module calling into the
    routes would reach the proofs past every sweep. So no module of the
    connector packages or the helpers imports the routes — the root
    modules are left out on purpose, since main.py must import the
    blueprints."""
    sources = _read(ATHENA, _package_paths(ATHENA))
    # Non-vacuous: the three packages and the helpers are walked…
    assert {"mcp/handlers.py", "mcp/tools.py", "dav/carddav.py",
            "services/comptabilite.py", "utils/kyc.py"} <= set(sources)
    # …and no root module is.
    assert [rel for rel in sources if "/" not in rel] == []
    offenders = {rel: lines for rel, source in sources.items()
                 if (lines := routes_imports(source))}
    assert offenders == {}


def test_no_model_nor_root_module_but_main_imports_the_routes():
    """The connector imports the models (mcp/handlers.py) and the root
    modules (mcp/oauth.py: security, auth, config), so a routes import
    there is the same conduit one hop later; only main.py registers the
    blueprints."""
    sources = _read(ATHENA, [*(ATHENA / "models").rglob("*.py"),
                             *ATHENA.glob("*.py")])
    assert {ASKER, REACHED_MODEL, "main.py", "security.py"} <= set(sources)
    offenders = {rel: lines for rel, source in sources.items()
                 if rel != "main.py" and (lines := routes_imports(source))}
    assert offenders == {}


def test_the_routes_sweep_sees_the_blueprints_main_registers():
    """Non-vacuous on the real tree: main.py's imports of the blueprints
    are the form the sweep must see — in a module it does not walk."""
    source = (ATHENA / "main.py").read_text(encoding="utf-8")
    assert routes_imports(source)


@pytest.mark.parametrize("snippet", [
    "from routes import parties\n",
    "import routes\n",
    "import routes.parties as rp\n",
    "from routes.parties import parties_bp\n",
    "def f():\n    from routes import parties\n    return parties\n",
    "from ..routes import parties\n",
    "from .. import routes\n",
    'importlib.import_module("routes.parties")\n',
    "from importlib import import_module\nm = import_module('routes')\n",
    'importlib.import_module(".parties", package="routes")\n',
    'importlib.import_module(".routes", package="mcp")\n',
    '__import__("routes.parties")\n',
    "import sys\nm = sys.modules['routes.parties']\n",
    "from sys import modules\nm = modules.get('routes.parties')\n",
])
def test_the_routes_sweep_catches_every_form(snippet):
    assert routes_imports(snippet), snippet


@pytest.mark.parametrize("snippet", [
    "import routes_legacy\n",
    'importlib.import_module("routes_legacy")\n',
    '"""Hoisted out of ``routes/invoices.py`` (routes.invoices)."""\n',
    "# from routes import parties\n",
    'NOTE = "from routes import parties"\n',
    "import sys\nm = sys.modules['models.partie']\n",
])
def test_the_routes_sweep_ignores_look_alikes_and_prose(snippet):
    assert routes_imports(snippet) == [], snippet
