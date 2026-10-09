"""`hoh agent-setup`: the text that makes a coding-agent session use VeriHarness.

Reference tests for the agent-session integration increment. They run the command through
`hoh.cli.main`, read the bundled texts through the same code path the command uses, and never start
Herdr or an agent (a stub `herdr` first on PATH logs every call; the log must stay empty). They
write only under `tmp_path`, set HOME and CLAUDE_CONFIG_DIR there, and never read the real ones.

The texts are held to the product in two ways a reader cannot do by eye: every `hoh` command line in
a fenced block is parsed by the real parser (so a flag renamed in the CLI turns a test red), and every
bundled file is held to the publication rules (English, no private paths or identifiers).

No literal home path is written in this file: the needles are assembled at run time, because
`tools/export_manifest.py` scans every exported file for them.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import stat
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import export_manifest as em  # noqa: E402

DOCS = REPO_ROOT / "src" / "hoh" / "agent_docs"
HOME_NEEDLE = "/ho" + "me/"
RULE_IDS = {"outside-herdr", "verdict", "no-bypass", "delivery", "own-panes", "user-config", "no-delete"}
FOOTER_KEYS = ("VERDICT", "CANDIDATE", "BASE", "SPEC_SHA256", "BLOCKING_OPEN")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """HOME, CLAUDE_CONFIG_DIR and PATH are the test's own; a stub `herdr` logs every call."""
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "herdr-calls.log"
    stub = stubs / "herdr"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\nexit 1\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setenv("PATH", str(stubs) + os.pathsep + os.environ.get("PATH", ""))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return type("World", (), {"tmp": tmp_path, "home": home, "log": log, "work": work})


def run(argv, capsys):
    from hoh import cli

    try:
        rc = cli.main(argv)
    except SystemExit as exc:                       # argparse's own refusals
        rc = exc.code
    out = capsys.readouterr()
    return rc, out.out, out.err


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = (p.read_bytes() if p.is_file() and not p.is_symlink() else
                    ("link:" + os.readlink(p)) if p.is_symlink() else "dir")
    return out


def bundled_files() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*") if p.is_file())


def fenced_hoh_lines(text: str) -> list[str]:
    lines, inside = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside and line.strip().startswith("hoh "):
            lines.append(line.strip())
    return lines


def parse_line(line: str):
    from hoh import cli

    argv = shlex.split(re.sub(r"<[^<>]+>", "x", line))
    assert argv[0] == "hoh"
    return cli.build_parser().parse_args(argv[1:])


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "a skill file opens with a front matter block"
    head, sep, _ = text[4:].partition("\n---\n")
    assert sep, "the front matter block is closed"
    out = {}
    for line in head.splitlines():
        key, _, value = line.partition(": ")
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
# A. the texts
# --------------------------------------------------------------------------- #


def test_print_skill_is_a_skill_file_with_valid_front_matter(world, capsys):
    rc, out, err = run(["agent-setup", "--print", "--kind", "skill"], capsys)
    assert (rc, err) == (0, "")
    fm = frontmatter(out)
    assert set(fm) == {"name", "description"}
    assert fm["name"] == "veriharness"
    desc = fm["description"]
    assert desc.startswith('"') and desc.endswith('"') and '"' not in desc[1:-1]
    assert 40 < len(desc) <= 1024
    assert "hoh" in desc and "Herdr" in desc
    assert "<!--" not in out, "the delegation placeholder is filled"
    # default kind is the skill, and the same bytes
    rc2, out2, _ = run(["agent-setup", "--print"], capsys)
    assert (rc2, out2) == (0, out)


def test_every_hoh_command_in_the_texts_parses_with_the_real_parser(world, capsys):
    from hoh import agent_setup, cli

    seen_cmds, seen_flags, count = set(), set(), 0
    texts = [agent_setup.render("skill"), agent_setup.render("snippet")]
    texts += [p.read_text(encoding="utf-8") for p in bundled_files() if p.suffix == ".md"]
    for text in texts:
        for line in fenced_hoh_lines(text):
            ns = parse_line(line)                    # SystemExit(2) on any unknown command or flag
            seen_cmds.add(ns.cmd)
            seen_flags.update(tok for tok in shlex.split(line) if tok.startswith("--"))
            count += 1
    assert count >= 14, count
    assert {"worktree", "start", "run", "status", "report", "list", "pause", "resume",
            "unblock", "cancel", "resume-quota", "storage", "project"} <= seen_cmds
    assert {"--iterations", "--max-iterations", "--planner", "--developer", "--qa",
            "--isolation", "--trust-worktree", "--run-id", "--spec", "--repo"} <= seen_flags
    # the checker can fail: a flag the CLI does not have is an error, not a pass
    with pytest.raises(SystemExit):
        parse_line("hoh run <run-id> --iters 1")
    # and a command named in backticks (not only in fenced blocks) exists
    choices = cli.build_parser()._subparsers._group_actions[0].choices
    spans = set()
    for text in texts:
        spans.update(re.findall(r"`hoh ((?:--root \S+ )?[a-z][a-z-]*)", text))
    assert spans and all(s.split()[-1] in choices for s in spans), spans - set(choices)


def test_the_rules_are_present_with_the_words_that_make_them_rules(world, capsys):
    from hoh import agent_setup

    needs = {
        "outside-herdr": ("HERDR_ENV", "--no-herdr", "do not start a run", "not harness-verified"),
        "verdict": ("accepted_candidate", "hoh status"),
        "no-bypass": ("verbatim", "NEEDS_YOU"),
        "delivery": ("--approve", "explicit yes"),
        "own-panes": ("Do not create", "never touch"),
        "no-delete": ("delete nothing", "version suffix"),
        "user-config": (".claude.json", "explicit yes", "hoh start", "standing"),
    }
    snippet_needs = {k: v for k, v in needs.items()}
    snippet_needs["no-bypass"] = ("verbatim",)
    snippet_needs["own-panes"] = ("never create",)
    snippet_needs["no-delete"] = ("delete nothing", "version suffix")
    snippet_needs["delivery"] = ("--approve", "explicit yes")
    snippet_needs["user-config"] = (".claude.json", "explicit yes", "hoh start", "standing")
    for delegation in ("ask", "standing"):
        for kind, want in (("skill", needs), ("snippet", snippet_needs)):
            text = agent_setup.render(kind, delegation)
            rules = re.findall(r"^- RULE ([a-z-]+): (.*)$", text, flags=re.M)
            ids = [r[0] for r in rules]
            assert sorted(ids) == sorted(RULE_IDS), (kind, ids)
            for rid, body in rules:
                for token in want[rid]:
                    assert token in body, (kind, rid, token)


def test_the_delegation_choice_changes_one_paragraph_and_ask_is_the_default(world, capsys):
    from hoh import agent_setup

    ask = agent_setup.render("skill", "ask")
    standing = agent_setup.render("skill", "standing")
    assert ask != standing
    a_lines, s_lines = ask.splitlines(), standing.splitlines()
    assert len(a_lines) == len(s_lines)
    differing = [i for i, (x, y) in enumerate(zip(a_lines, s_lines)) if x != y]
    assert len(differing) == 1
    i = differing[0]
    assert "wait for a yes" in a_lines[i] and "--max-iterations 3" not in a_lines[i]
    assert "standing delegation" in s_lines[i] and "--max-iterations 3" in s_lines[i]
    _, default_out, _ = run(["agent-setup", "--print", "--kind", "skill"], capsys)
    assert default_out == ask
    _, out_standing, _ = run(["agent-setup", "--print", "--delegation", "standing"], capsys)
    assert out_standing == standing


def test_the_snippet_is_short_and_points_at_the_skill(world, capsys):
    rc, out, err = run(["agent-setup", "--print", "--kind", "snippet"], capsys)
    assert (rc, err) == (0, "")
    assert not out.startswith("---"), "a snippet is a section for CLAUDE.md or AGENTS.md"
    assert len(out.splitlines()) <= 20 and len(out) <= 2600, (len(out.splitlines()), len(out))
    assert "veriharness/SKILL.md" in out and "hoh start" in out and "hoh run" in out


def test_the_bundle_is_exactly_what_the_command_knows(world, capsys):
    from hoh import agent_setup

    on_disk = {str(p.relative_to(DOCS)) for p in bundled_files()}
    known = set(agent_setup.SKILL_FILES) | {agent_setup.SNIPPET_SOURCE}
    assert on_disk == known, (on_disk ^ known)
    skill = agent_setup.render("skill")
    for rel in re.findall(r"`((?:references|templates)/[a-z.-]+)`", skill):
        assert rel in agent_setup.SKILL_FILES, rel


def test_the_templates_and_the_method_agree_with_each_other(world, capsys):
    ack = json.loads((DOCS / "templates" / "ack.template.json").read_text(encoding="utf-8"))
    assert list(ack) == ["handoff", "handoff_sha256", "successor_session", "at"]
    handoff = (DOCS / "templates" / "handoff.template.md").read_text(encoding="utf-8")
    for key in ack:
        assert f'"{key}"' in handoff, key
    assert "sha256" in handoff and "## 7. Acknowledgement" in handoff
    rules = (DOCS / "templates" / "reviewer-rules.template.md").read_text(encoding="utf-8")
    method = (DOCS / "references" / "method.md").read_text(encoding="utf-8")
    for key in FOOTER_KEYS:
        assert re.search(rf"^{key}: ", rules, flags=re.M), key
        assert key in method, key
    assert "RELEASE_YES requires BLOCKING_OPEN 0" in rules
    rotation = (DOCS / "references" / "rotation.md").read_text(encoding="utf-8")
    assert "No live succession has been run" in rotation, "the status of the product protocol is stated"
    flat = " ".join(rotation.split())
    for sub in ("hold", "rollover", "rollover-verify", "rollover-accept"):
        assert re.search(rf"hoh project {re.escape(sub)}(?![-\w])", flat), sub
    assert "operator's yes" in rotation and "HERDR_ENV" in rotation, "starting a successor is gated"


def test_published_texts_are_english_and_carry_no_private_marker(world, capsys):
    from hoh import agent_setup

    files = bundled_files()
    assert len(files) == len(agent_setup.SKILL_FILES) + 1
    for path in files:
        text = path.read_text(encoding="utf-8")
        name = str(path.relative_to(DOCS))
        assert text.isascii(), name
        assert em.german_density(text) < 0.04, (name, em.german_density(text))
        assert em.find_hardcoded_home_path(text) is None, name
        assert not em.contains_home_path(text), name
        assert em.find_token_shaped(text) is None, name
        assert HOME_NEEDLE not in text, name
        for pattern, what in (
            (r"/mnt/[a-z]/", "a Windows mount"),
            (r"\b[A-Za-z]:\\", "a Windows drive path"),
            (r"\bw[0-9][A-Za-z0-9]*:p[A-Za-z0-9]+\b", "a Herdr pane id"),
            (r"\b[0-9a-f]{8}\b", "a session or commit id"),
            (r"\.agents/|\.claude/projects", "an operator-local path"),
            (r"nvidia|GPU", "an operator-local hardware rule"),
            (r"\bS/", "an operator scratch alias"),
            (r"polling loop", "a remnant of an operator-local hardware rule"),
        ):
            assert not re.search(pattern, text), (name, what)
    # the same function the manifest uses decides these files ship
    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        assert em._classify(rel, REPO_ROOT) == ("INCLUDE", "package"), rel


def test_package_data_ships_every_bundled_file(world, capsys):
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = cfg["tool"]["setuptools"]["package-data"]["hoh"]
    assert len(bundled_files()) == 8, "the bundle exists: skill, snippet, two references, four templates"
    shipped = set()
    for pat in patterns:
        shipped.update(glob.glob(pat, root_dir=REPO_ROOT / "src" / "hoh", recursive=False))
    for path in bundled_files():
        assert str(path.relative_to(REPO_ROOT / "src" / "hoh")) in shipped, path


# --------------------------------------------------------------------------- #
# B. the command
# --------------------------------------------------------------------------- #


def test_install_skill_writes_exactly_the_bundle_into_the_named_directory(world, capsys):
    from hoh import agent_setup

    target = world.tmp / "proj" / "skills"
    target.mkdir(parents=True)
    before_home, before_work = snapshot(world.home), snapshot(world.work)
    rc, out, err = run(["agent-setup", "--install", str(target)], capsys)
    assert (rc, err) == (0, "")
    result = json.loads(out)
    expected = {str(target / rel) for rel, _ in agent_setup.files_for("skill")}
    assert set(result["written"]) == expected and len(expected) == len(agent_setup.SKILL_FILES)
    for rel, content in agent_setup.files_for("skill"):
        p = target / rel
        assert p.read_text(encoding="utf-8") == content and not p.is_symlink()
        assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()} == \
        {rel for rel, _ in agent_setup.files_for("skill")}
    assert snapshot(world.home) == before_home and snapshot(world.work) == before_work
    assert not world.log.exists(), "no herdr call"
    # the installed skill is the printed skill
    _, printed, _ = run(["agent-setup", "--print"], capsys)
    assert (target / "veriharness" / "SKILL.md").read_text(encoding="utf-8") == printed


def test_install_snippet_and_all_and_the_delegation_choice(world, capsys):
    a = world.tmp / "a"
    a.mkdir()
    rc, out, _ = run(["agent-setup", "--install", str(a), "--kind", "snippet",
                      "--delegation", "standing"], capsys)
    assert rc == 0 and json.loads(out)["written"] == [str(a / "veriharness-agent-rules.md")]
    assert "standing delegation" in (a / "veriharness-agent-rules.md").read_text(encoding="utf-8")
    assert [p.name for p in a.iterdir()] == ["veriharness-agent-rules.md"], "no CLAUDE.md, no AGENTS.md"
    b = world.tmp / "b"
    b.mkdir()
    rc, out, _ = run(["agent-setup", "--install", str(b), "--kind", "all"], capsys)
    assert rc == 0
    assert (b / "veriharness" / "SKILL.md").is_file() and (b / "veriharness-agent-rules.md").is_file()
    assert "wait for a yes" in (b / "veriharness" / "SKILL.md").read_text(encoding="utf-8")
    c = world.tmp / "c"
    c.mkdir()
    rc, out, _ = run(["agent-setup", "--install", str(c), "--delegation", "standing"], capsys)
    assert rc == 0
    assert "standing delegation" in (c / "veriharness" / "SKILL.md").read_text(encoding="utf-8")
    assert "wait for a yes" not in (c / "veriharness" / "SKILL.md").read_text(encoding="utf-8")


def test_install_never_replaces_a_file_and_a_refusal_writes_nothing(world, capsys):
    t = world.tmp / "t"
    t.mkdir()
    (t / "veriharness").mkdir()
    (t / "veriharness" / "SKILL.md").write_text("mine\n", encoding="utf-8")
    before = snapshot(t)
    rc, out, err = run(["agent-setup", "--install", str(t)], capsys)
    assert (rc, out) == (2, "") and err.startswith("agent-setup refused: ") and "SKILL.md" in err
    assert snapshot(t) == before
    # all or nothing: the snippet is free, the skill is not, --kind all writes neither
    rc, out, err = run(["agent-setup", "--install", str(t), "--kind", "all"], capsys)
    assert (rc, out) == (2, "") and snapshot(t) == before
    # the other way round: the snippet is taken, the skill is free, --kind all writes neither
    w = world.tmp / "w"
    w.mkdir()
    (w / "veriharness-agent-rules.md").write_text("mine\n", encoding="utf-8")
    before_w = snapshot(w)
    rc, out, err = run(["agent-setup", "--install", str(w), "--kind", "all"], capsys)
    assert (rc, out) == (2, "") and snapshot(w) == before_w
    # a dangling symlink at a target name is a name in use
    u = world.tmp / "u"
    u.mkdir()
    os.symlink(str(world.tmp / "nowhere"), u / "veriharness-agent-rules.md")
    rc, out, err = run(["agent-setup", "--install", str(u), "--kind", "snippet"], capsys)
    assert (rc, out) == (2, "") and not (world.tmp / "nowhere").exists()
    # a symlinked skill directory would write outside the named directory
    v, elsewhere = world.tmp / "v", world.tmp / "elsewhere"
    v.mkdir()
    elsewhere.mkdir()
    os.symlink(str(elsewhere), v / "veriharness")
    rc, out, err = run(["agent-setup", "--install", str(v)], capsys)
    assert (rc, out) == (2, "") and list(elsewhere.iterdir()) == []
    # a name that is a file where the install needs a directory: nothing is written first
    for sub in ("veriharness", "veriharness/references", "veriharness/templates"):
        x = world.tmp / ("x-" + sub.replace("/", "-"))
        x.mkdir()
        if "/" in sub:
            (x / "veriharness").mkdir()
        (x / sub).write_text("mine\n", encoding="utf-8")
        before_x = snapshot(x)
        for kind in ("skill", "all"):
            rc, out, err = run(["agent-setup", "--install", str(x), "--kind", kind], capsys)
            assert (rc, out) == (2, "") and err.startswith("agent-setup refused: "), (sub, kind, err)
            assert "not a directory" in err and snapshot(x) == before_x, (sub, kind)
    # every one of the three directories, symlinked out, is refused before anything is written
    for sub in ("veriharness", "veriharness/references", "veriharness/templates"):
        y, away = world.tmp / ("y-" + sub.replace("/", "-")), world.tmp / ("away-" + sub.replace("/", "-"))
        y.mkdir()
        away.mkdir()
        if "/" in sub:
            (y / "veriharness").mkdir()
        os.symlink(str(away), y / sub)
        before_y = snapshot(y)
        rc, out, err = run(["agent-setup", "--install", str(y)], capsys)
        assert (rc, out) == (2, "") and list(away.iterdir()) == [] and snapshot(y) == before_y, sub
    # the directory is never created, and a file is not a directory
    rc, out, err = run(["agent-setup", "--install", str(world.tmp / "missing")], capsys)
    assert (rc, out) == (2, "") and not (world.tmp / "missing").exists()
    f = world.tmp / "afile"
    f.write_text("x", encoding="utf-8")
    rc, out, _ = run(["agent-setup", "--install", str(f)], capsys)
    assert (rc, out) == (2, "") and f.read_text(encoding="utf-8") == "x"


def test_user_level_configuration_needs_the_explicit_flag(world, capsys, monkeypatch):
    home = world.home
    for name in (".claude", ".codex", ".agents", ".config"):
        (home / name / "skills").mkdir(parents=True)
    custom = world.tmp / "custom-claude"
    (custom / "skills").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    link = world.tmp / "innocent"
    os.symlink(str(home / ".claude" / "skills"), link)
    refused = [home / ".claude", home / ".claude" / "skills", home / ".codex", home / ".agents" / "skills",
               home / ".config", custom, custom / "skills", link]
    for d in refused:
        before = snapshot(d.resolve())
        rc, out, err = run(["agent-setup", "--install", str(d)], capsys)
        assert (rc, out) == (2, ""), d
        assert "--allow-user-config" in err, d
        assert snapshot(d.resolve()) == before, d
    # a project-level directory under HOME, and names that merely start alike, are fine
    for ok in (home / "proj" / ".claude" / "skills", home / ".claude-project", home / ".claudex"):
        ok.mkdir(parents=True)
        rc, out, err = run(["agent-setup", "--install", str(ok)], capsys)
        assert rc == 0, (ok, err)
    # a configuration directory that is itself a symlink (a dotfiles setup) is still user-level
    other = world.tmp / "other-home"
    other.mkdir()
    (other / "dot-codex" / "skills").mkdir(parents=True)
    os.symlink(str(other / "dot-codex"), other / ".codex")
    monkeypatch.setenv("HOME", str(other))
    before_dot = snapshot(other / "dot-codex")
    rc, out, err = run(["agent-setup", "--install", str(other / ".codex" / "skills")], capsys)
    assert (rc, out) == (2, "") and "--allow-user-config" in err and snapshot(other / "dot-codex") == before_dot
    monkeypatch.setenv("HOME", str(home))
    # relocated configuration homes named by the environment
    for var in ("CODEX_HOME", "XDG_CONFIG_HOME"):
        reloc = world.tmp / ("reloc-" + var.lower())
        (reloc / "skills").mkdir(parents=True)
        monkeypatch.setenv(var, str(reloc))
        rc, out, err = run(["agent-setup", "--install", str(reloc / "skills")], capsys)
        assert (rc, out) == (2, "") and "--allow-user-config" in err, var
        assert list((reloc / "skills").iterdir()) == [], var
        monkeypatch.delenv(var)
    # with the flag it writes
    rc, out, err = run(["agent-setup", "--install", str(home / ".claude" / "skills"),
                        "--allow-user-config"], capsys)
    assert rc == 0 and (home / ".claude" / "skills" / "veriharness" / "SKILL.md").is_file()
    assert not world.log.exists()


def test_usage_errors_and_refusals_keep_stdout_empty(world, capsys):
    d = world.tmp / "d"
    d.mkdir()
    for argv in (["agent-setup"],
                 ["agent-setup", "--print", "--install", str(d)],
                 ["agent-setup", "--print", "--kind", "docs"],
                 ["agent-setup", "--print", "--delegation", "always"]):
        rc, out, err = run(argv, capsys)
        assert rc == 2 and out == "" and err, argv
    rc, out, err = run(["agent-setup", "--print", "--kind", "all"], capsys)
    assert (rc, out) == (2, "") and err.startswith("agent-setup refused: ")
    assert list(d.iterdir()) == []


def test_a_path_that_cannot_be_used_is_a_refusal_and_never_a_crash(world, capsys):
    for bad in ("~no-such-user-hoh-test/x", "a\0b"):
        rc, out, err = run(["agent-setup", "--install", bad], capsys)
        assert (rc, out) == (2, ""), bad
        assert err.startswith("agent-setup refused: ") and "Traceback" not in err, bad


def test_print_writes_nothing_reads_no_environment_and_starts_nothing(world, capsys, monkeypatch):
    (world.tmp / "elsewhere-cwd").mkdir()
    (world.tmp / "elsewhere-cwd" / "CLAUDE.md").write_text("x", encoding="utf-8")
    before = {"home": snapshot(world.home), "work": snapshot(world.work), "tmp": snapshot(world.tmp)}
    rc, first, err = run(["agent-setup", "--print", "--kind", "skill"], capsys)
    assert (rc, err) == (0, "") and first.startswith("---\nname: veriharness\n")
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HOME", str(world.tmp / "elsewhere-home"))
    monkeypatch.setenv("HERDR_PANE_ID", "pane-that-does-not-exist")
    rc2, second, _ = run(["agent-setup", "--print", "--kind", "skill"], capsys)
    assert rc2 == 0 and first == second, "the text does not depend on the machine it is printed on"
    monkeypatch.chdir(world.tmp / "elsewhere-cwd")
    rc3, third, _ = run(["agent-setup", "--print", "--kind", "skill"], capsys)
    assert rc3 == 0 and third == first, "nor on the working directory"
    monkeypatch.chdir(world.work)
    assert before == {"home": snapshot(world.home), "work": snapshot(world.work),
                      "tmp": snapshot(world.tmp)}
    assert not world.log.exists(), "the stub herdr was never called"


def test_an_io_error_after_the_checks_is_reported_as_failed_with_what_was_written(world, capsys, monkeypatch):
    from hoh import agent_setup

    t = world.tmp / "t"
    t.mkdir()
    real_create, calls = agent_setup.durable_create, []

    def flaky(path, payload, **kw):
        calls.append(path)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        return real_create(path, payload, **kw)

    monkeypatch.setattr(agent_setup, "durable_create", flaky)
    rc, out, err = run(["agent-setup", "--install", str(t)], capsys)
    assert (rc, out) == (2, "") and err.startswith("agent-setup failed: ") and "refused" not in err
    written = sorted(str(p.relative_to(t)) for p in t.rglob("*") if p.is_file())
    assert len(written) == 2 and all(w in err for w in written), (written, err)


def test_the_global_root_option_is_accepted_and_ignored(world, capsys):
    rc, out, err = run(["--root", str(world.tmp / "no-such-root"), "agent-setup", "--print"], capsys)
    assert (rc, err) == (0, "") and out.startswith("---\nname: veriharness")
    assert not (world.tmp / "no-such-root").exists()


def test_the_default_run_line_does_not_write_user_configuration(world, capsys):
    from hoh import agent_setup

    skill = agent_setup.render("skill")
    assert "--trust-worktree" not in agent_setup.render("snippet")
    for delegation in ("ask", "standing"):
        text = agent_setup.render("skill", delegation)
        line = [x for x in text.splitlines() if x.startswith("Consent:")]
        assert len(line) == 1 and "RULE user-config" in line[0] and "its own yes" in line[0], delegation
        start = text.index("2. With the yes of RULE user-config")
        assert start < text.index("hoh start --repo"), "the yes comes before the first hoh start"
    blocks, current, inside = [], [], False
    for line in skill.splitlines():
        if line.strip().startswith("```"):
            if inside:
                blocks.append(current)
            current, inside = [], not inside
        elif inside:
            current.append(line.strip())
    runs = [b for b in blocks if any(x.startswith("hoh run ") for x in b)]
    assert len(runs) == 2, "the default run line and, separately, the optional one"
    assert all("--trust-worktree" not in x for x in runs[0])
    assert any("--trust-worktree" in x for x in runs[1])
    optional = skill[:skill.index("strict --trust-worktree")]
    lead = optional[optional.rindex("The first developer dispatch"):]
    for token in (".claude.json", "writes the user's agent configuration", "says yes"):
        assert token in lead, token
