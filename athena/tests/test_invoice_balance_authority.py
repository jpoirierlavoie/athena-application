"""I1 — ``models.invoice.balance_of`` is the only balance (plan, lot 0).

An invoice's live balance is ``amount_due − amount_paid``, and the write-off
lot makes it ``− written_off`` as well. A copy of that subtraction anywhere
else is a second authority that such a change leaves behind in silence: a
payment cap that would let a written-off invoice be paid again, a picker
that would still offer it. Two copies stood until lot 0, the administration
encaissement's cap (``models/admin_ledger._stage_create``,
« encaissement_excède_solde ») and the trust fee payment's
(``models/trust._stage_create``, « virement_excède_facture »); both now ask
``balance_of``, and section 2 proves they obey whatever it answers.

The sweep (section 1) reads every Python module of athena/ but its tests —
never a virtualenv or a vendored tree kept inside it — and every template of
both services. It follows the SIGN with which each stored field enters a
value, and refuses a value that takes one from the other — ``due − paid``,
or ``paid − due``, the balance negated — whatever the spelling:

* the field read as an attribute (``inv.amount_due``), a key
  (``inv["amount_due"]``), a call that names it (``inv.get("amount_due",
  0)``, ``getattr(inv, "amount_due")``, ``_cents(inv, "amount_due")``) or a
  bare name ``amount_due``;
* wrapped in any call (``int``, ``float``, ``Decimal``, ``sum`` over rows, a
  method of the read), a default (``or 0``), a conditional, a walrus or an
  ``await`` — in a template, a filter (``|int``) too;
* bound first to a name, directly or through other names, a tuple included,
  in the same function, an enclosing one or the module, or accumulated
  (``due += …`` in a loop, ``balance -= paid``); in a template, by
  ``{% set %}`` (its block form too), ``{% with %}`` or a namespace's
  attribute (``{% set ns.due = ns.due + inv.amount_due %}``). The analysis
  ignores order: a name bound to the field anywhere in its scope counts
  everywhere in it;
* among other terms, in any order (``due - written_off - paid``, ``w - paid
  + due``, ``-paid + due``, ``due - (paid + fee)``), negated by a literal
  factor (``paid * -1``), or summed as the items of a literal
  (``sum([due, -paid])``).

Out of its reach, by design — ``_OUT_OF_REACH_*`` below holds one example of
each: a comparison, which subtracts nothing (``paid + amount > due``,
``paid >= due``); a value carried by a container, an object's attribute or a
``for`` target (``row["due"]``, ``self.due`` — a template's namespace
excepted); a helper or a macro that receives the two fields as parameters
(``_rest(due, paid)``), ``operator.sub``, and any value another function
returns. Nor does it read JavaScript.

Two derivations stand apart from it by design: the financial statements'
dated balance (``models/financial_statements._World.balance``) subtracts the
REGISTERS' payments up to a day (``paid(iid, until)``), never the stored
``amount_paid``; and ``invoice.payment_updates`` caps a payment and flips
« payée » by comparing the new ``amount_paid`` to ``amount_due``. The
write-off lot changes each of them itself.

Both scanners walk the language's own syntax tree (``ast``, Jinja's parser):
no regular expression (CWE-1333). Section 2 runs on the shared fake
Firestore (``tests/_fake_firestore.py``), with invented figures.
"""

import ast
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from jinja2 import Environment, nodes

ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import fee_payment
    from models import invoice as invoice_model
    from models import trust

from tests._accounting_history import FEE_PAYEE  # noqa: E402
from tests._fake_firestore import install  # noqa: E402

DUE, PAID = "amount_due", "amount_paid"

# The one subtraction allowed: (module, qualified name of its function).
AUTHORITY = ("models/invoice.py", "balance_of")

# Directories that hold no first-party Python: those the sweep of
# tests/test_logging_setup.py skips; a dot-directory and a virtualenv root
# (a directory holding pyvenv.cfg), whatever their names, are skipped too.
NOT_CODE = {"tests", "venv", "node_modules", "__pycache__", "static", "templates"}

# Where the sweep must reach. It reads more — every package and the
# top-level modules — and this pin keeps it from ever reading less.
REQUIRED_ROOTS = {"client", "dav", "mcp", "models", "routes", "scripts",
                  "services", "utils"}

TEMPLATE_ROOTS = (ATHENA / "templates", ATHENA / "client" / "templates")

# Parses only, never renders; autoescape=True as Flask renders a .html
# template (Bandit B701).
_JINJA = Environment(autoescape=True)


# ══════════════════════════════════════════════════════════════════════
# 1. The sweep
# ══════════════════════════════════════════════════════════════════════
#
# A value's SIGNS are those with which a stored field enters it: {+1} for
# ``due``, {-1} for ``-due`` or ``x - due``, both for ``due - due``, none
# when the field does not enter it. A value takes one field from the other
# when the two enter it with opposite signs.

_NONE = frozenset()
_PLUS = frozenset({1})


def _flip(signs: frozenset) -> frozenset:
    return frozenset(-sign for sign in signs)


def _opposed(due: frozenset, paid: frozenset) -> bool:
    """Whether the two fields enter one value with opposite signs."""
    return (1 in due and -1 in paid) or (-1 in due and 1 in paid)


def _fixed_point(pairs: list, field: str, inherited: dict, signs_of) -> dict:
    """The signs of *field* each key carries: *inherited*'s, and what its
    bindings ``(key, [value, …])`` add, to a fixed point (``a =
    inv.amount_due; b = a`` binds both). A key's signs only grow, two at
    most: the loop ends."""
    env = dict(inherited)
    while True:
        changed = False
        for key, values in pairs:
            signs = env.get(key, _NONE).union(*(signs_of(v, field, env) for v in values))
            if signs != env.get(key, _NONE):
                env[key], changed = signs, True
        if not changed:
            return env


# ── Python ─────────────────────────────────────────────────────────────

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

# A bare read: every other expression computes a value.
_READS = (ast.Name, ast.Attribute, ast.Subscript, ast.Constant)


def _spells(node: ast.AST, field: str) -> bool:
    """A string constant naming *field*: a key, or a call's argument."""
    return isinstance(node, ast.Constant) and node.value == field


def _negative_literal(node: ast.AST) -> bool:
    """``-1``, ``-100``: a factor whose sign the code spells. Any other
    factor is taken as positive."""
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float))


def _signs(node: ast.AST, field: str, env: dict) -> frozenset:
    """The signs with which the stored *field* enters the value of *node* —
    read, wrapped, or through a name *env* binds."""
    if isinstance(node, ast.Name):
        return (_PLUS if node.id == field else _NONE) | env.get(node.id, _NONE)
    if isinstance(node, ast.Attribute):
        return _PLUS if node.attr == field else _NONE
    if isinstance(node, ast.Subscript):
        return _PLUS if _spells(node.slice, field) else _NONE
    if isinstance(node, ast.Call):
        operands = [*node.args, *(k.value for k in node.keywords)]
        if isinstance(node.func, ast.Attribute):
            operands.append(node.func.value)        # a method of a read
        if isinstance(node.func, ast.Name) and node.func.id == "sum":
            # sum([due, -paid]): a literal's items are the sum's terms.
            operands += [item for arg in node.args
                         if isinstance(arg, (ast.List, ast.Tuple, ast.Set))
                         for item in arg.elts]
        named = _PLUS if any(_spells(o, field) for o in operands) else _NONE
        return named.union(*(_signs(o, field, env) for o in operands))
    if isinstance(node, ast.BoolOp):
        return _NONE.union(*(_signs(v, field, env) for v in node.values))
    if isinstance(node, ast.IfExp):
        return _signs(node.body, field, env) | _signs(node.orelse, field, env)
    if isinstance(node, (ast.NamedExpr, ast.Starred, ast.Await)):
        return _signs(node.value, field, env)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _signs(node.operand, field, env)
        if isinstance(node.op, ast.USub):
            return _flip(_signs(node.operand, field, env))
        return _NONE
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        return _signs(node.elt, field, env)
    if isinstance(node, ast.BinOp):
        left, right = _signs(node.left, field, env), _signs(node.right, field, env)
        if isinstance(node.op, ast.Add):
            return left | right
        if isinstance(node.op, ast.Sub):
            return left | _flip(right)
        if isinstance(node.op, ast.Mult):
            return ((_flip(left) if _negative_literal(node.right) else left)
                    | (_flip(right) if _negative_literal(node.left) else right))
        if isinstance(node.op, (ast.Div, ast.FloorDiv)) and _negative_literal(node.right):
            return _flip(left)
        return left                     # a quotient, a remainder…: its first
    return _NONE


def _accumulated(node: ast.AugAssign) -> ast.BinOp:
    """``x -= v`` as the value it gives ``x``: ``x - v``."""
    return ast.BinOp(left=node.target, op=node.op, right=node.value)


def _pairs(target: ast.AST, value: ast.AST) -> list:
    """``(name, value)`` for each name *target* binds to a part of *value*."""
    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if (isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)):
        return [pair for t, v in zip(target.elts, value.elts) for pair in _pairs(t, v)]
    return []


def _env(own: list, field: str, inherited: dict) -> dict:
    """The signs of *field* each name of a scope carries: the enclosing
    scopes', and those its own bindings add — ``=`` (a tuple included), an
    annotated one, a walrus, an accumulation (``x += v``, ``x -= v``)."""
    pairs = []
    for node in own:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                pairs += _pairs(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            pairs += _pairs(node.target, node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            pairs.append((node.target.id, _accumulated(node)))
    return _fixed_point([(name, [value]) for name, value in pairs], field, inherited, _signs)


def _own_nodes(scope: ast.AST) -> list:
    """The nodes of *scope* outside the scopes nested in it — whose roots it
    holds, not their bodies."""
    own, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        own.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return own


def _computed(node: ast.AST):
    """The value *node* computes — an accumulation's included — or ``None``
    for a bare read or a statement."""
    if isinstance(node, ast.AugAssign):
        return _accumulated(node)
    if isinstance(node, ast.expr) and not isinstance(node, _READS):
        return node
    return None


def _outermost_first(node: ast.AST) -> tuple:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0),
            -(getattr(node, "end_lineno", 0) or 0), -(getattr(node, "end_col_offset", 0) or 0))


def python_findings(source: str, rel: str, *, allowed=(AUTHORITY,)) -> list[str]:
    """Every line of one module that computes a value taking one stored field
    from the other, as ``path:line (function): code`` — save in the
    *allowed* functions."""
    found = {}

    def visit(scope, qualname: str, due: dict, paid: dict) -> None:
        own = _own_nodes(scope)
        due, paid = _env(own, DUE, due), _env(own, PAID, paid)
        # Outermost first: a line's finding quotes its whole expression.
        for node in sorted(own, key=_outermost_first):
            value = _computed(node)
            if (value is not None and (rel, qualname) not in allowed
                    and _opposed(_signs(value, DUE, due), _signs(value, PAID, paid))):
                found.setdefault(node.lineno, f"{rel}:{node.lineno} "
                                 f"({qualname or '<module>'}): {ast.unparse(node)}")
            if isinstance(node, _SCOPES):
                name = getattr(node, "name", "<lambda>")
                visit(node, f"{qualname}.{name}" if qualname else name, due, paid)

    visit(ast.parse(source, filename=rel), "", {}, {})
    return [found[line] for line in sorted(found)]


def python_sources() -> list[pathlib.Path]:
    """Every first-party Python module of athena/, its tests excepted."""
    found = []
    for base, dirs, files in os.walk(ATHENA):
        here = pathlib.Path(base)
        dirs[:] = sorted(d for d in dirs
                         if d not in NOT_CODE and not d.startswith(".")
                         and not (here / d / "pyvenv.cfg").exists())
        found += [here / f for f in sorted(files) if f.endswith(".py")]
    return found


# ── Templates ──────────────────────────────────────────────────────────

# A bare read, or an assignment's target: every other expression computes
# a value.
_J_READS = (nodes.Name, nodes.Getattr, nodes.Getitem, nodes.Const,
            nodes.TemplateData, nodes.NSRef)


def _j_spells(node, field: str) -> bool:
    return isinstance(node, nodes.Const) and node.value == field


def _j_key(node):
    """The key a value is bound under: a name (``due``), or a namespace's
    attribute (``ns.due``: set as ``{% set ns.due = … %}``, read as
    ``ns.due``)."""
    if isinstance(node, nodes.Name):
        return node.name
    if isinstance(node, nodes.NSRef):
        return f"{node.name}.{node.attr}"
    if isinstance(node, nodes.Getattr) and isinstance(node.node, nodes.Name):
        return f"{node.node.name}.{node.attr}"
    return None


def _j_negative_literal(node) -> bool:
    return (isinstance(node, nodes.Neg) and isinstance(node.node, nodes.Const)
            and type(node.node.value) in (int, float))


def _j_signs(node, field: str, env: dict) -> frozenset:
    """:func:`_signs` for a Jinja expression."""
    if node is None:
        return _NONE
    if isinstance(node, nodes.Name):
        return (_PLUS if node.name == field else _NONE) | env.get(node.name, _NONE)
    if isinstance(node, nodes.Getattr):
        return (_PLUS if node.attr == field else _NONE) | env.get(_j_key(node), _NONE)
    if isinstance(node, nodes.Getitem):
        return _PLUS if _j_spells(node.arg, field) else _NONE
    if isinstance(node, (nodes.Call, nodes.Filter)):
        operands = [*node.args, *(k.value for k in node.kwargs)]
        if isinstance(node, nodes.Filter):
            operands.append(node.node)          # its input: inv.amount_due|int
            if node.name == "sum" and isinstance(node.node, (nodes.List, nodes.Tuple)):
                operands += node.node.items     # [due, -paid]|sum
        elif isinstance(node.node, nodes.Getattr):
            operands.append(node.node.node)     # a method of a read
        named = _PLUS if any(_j_spells(o, field) for o in operands) else _NONE
        return named.union(*(_j_signs(o, field, env) for o in operands))
    if isinstance(node, (nodes.Or, nodes.And, nodes.Add)):
        return _j_signs(node.left, field, env) | _j_signs(node.right, field, env)
    if isinstance(node, nodes.CondExpr):
        return _j_signs(node.expr1, field, env) | _j_signs(node.expr2, field, env)
    if isinstance(node, nodes.Pos):
        return _j_signs(node.node, field, env)
    if isinstance(node, nodes.Neg):
        return _flip(_j_signs(node.node, field, env))
    if isinstance(node, nodes.Sub):
        return _j_signs(node.left, field, env) | _flip(_j_signs(node.right, field, env))
    if isinstance(node, nodes.Mul):
        left, right = _j_signs(node.left, field, env), _j_signs(node.right, field, env)
        return ((_flip(left) if _j_negative_literal(node.right) else left)
                | (_flip(right) if _j_negative_literal(node.left) else right))
    if isinstance(node, (nodes.Div, nodes.FloorDiv)):
        left = _j_signs(node.left, field, env)
        return _flip(left) if _j_negative_literal(node.right) else left
    if isinstance(node, (nodes.Mod, nodes.Pow)):
        return _j_signs(node.left, field, env)
    return _NONE


def _j_pairs(target, value) -> list:
    """``(key, [value])`` for each name or namespace attribute *target* binds
    to a part of *value*."""
    if isinstance(target, (nodes.Name, nodes.NSRef)):
        return [(_j_key(target), [value])]
    if (isinstance(target, nodes.Tuple) and isinstance(value, nodes.Tuple)
            and len(target.items) == len(value.items)):
        return [pair for t, v in zip(target.items, value.items) for pair in _j_pairs(t, v)]
    return []


def _j_printed(block) -> list:
    """The expressions a ``{% set x %}…{% endset %}`` block prints: the
    value it binds."""
    outputs = [n for n in block.body if isinstance(n, nodes.Output)]
    outputs += [o for n in block.body for o in n.find_all(nodes.Output)]
    return [e for o in outputs for e in o.nodes if not isinstance(e, nodes.TemplateData)]


def _j_env(tree, field: str) -> dict:
    """The signs of *field* each name — or namespace attribute — of a
    template carries: ``{% set %}`` (a tuple, a block, a namespace's
    attribute included), ``{% with %}``, and a ``namespace(…)``'s own
    arguments. The whole template is one scope."""
    pairs = []
    for node in tree.find_all((nodes.Assign, nodes.AssignBlock, nodes.With)):
        if isinstance(node, nodes.Assign):
            pairs += _j_pairs(node.target, node.node)
            call = node.node
            if (isinstance(node.target, nodes.Name) and isinstance(call, nodes.Call)
                    and isinstance(call.node, nodes.Name) and call.node.name == "namespace"):
                pairs += [(f"{node.target.name}.{k.key}", [k.value]) for k in call.kwargs]
        elif isinstance(node, nodes.AssignBlock):
            if isinstance(node.target, (nodes.Name, nodes.NSRef)):
                pairs.append((_j_key(node.target), _j_printed(node)))
        else:
            for target, value in zip(node.targets, node.values):
                pairs += _j_pairs(target, value)
    return _fixed_point(pairs, field, {}, _j_signs)


def template_findings(source: str, name: str) -> list[str]:
    """Every line of one template that computes a value taking one stored
    field from the other, as ``path:line``."""
    tree = _JINJA.parse(source)
    due, paid = _j_env(tree, DUE), _j_env(tree, PAID)
    lines = {node.lineno for node in tree.find_all(nodes.Expr)
             if not isinstance(node, _J_READS)
             and _opposed(_j_signs(node, DUE, due), _j_signs(node, PAID, paid))}
    return [f"{name}:{line}" for line in sorted(lines)]


def template_sources() -> list[pathlib.Path]:
    return [p for root in TEMPLATE_ROOTS for p in sorted(root.rglob("*")) if p.is_file()]


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(ATHENA).as_posix()


# ── The sweep itself ────────────────────────────────────────────────────


def test_no_module_or_template_computes_an_invoice_balance_but_balance_of():
    """Every balance goes through ``invoice.balance_of``. A finding names the
    line: call ``balance_of(invoice)`` there (``-balance_of(invoice)`` for
    the reversed order)."""
    findings = []
    for path in python_sources():
        findings += python_findings(path.read_text(encoding="utf-8"), _rel(path))
    for path in template_sources():
        findings += template_findings(path.read_text(encoding="utf-8"), _rel(path))
    assert findings == [], "\n".join(findings)


def test_the_sweep_reads_every_package_and_both_template_trees():
    modules = {_rel(p) for p in python_sources()}
    assert {m.split("/")[0] for m in modules if "/" in m} >= REQUIRED_ROOTS
    # The two caps lot 0 rerouted, the authority, a top-level module.
    assert {"models/admin_ledger.py", "models/trust.py", "models/invoice.py",
            "main.py"} <= modules
    assert not any(m.startswith("tests/") for m in modules)
    templates = {_rel(p) for p in template_sources()}
    assert "templates/dashboard/index.html" in templates
    assert any(t.startswith("client/templates/") for t in templates)


def test_the_one_allowed_subtraction_is_balance_of_own():
    """Not vacuous, and the allowance is live: without it, the sweep finds
    exactly one subtraction in models/invoice.py — the one in balance_of."""
    source = (ATHENA / "models" / "invoice.py").read_text(encoding="utf-8")
    assert python_findings(source, AUTHORITY[0]) == []
    found = python_findings(source, AUTHORITY[0], allowed=())
    assert len(found) == 1, found
    assert f"({AUTHORITY[1]}):" in found[0], found


def test_a_virtualenv_kept_inside_athena_is_never_read(tmp_path, monkeypatch):
    """A developer's virtualenv may sit inside athena/ under any name: its
    pyvenv.cfg, not its name, keeps the sweep out."""
    pkg = tmp_path / "models"
    pkg.mkdir()
    (pkg / "own.py").write_text("x = 1\n", encoding="utf-8")
    env = tmp_path / "env-local"
    (env / "lib").mkdir(parents=True)
    (env / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (env / "lib" / "third.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("z = 1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ATHENA", tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in python_sources()] == ["models/own.py"]


# ── Planted examples: each form is caught, the innocent ones are not ────

_PLANTED_PYTHON = {
    # The two copies lot 0 removed, verbatim.
    "admin_ledger_cap": (
        "def _stage_create(invoice, amount):\n"
        "    balance = int(invoice.get(\"amount_due\", 0)) - int(invoice.get(\"amount_paid\", 0))\n"
        "    if amount > balance:\n"
        "        raise Abort\n"),
    "trust_cap": (
        "def _stage_create(invoice, amount):\n"
        "    if amount > int(invoice.get(\"amount_due\", 0)) - int(invoice.get(\"amount_paid\", 0)):\n"
        "        raise Abort\n"),
    "attribute": "def f(inv):\n    return inv.amount_due - inv.amount_paid\n",
    "get": "def f(inv):\n    return inv.get('amount_due', 0) - inv.get('amount_paid', 0)\n",
    "subscript": "def f(inv):\n    return inv['amount_due'] - inv['amount_paid']\n",
    "int_wrappers": "def f(inv):\n    return int(inv['amount_due']) - int(inv.amount_paid)\n",
    "or_default": ("def f(inv):\n"
                   "    return int(inv.get('amount_due') or 0) - int(inv.get('amount_paid') or 0)\n"),
    "decimal_dollars": ("def f(inv):\n"
                        "    return Decimal(inv['amount_due']) / 100 - Decimal(inv['amount_paid']) / 100\n"),
    "getattr": "def f(inv):\n    return getattr(inv, 'amount_due') - getattr(inv, 'amount_paid', 0)\n",
    "helper_naming_the_field": "def f(inv):\n    return _cents(inv, 'amount_due') - _cents(inv, 'amount_paid')\n",
    "method_of_a_read": ("def f(inv):\n"
                         "    return Decimal(inv['amount_due']).quantize(Q) - inv['amount_paid']\n"),
    "conditional": "def f(inv, x):\n    return (inv.amount_due if x else 0) - inv.amount_paid\n",
    "awaited": ("async def f(inv):\n"
                "    return (await cents(inv['amount_due'])) - inv['amount_paid']\n"),
    "bare_names": "def f(amount_due, amount_paid):\n    return amount_due - amount_paid\n",
    "bound_names": ("def f(inv):\n    due = int(inv['amount_due'])\n"
                    "    paid = int(inv['amount_paid'])\n    return due - paid\n"),
    "bound_through_names": ("def f(inv):\n    a = inv.amount_due\n    b = a\n"
                            "    return b - inv.amount_paid\n"),
    "bound_tuple": ("def f(inv):\n    due, paid = inv['amount_due'], inv['amount_paid']\n"
                    "    return due - paid\n"),
    "bound_annotated": "def f(inv):\n    due: int = inv['amount_due']\n    return due - inv['amount_paid']\n",
    "walrus": "def f(inv):\n    return (due := inv['amount_due']) - inv['amount_paid']\n",
    "bound_in_the_enclosing_function": (
        "def f(inv):\n    due = inv['amount_due']\n"
        "    def g():\n        return due - inv['amount_paid']\n    return g()\n"),
    "bound_at_module_level": "DUE = INV['amount_due']\ndef f(inv):\n    return DUE - inv['amount_paid']\n",
    "bound_negated": ("def f(inv):\n    credit = -inv['amount_paid']\n"
                      "    return inv['amount_due'] + credit\n"),
    "augmented": ("def f(inv):\n    balance = inv['amount_due']\n"
                  "    balance -= inv['amount_paid']\n    return balance\n"),
    "augmented_negated": ("def f(inv):\n    balance = inv['amount_due']\n"
                          "    balance += -inv['amount_paid']\n    return balance\n"),
    # Accumulators, the house's style for totals (get_invoice_summary).
    "accumulated": ("def f(inv):\n    balance = 0\n"
                    "    balance += int(inv.get('amount_due', 0))\n"
                    "    balance -= int(inv.get('amount_paid', 0))\n    return balance\n"),
    "accumulated_paid_first": ("def f(inv):\n    balance = 0\n"
                               "    balance -= inv['amount_paid']\n"
                               "    balance += inv['amount_due']\n    return balance\n"),
    "accumulated_in_a_loop": (
        "def summary(rows):\n    total_due = total_paid = 0\n    for inv in rows:\n"
        "        total_due += int(inv.get('amount_due', 0))\n"
        "        total_paid += int(inv.get('amount_paid', 0))\n"
        "    return {'total_outstanding': total_due - total_paid}\n"),
    "accumulated_by_a_product": ("def f(inv):\n    x = 1\n    x *= inv['amount_due']\n"
                                 "    return x - inv['amount_paid']\n"),
    "more_terms_left": "def f(inv, w):\n    return inv.amount_due - w - inv.amount_paid\n",
    "more_terms_right": "def f(inv, w):\n    return inv.amount_due - (inv.amount_paid + w)\n",
    "more_terms_in_any_order": "def f(inv, w):\n    return w - inv.amount_paid + inv.amount_due\n",
    "negated_among_terms": "def f(inv, w):\n    return -inv.amount_paid + w + inv.amount_due\n",
    "plus_negated": "def f(inv):\n    return inv.amount_due + -inv.amount_paid\n",
    "negated_first": "def f(inv):\n    return -inv.amount_paid + inv.amount_due\n",
    "negative_literal_factor": "def f(inv):\n    return inv.amount_due + inv.amount_paid * -1\n",
    "reversed": "def f(inv):\n    return inv['amount_paid'] - inv['amount_due']\n",
    "summed_over_rows": ("def f(rows):\n    return (sum(r['amount_due'] for r in rows)\n"
                         "            - sum(r['amount_paid'] for r in rows))\n"),
    "sum_of_a_literal": "def f(inv):\n    return sum([inv['amount_due'], -inv['amount_paid']])\n",
    "in_an_f_string": "def f(inv):\n    return f'{inv.amount_due - inv.amount_paid}'\n",
    "in_a_dict_comprehension": ("def f(inv, keys):\n"
                                "    return {k: inv['amount_due'] - inv['amount_paid'] for k in keys}\n"),
    "in_a_lambda": "KEY = lambda inv: inv['amount_due'] - inv['amount_paid']\n",
    "in_a_method": ("class C:\n    def balance(self):\n"
                    "        return self.inv.amount_due - self.inv.amount_paid\n"),
    "at_module_level": "BALANCE = INV['amount_due'] - INV['amount_paid']\n",
    # The allowance names a module AND a function: the same function anywhere
    # else is a second authority.
    "a_balance_of_elsewhere": "def balance_of(inv):\n    return inv['amount_due'] - inv['amount_paid']\n",
}

_INNOCENT_PYTHON = {
    "the_authority_called": "def f(inv):\n    return balance_of(inv)\n",
    "another_field": "def f(inv):\n    return inv['amount_due'] - inv['retainer_applied']\n",
    "the_statements_register_balance": (
        "class W:\n    def balance(self, iid, until):\n"
        "        inv = self.invoices.by_id[iid]\n"
        "        return _amount_due(inv) - self.paid(iid, until) - self.settled.get(iid, 0)\n"),
    "a_reduction_of_what_was_paid": ("def f(inv, amount):\n"
                                     "    paid = int(inv.get('amount_paid', 0))\n"
                                     "    return paid - amount\n"),
    "a_new_payment_against_the_due": ("def f(inv, amount):\n"
                                      "    due = int(inv.get('amount_due', 0))\n"
                                      "    return amount - due\n"),
    "a_payment_accumulated_then_compared": ("def f(inv, amount):\n"
                                            "    paid = int(inv.get('amount_paid', 0))\n"
                                            "    paid += amount\n"
                                            "    return paid > int(inv.get('amount_due', 0))\n"),
    "a_new_payment_against_an_accumulated_due": ("def f(inv, amount):\n    due = 0\n"
                                                 "    due += int(inv.get('amount_due', 0))\n"
                                                 "    return amount - due\n"),
    "both_fields_added": "def f(inv):\n    return int(inv['amount_due']) + int(inv['amount_paid'])\n",
    "both_fields_negated": "def f(inv):\n    return -inv['amount_due'] - inv['amount_paid']\n",
    "rows_side_by_side": ("def f(inv):\n"
                          "    return [_row('Dû', inv['amount_due']), _row('Payé', -inv['amount_paid'])]\n"),
    "names_bound_in_a_sibling_function": (
        "def a(inv):\n    due = inv['amount_due']\n    return due\n"
        "def b(due, paid):\n    return due - paid\n"),
    "prose": "HELP = 'amount_due - amount_paid'\n",
}

# The module docstring's « Out of its reach », one example each — by design.
_OUT_OF_REACH_PYTHON = {
    "a_comparison_cap": (
        "def _stage_create(invoice, amount):\n"
        "    if amount + int(invoice.get('amount_paid', 0)) > int(invoice.get('amount_due', 0)):\n"
        "        raise Abort\n"),
    "a_settled_flag": ("def f(inv):\n"
                       "    return int(inv.get('amount_paid', 0)) >= int(inv.get('amount_due', 0))\n"),
    "a_container": ("def f(inv):\n    row = {'due': inv['amount_due'], 'paid': inv['amount_paid']}\n"
                    "    return row['due'] - row['paid']\n"),
    "an_attribute": ("class C:\n    def __init__(self, inv):\n"
                     "        self.due = inv['amount_due']\n        self.paid = inv['amount_paid']\n"
                     "    def balance(self):\n        return self.due - self.paid\n"),
    "a_for_target": ("def f(rows):\n"
                     "    for due, paid in ((r['amount_due'], r['amount_paid']) for r in rows):\n"
                     "        yield due - paid\n"),
    "a_helper_s_parameters": ("def _rest(due, paid):\n    return due - paid\n"
                              "def f(inv):\n    return _rest(inv['amount_due'], inv['amount_paid'])\n"),
    "operator_sub": ("def f(inv):\n"
                     "    return operator.sub(inv['amount_due'], inv['amount_paid'])\n"),
    "another_function_s_values": "def f(inv):\n    return due_of(inv) - paid_of(inv)\n",
}


@pytest.mark.parametrize("name", sorted(_PLANTED_PYTHON))
def test_a_planted_python_balance_is_caught(name):
    assert python_findings(_PLANTED_PYTHON[name], "models/planted.py"), name


@pytest.mark.parametrize("name", sorted(_INNOCENT_PYTHON))
def test_an_innocent_python_line_is_not(name):
    assert python_findings(_INNOCENT_PYTHON[name], "models/planted.py") == [], name


@pytest.mark.parametrize("name", sorted(_OUT_OF_REACH_PYTHON))
def test_a_documented_python_limit_is_not_caught(name):
    """The limits are written, and pinned: a scanner that learns one of these
    moves it to _PLANTED_PYTHON and drops it from the module docstring."""
    assert python_findings(_OUT_OF_REACH_PYTHON[name], "models/planted.py") == [], name


_PLANTED_TEMPLATES = {
    "attribute": "{{ inv.amount_due - inv.amount_paid }}",
    "filtered": "{{ (inv.amount_due|int) - (inv.amount_paid|int) }}",
    "get": "{{ inv.get('amount_due', 0) - inv.get('amount_paid', 0) }}",
    "subscript": "{{ inv['amount_due'] - inv['amount_paid'] }}",
    "or_default": "{{ (inv.amount_due or 0) - (inv.amount_paid or 0) }}",
    "conditional": "{{ (inv.amount_due if x else 0) - inv.amount_paid }}",
    "set": "{% set due = inv.amount_due %}{% set paid = inv.amount_paid %}{{ due - paid }}",
    "set_through_names": "{% set a = inv.amount_due %}{% set b = a %}{{ b - inv.amount_paid }}",
    "set_tuple": "{% set due, paid = inv.amount_due, inv.amount_paid %}{{ due - paid }}",
    "set_block": "{% set due %}{{ inv.amount_due }}{% endset %}{{ due|int - inv.amount_paid }}",
    "set_negated": "{% set credit = -inv.amount_paid %}{{ inv.amount_due + credit }}",
    "with": "{% with due = inv.amount_due, paid = inv.amount_paid %}{{ due - paid }}{% endwith %}",
    # A namespace: the one way a template accumulates across a loop.
    "namespace_accumulated": (
        "{% set ns = namespace(due=0, paid=0) %}{% for inv in invoices %}"
        "{% set ns.due = ns.due + inv.amount_due %}{% set ns.paid = ns.paid + inv.amount_paid %}"
        "{% endfor %}{{ ns.due - ns.paid }}"),
    "namespace_initialised": "{% set ns = namespace(due=inv.amount_due) %}{{ ns.due - inv.amount_paid }}",
    "inside_a_tag": "{% if inv.amount_due - inv.amount_paid > 0 %}x{% endif %}",
    "more_terms": "{{ inv.amount_due - (inv.amount_paid + fee) }}",
    "more_terms_in_any_order": "{{ w - inv.amount_paid + inv.amount_due }}",
    "plus_negated": "{{ inv.amount_due + -inv.amount_paid }}",
    "negative_literal_factor": "{{ inv.amount_due + inv.amount_paid * -1 }}",
    "reversed": "{{ inv.amount_paid - inv.amount_due }}",
    "summed_over_rows": "{{ rows|sum(attribute='amount_due') - rows|sum(attribute='amount_paid') }}",
    "sum_of_a_literal": "{{ [inv.amount_due, -inv.amount_paid]|sum }}",
}

_INNOCENT_TEMPLATES = {
    "the_route_value": "{{ inv._balance|cents_fr }}",
    "another_field": "{{ inv.amount_due - inv.retainer_applied }}",
    "printed_side_by_side": "{{ inv.amount_due|cents_fr }} − {{ inv.amount_paid|cents_fr }}",
    "both_fields_added": "{{ inv.amount_due + inv.amount_paid }}",
    "a_namespace_of_another_field": (
        "{% set ns = namespace(held=0) %}{% for i in invoices %}"
        "{% set ns.held = ns.held + i.retainer_applied %}{% endfor %}"
        "{{ inv.amount_due - ns.held }}"),
    "prose": "amount_due - amount_paid",
}

# The module docstring's « Out of its reach », one example each — by design.
_OUT_OF_REACH_TEMPLATES = {
    "a_comparison": "{% if inv.amount_paid >= inv.amount_due %}payée{% endif %}",
    "a_container": "{% set row = {'due': inv.amount_due} %}{{ row['due'] - inv.amount_paid }}",
    "a_for_target": "{% for due, paid in [(inv.amount_due, inv.amount_paid)] %}{{ due - paid }}{% endfor %}",
    "a_macro_s_parameters": ("{% macro rest(due, paid) %}{{ due - paid }}{% endmacro %}"
                             "{{ rest(inv.amount_due, inv.amount_paid) }}"),
}


@pytest.mark.parametrize("name", sorted(_PLANTED_TEMPLATES))
def test_a_planted_template_balance_is_caught(name):
    assert template_findings(_PLANTED_TEMPLATES[name], "templates/planted.html"), name


@pytest.mark.parametrize("name", sorted(_INNOCENT_TEMPLATES))
def test_an_innocent_template_is_not(name):
    assert template_findings(_INNOCENT_TEMPLATES[name], "templates/planted.html") == [], name


@pytest.mark.parametrize("name", sorted(_OUT_OF_REACH_TEMPLATES))
def test_a_documented_template_limit_is_not_caught(name):
    """The limits are written, and pinned: a scanner that learns one of these
    moves it to _PLANTED_TEMPLATES and drops it from the module docstring."""
    assert template_findings(_OUT_OF_REACH_TEMPLATES[name], "templates/planted.html") == [], name


# ══════════════════════════════════════════════════════════════════════
# 2. The two caps obey balance_of
# ══════════════════════════════════════════════════════════════════════
#
# The sweep proves no copy is LEFT; these prove the caps ASK the authority:
# with balance_of answering a figure below the stored amount_due −
# amount_paid, a payment above that figure is refused (the inline copies
# accepted it). Then, on the real balance of a partly paid invoice, the cap
# holds to the cent — the behaviour the existing refusals pin, unchanged.

_UTC = timezone.utc
_INVOICE = {
    "id": "inv1", "invoice_number": "2031-F007", "dossier_id": "dos1",
    "dossier_file_number": "2031-004", "dossier_title": "Exemple c. Témoin",
    "client_id": "c1", "status": "envoyée", "total": 100000,
    "retainer_applied": 0, "amount_due": 100000, "amount_paid": 0,
    "paid_date": None, "etag": "inv1-e0",
}


def _d(day: int) -> datetime:
    return datetime(2031, 3, day, tzinfo=_UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    """Two registers, a dossier of one client with cleared trust funds, and
    one issued invoice of 1 000 $ — invented, in 2031, the ONE Montréal
    clock read frozen on 2031-03-20."""
    from utils import deadlines as dl

    frozen = datetime(2031, 3, 20, 16, 0, tzinfo=_UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "t0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2031-004", "title": "Exemple c. Témoin",
        "client_ids": ["c1"], "clients": [{"id": "c1", "name": "Cliente Exemple"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    f.seed("invoices/inv1", dict(_INVOICE))
    deposit, errs = trust.create_transaction(_trust_entry(
        direction="recette", purpose="dépôt_client", amount=200000,
        counterparty="Cliente Exemple", date=_d(3)))
    assert errs == [], errs
    _, errs = trust.clear_transaction(deposit["id"], _d(3))
    assert errs == [], errs
    return f


def _encaissement(amount: int) -> dict:
    return {"account_id": "ops1", "kind": "encaissement_facture",
            "invoice_id": "inv1", "amount": amount, "method": "virement",
            "counterparty": "Cliente Exemple", "date": _d(10),
            "description": "", "reference": ""}


def _trust_entry(**over) -> dict:
    # The payee art. 58 accepts: the firm profile's, shared by the suite.
    entry = {"account_id": "acc1", "direction": "déboursé", "amount": 0,
             "purpose": "virement_honoraires", "method": "chèque",
             "counterparty": FEE_PAYEE, "dossier_id": "dos1",
             "client_id": "c1", "date": _d(10), "invoice_id": "inv1",
             "description": "", "reference": "77"}
    entry.update(over)
    return entry


def _fee(amount: int):
    return fee_payment.create_fee_payment(_trust_entry(amount=amount),
                                          admin_account_id="ops1")


def _paid(fake) -> int:
    return fake.peek("invoices/inv1")["amount_paid"]


def test_the_encaissement_cap_is_what_balance_of_answers(fake, monkeypatch):
    """Fails on the inline copy: it read 1 000 $ owed and took 300,01 $."""
    monkeypatch.setattr(invoice_model, "balance_of", lambda invoice: 30000)
    entry, errs = al.create_transaction(_encaissement(30001))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["encaissement_excède_solde"]]
    assert _paid(fake) == 0
    entry, errs = al.create_transaction(_encaissement(30000))
    assert errs == [] and entry
    assert _paid(fake) == 30000


def test_the_fee_payment_cap_is_what_balance_of_answers(fake, monkeypatch):
    """Fails on the inline copy: it read 1 000 $ owed and withdrew 300,01 $
    of the client's trust funds."""
    monkeypatch.setattr(invoice_model, "balance_of", lambda invoice: 30000)
    result, errs = _fee(30001)
    assert result is None
    assert errs == [trust._ABORT_MESSAGES["virement_excède_facture"]]
    assert _paid(fake) == 0
    result, errs = _fee(30000)
    assert errs == [] and result
    assert _paid(fake) == 30000


def test_on_a_partly_paid_invoice_each_cap_holds_to_the_cent(fake):
    """400 $ received, 600 $ owed: 600,01 $ is refused by both registers,
    600 $ is taken and settles the invoice."""
    _, errs = al.create_transaction(_encaissement(40000))
    assert errs == [], errs
    assert invoice_model.balance_of(fake.peek("invoices/inv1")) == 60000

    entry, errs = al.create_transaction(_encaissement(60001))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["encaissement_excède_solde"]]
    result, errs = _fee(60001)
    assert result is None
    assert errs == [trust._ABORT_MESSAGES["virement_excède_facture"]]
    assert _paid(fake) == 40000

    result, errs = _fee(60000)
    assert errs == [] and result
    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 100000
    assert invoice["status"] == "payée"
