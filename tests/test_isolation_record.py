"""The receipt has to say how the check ran, measured rather than configured.

Until this record existed, the claim "this acceptance check ran sandboxed"
was checkable only by reading the configuration that *asked* for a sandbox.
That is the inference this project refuses everywhere else, and it is the one
an external reader would most want to verify.

Each test here has the shape the project's rules require: the property, and a
control that breaks it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from hoh.contracts import AcceptanceCheck, Candidate, IsolationRecord
from hoh.runner import run_check
from hoh.sandbox import Isolation, SandboxUnavailable


def bwrap_nutzbar() -> bool:
    if not shutil.which("bwrap"):
        return False
    p = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent", "true"],
        capture_output=True,
    )
    return p.returncode == 0


braucht_bwrap = pytest.mark.skipif(
    not bwrap_nutzbar(), reason="this machine cannot create the namespace"
)


def arena_mit_datei(tmp_path: pathlib.Path) -> pathlib.Path:
    arena = tmp_path / "arena"
    arena.mkdir()
    (arena / "hello.txt").write_text("hi\n")
    return arena


def kandidat(arena: pathlib.Path) -> Candidate:
    return Candidate(
        candidate_id="c1", repo_path=str(arena), commit="a" * 40,
        tree_clean=True, tree_digest="d" * 16,
    )


def pruefung(command="cat hello.txt", expect=0) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_id="k1", command=command, expect_exit=expect, description="d",
        expect_reason=(
            "" if expect == 0 else "a non-zero exit is the property being measured"
        ),
    )


class Unbrauchbar:
    """A backend that reports itself unusable and must never be run."""

    name = "broken"

    def unavailable(self) -> str:
        return "namespaces are not permitted here"

    def run(self, argv, spec):                     # pragma: no cover - the point
        raise AssertionError("an unavailable backend must not be executed")


class LuegtUeberIsolation:
    """A backend that runs the command unisolated while claiming a sandbox.

    The upward lie, which is the one that matters. A backend claiming *less*
    isolation than it applied is merely modest; one claiming more is the whole
    threat model, and until the marker existed the runner had no way to tell.
    """

    name = "liar"

    def unavailable(self) -> str | None:
        return None

    def plan(self, argv, spec):
        from hoh.sandbox import LaunchPlan

        # Runs the command exactly as an ordinary subprocess -- no namespace,
        # no read-only bind -- but hands back a plan shaped like a sandbox's
        # and claims to have proved something.
        return LaunchPlan(
            argv=list(argv), env={"PATH": "/usr/bin:/bin"},
            cwd=str(spec.candidate),
            proves_isolation=True,
        )

    def run(self, argv, spec):
        from hoh.sandbox import SandboxResult

        return SandboxResult(0, "", "", isolation=Isolation.NONE, detail="lied")


# --------------------------------------------------------------------------- #
# The unsandboxed path says so
# --------------------------------------------------------------------------- #


def test_the_default_path_records_no_isolation(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena
    )
    assert r.isolation is not None
    assert r.isolation.requested == "none"
    assert r.isolation.effective == "none"
    assert r.isolation.honoured()
    assert r.isolation.candidate_mount_mode == "read-write"
    assert r.isolation.network_policy == "allowed"
    # It applies limits, and says which -- "no record" and "no limits" are
    # different claims.
    assert "cpu=" in r.isolation.resource_limit_policy


# --------------------------------------------------------------------------- #
# The sandboxed path says what it actually did
# --------------------------------------------------------------------------- #


@braucht_bwrap
def test_strict_records_the_backend_and_the_regime(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0 and r.runner_ok
    iso = r.isolation
    assert iso.requested == "strict" and iso.effective == "strict"
    assert iso.backend == "bubblewrap"
    assert iso.backend_probe == ""            # the probe found nothing to report
    assert iso.fallback_to_none is False
    assert iso.network_policy == "denied"
    assert iso.candidate_mount_mode == "read-only"
    assert "timeout=60s" in iso.resource_limit_policy
    assert iso.honoured()


# --------------------------------------------------------------------------- #
# Fail-closed, including for a backend the caller supplied
# --------------------------------------------------------------------------- #


def test_an_unavailable_injected_backend_fails_closed(tmp_path):
    """O106: an injected backend used to skip the probe altogether.

    `select()` refuses an unavailable backend, so the path that *resolves* one
    failed closed. A caller passing `sandbox=` went straight to `run()`, and
    whether that failed closed depended on the backend choosing to re-probe
    inside its own `run`. That made a contract property an implementation
    detail of one class.
    """
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=Unbrauchbar(), timeout=60,
    )
    assert r.runner_ok is False
    assert r.exit_code == 126
    assert r.outcome(0).value == "INCONCLUSIVE"
    iso = r.isolation
    assert iso.requested == "strict"
    # Neither "none" nor "strict": both read as a completed execution.
    assert iso.effective == "refused"
    assert iso.fallback_to_none is False
    assert iso.backend == "broken"
    assert "namespaces are not permitted" in iso.backend_probe
    assert iso.network_policy == "not applied: isolation refused"
    assert not iso.honoured()


def test_a_refused_run_does_not_describe_a_regime_it_never_applied(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=Unbrauchbar(), timeout=60,
    )
    for feld in ("network_policy", "candidate_mount_mode", "resource_limit_policy"):
        assert getattr(r.isolation, feld) == "not applied: isolation refused"


def test_a_backend_that_claims_isolation_it_did_not_apply_is_caught(tmp_path):
    """The upward lie, which is the one that matters.

    Until the command proved from inside that it was somewhere else, every
    policy field on the receipt was an echo of the request: a backend that ran
    the command in the runner's own namespaces, with the candidate writable,
    produced `effective=strict`, `network_policy=denied`,
    `candidate_mount_mode=read-only` and `honoured()=True`.

    Now the launched command records its own mount and network namespace ids
    before the check runs. They equal the runner's, so the claim collapses --
    and, because a check that did not run in the regime it was supposed to has
    measured nothing about the product, the receipt is INCONCLUSIVE rather
    than a verdict.
    """
    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=LuegtUeberIsolation(),
        timeout=60,
    )
    assert r.isolation.requested == "strict"
    # The command ran and left no proof. From outside, that is indistinguishable
    # from bubblewrap failing during setup -- so the value claims neither.
    assert r.isolation.effective == "unverified"
    assert not r.isolation.honoured()
    assert r.runner_ok is False
    assert r.outcome(0).value == "INCONCLUSIVE", "an unisolated run gave a verdict"
    assert "left no proof" in log


class LuegtMitBeweis:
    """The sophisticated liar: runs unisolated *and* writes a real marker.

    The marker is honest -- it records the namespaces the command actually ran
    in -- so the lie is caught by comparison rather than by absence. This is
    the case that distinguishes "no proof" from "proof of the opposite".
    """

    name = "liar-with-proof"

    def unavailable(self) -> str | None:
        return None

    def plan(self, argv, spec):
        from hoh.sandbox import MARKER_PROLOGUE, LaunchPlan

        script = MARKER_PROLOGUE + argv[2]
        return LaunchPlan(
            argv=[argv[0], "-c", script],
            env={
                "PATH": "/usr/bin:/bin",
                "HOH_PROOF_FD": str(spec.proof_fd),
                "HOH_CANDIDATE": str(spec.candidate),
            },
            cwd=str(spec.candidate), proves_isolation=True,
        )

    def run(self, argv, spec):                   # pragma: no cover - unused here
        raise NotImplementedError


def test_a_marker_showing_the_runners_own_namespaces_refutes_the_claim(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=LuegtMitBeweis(),
        timeout=60,
    )
    assert r.isolation.effective == "none"
    assert r.isolation.fallback_to_none is True
    assert r.isolation.verified_from_inside is True   # a marker was produced
    assert not r.isolation.honoured()
    assert r.runner_ok is False
    assert r.outcome(0).value == "INCONCLUSIVE"
    assert "ran in the runner's own" in log


# --------------------------------------------------------------------------- #
# The record itself
# --------------------------------------------------------------------------- #


def test_honoured_is_false_when_the_effective_isolation_differs():
    assert not IsolationRecord(requested="strict", effective="none").honoured()
    assert IsolationRecord(
        requested="strict", effective="strict", verified_from_inside=True
    ).honoured()


def test_a_matching_name_is_not_enough_without_a_measurement():
    """`effective == requested` is the backend agreeing with itself. Isolation
    that was requested has to have been shown, and a record that shows nothing
    is not honoured however consistent its other fields are."""
    assert not IsolationRecord(
        requested="strict", effective="strict", verified_from_inside=False
    ).honoured()
    # A request for no isolation has no claim to verify, so it needs no proof.
    assert IsolationRecord(requested="none", effective="none").honoured()


def test_a_refusal_is_never_honoured_even_though_nothing_contradicts_it():
    """The trap this value exists to close: on a refusal every other field is
    consistent with success, because no execution produced a contradiction."""
    assert not IsolationRecord(requested="strict", effective="refused").honoured()


def test_honoured_is_false_on_a_fallback_even_when_the_names_match():
    assert not IsolationRecord(
        requested="strict", effective="strict", fallback_to_none=True
    ).honoured()


def test_an_old_receipt_without_the_record_means_unknown(tmp_path):
    """A missing record is not evidence of an unsandboxed run, and not of a
    sandboxed one either. It is absence, and absence has its own value."""
    from hoh.contracts import Receipt

    r = Receipt(
        receipt_id="x", run_id="r", iteration=1, attempt=1, check_id="k",
        candidate_binding="b", command="true", exit_code=0,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:00:01Z",
        stdout_digest="d", runner_identity="t",
    )
    assert r.isolation is None


# --------------------------------------------------------------------------- #
# What STRICT actually buys, measured inside a real check
# --------------------------------------------------------------------------- #


@braucht_bwrap
def test_the_candidate_is_read_only_inside_strict(tmp_path):
    """With a positive control, because `exit_code != 0` on its own is what a
    sandbox that never started also produces.

    A reviewer mutated the backend so every bwrap invocation failed during
    setup -- nothing executed at all -- and this test still passed. The
    honoured() assertion is the control: it is true only when the launched
    command reported, from inside, that it was in different namespaces and
    that the candidate was read-only there.
    """
    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="echo x > hello.txt", expect=0),
        kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    assert r.isolation.honoured(), "the sandbox never started; nothing was measured"
    assert r.isolation.candidate_mount_mode == "read-only"
    assert r.exit_code != 0, "a write into the candidate must not succeed"
    assert (arena / "hello.txt").read_text() == "hi\n"

    # ... and the same write succeeds without isolation, so the failure above
    # is the sandbox and not the command.
    ohne, _ = run_check(
        pruefung(command="echo x > hello.txt", expect=0),
        kandidat(arena), run_id="r", iteration=2, attempt=1, cwd=arena,
    )
    assert ohne.exit_code == 0
    assert (arena / "hello.txt").read_text() == "x\n"


@braucht_bwrap
def test_a_check_cannot_reach_the_ancestor_repository_under_strict(tmp_path):
    """Limit 6, in the runner rather than in the sandbox unit test.

    `env -u GIT_CEILING_DIRECTORIES` is included on purpose: the unsandboxed
    mitigation is an environment variable, and an environment variable is
    something the measured command can remove.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    arena = repo / "arena"
    arena.mkdir()
    (arena / "hello.txt").write_text("hi\n")

    befehl = "env -u GIT_CEILING_DIRECTORIES git rev-parse --show-toplevel"

    # The positive control first. Without it, "git found nothing" could just as
    # well mean git was missing, the command was misspelled, or the arena was
    # never inside a repository -- and the test would pass for a reason that
    # has nothing to do with isolation.
    ohne, ohne_log = run_check(
        pruefung(command=befehl), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena, timeout=60,
    )
    assert ohne.exit_code == 0, "the unsandboxed control did not reach the ancestor"
    assert str(repo) in ohne_log.split("--- output ---", 1)[1], (
        "unsandboxed, git is expected to climb out of the arena and name the "
        "ancestor repository -- that is limit 6"
    )

    r, log = run_check(
        pruefung(command=befehl), kandidat(arena),
        run_id="r", iteration=2, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    ausgabe = log.split("--- output ---", 1)[1]
    assert r.exit_code != 0, "git found a repository inside the sandbox"
    assert "not a git repository" in ausgabe
    # git names the boundary it stopped at in its error text, so the ancestor's
    # path appears -- what must not appear is a *toplevel* answer.
    assert f"{repo}\n" not in ausgabe


@braucht_bwrap
def test_the_network_is_unavailable_under_strict(tmp_path):
    """Against a socket this test opens itself, so both directions are real.

    Connecting to an arbitrary address proves little: a refusal and a dropped
    packet look much the same, and a sandbox that never started fails too. A
    listener on loopback that the *unsandboxed* control reaches and the
    sandboxed run does not is the difference that only a network namespace
    explains.
    """
    import socket as sock_mod

    server = sock_mod.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    try:
        arena = arena_mit_datei(tmp_path)
        befehl = (
            "python3 -c \"import socket;"
            f"socket.create_connection(('127.0.0.1',{port}),timeout=3)\""
        )

        ohne, _ = run_check(
            pruefung(command=befehl), kandidat(arena),
            run_id="r", iteration=1, attempt=1, cwd=arena, timeout=30,
        )
        assert ohne.exit_code == 0, (
            "the unsandboxed control could not reach a listener on loopback, so "
            "the sandboxed failure below cannot be attributed to the namespace"
        )

        r, _log = run_check(
            pruefung(command=befehl), kandidat(arena),
            run_id="r", iteration=2, attempt=1, cwd=arena,
            isolation=Isolation.STRICT, timeout=30,
        )
        assert r.isolation.honoured(), "the sandbox never started; nothing was measured"
        assert r.isolation.network_policy == "denied"
        assert r.exit_code != 0, "a connection succeeded inside a network-isolated run"
    finally:
        server.close()


@braucht_bwrap
def test_home_and_tmpdir_point_into_the_scratch_area(tmp_path):
    """HOME/TMPDIR must be redirected to a scratch directory beside the
    arena -- never the arena itself, and never the real host home.

    A plain "the host home is not a substring of the output" check is not
    portable: it silently assumes the host's own `$HOME` cannot lie on the
    same path lineage as `tmp_path`, which does not hold in every sandbox
    this suite runs in. So the property is checked structurally instead --
    against what the runner actually put in `$HOME`, not against where this
    particular machine happens to keep its home directory.
    """
    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command='echo "HOME=$HOME TMPDIR=$TMPDIR"'),
        kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0
    assert ".hoh-scratch-" in log

    output = log.split("--- output ---")[1]
    zeile = next(z for z in output.splitlines() if z.startswith("HOME="))
    home_teil, tmp_teil = zeile.split(" ", 1)
    home_wert = home_teil.split("=", 1)[1]
    tmp_wert = tmp_teil.split("=", 1)[1]

    assert home_wert == tmp_wert
    scratch = pathlib.Path(home_wert)
    assert scratch.parent == arena.parent, "scratch must sit BESIDE the arena"
    assert scratch.name.startswith(".hoh-scratch-")
    assert scratch != arena, "HOME/TMPDIR must not point at the arena itself"
    assert str(scratch) != str(pathlib.Path.home()), (
        "HOME/TMPDIR must not point at the real host home"
    )


@braucht_bwrap
def test_select_refuses_strict_when_no_backend_can_provide_it():
    from hoh.sandbox import select

    with pytest.raises(SandboxUnavailable, match="no backend can provide"):
        select(Isolation.STRICT, backends=[Unbrauchbar()])


# --------------------------------------------------------------------------- #
# The ceilings are applied in the real path, not merely declared in a spec
# --------------------------------------------------------------------------- #


@braucht_bwrap
def test_the_ceilings_are_read_back_from_inside_the_check(tmp_path):
    """O107: requesting STRICT used to *remove* ceilings the weaker path applied.

    `SandboxSpec` declared `memory_bytes` and `max_open_files`, and the runner
    passed neither -- so an acceptance check asking for isolation ran with no
    address-space, CPU or file-size limit at all, while the same check without
    isolation ran under three. The receipt said so honestly
    (`as=none,nofile=none`), which is how it was found: the field was added
    for the evidence, and the evidence immediately contradicted the intent.

    Read back from inside the sandboxed process, not from the spec that asked
    for it. A limit checked by re-reading the configuration is not a limit.
    """
    from hoh.runner import (
        RLIMIT_ADDRESS_SPACE,
        RLIMIT_CPU_SECONDS,
        RLIMIT_FILE_SIZE,
        RLIMIT_OPEN_FILES,
    )

    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="ulimit -v; ulimit -t; ulimit -f; ulimit -n"),
        kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0
    v, t_, f, n = log.split("--- output ---", 1)[1].split()[:4]
    # bash's ulimit reports -v and -f in 1024-byte blocks, -t in seconds.
    assert int(v) * 1024 == RLIMIT_ADDRESS_SPACE
    assert int(t_) == RLIMIT_CPU_SECONDS
    assert int(f) * 1024 == RLIMIT_FILE_SIZE
    assert int(n) == RLIMIT_OPEN_FILES
    assert f"as={RLIMIT_ADDRESS_SPACE}" in r.isolation.resource_limit_policy


@braucht_bwrap
def test_the_unsandboxed_path_applies_the_same_three_ceilings(tmp_path):
    """The control for the one above: STRICT has to be a superset, so the
    comparison is only meaningful if the weaker path really does apply them."""
    from hoh.runner import RLIMIT_ADDRESS_SPACE, RLIMIT_CPU_SECONDS

    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="ulimit -v; ulimit -t"),
        kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
    )
    v, t_ = log.split("--- output ---", 1)[1].split()[:2]
    assert int(v) * 1024 == RLIMIT_ADDRESS_SPACE
    assert int(t_) == RLIMIT_CPU_SECONDS


@braucht_bwrap
def test_the_file_descriptor_ceiling_actually_stops_a_check(tmp_path):
    """Measured against the ceiling that is *in force*, not against the constant.

    The first version opened `RLIMIT_OPEN_FILES + 500` descriptors. On a
    machine whose own soft limit is the common 1024, removing the ceiling
    entirely left the test passing -- it hit the inherited limit instead and
    could not tell the difference. So the target is read out of the receipt.
    """
    arena = arena_mit_datei(tmp_path)
    r0, log0 = run_check(
        pruefung(command="ulimit -n"), kandidat(arena),
        run_id="r", iteration=2, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=60,
    )
    grenze = int(log0.split("--- output ---", 1)[1].split()[0])
    assert f"nofile={grenze}" in r0.isolation.resource_limit_policy, (
        "the receipt reports a ceiling other than the one in force"
    )

    skript = (
        "python3 -c \""
        "import sys;"
        "offen=[];"
        "\nfor i in range(%d):\n"
        "    try:\n"
        "        offen.append(open('/dev/null'))\n"
        "    except OSError:\n"
        "        sys.exit(7)\n"
        "sys.exit(0)\"" % (grenze + 500)
    )
    r, _ = run_check(
        pruefung(command=skript, expect=7), kandidat(arena),
        run_id="r", iteration=3, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=120,
    )
    assert r.exit_code == 7, "the descriptor ceiling did not stop the check"


@braucht_bwrap
def test_a_check_that_overruns_its_deadline_is_inconclusive_not_failed(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(command="sleep 30"), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=2,
    )
    assert r.runner_ok is False
    assert r.exit_code == 124
    assert r.outcome(0).value == "INCONCLUSIVE"


# --------------------------------------------------------------------------- #
# The controller carries the request down to both checks
# --------------------------------------------------------------------------- #


def test_the_controller_passes_its_isolation_to_the_candidate_check(monkeypatch):
    """Wiring, measured at the boundary it crosses.

    Until this was threaded through, `sandbox.py` existed, `runner.py` honoured
    it, and *nothing above them could ask for it* -- the controller called
    `run_check` with no isolation argument at all. A capability nothing can
    request is a capability the product does not have, however well tested its
    implementation is.
    """
    from hoh import controller as ctrl

    gesehen = {}

    def falsch(*a, **kw):
        gesehen["isolation"] = kw.get("isolation")
        raise RuntimeError("stop here: only the argument is under test")

    class Zustand:
        run_id, iteration, attempt = "r", 1, 1

    monkeypatch.setattr(ctrl, "run_check", falsch)
    c = ctrl.Controller.__new__(ctrl.Controller)
    c.store = None
    c.isolation = Isolation.STRICT
    with pytest.raises(RuntimeError):
        c._execute(Zustand(), pruefung(), kandidat(pathlib.Path("/tmp")))
    assert gesehen["isolation"] is Isolation.STRICT


def test_the_controller_defaults_to_the_historical_path(tmp_path):
    from hoh import controller as ctrl

    c = ctrl.Controller.__new__(ctrl.Controller)
    ctrl.Controller.__init__(
        c, store=None, dispatcher=None, spec_path=tmp_path / "spec.md"
    )
    assert c.isolation is None


def test_the_cli_refuses_an_isolation_it_does_not_know():
    from hoh.cli import _isolation

    assert _isolation("none") is None
    assert _isolation("strict") is Isolation.STRICT
    with pytest.raises(ValueError, match="unknown isolation"):
        _isolation("medium")


# --------------------------------------------------------------------------- #
# STRICT is a superset: it keeps everything the weaker path enforces
# --------------------------------------------------------------------------- #


@braucht_bwrap
def test_the_output_ceiling_holds_under_strict_too(tmp_path):
    """The sandboxed branch used to buffer output in memory instead of
    streaming it under a ceiling.

    Measured by a reviewer on 64 MiB of check output: unsandboxed it stopped
    at the ceiling with `runner_ok=False` and INCONCLUSIVE; sandboxed it
    swallowed all of it, reported `truncated=False` and returned PASS. Two
    different verdicts for the same check, decided by a flag that is supposed
    to be about isolation -- and a reinstatement of the memory regression the
    module docstring says was removed.
    """
    arena = arena_mit_datei(tmp_path)
    befehl = "python3 -c \"print('x'*200000)\" ; " * 8
    ceiling = 50_000

    ergebnisse = {}
    for i, iso in enumerate((None, Isolation.STRICT)):
        r, _ = run_check(
            pruefung(command=befehl), kandidat(arena),
            run_id="r", iteration=i + 1, attempt=1, cwd=arena,
            isolation=iso, timeout=120, max_output_bytes=ceiling,
        )
        ergebnisse[iso] = r

    for iso, r in ergebnisse.items():
        assert r.truncated is True, f"{iso}: the ceiling was not applied"
        assert r.runner_ok is False, f"{iso}: overrunning output gave a verdict"
        assert r.exit_code == 126
        assert r.outcome(0).value == "INCONCLUSIVE"


@braucht_bwrap
def test_a_timeout_under_strict_kills_only_the_sandbox(tmp_path):
    """`_supervise` ends an overrunning check with `killpg`. Without its own
    session the sandboxed process shares the runner's process group, so the
    first timeout on this path signalled the runner, its parent and everything
    else in that group -- the test suite terminated itself the first time the
    supervisor was wired in. This test is that accident, pinned."""
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(command="sleep 30"), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=2,
    )
    assert r.exit_code == 124 and r.runner_ok is False
    # Reaching this line at all is the assertion: the process running it was
    # in the group the supervisor signalled.


@braucht_bwrap
def test_the_ceilings_are_reported_as_applied_not_as_requested(tmp_path):
    """A receipt promising 600 CPU-seconds to a check killed at 60 is worse
    than no receipt. The clamp is against the runner's own current soft limit
    as well as the hard one, so a ceiling can never *raise* one."""
    from hoh.sandbox import SandboxSpec, applied_limits

    spec = SandboxSpec(
        candidate=tmp_path, scratch=tmp_path,
        memory_bytes=1 << 62, max_open_files=1 << 30,
        cpu_seconds=10**9, file_size_bytes=1 << 62, timeout=5,
    )
    text = applied_limits(spec)
    import resource

    for name, res in (("nofile", resource.RLIMIT_NOFILE), ("cpu", resource.RLIMIT_CPU)):
        weich, _hart = resource.getrlimit(res)
        if weich != resource.RLIM_INFINITY:
            assert f"{name}={weich}" in text, (
                f"{name} was reported above the limit the runner itself has"
            )


def test_an_unknown_isolation_string_is_refused_before_anything_runs(tmp_path):
    """A plain string slipped past every `is` comparison: the NONE path was
    never taken, the command ran sandboxed, and the record assembly then
    raised AttributeError -- an execution that left no receipt."""
    arena = arena_mit_datei(tmp_path)
    with pytest.raises(ValueError, match="unknown isolation"):
        run_check(
            pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
            cwd=arena, isolation="medium",
        )


def test_a_stringly_typed_isolation_is_coerced_rather_than_misrouted(tmp_path):
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation="none",
    )
    assert r.isolation.requested == "none"
    assert r.isolation.effective == "none"
    assert r.exit_code == 0


# --------------------------------------------------------------------------- #
# The run remembers what it was started under
# --------------------------------------------------------------------------- #


class FakeState:
    def __init__(self, isolation="none"):
        self.isolation = isolation


def test_a_continued_run_keeps_its_isolation_without_the_flag():
    """`hoh run <id>` continues an existing run. Without this, a second
    invocation ran the remaining iterations unsandboxed and said so only in
    the receipts, while the run as a whole still read as a sandboxed one."""
    from hoh.cli import _isolation_for_run

    s = FakeState("strict")
    assert _isolation_for_run(s, None) is Isolation.STRICT
    assert s.isolation == "strict"


def test_raising_the_isolation_mid_run_is_allowed():
    from hoh.cli import _isolation_for_run

    s = FakeState("none")
    assert _isolation_for_run(s, "strict") is Isolation.STRICT
    assert s.isolation == "strict"


def test_lowering_the_isolation_mid_run_is_refused():
    from hoh.cli import _isolation_for_run

    s = FakeState("strict")
    with pytest.raises(ValueError, match="part of it under a sandbox"):
        _isolation_for_run(s, "none")
    assert s.isolation == "strict", "a refused change must not take effect"


def test_the_state_carries_the_field_so_it_survives_a_restart(tmp_path):
    from hoh.contracts import RunState

    s = RunState(
        run_id="r", repo_path=str(tmp_path), project_name="p",
        spec_path=str(tmp_path / "s.md"), spec_digest="d", policy_digest="d",
        profile_digest="d", isolation="strict",
    )
    wieder = RunState.model_validate_json(s.model_dump_json())
    assert wieder.isolation == "strict"
    # The default is the historical path, so nothing changes for runs that
    # never asked.
    assert RunState(
        run_id="r", repo_path=str(tmp_path), project_name="p",
        spec_path=str(tmp_path / "s.md"), spec_digest="d", policy_digest="d",
        profile_digest="d",
    ).isolation == "none"


# --------------------------------------------------------------------------- #
# The verdict is checkable without trusting the verdict
# --------------------------------------------------------------------------- #


@braucht_bwrap
def test_the_receipt_carries_both_sides_of_the_namespace_comparison(tmp_path):
    """`verified_from_inside` is this runner's verdict on two numbers. A reader
    who does not want to take the verdict on trust needs the numbers -- and the
    code that produces the verdict has already been wrong about exactly this
    once."""
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    iso = r.isolation
    assert set(iso.observed_namespaces) == {"mnt_ns", "net_ns"}
    assert set(iso.runner_namespaces) == {"mnt_ns", "net_ns"}
    # Redo the comparison by hand: that is the point of publishing both sides.
    for schluessel in ("mnt_ns", "net_ns"):
        assert iso.observed_namespaces[schluessel] != iso.runner_namespaces[schluessel]
        assert iso.observed_namespaces[schluessel].startswith(
            schluessel.split("_")[0]
        )


def test_the_unsandboxed_path_publishes_no_comparison(tmp_path):
    """There is no claim there, so there is nothing to check -- and an empty
    dict says that more honestly than a pair of equal numbers would."""
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena
    )
    assert r.isolation.observed_namespaces == {}
    assert r.isolation.runner_namespaces == {}


# --------------------------------------------------------------------------- #
# The check must not be able to write its own proof
# --------------------------------------------------------------------------- #


class OhneNamespace:
    """Runs the command as a plain subprocess while claiming to prove isolation.

    The forgery vehicle. The proof used to be a file in the scratch directory
    -- which is bind-mounted read-write and is also the check's `$HOME` and
    `$TMPDIR` -- and the runner read it *after* the check finished. So a
    three-line `echo` into `$HOME/.hoh-sandbox-proof` made a bare, unsandboxed
    `bash` report `honoured() == True`, `candidate_mount_mode="read-only"`,
    `network_policy="denied"`, and a product PASS.
    """

    name = "no-namespace"

    def unavailable(self) -> str | None:
        return None

    def plan(self, argv, spec):
        from hoh.sandbox import MARKER_PROLOGUE, LaunchPlan

        return LaunchPlan(
            argv=[argv[0], "-c", MARKER_PROLOGUE + argv[2]],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(spec.scratch),
                "TMPDIR": str(spec.scratch),
                "HOH_PROOF_FD": str(spec.proof_fd),
                "HOH_CANDIDATE": str(spec.candidate),
            },
            cwd=str(spec.candidate), proves_isolation=True,
        )

    def run(self, argv, spec):                   # pragma: no cover - unused
        raise NotImplementedError


def test_a_check_cannot_forge_the_proof_by_writing_the_scratch_file(tmp_path):
    """The old attack, against the new mechanism.

    The check writes exactly the three lines that used to be believed --
    plausible namespace ids and `ro` -- into the file the proof used to live
    in. There is no such file any more: the proof went out over a pipe before
    the check started, and the pipe is append-only.
    """
    arena = arena_mit_datei(tmp_path)
    faelschung = (
        'printf "mnt:[4026999999]\\nnet:[4026999998]\\nro\\n" '
        '> "$HOME/.hoh-sandbox-proof"; echo done'
    )
    r, log = run_check(
        pruefung(command=faelschung), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, sandbox=OhneNamespace(), timeout=60,
    )
    assert r.isolation.observed_namespaces.get("mnt_ns") != "mnt:[4026999999]", (
        "the forged file was believed"
    )
    assert r.isolation.effective == "none"
    assert r.isolation.fallback_to_none is True
    assert not r.isolation.honoured()
    assert r.runner_ok is False
    assert r.outcome(0).value == "INCONCLUSIVE"


def test_a_check_appending_to_the_proof_pipe_cannot_change_it(tmp_path):
    """The pipe is shared with the check's descriptors, so it can append. It
    cannot alter the bytes the prologue already sent, and only the first three
    lines are parsed."""
    arena = arena_mit_datei(tmp_path)
    anhaengen = (
        'echo "mnt:[4026999999]" >&$HOH_PROOF_FD; '
        'echo "net:[4026999998]" >&$HOH_PROOF_FD; '
        'echo ro >&$HOH_PROOF_FD; echo done'
    )
    r, _ = run_check(
        pruefung(command=anhaengen), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, sandbox=OhneNamespace(), timeout=60,
    )
    assert r.isolation.observed_namespaces.get("mnt_ns") != "mnt:[4026999999]"
    assert not r.isolation.honoured()


@braucht_bwrap
def test_a_check_that_tidies_its_own_tmpdir_is_not_punished(tmp_path):
    """An ordinary passing check that cleans up after itself used to destroy
    the proof and be scored as an infrastructure refusal."""
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(command='echo "tests: 12 passed"; rm -f "$TMPDIR"/.hoh-* ; exit 0'),
        kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0 and r.runner_ok is True
    assert r.isolation.honoured()


@braucht_bwrap
def test_a_candidate_that_was_never_mounted_is_not_read_only(tmp_path):
    """`[ -w path ]` is false for a path that is not there.

    Two-valued logic therefore reported a candidate that had never been bound
    into the sandbox as read-only -- the exact failure the proof exists to
    catch, producing a clean receipt with no adversarial check involved.
    """
    from hoh.sandbox import MARKER_PROLOGUE, BubblewrapSandbox, LaunchPlan

    class OhneKandidatenBind(BubblewrapSandbox):
        name: str = "no-candidate-bind"

        def plan(self, argv, spec):
            # The real bwrap argv with the candidate's bind removed.
            voll = self._argv([argv[0], "-c", MARKER_PROLOGUE + argv[2]], spec)
            gekuerzt, i = [], 0
            while i < len(voll):
                if (voll[i] == "--ro-bind" and i + 1 < len(voll)
                        and voll[i + 1] == str(spec.candidate)):
                    i += 3            # drop the candidate's bind entirely
                    continue
                if voll[i] == "--chdir":
                    # ... and start somewhere that exists, so the command runs
                    # and can report. Otherwise bwrap dies at chdir and the
                    # case under test never happens.
                    gekuerzt += ["--chdir", "/tmp"]
                    i += 2
                    continue
                gekuerzt.append(voll[i])
                i += 1
            return LaunchPlan(argv=gekuerzt, env={}, cwd=None, proves_isolation=True)

    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="true"), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, sandbox=OhneKandidatenBind(), timeout=60,
    )
    assert r.isolation.candidate_mount_mode == "not present"
    assert not r.isolation.honoured()
    assert r.runner_ok is False
    assert "not present inside the sandbox" in log


@braucht_bwrap
def test_the_network_policy_is_derived_from_the_namespace_not_the_request(tmp_path):
    """It was `"denied" if not spec.network` -- the request echoed back, in the
    field a reader would most want measured."""
    from hoh.sandbox import MARKER_PROLOGUE, BubblewrapSandbox, LaunchPlan

    class MitNetz(BubblewrapSandbox):
        name: str = "shares-the-network"

        def plan(self, argv, spec):
            voll = self._argv([argv[0], "-c", MARKER_PROLOGUE + argv[2]], spec)
            return LaunchPlan(
                argv=[a for a in voll if a != "--unshare-net"],
                env={}, cwd=None, proves_isolation=True,
            )

    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(command="true"), kandidat(arena),
        run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, sandbox=MitNetz(), timeout=60,
    )
    assert r.isolation.network_policy == "allowed"
    assert not r.isolation.honoured()
    assert r.runner_ok is False


def test_a_binary_proof_does_not_escape_without_a_receipt(tmp_path):
    """A `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it used
    to escape `run_check` entirely -- a run that ended with no receipt at all,
    which is the one outcome this module exists to prevent."""
    arena = arena_mit_datei(tmp_path)

    class BinaererBeweis(OhneNamespace):
        name = "binary-proof"

        def plan(self, argv, spec):
            plan = OhneNamespace.plan(self, argv, spec)
            return plan.__class__(
                argv=[argv[0], "-c",
                      f'printf "\\xff\\xfe\\x00\\x80" >&{spec.proof_fd}; ' + argv[2]],
                env=plan.env, cwd=plan.cwd, proves_isolation=True,
            )

    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=BinaererBeweis(), timeout=60,
    )
    assert r.receipt_id, "run_check must always return a receipt"
    assert not r.isolation.honoured()


# --------------------------------------------------------------------------- #
# The properties that survived mutation, now pinned
# --------------------------------------------------------------------------- #


def test_the_runner_sets_verified_from_inside_to_false_when_nothing_proved(tmp_path):
    """Hard-coding this field to True passed the entire suite.

    It is the field `honoured()` hangs on and the one the assurance metric's
    derivation advertises as the namespace comparison, and every test that
    mentioned it either asserted it was True or built the record by hand. A
    field nothing can be seen to falsify is not a measurement.
    """
    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=LuegtUeberIsolation(),
        timeout=60,
    )
    assert r.isolation.verified_from_inside is False
    assert r.isolation.effective == "unverified"


def test_a_short_proof_does_not_default_the_missing_line(tmp_path):
    """Defaulting `candidate_writable` to "ro" for a two-line proof survived
    every test: the convenient weaker value taken as the measurement, one more
    time."""
    from hoh.sandbox import marker_reading

    assert marker_reading("mnt:[1]\nnet:[2]\n") == {
        "mnt_ns": "mnt:[1]", "net_ns": "net:[2]"
    }
    assert "candidate_writable" not in marker_reading("mnt:[1]\n")
    # And only the first three lines are ever read.
    viele = "\n".join(f"line{i}" for i in range(50))
    assert set(marker_reading(viele)) == {"mnt_ns", "net_ns", "candidate_writable"}
    assert marker_reading(viele)["mnt_ns"] == "line0"


def test_a_two_line_proof_is_not_honoured(tmp_path):
    arena = arena_mit_datei(tmp_path)

    class Kurz(OhneNamespace):
        name = "short-proof"

        def plan(self, argv, spec):
            plan = OhneNamespace.plan(self, argv, spec)
            return plan.__class__(
                argv=[argv[0], "-c",
                      f'{{ readlink /proc/self/ns/mnt; readlink /proc/self/ns/net; }} '
                      f">&{spec.proof_fd}; " + argv[2]],
                env=plan.env, cwd=plan.cwd, proves_isolation=True,
            )

    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, sandbox=Kurz(), timeout=60,
    )
    # This backend also runs in the runner's own namespaces, and that is the
    # stronger complaint, so it is the one reported. Either way the run is
    # refused; the missing third line is covered as a unit above.
    assert not r.isolation.honoured()
    assert r.runner_ok is False
    assert r.isolation.complaint


def test_the_soft_limit_clamp_is_measured_not_assumed():
    """O107's clamp had no test: `test_the_ceilings_are_reported_as_applied...`
    never ran under lowered soft limits, so removing the clamp survived."""
    import resource

    from hoh.sandbox import SandboxSpec, _rlimit_paare

    weich, hart = resource.getrlimit(resource.RLIMIT_NOFILE)
    spec = SandboxSpec(
        candidate=pathlib.Path("/tmp"), scratch=pathlib.Path("/tmp"),
        max_open_files=(weich + 1000) if weich != resource.RLIM_INFINITY else 1 << 30,
    )
    paare = dict(_rlimit_paare(spec))
    if weich != resource.RLIM_INFINITY:
        assert paare[resource.RLIMIT_NOFILE] == weich, (
            "a ceiling that raises the runner's own soft limit is not a ceiling"
        )


def test_the_unsandboxed_path_clamps_the_same_way():
    """The twin was left behind when O107 was fixed on the sandboxed side."""
    import resource

    from hoh.runner import _unsandboxed_limits

    angewandt = dict(_unsandboxed_limits())
    for res in (resource.RLIMIT_CPU, resource.RLIMIT_AS, resource.RLIMIT_FSIZE):
        weich, hart = resource.getrlimit(res)
        for grenze in (weich, hart):
            if grenze != resource.RLIM_INFINITY:
                assert angewandt[res] <= grenze, (
                    f"{res}: the unsandboxed path reported a ceiling above the "
                    "one the runner itself has"
                )


def test_the_receipt_reports_the_unsandboxed_ceilings_as_applied(tmp_path):
    from hoh.runner import _unsandboxed_limits

    arena = arena_mit_datei(tmp_path)
    r, _ = run_check(
        pruefung(), kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena
    )
    for _res, wert in _unsandboxed_limits():
        assert f"={wert}" in r.isolation.resource_limit_policy


@braucht_bwrap
def test_the_pid_namespace_is_unshared(tmp_path):
    """Deleting `--unshare-pid/ipc/uts` passed every test."""
    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="echo $$; ls /proc | grep -c '^[0-9]*$'"),
        kandidat(arena), run_id="r", iteration=1, attempt=1,
        cwd=arena, isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0
    zeilen = log.split("--- output ---", 1)[1].split()
    # In its own pid namespace the shell is pid 1 or 2, and /proc lists a
    # handful of processes rather than the machine's.
    assert int(zeilen[0]) < 10, "the sandbox shares the runner's pid namespace"
    assert int(zeilen[1]) < 20


@braucht_bwrap
def test_the_uts_and_ipc_namespaces_are_unshared(tmp_path):
    """Compared by namespace id, not by hostname: `--unshare-uts` gives the
    sandbox its own UTS namespace without renaming the host, so `hostname`
    reports the same string on both sides and proves nothing."""
    import os as os_mod

    arena = arena_mit_datei(tmp_path)
    r, log = run_check(
        pruefung(command="readlink /proc/self/ns/uts; readlink /proc/self/ns/ipc"),
        kandidat(arena), run_id="r", iteration=1, attempt=1, cwd=arena,
        isolation=Isolation.STRICT, timeout=60,
    )
    assert r.exit_code == 0
    uts, ipc = log.split("--- output ---", 1)[1].split()[:2]
    assert uts != os_mod.readlink("/proc/self/ns/uts"), "the uts namespace is shared"
    assert ipc != os_mod.readlink("/proc/self/ns/ipc"), "the ipc namespace is shared"


def test_nosandbox_refuses_strict_through_the_plan_as_well(tmp_path):
    """`NoSandbox.plan` is the path the runner takes now; the refusal used to
    live only in `run()`, which the runner no longer calls."""
    from hoh.sandbox import Isolation as I, NoSandbox, SandboxSpec, SandboxUnavailable

    spec = SandboxSpec(
        candidate=tmp_path, scratch=tmp_path, isolation=I.STRICT
    )
    with pytest.raises(SandboxUnavailable, match="cannot provide"):
        NoSandbox().plan(["/bin/bash", "-c", "true"], spec)


# --------------------------------------------------------------------------- #
# The rule the CI had to find for us
# --------------------------------------------------------------------------- #


def test_every_test_that_really_needs_a_sandbox_says_so():
    """A test that asks the *real* backend for STRICT needs `@braucht_bwrap`.

    Found by CI, not here: on a runner that cannot create the namespace, the
    runner refuses -- correctly -- and the check comes back 126. On this
    machine it passes. One test in this file was missing the marker, and the
    only place that could show was a machine that is not this one.

    So the rule is checked here rather than remembered. A test that injects its
    own backend is exempt: it never touches bubblewrap.
    """
    import ast

    quelle = pathlib.Path(__file__).read_text()
    zeilen = quelle.splitlines()
    fehlend = []
    for knoten in ast.parse(quelle).body:
        if not isinstance(knoten, ast.FunctionDef):
            continue
        if not knoten.name.startswith("test_"):
            continue
        text = "\n".join(zeilen[knoten.lineno - 1: knoten.end_lineno])
        if "run_check(" not in text or "Isolation.STRICT" not in text:
            continue
        if "sandbox=" in text:
            continue
        namen = {getattr(d, "id", getattr(d, "attr", ""))
                 for d in knoten.decorator_list}
        if "braucht_bwrap" not in namen:
            fehlend.append(knoten.name)
    assert not fehlend, (
        "these ask the real backend for STRICT and will fail on a machine "
        f"without namespaces: {fehlend}"
    )
