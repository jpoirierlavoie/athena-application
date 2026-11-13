"""The app's bytecode is compiled by Cloud Build, and it ships (2026-11-12).

App Engine's disk is read-only: with no bytecode in the upload, every worker
recompiled the app's own source at every start. ``cloudbuild.yaml`` compiles
it — checked-hash, once the tests pass — and ``.gcloudignore`` lets it ship:
the one exception to CLAUDE.md's « no deploy-time build », by the lawyer's
decision. Either half alone is useless, so both are pinned.
"""

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def _test_step() -> list[str]:
    """The lines of cloudbuild.yaml's first step, the one that runs the tests."""
    lines = (REPO / "cloudbuild.yaml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if "Step 1" in l)
    end = next(i for i, l in enumerate(lines) if "Step 2" in l)
    return [l.strip() for l in lines[start:end]]


def test_cloud_build_compiles_the_bytecode_checked_hash_once_the_tests_pass():
    step = _test_step()
    tests = next(i for i, l in enumerate(step) if "python -m pytest tests/" in l)
    assert step[tests].startswith("cd athena && ")
    compile_lines = [i for i, l in enumerate(step) if l.startswith("python -m compileall")]
    assert len(compile_lines) == 1
    line = step[compile_lines[0]]
    assert compile_lines[0] > tests                   # never before the tests
    assert "--invalidation-mode checked-hash" in line  # never timestamp-based
    assert " -f " in line and "-x '/tests/'" in line and line.endswith(" .")
    assert "set -euo pipefail" in step                # a failed compile fails the build


def test_the_upload_lets_the_bytecode_ship_and_still_leaves_the_tests_out():
    rules = [l.strip() for l in (REPO / "athena" / ".gcloudignore")
             .read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert "__pycache__/" not in rules and "*.pyc" not in rules
    assert "tests/" in rules and ".env" in rules


def test_claude_md_names_the_exception():
    first = (REPO / "CLAUDE.md").read_text(encoding="utf-8").split("\n\n")[1]
    assert "no deploy-time build — save the Python bytecode" in first


def test_cloudbuild_substitutes_short_sha_rather_than_escaping_it_to_bash():
    """$SHORT_SHA is a Cloud Build substitution; $$SHORT_SHA passes literal $SHORT_SHA to bash -u."""
    text = (REPO / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "$$SHORT_SHA" not in text
    assert "$SHORT_SHA" in text


def test_cloudbuild_parallelizes_staging_and_tests():
    text = (REPO / "cloudbuild.yaml").read_text(encoding="utf-8")
    assert "-n auto --dist loadfile" in text
    assert "machineType: 'E2_HIGHCPU_8'" in text
    assert text.count("waitFor: ['test-suite']") == 2
    assert "waitFor: ['stage-default']" not in text
