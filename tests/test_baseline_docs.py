"""The baseline documents: do they ever say more than their source?

These are renderings of measurements taken elsewhere, so the failure mode is
not a wrong number -- it is a number with no source, or a question answered
that nothing here can answer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "baseline_docs_probe", ROOT / "tools" / "baseline_docs.py")
bd = importlib.util.module_from_spec(_spec)
sys.modules["baseline_docs_probe"] = bd
_spec.loader.exec_module(bd)

from conftest import PROGRAM_REGISTER, needs_evidence  # noqa: E402


def test_the_cli_surface_is_read_from_the_parser_not_from_a_document():
    """A command list in a README is a claim about software. This one is the
    software: if a command is removed, the baseline stops naming it without
    anybody editing a file."""
    surface = bd.cli_surface()
    assert surface["source"].startswith("python3 -m hoh.cli"), surface
    assert {"run", "start", "status", "project"} <= set(surface["commands"]), surface


def test_the_unanswerable_questions_are_marked_and_not_estimated():
    """Time to first verified result depends on a provider, a machine and a
    person. A product baseline that guessed it would publish a number about
    this laptop as a number about the product."""
    needs_evidence(PROGRAM_REGISTER)
    text = bd.product_baseline(bd.measure())
    for question in ("time to first run", "time to first verified result",
                     "manual interventions per run"):
        row = [line for line in text.splitlines()
               if line.startswith("|") and question in line]
        assert row, question
        assert bd.NOT_MEASURED in row[0], row[0]
        # and the reason is in the same row, not left to the reader
        assert "depends on" in row[0] or "campaign" in row[0], row[0]


def test_every_figure_in_the_current_baseline_names_a_source():
    needs_evidence(PROGRAM_REGISTER)
    text = bd.current_baseline(bd.measure())
    figures = [line for line in text.splitlines()
               if line.startswith("* ") and any(ch.isdigit() for ch in line)]
    assert figures, "no figures rendered at all"
    for line in figures:
        assert "`" in line, f"a figure with no source: {line}"


def test_the_configuration_count_is_the_variables_it_lists():
    """A count and a list that disagree is the shape this project keeps
    finding: the number is quoted and the list is what is true."""
    cfg = bd.configuration_decisions()
    assert cfg["count"] == len(cfg["variables"])
    assert "VERIHARNESS_EXPORT_CHECKOUT" in cfg["variables"], cfg["variables"]
    for name, files in cfg["variables"].items():
        assert files and all(f.endswith(".py") for f in files), name


def test_the_contract_trace_has_a_row_for_every_requirement():
    needs_evidence(PROGRAM_REGISTER)
    body = bd.measure()
    text = bd.contract_trace(body)
    for cap in body["capabilities"]:
        assert f"`{cap['id']}`" in text, cap["id"]


def test_a_board_that_is_not_there_is_not_a_board_that_passed():
    """The rendering reads the board, and an absent board must not come out as
    a baseline with no red rows."""
    rows = bd.board_rows()
    assert rows["source"], rows
    if not (ROOT / "docs/READINESS.md").is_file():
        assert rows["source"] == bd.NOT_MEASURED
    else:
        assert rows["rows"], "a board with no rows read is a parser that broke"


# --- the five measurements V3.3 P0 asks for -----------------------------------

import json  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402

_CITE = re.compile(r"[\w./@\-]+\.\w+:\d+")


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path, omit=()) -> Path:
    """A miniature repository. Line numbers are part of the contract: the
    tests below quote them. A partial tree is built by leaving files out."""
    def put(rel: str, text: str) -> None:
        if rel not in omit:
            _put(root, rel, text)

    put("pyproject.toml", "\n".join([
        "[project]", 'name = "demo"', 'requires-python = ">=3.11"',
        'dependencies = ["pydantic>=2.0"]', "",
        "[project.optional-dependencies]", 'test = ["pytest>=7"]', "",
        "[project.scripts]", 'hoh = "hoh.cli:main"', "",
        "[build-system]", 'build-backend = "setuptools.build_meta"', ""]))
    put("README.md", "\n".join(
        ["# demo", "", "Install:", "", "```", "pip install hoh", "```", "", "",
         "python -m pip install -e .", ""]))
    put("docs/OPERATIONS.md", "\n".join(
        ["# ops", "", "```", "python3 -m pip install -e .", "```", ""]))
    put("tools/clean_install_check.py", "\n".join(
        ['"""Check.', "", "In order:", "", "  1. build a wheel",
         "  2. install it into a fresh venv", '"""', ""]))
    put("dogfood/external-ci/EXACT_HEAD_CI.json", json.dumps({
        "run_id": 77, "measured_at": "2026-09-23T03:20:57Z",
        "jobs": [{"name": "clean install from a built wheel",
                  "conclusion": "success"}]}, indent=1))
    put(".github/releases/v0.1.0.json", json.dumps({
        "testpypi_result": "PASS", "production_pypi_result": "PASS",
        "files": [{"published_at": "2026-09-15T17:45:19Z"}]}, indent=1))
    put("src/hoh/contracts.py", "\n".join(
        ["from enum import StrEnum", "", "", "class Role(StrEnum):",
         "    PLANNER = 'planner'", "    QA = 'qa'", ""]))
    put("src/hoh/dispatchers.py", "\n".join(
        ["# dispatchers", "", "class _Base:", "    pass",
         "class HerdrDispatcher(_Base):", "    pass", "", "",
         "def build_dispatcher():", "    return HerdrDispatcher()", ""]))
    put("src/hoh/runner.py", "\n".join(
        ["import subprocess", "import threading", "import fcntl", "", "",
         "def go(): pass",
         "subprocess.run(['x'])",
         "subprocess.Popen(['y'])",
         "fcntl.flock(0, 0)",
         "threading.Thread(target=go, name='collector', daemon=True)",
         "threading.Thread(target=go)",
         "injected = go or subprocess.run",
         "threading.Thread(target=go, daemon=flag)", ""]))
    put("src/hoh/orchestrator.py", "\n".join(
        ["NEEDS_A_HUMAN = frozenset({HaltClass.MERGE_CONFLICT, "
         "HaltClass.AMBIGUOUS})", "", "", "class Health:",
         "    WAITING_FOR_DECISION = 'w'", "    READY = 'r'", ""]))
    put("src/hoh/cli.py", "\n".join(
        ["import argparse", "sub.add_parser('unblock', help='x')",
         "sub.add_argument('--by', help=\"the captain's explicit approval\")",
         "sub.add_argument('--n', help='a number')", "", "",
         "def f(human_owner):", "    pass", ""]))
    put("ops/hoh-a.timer", "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=15min\n")
    put("ops/hoh-a.service",
        "[Service]\nType=oneshot\nExecStart=%h/bin/hoh resume-quota\n")
    put("ops/hoh-b@.timer", "[Timer]\nOnUnitActiveSec=30min\n")
    put("ops/hoh-b@.service", "[Service]\nExecStart=%h/bin/hoh storage\n")
    put("plugin/herdr-plugin.toml", "\n".join(
        ['id = "hoh"', "", "[[actions]]", 'id = "list"',
         'command = ["./bin/hoh", "list"]', "", "[[actions]]",
         'id = "status"', 'command = ["./bin/hoh-latest", "status"]', ""]))
    put("DOGFOOD_LEDGER.md", "\n".join(
        ["# Ledger -- run x (2026-09-07)", "", "### O5 -- first (2026-09-08)",
         "", "### O9 -- second (2026-09-30)", ""]))
    run = "dogfood/{}/{}/run-evidence/results/{}"
    put(run.format("alpha", "20260922T1340Z", "plan-i1-a1.json"), "{}")
    put("dogfood/alpha/20260923T0000Z/notes.txt", "no run-evidence here")
    for name in ("plan-i1-a1", "plan-i2-a1", "plan-i3-a1", "plan-i1-a2"):
        put(run.format("beta", "20261001T1000Z", name + ".json"), "{}")
    (root / "dogfood/alpha/notes").mkdir(parents=True, exist_ok=True)
    put("dogfood/ATTRIBUTION.json", json.dumps({"entries": [
        {"id": "a", "category": "MAIN_ORCHESTRATOR_DIRECT",
         "is_development_node": True},
        {"id": "b", "category": "MAIN_ORCHESTRATOR_DIRECT",
         "is_development_node": True},
        {"id": "c", "category": "VERIHARNESS_RUN",
         "is_development_node": False}]}))
    return root


def _by_text(result: dict) -> dict:
    return {text: src for text, src in result["rows"]}


def _row(result: dict, start: str):
    hits = [(t, s) for t, s in result["rows"] if t.startswith(start)]
    assert len(hits) == 1, (start, hits)
    return hits[0]


def _cited_lines_exist(root: Path, rows) -> None:
    for text, src in rows:
        for cited in _CITE.findall(src):
            path, _, n = cited.rpartition(":")
            lines = (root / path).read_text(encoding="utf-8").splitlines()
            assert 1 <= int(n) <= len(lines) and lines[int(n) - 1].strip(), \
                (text, cited)


def _body(root: Path, cli=None) -> dict:
    return {
        "measured_at_utc": "t",
        "cli": cli or {"commands": ["amend", "start"],
                       "groups": {"amend": ["clarify", "correct"]},
                       "source": "python3 -m hoh.cli --help"},
        "package": {"modules": ["a.py"], "classes": 1, "functions": 1,
                    "source": "ast over src/hoh/*.py"},
        "manifest": {"entries": 2, "included": 1, "source": "EXPORT_MANIFEST.json"},
        "matrix_counts": {}, "matrix_source": "register",
        "install": bd.fresh_install_path(root),
        "runtime": bd.runtime_model(root),
        "human": bd.human_intervention_points(root),
        "dogfood": bd.dogfood_evidence(root),
    }


def test_the_install_path_is_derived_offline_and_each_figure_cites_its_line(
        tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the install path must be derived offline")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    root = _tree(tmp_path)
    got = bd.fresh_install_path(root)
    rows = _by_text(got)
    assert rows["python >=3.11"] == "pyproject.toml:3"
    assert rows["runtime dependencies: pydantic>=2.0"] == "pyproject.toml:4"
    assert rows["optional extra `test`: pytest>=7"] == "pyproject.toml:7"
    assert rows["console script `hoh` -> `hoh.cli:main`"] == "pyproject.toml:10"
    assert rows["build backend: setuptools.build_meta"] == "pyproject.toml:13"
    assert rows["documented command `pip install hoh`"] == "README.md:6"
    assert rows["documented command `python -m pip install -e .`"] == "README.md:10"
    assert rows["documented command `python3 -m pip install -e .`"] \
        == "docs/OPERATIONS.md:4"
    assert rows["clean-install check step 2: install it into a fresh venv"] \
        == "tools/clean_install_check.py:6"
    assert got["limits"] == []
    _cited_lines_exist(root, got["rows"])


def test_recorded_clean_install_evidence_carries_its_own_date_and_is_not_invented(
        tmp_path):
    got = bd.fresh_install_path(_tree(tmp_path / "full"))
    rec = dict(got["recorded"])
    ci = ("CI job `clean install from a built wheel`: success, run 77, "
          "recorded 2026-09-23T03:20:57Z")
    assert rec[ci] == "dogfood/external-ci/EXACT_HEAD_CI.json:6"
    assert any(t == "v0.1.0 receipt: TestPyPI PASS, PyPI PASS, published "
               "2026-09-15" for t in rec), rec
    # no job, no receipt: nothing is defaulted to "success"
    bare = _tree(tmp_path / "bare", omit=(
        "dogfood/external-ci/EXACT_HEAD_CI.json",
        ".github/releases/v0.1.0.json"))
    _put(bare, "dogfood/external-ci/EXACT_HEAD_CI.json",
         json.dumps({"run_id": 1, "measured_at": "x", "jobs": []}))
    out = bd.fresh_install_path(bare)
    assert out["recorded"] == []
    assert any("no recorded clean-install evidence" in t
               for t, _ in out["limits"]), out["limits"]


def test_the_runtime_model_is_read_from_ops_plugin_and_src(tmp_path):
    root = _tree(tmp_path)
    got = bd.runtime_model(root)
    rows = _by_text(got)
    a = "systemd timer `hoh-a` every 15min runs `%h/bin/hoh resume-quota` (`Type=oneshot`)"
    assert rows[a] == "ops/hoh-a.timer:3; ops/hoh-a.service:3; ops/hoh-a.service:2"
    b = "systemd timer `hoh-b@` every 30min runs `%h/bin/hoh storage`"
    assert rows[b] == "ops/hoh-b@.timer:2; ops/hoh-b@.service:2"
    for text, _ in got["rows"]:
        assert "one-shot" not in text and "argument-free" not in text, text
    assert rows["Herdr plugin action `list` runs `./bin/hoh list`"] \
        == "plugin/herdr-plugin.toml:4"
    assert rows["Herdr plugin action `status` runs `./bin/hoh-latest status`"] \
        == "plugin/herdr-plugin.toml:8"
    assert rows["entry point `hoh` -> `hoh.cli:main`"] == "pyproject.toml:10"
    assert rows["roles: planner, qa"] == "src/hoh/contracts.py:4"
    assert rows["role dispatcher `HerdrDispatcher`"] == "src/hoh/dispatchers.py:5"
    assert rows["dispatcher chosen by `build_dispatcher`"] \
        == "src/hoh/dispatchers.py:9"
    assert rows["daemon thread `collector` inside the calling process"] \
        == "src/hoh/runner.py:10"
    assert rows["thread inside the calling process"] == "src/hoh/runner.py:11"
    assert rows["thread (daemon argument is not a literal) inside the calling "
                "process"] == "src/hoh/runner.py:13"
    text, src = _row(got, "child-process references")
    assert text.endswith("called or passed on): 3 in 1 module(s) "
                         "(first: `src/hoh/runner.py`)"), text
    assert src == "src/hoh/runner.py:7"
    assert _row(got, "file-lock references") == (
        "file-lock references: 1 in 1 module(s)", "src/hoh/runner.py:9")
    assert rows["modules parsed: 5"] == "ast over src/hoh/*.py"
    _cited_lines_exist(root, got["rows"])


def test_resident_listeners_and_forks_are_counted_through_dotted_names_and_imports(
        tmp_path):
    root = _tree(tmp_path)
    cases = [
        ("x = 1\n", 0),
        ("import socketserver\nsocketserver.TCPServer(a, b)\n", 1),
        ("import http.server\nhttp.server.HTTPServer(a, b)\n", 1),
        ("from socket import socket\nsocket()\n", 1),
        ("from http import server\nserver.HTTPServer(a, b)\n", 1),
        ("import os as o\no.fork()\n", 1),
        ("import socket\nsocket.socket()\n", 1),
        ("class mysocket: ...\nmysocket.socket()\n", 0),
        # an alias made by assignment is not followed -- and the row says so
        ("import socket\ns = socket.socket\ns()\n", 1),
    ]
    for source, want in cases:
        _put(root, "src/hoh/serve.py", source)
        text, _ = _row(bd.runtime_model(root), "resident listener or fork")
        assert text.endswith(f": {want}"), (source, text)
        assert "not followed" in text
    _put(root, "src/hoh/serve.py", "\nimport socket\nsocket.socket()\n")
    assert _row(bd.runtime_model(root), "resident listener or fork")[1] \
        == "src/hoh/serve.py:3"


def test_the_human_points_are_anchored_to_the_lines_that_name_them(tmp_path):
    root = _tree(tmp_path)
    got = bd.human_intervention_points(root)
    rows = _by_text(got)
    assert rows["`NEEDS_A_HUMAN` = {MERGE_CONFLICT, AMBIGUOUS}"] \
        == "src/hoh/orchestrator.py:1"
    assert rows["`Health.WAITING_FOR_DECISION`"] == "src/hoh/orchestrator.py:5"
    assert rows["CLI subcommand `unblock`"] == "src/hoh/cli.py:2"
    assert rows["parameter `human_owner`"] == "src/hoh/cli.py:7"
    by = [t for t in rows if t.startswith("CLI argument `--by`")]
    assert len(by) == 1 and rows[by[0]] == "src/hoh/cli.py:3", rows
    assert not any("--n" in t or "READY" in t for t in rows), rows
    assert rows["modules searched: 5"] == "ast over src/hoh/*.py"
    for text, src in got["rows"]:
        m = re.fullmatch(r"(.+\.py):(\d+)", src)
        if not m:
            continue
        line = (root / m.group(1)).read_text().splitlines()[int(m.group(2)) - 1]
        quoted = re.findall(r"`([^`]+)`", text)[0].split(".")[-1].split()[0]
        assert quoted in line, (text, line)


def test_the_dogfood_figures_are_counted_from_the_tree(tmp_path):
    got = bd.dogfood_evidence(_tree(tmp_path))
    rows = _by_text(got)
    assert ("3 run directories in 2 strand(s); 2 with run-evidence; 5 "
            "role-result files") in rows
    assert ("stamps 20260922T1340Z (`alpha`) to 20261001T1000Z (`beta`): 9 "
            "calendar day(s) between first and last") in rows
    assert ("ledger headings are dated 2026-09-07 to 2026-09-30: 23 calendar "
            "day(s)") in rows
    assert ("deepest run: 3 iteration(s) (`beta/20261001T1000Z`); 1 run(s) "
            "with two or more") in rows            # the -a2 attempt adds none
    assert "2 ledger entries O5 to O9" in rows
    assert ("3 attribution entries, 2 development node(s): "
            "MAIN_ORCHESTRATOR_DIRECT 2, VERIHARNESS_RUN 1") in rows
    assert not any("impossible date" in t for t, _ in got["limits"]), got["limits"]
    assert any("`runs/`" in t for t, _ in got["limits"])


def test_a_tree_without_its_sources_is_not_measured_rather_than_zero(tmp_path):
    for fn in (bd.fresh_install_path, bd.runtime_model,
               bd.human_intervention_points, bd.dogfood_evidence):
        got = fn(tmp_path)
        assert got["source"] == bd.NOT_MEASURED, fn
        assert got["rows"] == [] and got["why"], got
    text = "\n".join(bd.measurement_sections({"cli": {"commands": []},
                                              "install": bd.fresh_install_path(tmp_path),
                                              "runtime": bd.runtime_model(tmp_path),
                                              "human": bd.human_intervention_points(tmp_path),
                                              "dogfood": bd.dogfood_evidence(tmp_path)}))
    assert "* NOT_MEASURED" in text
    for title in bd.MEASUREMENT_SECTIONS.values():
        assert bd.section_status(text, title, tmp_path)[0] is False, title


def test_malformed_dogfood_inputs_are_skipped_and_counted_never_raised(tmp_path):
    root = _tree(tmp_path / "t")
    _put(root, "DOGFOOD_LEDGER.md", (root / "DOGFOOD_LEDGER.md").read_text()
         + "\n### O10 -- bad (2026-02-30)\n")
    _put(root, "dogfood/alpha/20261399T0000Z/run-evidence/results/x-i1-a1.json", "{}")
    _put(root, "dogfood/ATTRIBUTION.json", json.dumps({"entries": ["a"]}))
    got = bd.dogfood_evidence(root)
    assert got["source"] != bd.NOT_MEASURED
    limits = " | ".join(t for t, _ in got["limits"])
    assert "1 run director(ies) with an impossible date" in limits
    assert "1 ledger heading(s) with an impossible date" in limits
    assert "ATTRIBUTION.json is missing or malformed" in limits
    assert not any("20261399" in t for t, _ in got["rows"])
    only = tmp_path / "only"
    _put(only, "DOGFOOD_LEDGER.md", "# x\n")
    _put(only, "dogfood/alpha/20261399T0000Z/run-evidence/a.json", "{}")
    assert bd.dogfood_evidence(only)["source"] == bd.NOT_MEASURED


def test_a_partial_result_prints_its_limits_or_is_not_measured(tmp_path):
    trivial = tmp_path / "trivial"
    _put(trivial, "src/hoh/x.py", "x = 1\n")
    assert bd.runtime_model(trivial)["source"] == bd.NOT_MEASURED
    assert bd.human_intervention_points(trivial)["source"] == bd.NOT_MEASURED
    broken = tmp_path / "broken"
    _put(broken, "src/hoh/x.py", "def (:\n")
    assert bd.runtime_model(broken)["source"] == bd.NOT_MEASURED
    # a ledger and an empty dogfood/, and runs without any ledger
    empty = tmp_path / "empty"
    _put(empty, "DOGFOOD_LEDGER.md", "# x\n")
    (empty / "dogfood").mkdir()
    assert bd.dogfood_evidence(empty)["source"] == bd.NOT_MEASURED
    noledger = _tree(tmp_path / "noledger", omit=("DOGFOOD_LEDGER.md",))
    assert bd.dogfood_evidence(noledger)["source"] == bd.NOT_MEASURED

    root = _tree(tmp_path / "partial", omit=(
        "README.md", "docs/OPERATIONS.md", "tools/clean_install_check.py",
        "dogfood/external-ci/EXACT_HEAD_CI.json",
        ".github/releases/v0.1.0.json", "dogfood/ATTRIBUTION.json",
        "ops/hoh-a.timer", "ops/hoh-b@.timer"))
    _put(root, "src/hoh/broken.py", "def (:\n")
    body = _body(root)
    inst, run, dog = body["install"], body["runtime"], body["dogfood"]
    assert inst["source"] != bd.NOT_MEASURED and len(inst["limits"]) == 3
    assert any("1 module(s) of src/hoh could not be parsed" in t
               for t, _ in run["limits"]), run["limits"]
    assert any("no systemd timer" in t for t, _ in run["limits"])
    assert any("ATTRIBUTION.json" in t for t, _ in dog["limits"])
    assert any("`runs/`" in t for t, _ in dog["limits"])
    text = "\n".join(bd.measurement_sections(body))
    assert "* limit: " in text
    assert bd.section_status(text, "Fresh install path", root)[0] is True


def test_a_section_is_measured_only_when_it_reads_back_as_such(tmp_path):
    _put(tmp_path, "a.txt", "one\ntwo\n")
    _put(tmp_path, "ops/u@.timer", "x\ny\nz\n")

    def check(body: str, title: str = "A", extra: str = ""):
        return bd.section_status(f"# top\n\n## A\n\n{body}\n\n{extra}", title,
                                 tmp_path)

    assert check("* a figure — `a.txt:2`") == (True, "1 sourced figure(s)")
    assert bd.section_status(None, "A", tmp_path) == (
        False, "VERIHARNESS_CURRENT_BASELINE.md is missing")
    assert check("* f — `a.txt:1`", "Z") == (False, "no section `Z`")
    assert check("* NOT_MEASURED — why") == (
        False, "the section reports NOT_MEASURED")
    assert check("* NOT_MEASURED — why\n* f — `a.txt:1`")[0] is False
    assert check("* a figure with no source") == (
        False, "the section has no figure with a source")
    # an empty section followed by a section with figures
    doc = "## A\n\n## B\n\n* f — `a.txt:1`\n"
    assert bd.section_status(doc, "A", tmp_path)[0] is False
    assert bd.section_status(doc, "B", tmp_path)[0] is True
    # limits are not figures
    assert check("* limit: l — `a.txt:1`")[0] is False
    assert check("* limit: l — `a.txt:1`\n* f — `a.txt:2`")[0] is True
    for bad in ("a.txt:3", "gone.txt:1", "a.txt:0"):
        assert check(f"* f — `{bad}`")[0] is False, bad
    for good in ("a.txt:1; a.txt:2", "a.txt:1 to a.txt:2", "ops/u@.timer:3"):
        assert check(f"* f — `{good}`")[0] is True, good
    for bad in ("a.txt:1; a.txt:9", "gone.txt:5; a.txt:1", "a.txt:1 to a.txt:99",
                "a.txt:1; a.txt:2; a.txt:3"):
        assert check(f"* f — `{bad}`")[0] is False, bad
    assert check("* f — `a.txt:1; ops/u@.timer:4`") == (
        False, "cited line ops/u@.timer:4 does not exist")
    # a limit's citation is checked as well
    assert check("* f — `a.txt:1`\n* limit: l — `a.txt:7`")[0] is False
    # the only end rule: the next line starting with `#`, at any level
    assert bd.section_status("## A\n### sub\n* f — `a.txt:1`\n", "A",
                             tmp_path)[0] is False
    # structural, on purpose: the value is not checked
    assert check("* 9999 widgets — `a.txt:1`")[0] is True


def test_the_five_sections_are_exactly_the_plans_measure_items():
    needs_evidence(PROGRAM_REGISTER)
    scope = json.loads((ROOT / "program/v3_3/AUTHORITATIVE_PROGRAM_SCOPE.json")
                       .read_text())
    ids = {i["id"] for i in scope["items"]
           if i["phase"] == "P0" and i["kind"] == "measurement"}
    assert len(ids) == 5 and set(bd.MEASUREMENT_SECTIONS) == ids


def test_the_rendered_current_baseline_reads_back_measured_for_every_item(tmp_path):
    root = _tree(tmp_path)
    text = bd.current_baseline(_body(root))
    for title in bd.MEASUREMENT_SECTIONS.values():
        assert f"\n## {title}\n" in text, title
        assert bd.section_status(text, title, root)[0] is True, \
            (title, bd.section_status(text, title, root))
    assert "* 2 top-level commands: amend, start" in text
    assert "* `amend` has 2 subcommand(s): clarify, correct" in text


def test_the_real_cli_groups_are_read_from_the_parser():
    surface = bd.cli_surface()
    assert surface["source"].startswith("python3 -m hoh.cli"), surface
    groups = surface["groups"]
    # clarify/correct/narrow/widen are the choices of `amend --kind`, not
    # subcommands: `amend` is a command and not a group
    assert "amend" not in groups and "amend" in surface["commands"]
    assert {"unblock", "drive", "rollover"} <= set(groups["project"])
    assert set(groups) <= set(surface["commands"])


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_cli_child_gets_a_home_and_writes_no_bytecode(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    seen = []

    def fake(argv, **kw):
        seen.append(kw["env"])
        return _Done(0, "usage {a,b}")

    monkeypatch.setattr(bd.subprocess, "run", fake)
    assert bd.cli_surface()["commands"] == ["a", "b"]
    assert len(seen) == 3
    for env in seen:
        assert env["HOME"]
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["HERDR_ENV"] == "1"


def test_a_failing_group_help_makes_that_group_not_measured(monkeypatch):
    answers = {("--help",): _Done(0, "usage {a,b,c}"),
               ("a", "--help"): _Done(2, "", "boom"),
               ("b", "--help"): _Done(0, "usage {x,y}"),
               ("c", "--help"): _Done(0, "usage")}
    monkeypatch.setattr(bd.subprocess, "run",
                        lambda argv, **kw: answers[tuple(argv[3:])])
    surface = bd.cli_surface()
    assert surface["groups"] == {"b": ["x", "y"]}
    assert surface["group_failures"] == {"a": "`a --help` exited 2"}
    text = "\n".join(bd.measurement_sections({"cli": surface}))
    assert ("* limit: `a` is not measured: `a --help` exited 2 — "
            "`python3 -m hoh.cli a --help`") in text
    assert "* `b` has 2 subcommand(s): x, y" in text


def test_the_real_runtime_and_human_sections_cite_lines_that_exist():
    needs_evidence(PROGRAM_REGISTER)
    inst, run, human = (bd.fresh_install_path(), bd.runtime_model(),
                        bd.human_intervention_points())
    for part in (inst, run, human):
        assert part["source"] != bd.NOT_MEASURED, part
        _cited_lines_exist(ROOT, part["rows"] + part["limits"])
    _cited_lines_exist(ROOT, inst["recorded"])
    texts = [t for t, _ in run["rows"]]
    assert "role dispatcher `HerdrDispatcher`" in texts
    assert "role dispatcher `HarnessDispatcher`" in texts
    assert any(t.startswith("systemd timer `hoh-resume-quota` every 15min")
               for t in texts), texts
    htexts = [t for t, _ in human["rows"]]
    assert any(t.startswith("`NEEDS_A_HUMAN`") for t in htexts), htexts
    assert "`Health.WAITING_FOR_DECISION`" in htexts
    body = {"cli": {"commands": ["run"], "source": "python3 -m hoh.cli --help"},
            "install": inst, "runtime": run, "human": human,
            "dogfood": {"source": bd.NOT_MEASURED, "why": "not run here",
                        "rows": []}}
    text = "\n".join(bd.measurement_sections(body))
    for title in ("Fresh install path", "Current service and runtime model",
                  "Current human intervention points"):
        assert bd.section_status(text, title, ROOT)[0] is True, \
            (title, bd.section_status(text, title, ROOT))
    assert "plugin/herdr-plugin.toml:" in text


# --- p0m-fix: subcommands, timeouts and the minors of the p0-measure reviews ---

_RUN_HELP = """usage: hoh run [-h] [--iterations ITERATIONS]
               [--isolation {none,strict}]
               run_id

positional arguments:
  run_id

options:
  -h, --help            show this help message and exit
  --isolation {none,strict}
                        how acceptance checks are executed.
"""

_GROUP_HELP = """usage: hoh amend [-h] [--mode {fast,slow}]
                {clarify,correct,narrow} ...

positional arguments:
  {clarify,correct,narrow}
    clarify             one
    correct             two

options:
  -h, --help            show this help message and exit
  --mode {fast,slow}    a choice
"""


def test_subcommands_come_only_from_the_subparsers_positional():
    sub = bd._subcommands
    assert sub(_RUN_HELP) is None
    assert sub(_GROUP_HELP) == ["clarify", "correct", "narrow"]
    # the positional block wins over an earlier brace list in the usage
    early = ("usage: x [-h] {q,r} ...\n\npositional arguments:\n"
             "  {a,b}\n    a   one\n")
    assert sub(early) == ["a", "b"]
    # a block that ends at `options:`: a later `{no,yes}` is not a subcommand list
    ended = ("usage: x [-h] {q,r}\n\npositional arguments:\n  run_id\n\n"
             "options:\n  {no,yes}\n")
    assert sub(ended) is None
    # stubs without a `positional arguments:` line
    assert sub("usage {a,b}") == ["a", "b"]
    assert sub("usage: x [-h] [--o {p,q}] {a,b}") == ["a", "b"]
    assert sub("usage: x [-h] [--o {p,q}]") is None
    assert sub("") is None


def test_the_real_run_command_has_no_subcommands_and_the_groups_are_not_options():
    surface = bd.cli_surface()
    groups = surface["groups"]
    assert "run" in surface["commands"] and "run" not in groups, surface
    assert {"goal", "project", "storage"} <= set(groups), sorted(groups)
    assert "amend" not in groups
    for command, subs in groups.items():
        assert not {"none", "strict"} <= set(subs), (command, subs)
    text = "\n".join(bd.measurement_sections({"cli": surface}))
    assert "`project` has " in text
    assert "`run` has " not in text


def test_a_hung_help_is_a_failure_of_that_command_not_a_hang(monkeypatch):
    timeouts = []

    def fake(argv, **kw):
        timeouts.append(kw.get("timeout"))
        if tuple(argv[3:]) == ("a", "--help"):
            raise bd.subprocess.TimeoutExpired(argv, kw["timeout"])
        return {("--help",): _Done(0, "usage {a,b}"),
                ("b", "--help"): _Done(0, "usage {x,y}")}[tuple(argv[3:])]

    monkeypatch.setattr(bd.subprocess, "run", fake)
    surface = bd.cli_surface()
    assert surface["group_failures"] == {"a": "`a --help` timed out after 60 s"}
    assert surface["groups"] == {"b": ["x", "y"]}
    assert timeouts and all(t == bd.CLI_TIMEOUT and t > 0 for t in timeouts)

    def top_hangs(argv, **kw):
        raise bd.subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(bd.subprocess, "run", top_hangs)
    got = bd.cli_surface()
    assert got["source"] == bd.NOT_MEASURED and got["commands"] == []
    assert got["why"].startswith("the CLI did not answer --help")

    def cannot_start(argv, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(bd.subprocess, "run", cannot_start)
    got = bd.cli_surface()
    assert got["source"] == bd.NOT_MEASURED and got["commands"] == []
    assert got["why"].startswith("the CLI did not answer --help")


def test_a_cite_outside_the_repository_or_without_an_extension_is_resolved(
        tmp_path):
    root = tmp_path / "repo"
    _put(root, "Makefile", "all:\n\ttrue\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")

    def check(src: str):
        return bd.section_status(f"## A\n\n* f — `{src}`\n", "A", root)

    assert check("Makefile:2")[0] is True
    assert check("Makefile:1")[0] is True
    assert check("Makefile:3") == (False, "cited line Makefile:3 does not exist")
    assert check("gone:1")[0] is False
    for bad in ("../outside.txt:1", f"{outside}:1",
                "Makefile:1; ../outside.txt:1"):
        ok, why = check(bad)
        assert ok is False and "is outside the repository" in why, (bad, why)
    # a symbolic link that leaves the repository is outside as well
    (root / "link.txt").symlink_to(outside)
    ok, why = check("link.txt:1")
    assert ok is False and "is outside the repository" in why, why
    assert bd._inside(root, "Makefile") is True
    assert bd._inside(root, "..") is False


def test_a_timer_runs_the_unit_it_names_and_comments_are_not_settings(tmp_path):
    root = _tree(tmp_path)
    _put(root, "ops/hoh-c.timer",
         "[Timer]\n# OnUnitActiveSec=99min\nUnit=hoh-other.service\n"
         "OnUnitActiveSec=7min\n")
    _put(root, "ops/hoh-c.service",
         "[Service]\nExecStart=%h/bin/hoh not-this\n")
    _put(root, "ops/hoh-other.service",
         "[Service]\nType=simple\nExecStart=%h/bin/hoh this-one\n")
    got = bd.runtime_model(root)
    text, src = _row(got, "systemd timer `hoh-c`")
    assert text == ("systemd timer `hoh-c` every 7min runs `%h/bin/hoh "
                    "this-one` (`Type=simple`)")
    assert src == ("ops/hoh-c.timer:4; ops/hoh-other.service:3; "
                   "ops/hoh-other.service:2")
    assert not any("not-this" in t for t, _ in got["rows"])
    _cited_lines_exist(root, got["rows"])
    # a unit that is not a plain service file name is a limit
    _put(root, "ops/hoh-c.timer",
         "[Timer]\nUnit=../evil.service\nOnUnitActiveSec=7min\n")
    got = bd.runtime_model(root)
    assert not any(t.startswith("systemd timer `hoh-c`") for t, _ in got["rows"])
    assert ("systemd timer `hoh-c` names a unit `../evil.service` that is not "
            "a service file of ops/", "ops/hoh-c.timer") in got["limits"]
    _put(root, "ops/hoh-c.timer",
         "[Timer]\nUnit=hoh-x.target\nOnUnitActiveSec=7min\n")
    got = bd.runtime_model(root)
    assert any("names a unit `hoh-x.target`" in t for t, _ in got["limits"])


def test_a_symbolic_link_under_dogfood_is_not_followed(tmp_path):
    root = _tree(tmp_path / "repo")
    foreign = tmp_path / "foreign"
    _put(foreign, "20270101T0000Z/run-evidence/results/x-i1-a1.json", "{}")
    stamp = foreign / "20270101T0000Z"
    (root / "dogfood" / "linked").symlink_to(foreign, target_is_directory=True)
    (root / "dogfood/alpha/20270102T0000Z").symlink_to(
        stamp, target_is_directory=True)
    got = bd.dogfood_evidence(root)
    rows = _by_text(got)
    assert ("3 run directories in 2 strand(s); 2 with run-evidence; 5 "
            "role-result files") in rows
    assert not any("20270101" in t or "20270102" in t for t, _ in got["rows"])
    assert any(t.startswith("2 symbolic link(s) to a directory")
               for t, _ in got["limits"]), got["limits"]
    # no link, no limit
    clean = bd.dogfood_evidence(_tree(tmp_path / "clean"))
    assert not any("symbolic link" in t for t, _ in clean["limits"])


def test_valid_toml_of_the_wrong_shape_is_a_missing_source_not_an_exception(
        tmp_path):
    odd = tmp_path / "odd"
    _put(odd, "pyproject.toml", "project = [1]\n")
    assert bd.fresh_install_path(odd)["source"] == bd.NOT_MEASURED
    assert bd._toml_scripts({"project": [1]}) == {}
    root = _tree(tmp_path / "full")
    _put(root, "pyproject.toml", "project = [1]\n")
    _put(root, "plugin/herdr-plugin.toml", "actions = 5\n")
    got = bd.runtime_model(root)
    assert got["source"] != bd.NOT_MEASURED
    assert not any(t.startswith("entry point") for t, _ in got["rows"])
    assert not any(t.startswith("Herdr plugin action") for t, _ in got["rows"])


def test_a_relative_import_is_not_the_standard_library_module(tmp_path):
    root = _tree(tmp_path)
    cases = [("from . import socket\nsocket.socket()\n", 0),
             ("from .socket import socket\nsocket()\n", 0),
             ("from socket import socket\nsocket()\n", 1)]
    for source, want in cases:
        _put(root, "src/hoh/serve.py", source)
        text, _ = _row(bd.runtime_model(root), "resident listener or fork")
        assert text.endswith(f": {want}"), (source, text)
        assert "relative imports never match" in text
        assert "get_context().Process" in text
        assert "not followed" in text


def test_the_cited_line_of_a_cli_definition_is_the_line_of_its_literal(tmp_path):
    root = _tree(tmp_path)
    _put(root, "src/hoh/cli.py", "\n".join([
        "import argparse", "sub.add_parser(", "    'unblock',", "    help='x')",
        "ub.add_argument(", "    '--by',", "    help='the captain decides')",
        ""]))
    rows = _by_text(bd.human_intervention_points(root))
    assert rows["CLI subcommand `unblock`"] == "src/hoh/cli.py:3"
    by = [t for t in rows if t.startswith("CLI argument `--by`")]
    assert len(by) == 1 and rows[by[0]] == "src/hoh/cli.py:6", rows


def test_every_marker_help_word_and_parameter_form_is_a_human_point(tmp_path):
    root = tmp_path
    _put(root, "src/hoh/h.py", "\n".join([
        "class C:",                                       # 1
        "    HUMAN_X = 1",                                # 2
        "    OPERATOR_Y = 2",                             # 3
        "    CAPTAIN_Z = 3",                              # 4
        "    WAITING_FOR_DECISION = 4",                   # 5
        "    NEEDS_A_HUMAN_W = 5",                        # 6
        "    HUMAN_ANN: int = 6",                         # 7
        "def f(Human_Owner): pass",                       # 8
        "def g(*, human_kw): pass",                       # 9
        "h = lambda human_l: human_l",                    # 10
        "sub.add_parser('approve-it')",                   # 11
        "sub.add_argument('--a', help='the Captain')",    # 12
        "sub.add_argument('--b', help='an OPERATOR')",    # 13
        "sub.add_argument('--c', help='a human')",        # 14
        "sub.add_argument('--d', help='who decides')",    # 15
        "sub.add_argument('--e', help='humanity')",       # 16
        ""]))
    got = bd.human_intervention_points(root)
    rows = _by_text(got)
    want = {
        "`C.HUMAN_X`": 2, "`C.OPERATOR_Y`": 3, "`C.CAPTAIN_Z`": 4,
        "`C.WAITING_FOR_DECISION`": 5, "`C.NEEDS_A_HUMAN_W`": 6,
        "`C.HUMAN_ANN`": 7, "parameter `Human_Owner`": 8,
        "parameter `human_kw`": 9, "parameter `human_l`": 10,
        "CLI subcommand `approve-it`": 11,
        "CLI argument `--a`: the Captain": 12,
        "CLI argument `--b`: an OPERATOR": 13,
        "CLI argument `--c`: a human": 14,
        "CLI argument `--d`: who decides": 15,
    }
    for text, line in want.items():
        assert rows.get(text) == f"src/hoh/h.py:{line}", (text, rows)
    assert not any("--e" in t or "humanity" in t for t in rows), rows


def test_a_fenced_ledger_heading_and_a_non_boolean_node_flag_are_not_counted(
        tmp_path):
    root = _tree(tmp_path)
    _put(root, "DOGFOOD_LEDGER.md", "\n".join([
        "# Ledger -- run x (2026-09-07)", "", "### O5 -- first (2026-09-08)",
        "", "```", "### O99 -- not a heading (2026-12-31)", "```", "",
        "### O9 -- second (2026-09-30)", ""]))
    _put(root, "dogfood/ATTRIBUTION.json", json.dumps({"entries": [
        {"id": "a", "category": "X", "is_development_node": True},
        {"id": "b", "category": "X", "is_development_node": "yes"},
        {"id": "c", "category": "X", "is_development_node": 1}]}))
    rows = _by_text(bd.dogfood_evidence(root))
    assert "2 ledger entries O5 to O9" in rows
    assert any("2026-09-07 to 2026-09-30" in t for t in rows), list(rows)
    assert any("1 development node(s)" in t for t in rows), list(rows)
