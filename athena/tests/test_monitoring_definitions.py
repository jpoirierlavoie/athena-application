"""The GCP monitoring definitions under ``monitoring/``, tied to the code.

``monitoring/metrics/<metric>.json`` (one log-based counter each) and
``monitoring/policies/<metric>.json`` (one alert policy each, named by the
counter it watches) are the SINGLE source of the security alerts
(DEPLOYMENT.md §6.10): read back from production and found equal when they
were written (November 2026), they stay equal to it as long as every change
goes through them (§6.10, step 5), and
the email's French text lives only there. ``utils/deployment_inventory.SECURITY_METRICS``
names them and restates a few values for the inventory, the probe and the
docs; each restated value is pinned here, or in
``tests/test_deployment_inventory.py`` where the house pins the table.

Nothing in GCP checks these files against the code, and every defect here is
SILENT: a counter filtering on a renamed log or another project counts zero,
an event nothing emits counts zero, a policy whose threshold is raised above
zero — or whose condition becomes a ratio (``denominatorFilter``) or a
forecast — stays silent on one event, an aggregation other than the summed
5-minute window makes the docs' « count > 0 over 5 minutes »
(DEPLOYMENT.md §6.10, OBSERVABILITY.md) false, and a
channel id in the file leaks it (the repo is public). So each check below is
a function returning the problems it
finds, and each runs twice: on the real files, where it must find nothing,
and on a planted mutation, where it must find the defect (CLAUDE.md, « every
guard proves it catches something »).

Linear string handling and one strict, linear regex (CWE-1333).
"""

import ast
import copy
import functools
import json
import os
import pathlib
import re
import sys
from typing import get_args

import pytest

ATHENA = pathlib.Path(__file__).resolve().parent.parent
ROOT = ATHENA.parent
sys.path.insert(0, str(ATHENA))

from utils.deployment_inventory import (  # noqa: E402
    ALERT_AUTO_CLOSE,
    ALERT_RESOURCE_TYPE,
    ALERT_WINDOW,
    MONITORING_DIR,
    OWNER_LITERALS,
    SECURITY_METRICS,
)
from utils.logging_setup import APP_LOG_NAME, SecurityEvent  # noqa: E402

MONITORING = ROOT / MONITORING_DIR
METRICS_DIR = MONITORING / "metrics"
POLICIES_DIR = MONITORING / "policies"
BY_METRIC = {m.metric: m for m in SECURITY_METRICS}


# ── Loading ──────────────────────────────────────────────────────────────


def decode_problems(raw: bytes) -> list:
    """UTF-8, no BOM, and JSON. A BOM is what Windows PowerShell writes, and
    it is not JSON; gcloud reads these files as given."""
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("starts with a UTF-8 BOM")
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return problems + ["is not UTF-8"]
    try:
        json.loads(text)
    except ValueError:
        problems.append("is not JSON")
    return problems


def _load(path: pathlib.Path) -> dict:
    raw = path.read_bytes()
    problems = decode_problems(raw)
    assert not problems, (path.name, problems)
    return json.loads(raw.decode("utf-8"))


def _metric_docs() -> dict:
    return {p.stem: _load(p) for p in sorted(METRICS_DIR.glob("*.json"))}


def _policy_docs() -> dict:
    return {p.stem: _load(p) for p in sorted(POLICIES_DIR.glob("*.json"))}


def _section_titles(markdown: str) -> tuple:
    """(numbers, titles) of DEPLOYMENT.md's headings, fenced code skipped: a
    shell comment there starts with « # » too."""
    numbers, titles = set(), []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):  # list items indent theirs
            fenced = not fenced
            continue
        if fenced or not line.startswith("#"):
            continue
        heading = line.lstrip("#")
        if not heading.startswith(" "):
            continue
        number, _, title = heading.strip().partition(" ")
        numbers.add(number.rstrip("."))
        titles.append(title)
    return numbers, titles


_DEPLOYMENT_SECTIONS = _section_titles(
    (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8"))


# ── a. JSON hygiene and shape ────────────────────────────────────────────

METRIC_KEYS = {"name", "description", "filter", "metricDescriptor"}
COUNTER = {"metricKind": "DELTA", "valueType": "INT64", "unit": "1"}
POLICY_KEYS = {"displayName", "documentation", "conditions", "combiner",
               "enabled", "alertStrategy"}
# A live policy's output-only fields (name, creationRecord, mutationRecord)
# and its channel all carry the project's ids: a copy of a REST read sheds
# them, and the channel is passed at creation, never versioned (the
# repository is public).
NEVER_IN_A_POLICY = {"notificationChannels", "name", "creationRecord",
                     "mutationRecord"}


def metric_shape_problems(stem: str, doc: dict) -> list:
    problems = []
    if set(doc) != METRIC_KEYS:
        problems.append("keys %s, not exactly %s"
                        % (sorted(doc), sorted(METRIC_KEYS)))
    if doc.get("metricDescriptor") != COUNTER:
        problems.append("descriptor %r is not the plain counter %r"
                        % (doc.get("metricDescriptor"), COUNTER))
    if doc.get("name") != stem:
        problems.append("name %r is not the file's stem %r"
                        % (doc.get("name"), stem))
    description = doc.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("no description")
    return problems


def policy_key_problems(doc: dict) -> list:
    problems = []
    for key in sorted(set(doc) & NEVER_IN_A_POLICY):
        problems.append(key + " is output-only or private: never versioned")
    for key in sorted(set(doc) - POLICY_KEYS - NEVER_IN_A_POLICY):
        problems.append(key + " is not an allowed key")
    for i, condition in enumerate(doc.get("conditions") or []):
        if isinstance(condition, dict) and "name" in condition:
            problems.append("conditions[%d].name is output-only" % i)
    return problems


def test_the_folder_holds_only_the_json_definitions():
    """A definition anywhere else — a YAML copy, a nested folder — would be
    a second source that no check reads (gcloud takes YAML too)."""
    found = sorted(p.relative_to(MONITORING).as_posix()
                   for p in MONITORING.rglob("*")
                   if p.is_file() and p.suffix in (".json", ".yaml", ".yml"))
    assert found, "monitoring/ holds no definition"
    strays = [f for f in found
              if not (f.startswith(("metrics/", "policies/"))
                      and f.endswith(".json") and f.count("/") == 1)]
    assert strays == []


def test_every_file_is_utf8_json_without_a_bom():
    # Read BEFORE the assert: under `python -O` an assert is not evaluated,
    # and a file read belongs to no assertion (CodeQL py/side-effect-in-assert).
    for path in sorted(MONITORING.rglob("*.json")):
        raw = path.read_bytes()
        assert decode_problems(raw) == [], path.name


@pytest.mark.parametrize("raw, expected", [
    (b'\xef\xbb\xbf{"name": "x"}', "BOM"),
    ('{"description": "é"}'.encode("latin-1"), "not UTF-8"),
    (b'{"name": "x",}', "not JSON"),
])
def test_the_decode_check_catches_a_planted_defect(raw, expected):
    assert any(expected in p for p in decode_problems(raw))


def test_every_metric_file_is_exactly_a_plain_counter_named_by_its_stem():
    docs = _metric_docs()
    assert docs, "no metric file"
    for stem, doc in docs.items():
        assert metric_shape_problems(stem, doc) == [], stem


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.update(labelExtractors={"x": "EXTRACT(y)"}), "keys"),
    (lambda d: d.pop("description"), "keys"),
    (lambda d: d["metricDescriptor"].update(metricKind="GAUGE"), "descriptor"),
    (lambda d: d["metricDescriptor"].update(valueType="DOUBLE"), "descriptor"),
    (lambda d: d["metricDescriptor"].update(unit="By"), "descriptor"),
    (lambda d: d["metricDescriptor"].update(labels=[]), "descriptor"),
    (lambda d: d.update(name="security_other"), "stem"),
    (lambda d: d.update(description="  "), "description"),
])
def test_the_metric_shape_check_catches_a_planted_mutation(mutate, expected):
    stem, doc = next(iter(_metric_docs().items()))
    planted = copy.deepcopy(doc)
    mutate(planted)
    assert any(expected in p for p in metric_shape_problems(stem, planted))


def test_no_policy_file_carries_a_channel_or_an_output_only_field():
    docs = _policy_docs()
    assert docs, "no policy file"
    for stem, doc in docs.items():
        assert policy_key_problems(doc) == [], stem


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.update(notificationChannels=[
        "projects/p/notificationChannels/1"]), "notificationChannels"),
    (lambda d: d.update(name="projects/p/alertPolicies/1"), "name"),
    (lambda d: d.update(creationRecord={}), "creationRecord"),
    (lambda d: d.update(mutationRecord={}), "mutationRecord"),
    (lambda d: d.update(severity="CRITICAL"), "severity"),
    (lambda d: d["conditions"][0].update(
        name="projects/p/alertPolicies/1/conditions/2"), "conditions[0].name"),
])
def test_the_policy_key_check_catches_a_planted_mutation(mutate, expected):
    planted = copy.deepcopy(next(iter(_policy_docs().values())))
    mutate(planted)
    assert any(expected in p for p in policy_key_problems(planted))


# ── b. The counter's filter ──────────────────────────────────────────────
#
# logName="projects/<p>/logs/<log>" AND jsonPayload.event=<"e" | ("e1" OR …)>
# Deterministic at every step — each repetition opens on a fixed literal —
# so matching is linear in the input (CWE-1333).

_FILTER = re.compile(
    r'logName="projects/([a-z][-a-z0-9]{4,28}[a-z0-9])/logs/([a-z][-a-z0-9_.]*)"'
    r' AND jsonPayload\.event='
    r'(?:"([a-z_]+)"|\(("[a-z_]+"(?: OR "[a-z_]+")+)\))'
)


def parse_filter(text: str):
    """(project, log, events), or None when the filter has any other shape —
    another clause, another operator, no ``logName`` (every log counted)."""
    match = _FILTER.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    project, log, single, several = match.groups()
    if single is not None:
        return project, log, (single,)
    return project, log, tuple(e.strip('"') for e in several.split(" OR "))


def filter_problems(doc: dict) -> list:
    parsed = parse_filter(doc.get("filter"))
    if parsed is None:
        return ["filter %r is not logName=\"projects/<p>/logs/<log>\" AND "
                "jsonPayload.event=<\"e\" | (\"e1\" OR …)>" % doc.get("filter")]
    project, log, events = parsed
    problems = []
    if project != OWNER_LITERALS["GCP project id"]:
        problems.append("project %r is not the deployment's" % project)
    if log != APP_LOG_NAME:
        problems.append("log %r is not the handler's %r" % (log, APP_LOG_NAME))
    if len(set(events)) != len(events):
        problems.append("an event is counted twice: %r" % (events,))
    return problems


def test_every_counter_filters_the_app_log_of_the_deployment_project():
    for stem, doc in _metric_docs().items():
        assert filter_problems(doc) == [], stem


_GOOD = ('logName="projects/%s/logs/%s" AND jsonPayload.event="redirect_rejected"'
         % (OWNER_LITERALS["GCP project id"], APP_LOG_NAME))


@pytest.mark.parametrize("planted, expected", [
    (_GOOD.replace("projects/" + OWNER_LITERALS["GCP project id"],
                   "projects/projet-autre"), "project"),
    (_GOOD.replace("/logs/" + APP_LOG_NAME, "/logs/pallas"), "log"),
    (_GOOD + " AND severity>=WARNING", "is not logName"),
    (_GOOD.replace(" AND ", " "), "is not logName"),
    (_GOOD.split(" AND ")[1], "is not logName"),
    (_GOOD.replace("logName=", "logName:"), "is not logName"),
    (_GOOD.replace('="redirect_rejected"', '=("a" or "b")'), "is not logName"),
    (_GOOD.replace('="redirect_rejected"', '=("a" OR "a")'), "twice"),
])
def test_the_filter_check_catches_a_planted_mutation(planted, expected):
    assert any(expected in p for p in filter_problems({"filter": planted}))


def test_the_filter_parser_reads_one_event_and_several():
    project = OWNER_LITERALS["GCP project id"]
    assert parse_filter(_GOOD) == (project, APP_LOG_NAME, ("redirect_rejected",))
    several = _GOOD.replace('="redirect_rejected"', '=("a_b" OR "c")')
    assert parse_filter(several) == (project, APP_LOG_NAME, ("a_b", "c"))


# ── c. Each counted event is the table's, declared, and EMITTED ──────────
#
# Round 1's inventory test checked declaration and documentation only, and
# passed with both new emitters removed — yet a declared event that nothing
# emits is exactly the defect request_too_large and appspot_blocked carried
# from 2026-04-25 to 2026-11-08. The emitters are found by an AST sweep.

_SKIP_DIRS = {"tests", "__pycache__", ".venv", "venv", "node_modules",
              ".pytest_cache", ".git"}


def emitted_events(root: pathlib.Path) -> set:
    """Every string constant passed as the event — first argument, or
    ``event=`` — to a call of ``log_security_event`` in ``root/**/*.py``,
    ``tests/`` excluded."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            source = pathlib.Path(dirpath, name).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (func.id if isinstance(func, ast.Name)
                          else func.attr if isinstance(func, ast.Attribute)
                          else None)
                if called != "log_security_event":
                    continue
                event = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "event"), None)
                if isinstance(event, ast.Constant) and isinstance(
                        event.value, str):
                    found.add(event.value)
    return found


@functools.lru_cache(maxsize=1)
def _emitted_in_athena() -> frozenset:
    return frozenset(emitted_events(ATHENA))


def event_problems(stem: str, doc: dict, declared: set, emitted: set) -> list:
    parsed = parse_filter(doc.get("filter"))
    if parsed is None:
        return ["the filter does not parse"]
    events = parsed[2]
    problems = []
    entry = BY_METRIC.get(stem)
    if entry is None:
        problems.append("no SECURITY_METRICS entry")
    elif tuple(entry.events) != events:
        problems.append("events %r, SECURITY_METRICS says %r"
                        % (events, tuple(entry.events)))
    for event in events:
        if event not in declared:
            problems.append(event + " is not declared in SecurityEvent")
        if event not in emitted:
            problems.append(event + " has no emitter: it would count zero")
    return problems


def test_every_counted_event_is_the_tables_declared_and_emitted():
    declared = set(get_args(SecurityEvent))
    emitted = _emitted_in_athena()
    for stem, doc in _metric_docs().items():
        assert event_problems(stem, doc, declared, emitted) == [], stem


def test_the_emitter_sweep_reads_calls_not_names_and_skips_the_tests(tmp_path):
    """A planted tree: what counts is a CALL whose event is a constant,
    outside tests/."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text(
        'log_security_event("redirect_rejected", "warning")\n'
        'log_security_event(EVENT, "warning")\n', encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text(
        "from utils import logging_setup\n"
        'logging_setup.log_security_event(event="appcheck_disabled",'
        ' severity="warning")\n'
        "# log_security_event(\"origin_secret_disabled\")\n"
        'NOTE = "log_security_event(\'request_too_large\')"\n',
        encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text(
        'log_security_event("appspot_blocked", "warning")\n',
        encoding="utf-8")
    found = emitted_events(tmp_path)
    assert found == {"redirect_rejected", "appcheck_disabled"}


def test_the_event_check_catches_a_missing_emitter_and_a_planted_mutation(
        tmp_path):
    """The planted tree emits every counted event but appspot_blocked: the
    check names it. Then three planted filters: an event the table does not
    count, one nothing declares, and the table's own events reordered."""
    declared = set(get_args(SecurityEvent))
    counted = {e for m in SECURITY_METRICS for e in m.events}
    (tmp_path / "emitters.py").write_text("".join(
        'log_security_event("%s", "warning")\n' % e
        for e in sorted(counted - {"appspot_blocked"})), encoding="utf-8")
    emitted = emitted_events(tmp_path)
    docs = _metric_docs()
    assert any("appspot_blocked has no emitter" in p for p in event_problems(
        "security_appspot_blocked", docs["security_appspot_blocked"],
        declared, emitted))

    everything = _emitted_in_athena()
    stem = "security_control_disabled"
    for planted, expected in (
        (docs[stem]["filter"].replace('"origin_secret_disabled"',
                                      '"csrf_failure"'), "SECURITY_METRICS says"),
        (docs[stem]["filter"].replace('"origin_secret_disabled"',
                                      '"made_up_event"'), "not declared"),
        (docs[stem]["filter"].replace(
            '"appcheck_disabled" OR "origin_secret_disabled"',
            '"origin_secret_disabled" OR "appcheck_disabled"'),
         "SECURITY_METRICS says"),
    ):
        problems = event_problems(stem, {"filter": planted}, declared,
                                  everything)
        assert any(expected in p for p in problems), (planted, problems)


# ── d. Each policy fires on ONE event and says where to read ─────────────


def deployment_references(text: str) -> list:
    """Every DEPLOYMENT.md section a text names: ``("§", "6.10")`` for
    « DEPLOYMENT.md §6.10 », ``("«»", "Alertes de sécurité")`` for
    « DEPLOYMENT.md, « Alertes de sécurité » »."""
    marker = "DEPLOYMENT.md"
    references = []
    i = text.find(marker)
    while i >= 0:
        rest = text[i + len(marker):]
        if rest.startswith(" §"):
            j = 2
            while j < len(rest) and (rest[j].isdigit() or rest[j] == "."):
                j += 1
            references.append(("§", rest[2:j].rstrip(".")))
        elif rest.startswith(", « "):
            end = rest.find(" »")
            if end > 4:
                references.append(("«»", rest[4:end]))
        i = text.find(marker, i + len(marker))
    return references


def reference_exists(reference: tuple, sections: tuple) -> bool:
    numbers, titles = sections
    kind, value = reference
    if kind == "§":
        return bool(value) and value in numbers
    return any(title.startswith(value) for title in titles)


# What a condition, and its threshold, may hold: a whitelist, because a key
# the checks below do not read can still change what fires — a
# `denominatorFilter` makes the count a ratio over another metric (no value
# while that one is zero), `forecastOptions` a forecast.
CONDITION_KEYS = {"displayName", "conditionThreshold"}
THRESHOLD_KEYS = {"filter", "comparison", "thresholdValue", "duration",
                  "trigger", "aggregations"}


def policy_problems(stem: str, doc: dict, metric_stems: set,
                    sections: tuple = _DEPLOYMENT_SECTIONS) -> list:
    problems = []
    if stem not in metric_stems:
        problems.append("names no metric file")
    expected = ('metric.type="logging.googleapis.com/user/%s" AND '
                'resource.type="%s"' % (stem, ALERT_RESOURCE_TYPE))
    conditions = doc.get("conditions") or []
    if not conditions:
        problems.append("no condition")
    for i, condition in enumerate(conditions):
        at = "conditions[%d]: " % i
        for key in sorted(set(condition) - CONDITION_KEYS):
            problems.append(at + key + " is not an allowed key")
        threshold = condition.get("conditionThreshold")
        if not isinstance(threshold, dict):
            problems.append(at + "not a threshold condition")
            continue
        for key in sorted(set(threshold) - THRESHOLD_KEYS):
            problems.append(at + "conditionThreshold." + key
                            + " is not an allowed key")
        if threshold.get("filter") != expected:
            problems.append(at + "filter %r, expected %r"
                            % (threshold.get("filter"), expected))
        if threshold.get("comparison") != "COMPARISON_GT":
            problems.append(at + "comparison is not COMPARISON_GT")
        # The API omits a zero threshold when it echoes a policy back.
        if threshold.get("thresholdValue", 0) != 0:
            problems.append(at + "threshold is not 0")
        if threshold.get("duration") != "0s":
            problems.append(at + "duration is not 0s")
        if threshold.get("trigger") != {"count": 1}:
            problems.append(at + "trigger is not a count of 1")
        if threshold.get("aggregations") != [{
                "alignmentPeriod": ALERT_WINDOW,
                "perSeriesAligner": "ALIGN_SUM",
                "crossSeriesReducer": "REDUCE_SUM"}]:
            problems.append(at + "aggregations are not ALIGN_SUM over %s "
                            "then REDUCE_SUM" % ALERT_WINDOW)
    if doc.get("enabled") is not True:
        problems.append("not enabled")
    if (doc.get("alertStrategy") or {}).get("autoClose") != ALERT_AUTO_CLOSE:
        problems.append("autoClose is not %s" % ALERT_AUTO_CLOSE)
    entry = BY_METRIC.get(stem)
    if entry is None or doc.get("displayName") != entry.policy:
        problems.append("displayName %r is not the table's %r"
                        % (doc.get("displayName"),
                           entry.policy if entry else None))
    documentation = doc.get("documentation") or {}
    if documentation.get("mimeType") != "text/markdown":
        problems.append("documentation is not text/markdown")
    references = deployment_references(documentation.get("content") or "")
    if not references:
        problems.append("documentation names no DEPLOYMENT.md section")
    for reference in references:
        if not reference_exists(reference, sections):
            problems.append("documentation names a DEPLOYMENT.md section "
                            "that does not exist: %r" % (reference,))
    return problems


def test_every_policy_fires_on_one_event_and_names_an_existing_section():
    metric_stems = set(_metric_docs())
    docs = _policy_docs()
    assert docs
    for stem, doc in docs.items():
        assert policy_problems(stem, doc, metric_stems) == [], stem


def test_the_policy_texts_name_the_sections_they_mean():
    """Not vacuous: production's texts name §4, §6.4 (the secrets' access
    and rotation), §7 and « Alertes de sécurité » (§6.10's title, which
    therefore must keep that wording)."""
    found = set()
    for doc in _policy_docs().values():
        found |= set(deployment_references(doc["documentation"]["content"]))
    assert found >= {("§", "4"), ("§", "6.4"), ("§", "7"),
                     ("«»", "Alertes de sécurité")}


def _threshold(d: dict) -> dict:
    return d["conditions"][0]["conditionThreshold"]


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: _threshold(d).update(comparison="COMPARISON_GE"),
     "COMPARISON_GT"),
    (lambda d: _threshold(d).update(thresholdValue=1), "threshold"),
    (lambda d: _threshold(d).update(duration="60s"), "duration"),
    (lambda d: _threshold(d).update(trigger={"count": 2}), "trigger"),
    (lambda d: _threshold(d)["aggregations"][0].update(
        perSeriesAligner="ALIGN_RATE"), "aggregations"),
    (lambda d: _threshold(d)["aggregations"][0].update(
        crossSeriesReducer="REDUCE_COUNT"), "aggregations"),
    (lambda d: _threshold(d)["aggregations"][0].update(
        alignmentPeriod="60s"), "aggregations"),
    (lambda d: _threshold(d).update(filter=_threshold(d)["filter"].replace(
        ' AND resource.type="gae_app"', "")), "filter"),
    (lambda d: _threshold(d).update(filter=_threshold(d)["filter"].replace(
        "security_redirect_rejected", "security_request_too_large")),
     "filter"),
    (lambda d: d["conditions"][0].pop("conditionThreshold"),
     "not a threshold"),
    (lambda d: _threshold(d).update(
        denominatorFilter='metric.type="logging.googleapis.com/user/x"'),
     "conditionThreshold.denominatorFilter is not an allowed key"),
    (lambda d: _threshold(d).update(forecastOptions={
        "forecastHorizon": "3600s"}),
     "conditionThreshold.forecastOptions is not an allowed key"),
    (lambda d: _threshold(d).update(
        evaluationMissingData="EVALUATION_MISSING_DATA_INACTIVE"),
     "conditionThreshold.evaluationMissingData is not an allowed key"),
    (lambda d: d["conditions"][0].update(conditionAbsent={
        "filter": "x", "duration": "300s"}),
     "conditionAbsent is not an allowed key"),
    (lambda d: d.update(conditions=[]), "no condition"),
    (lambda d: d.update(enabled=False), "not enabled"),
    (lambda d: d.pop("alertStrategy"), "autoClose"),
    (lambda d: d.update(displayName="Sécurité — autre chose"), "displayName"),
    (lambda d: d["documentation"].update(mimeType="text/plain"),
     "text/markdown"),
    (lambda d: d["documentation"].update(content=d["documentation"][
        "content"].replace("« Alertes de sécurité »", "« Alertes perdues »")),
     "does not exist"),
    (lambda d: d["documentation"].update(content="Voir la documentation."),
     "names no DEPLOYMENT.md section"),
])
def test_the_policy_check_catches_a_planted_mutation(mutate, expected):
    stem = "security_redirect_rejected"
    planted = copy.deepcopy(_policy_docs()[stem])
    mutate(planted)
    problems = policy_problems(stem, planted, set(_metric_docs()))
    assert any(expected in p for p in problems), problems


def test_the_section_check_catches_a_missing_number_and_ignores_code():
    sections = _section_titles(
        "## 4. Configuration\n```bash\n# 7. not a heading\n```\n"
        "### 6.10 Alertes de sécurité (Cloud Monitoring)\n")
    assert reference_exists(("§", "4"), sections)
    assert reference_exists(("§", "6.10"), sections)
    assert reference_exists(("«»", "Alertes de sécurité"), sections)
    assert not reference_exists(("§", "7"), sections)
    assert not reference_exists(("«»", "Cloud Monitoring"), sections)
    stem = "security_appspot_blocked"
    planted = copy.deepcopy(_policy_docs()[stem])
    metric_stems = set(_metric_docs())
    problems = policy_problems(stem, planted, metric_stems, sections)
    assert any("('§', '7')" in p for p in problems), problems
    problems = policy_problems("security_unknown", planted, metric_stems)
    assert any("names no metric file" in p for p in problems), problems


# ── e. The counters without an alert are the table's ─────────────────────


def alert_set_problems(metric_stems: set, policy_stems: set) -> list:
    problems = []
    table = {m.metric for m in SECURITY_METRICS}
    with_policy = {m.metric for m in SECURITY_METRICS if m.policy}
    if metric_stems != table:
        problems.append("metric files %s, SECURITY_METRICS %s"
                        % (sorted(metric_stems), sorted(table)))
    if policy_stems != with_policy:
        problems.append("policy files %s, SECURITY_METRICS with a policy %s"
                        % (sorted(policy_stems), sorted(with_policy)))
    if metric_stems - policy_stems != table - with_policy:
        problems.append("counters without an alert %s, the table's %s"
                        % (sorted(metric_stems - policy_stems),
                           sorted(table - with_policy)))
    return problems


def test_the_counters_without_an_alert_are_exactly_the_tables():
    metric_stems, policy_stems = set(_metric_docs()), set(_policy_docs())
    assert alert_set_problems(metric_stems, policy_stems) == []
    assert metric_stems - policy_stems == {"security_request_too_large"}
    for m in SECURITY_METRICS:
        assert bool(m.policy) != bool(m.no_alert_reason), m.metric


@pytest.mark.parametrize("planted", [
    lambda metrics, policies: (metrics, policies | {
        "security_request_too_large"}),
    lambda metrics, policies: (metrics, policies - {
        "security_appspot_blocked"}),
    lambda metrics, policies: (metrics - {"security_request_too_large"},
                               policies),
    lambda metrics, policies: (metrics | {"security_new_counter"}, policies),
])
def test_the_alert_set_check_catches_a_planted_mutation(planted):
    metric_stems, policy_stems = planted(set(_metric_docs()),
                                         set(_policy_docs()))
    assert alert_set_problems(metric_stems, policy_stems) != []
