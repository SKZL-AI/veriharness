"""What a fresh orchestrator session decides, holding nothing but the state file.

This is the test that matters for the orchestration layer, and the reason is
worth stating rather than assuming. The campaign that produced this project's
own release ran its orchestration as an agent session's behaviour: the session
knew which runs existed, what each had produced, what the gates had said and
what still had to happen. None of that was written anywhere a *second* session
could read. If the session had ended, recovery meant a person reconstructing
intent from git history.

So every test below kills the session at a different point and asks the same
question of what survives on disk: `resume / retry / evaluate / block / repair
/ closed`. A state that cannot answer it unambiguously is a state that needs a
human, and the tests say so rather than picking a plausible answer.

Two rules are load-bearing throughout:

* An unclear state **blocks**. It does not restart. Restarting on an unclear
  state is how work gets done twice or silently lost.
* `NOT_RUN` is never green. A closure that checked nothing has established
  nothing, and an empty gate set is the purest form of that mistake.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from hoh.project import (
    ActionClass,
    DecisionKind,
    DecisionRecord,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
    digest_obj,
)
from hoh.projectstore import (
    BLOCK,
    CLOSED,
    EVALUATE,
    REPAIR,
    RESUME,
    RETRY,
    ProjectStore,
    list_projects,
    resume_decision,
)
from hoh.store import LockBusy, StaleWrite, StoreError


def projekt(tmp_path: Path, **kw) -> ProjectState:
    return ProjectState(project_id=kw.pop("project_id", "p"), repo_path=str(tmp_path), **kw)


def gate(name: str, outcome: GateOutcome = GateOutcome.GREEN, subject: str = "abc1234") -> GateResult:
    return GateResult(name=name, outcome=outcome, subject=subject, exit_code=0 if outcome is GateOutcome.GREEN else 1)


# --------------------------------------------------------------------------- #
# The ten fault-injection points
# --------------------------------------------------------------------------- #

def test_kill_waehrend_planung_laesst_knoten_ready(tmp_path):
    """1. Killed before the run started: the node is still READY, resume it.

    Nothing was dispatched, so there is nothing to evaluate and nothing to
    undo. This is the only kill point with an unambiguous cheap answer.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a")]
    verdikt, grund = resume_decision(s)
    assert verdikt == RESUME, grund
    assert "a" in grund


@pytest.mark.parametrize("phase", ["development", "qa", "kurz vor dem Merge"])
def test_kill_waehrend_eines_laufs_wird_ausgewertet_nicht_geraten(tmp_path, phase):
    """2, 3, 4. Killed mid-run: RUNNING with no live controller is ambiguous.

    Whether the work finished, crashed halfway through a merge, or is still
    going in a process this session cannot see is *not* readable from project
    state. All three kill points therefore produce the same verdict, and it is
    EVALUATE rather than RESUME: re-dispatching a run that may already have
    merged is how a candidate gets applied twice.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.RUNNING, run_id="a", note=phase)]
    verdikt, grund = resume_decision(s)
    assert verdikt == EVALUATE, grund
    assert "RUNNING" in grund
    assert "crashed" in grund or "finished" in grund


def test_kill_nach_merge_vor_globaler_closure_verlangt_reparatur(tmp_path):
    """5. Killed after the merge, before global closure ran.

    Every node has settled, so the DAG is terminal -- and this is exactly the
    state that must not be read as done. No gate has run, so nothing has been
    established about the merged result. `DAG_TERMINAL != RC_CLOSED`.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.dag_terminal()
    assert not s.rc_closed()
    verdikt, grund = resume_decision(s)
    assert verdikt == REPAIR, grund
    assert "no global gate has run" in grund


def test_kill_waehrend_der_reparaturknoten_erzeugung(tmp_path):
    """6. Killed while creating a repair node: the half-made node is READY.

    The gate that produced it is on record as red, the repair node exists and
    has not run. The next session picks it up rather than re-deriving whether
    a repair is needed -- the decision record is what makes that safe.
    """
    s = projekt(tmp_path)
    s.nodes = [
        TaskNode(id="a", lifecycle=Lifecycle.MERGED),
        TaskNode(id="rep1", repair_of="a", closure_generation=1),
    ]
    s.gates = [gate("union", GateOutcome.RED)]
    s.closure_generation = 1
    verdikt, grund = resume_decision(s)
    assert verdikt == RESUME, grund
    assert "rep1" in grund
    assert not s.rc_closed(), "an outstanding repair node is not a closed release"


def test_neuer_orchestrator_ohne_jeden_kontext_entscheidet_gleich(tmp_path):
    """7. Restart with a completely fresh context.

    The point of the whole module: a session that has never seen this project
    reads the file and reaches the same verdict as the one that wrote it. No
    conversation, no memory, no inference from git.
    """
    laden = ProjectStore(tmp_path, "p")
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED), TaskNode(id="b", dependencies=["a"])]
    laden.create(s)

    # A different store object, as a different process would construct it.
    frisch = ProjectStore(tmp_path, "p").read_state()
    assert resume_decision(frisch) == resume_decision(s)
    assert resume_decision(frisch)[0] == RESUME


def test_doppelter_orchestratorstart_wird_abgewiesen(tmp_path):
    """8. Two controllers: the second is refused, not queued behind the first."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    with s.lock():
        zweiter = ProjectStore(tmp_path, "p")
        with pytest.raises(LockBusy) as exc:
            with zweiter.lock():
                pass
    assert "already held" in str(exc.value)
    # Released afterwards -- a crashed orchestrator must not wedge the project.
    with ProjectStore(tmp_path, "p").lock():
        pass


def test_provider_ausfall_ist_kein_verdikt(tmp_path):
    """9. A provider 429/529 leaves the node BLOCKED, and blocked is not failed.

    The distinction matters because a quota exhaustion looks, from inside the
    loop, exactly like a role that broke its contract. Recording it as a
    rejection would burn the node's retry budget on an outage.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="provider 429")]
    verdikt, grund = resume_decision(s)
    assert verdikt == BLOCK, grund
    assert "outside the orchestrator" in grund
    assert s.nodes[0].rejections == 0, "an outage is not a rejection"


def test_beschaedigter_zustand_blockiert_statt_zu_ueberschreiben(tmp_path):
    """10. Corrupted or incomplete state: unreadable blocks, it is not replaced."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    fortschritt = s.read_state()
    fortschritt.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.write_state(fortschritt)          # now a predecessor exists to fall back to

    s.state_path.write_text('{"project_id": "p", "nodes": [ {"id": ', encoding="utf-8")
    with pytest.raises(StoreError) as exc:
        s.read_state()
    assert "unreadable" in str(exc.value)

    # The damaged state blocks; it is not silently replaced. And the way back
    # is on disk, because writing parks the predecessor rather than deleting
    # it. Note what is *not* claimed: the very first write of a project has no
    # predecessor, so "there is always a way back" would be false. What holds
    # is that no write ever destroys one that existed.
    geparkt = s.parked_states()
    assert geparkt, "the parked predecessor is the way back"
    zurueck = ProjectState.model_validate_json(geparkt[-1].read_text())
    assert zurueck.project_id == "p"


# --------------------------------------------------------------------------- #
# Storage discipline
# --------------------------------------------------------------------------- #

def test_stale_writer_wird_abgewiesen(tmp_path):
    """The lock serializes; only the sequence number catches a stale writer.

    A crashed orchestrator releases its lock when its process dies, so the next
    session can hold the lock legitimately while carrying a state it read
    before the crash. Fencing is what makes that safe.
    """
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    alt = s.read_state()          # what a crashed session would still hold
    neu = s.read_state()
    neu.nodes = [TaskNode(id="a")]
    s.write_state(neu)            # the live session advances
    alt.nodes = [TaskNode(id="z")]
    with pytest.raises(StaleWrite) as exc:
        s.write_state(alt)
    assert "reload instead of overwriting" in str(exc.value)
    assert [n.id for n in s.read_state().nodes] == ["a"]


def test_schreiben_loescht_nie_den_vorgaenger(tmp_path):
    """Replace by versioned rename, never delete -- and the counter never
    collides, even when earlier versions are moved away."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    for i in range(3):
        st = s.read_state()
        st.nodes = [TaskNode(id=f"n{i}")]
        s.write_state(st)
    geparkt = s.parked_states()
    assert len(geparkt) == 3, geparkt
    versionen = sorted(int(p.name.split(".v")[1].split(".")[0]) for p in geparkt)
    assert versionen == [1, 2, 3]

    # Archive the earliest, as pruning would, then write again: a count-based
    # counter would now reuse v3 and collide.
    archiv = s.dir / "attic"
    archiv.mkdir()
    geparkt[0].rename(archiv / geparkt[0].name)
    st = s.read_state()
    st.nodes = [TaskNode(id="danach")]
    s.write_state(st)
    neu = [int(p.name.split(".v")[1].split(".")[0]) for p in s.parked_states()]
    assert max(neu) == 4, f"counter jumped backwards: {neu}"


def test_start_ist_idempotent(tmp_path):
    """A retried start returns the existing project rather than replacing it.

    Retrying a command is the most ordinary thing a resuming session does; a
    start that overwrote would make it data loss.
    """
    s = ProjectStore(tmp_path, "p")
    erst = projekt(tmp_path)
    erst.nodes = [TaskNode(id="a")]
    s.create(erst)
    wieder = s.create(projekt(tmp_path, project_id="p"))
    assert [n.id for n in wieder.nodes] == ["a"]


def _halte_lock(pfad: str, bereit, weiter):
    store = ProjectStore(pfad, "p")
    with store.lock():
        bereit.set()
        weiter.wait(timeout=10)


def test_lock_haelt_ueber_prozessgrenzen(tmp_path):
    """The lock is a real file lock, not an in-process convention."""
    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    ctx = multiprocessing.get_context("fork")
    bereit, weiter = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_halte_lock, args=(str(tmp_path), bereit, weiter))
    p.start()
    try:
        assert bereit.wait(timeout=10), "child never took the lock"
        with pytest.raises(LockBusy):
            with s.lock():
                pass
    finally:
        weiter.set()
        p.join(timeout=10)
    with s.lock():
        pass


# --------------------------------------------------------------------------- #
# Closure is a fixpoint, not the end of a list
# --------------------------------------------------------------------------- #

def test_terminaler_dag_ist_keine_geschlossene_freigabe(tmp_path):
    """The distinction this project had to learn twice, as an assertion."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.dag_terminal()
    assert not s.rc_closed()

    s.gates = [gate("union"), gate("claims")]
    s.measurement_head = "abc1234"
    assert s.rc_closed()
    assert resume_decision(s)[0] == CLOSED


def test_not_run_zaehlt_nie_als_gruen(tmp_path):
    """A gate that did not run is not a gate that passed."""
    g = gate("union", GateOutcome.NOT_RUN)
    assert not g.counts_as_green
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.gates = [g]
    assert not s.gates_green()
    assert not s.rc_closed()
    verdikt, grund = resume_decision(s)
    assert verdikt == REPAIR
    assert "union" in grund


def test_leere_gate_menge_ist_nicht_gruen(tmp_path):
    """Zero gates is not green. Returning True for an empty set is the purest
    form of the failure this project exists to prevent."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    assert s.gates == []
    assert not s.gates_green()


def test_offener_reparaturknoten_verhindert_closure(tmp_path):
    """Green gates plus an outstanding repair node is still not closed."""
    s = projekt(tmp_path)
    s.nodes = [
        TaskNode(id="a", lifecycle=Lifecycle.MERGED),
        TaskNode(id="rep1", repair_of="a", lifecycle=Lifecycle.READY),
    ]
    s.gates = [gate("union")]
    assert not s.dag_terminal()
    assert not s.rc_closed()
    s.nodes[1].lifecycle = Lifecycle.MERGED
    assert s.rc_closed()


def test_nur_das_juengste_ergebnis_je_gate_zaehlt(tmp_path):
    """A gate that was red and is now green counts as green -- and the reverse."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.gates = [gate("union", GateOutcome.RED), gate("union", GateOutcome.GREEN)]
    assert s.gates_green()
    s.gates.append(gate("union", GateOutcome.RED))
    assert not s.gates_green()


# --------------------------------------------------------------------------- #
# The DAG itself
# --------------------------------------------------------------------------- #

def test_unbekannte_abhaengigkeit_blockiert_statt_zu_starten(tmp_path):
    """A dependency on a node that does not exist reads, to a scheduler,
    exactly like a satisfied one. It must not."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="b", dependencies=["gibtesnicht"])]
    assert s.ready() == []
    assert s.unknown_dependencies() == {"b": ["gibtesnicht"]}
    verdikt, grund = resume_decision(s)
    assert verdikt == BLOCK
    assert "gibtesnicht" in grund


def test_aufgegebener_knoten_blockiert_seine_nachfolger_nicht(tmp_path):
    """ABANDONED settles a node: a dependent may proceed.

    Otherwise one abandoned node wedges every path behind it, and the only way
    out is editing state by hand.
    """
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", lifecycle=Lifecycle.ABANDONED), TaskNode(id="b", dependencies=["a"])]
    assert [n.id for n in s.ready()] == ["b"]


def test_ablehnung_ist_eingabe_der_naechsten_iteration(tmp_path):
    """A rejected node returns as READY, and the verdict says retry, not resume."""
    s = projekt(tmp_path)
    s.nodes = [TaskNode(id="a", rejections=2)]
    verdikt, grund = resume_decision(s)
    assert verdikt == RETRY, grund
    assert "not an endpoint" in grund


def test_externe_aktion_bleibt_als_solche_erkennbar(tmp_path):
    """The action class survives a restart: it is state, not a judgement the
    next session has to make again."""
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="push", action_class=ActionClass.EXTERNAL)]
    s.create(st)
    assert s.read_state().nodes[0].action_class is ActionClass.EXTERNAL


# --------------------------------------------------------------------------- #
# Decisions are records, not remembered intentions
# --------------------------------------------------------------------------- #

def test_entscheidung_ist_an_ihren_inhalt_gebunden(tmp_path):
    """A decision record's digest is derived from its payload, and a mismatch
    is refused: a rebound record is a new record, not an edited one."""
    d = DecisionRecord(
        id="D1",
        kind=DecisionKind.MERGE_RELEASE,
        actor="orchestrator",
        reason="every criterion passed and one was proven red on the predecessor",
        payload={"node": "a", "candidate": "deadbeef"},
    )
    assert d.payload_digest == digest_obj({"node": "a", "candidate": "deadbeef"})

    with pytest.raises(ValueError) as exc:
        DecisionRecord(
            id="D2",
            kind=DecisionKind.MERGE_RELEASE,
            actor="orchestrator",
            reason="x",
            payload={"node": "a"},
            payload_digest="0000000000000000",
        )
    assert "does not match its payload" in str(exc.value)


def test_digest_ist_reihenfolgeunabhaengig(tmp_path):
    """Same decision, different key order, same digest -- otherwise binding is
    decorative."""
    assert digest_obj({"a": 1, "b": 2}) == digest_obj({"b": 2, "a": 1})
    assert digest_obj({"a": 1}) != digest_obj({"a": 2})


def test_entscheidungen_ueberleben_den_neustart(tmp_path):
    """The record of *why* survives, not just the resulting state.

    This is what a fresh session cannot otherwise recover: the tree shows that
    a node was abandoned, never that it was abandoned because its premise had
    been disproved.
    """
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.ABANDONED)]
    st.decisions = [
        DecisionRecord(
            id="D1",
            kind=DecisionKind.ABANDON_NODE,
            actor="orchestrator",
            reason="premise disproved by the d8b gate failure; re-running would repeat it",
            payload={"node": "a"},
            evidence=["gate:union@abc1234"],
        )
    ]
    s.create(st)
    zurueck = ProjectStore(tmp_path, "p").read_state()
    assert zurueck.decisions[0].reason.startswith("premise disproved")
    assert zurueck.decisions[0].payload_digest == st.decisions[0].payload_digest


def test_gate_ergebnis_traegt_seinen_gegenstand(tmp_path):
    """A gate result names the commit it ran against.

    A green recorded without its subject cannot later be told apart from a
    green that has gone stale -- this project shipped a release whose figures
    were wrong for exactly that reason.
    """
    g = gate("union", subject="30ff053")
    assert g.subject == "30ff053"
    with pytest.raises(Exception):
        GateResult(name="union", outcome=GateOutcome.GREEN)  # type: ignore[call-arg]


def test_projekte_werden_aufgelistet(tmp_path):
    assert list_projects(tmp_path) == []
    ProjectStore(tmp_path, "eins").create(projekt(tmp_path, project_id="eins"))
    ProjectStore(tmp_path, "zwei").create(projekt(tmp_path, project_id="zwei"))
    assert list_projects(tmp_path) == ["eins", "zwei"]


def test_zustand_ist_reines_json_und_wieder_einlesbar(tmp_path):
    """The state file is readable by something that is not this code.

    A durable state that only its own writer can parse is a slightly more
    durable conversation.
    """
    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", writes=["README.md"], semantic_reads=["tools/x.py"])]
    st.gates = [gate("union")]
    s.create(st)
    roh = json.loads(s.state_path.read_text())
    assert roh["nodes"][0]["writes"] == ["README.md"]
    assert roh["gates"][0]["subject"] == "abc1234"
    assert roh["schema_version"] >= 1


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #

def test_cli_meldet_unlesbaren_zustand_mit_eigenem_exitcode(tmp_path, capsys):
    """A script reading `hoh project` must not see 0 for a damaged state.

    Three distinct codes, because the remedies differ: 0 answered, 2 no such
    project, 3 the state exists and cannot be trusted.
    """
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 0
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    assert main(["--root", str(tmp_path), "project", "status", "gibtesnicht"]) == 2

    fortschritt = s.read_state()
    fortschritt.nodes = [TaskNode(id="a")]
    s.write_state(fortschritt)
    s.state_path.write_text("{ not json", encoding="utf-8")
    assert main(["--root", str(tmp_path), "project", "status", "p"]) == 3


def test_cli_resume_ist_maschinenlesbar(tmp_path, capsys):
    """`resume` prints JSON: the verdict is for a caller, not only a reader."""
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    st.gates = [gate("union")]
    st.measurement_head = "abc1234"
    s.create(st)

    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "resume", "p"]) == 0
    daten = json.loads(capsys.readouterr().out)
    assert daten["verdict"] == CLOSED
    assert daten["rc_closed"] is True
    assert daten["measurement_head"] == "abc1234"


def test_cli_list_zeigt_jedes_projekt_mit_verdikt(tmp_path, capsys):
    from hoh.cli import main

    ProjectStore(tmp_path, "eins").create(projekt(tmp_path, project_id="eins"))
    ProjectStore(tmp_path, "zwei").create(projekt(tmp_path, project_id="zwei"))
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "project", "list"]) == 0
    aus = capsys.readouterr().out
    assert "eins" in aus and "zwei" in aus


# --------------------------------------------------------------------------- #
# A blocked node needs a way back, and it has to leave a record
# --------------------------------------------------------------------------- #

def test_unblock_verlangt_einen_grund_und_schreibt_ihn_auf(tmp_path):
    """Refusing to decide was right; leaving no way back is not.

    Without this the only route out of a blocked project is editing the state
    file by hand -- the one thing a durable, digest-bound state exists to make
    unnecessary. The reason is required, because "someone unblocked it" with no
    why is exactly the unrecoverable intent this layer was built to stop losing.
    """
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED,
                         note="accepted but the candidate did not land")]
    s.create(st)

    nachher = unblock(s, "a", "untracked build output removed; the merge can land now")
    n = nachher.node("a")
    assert n.lifecycle is Lifecycle.READY
    # The original reason stays readable: what it was blocked for matters after
    # it is running again.
    assert "did not land" in n.note
    assert "untracked build output" in n.note
    d = nachher.decisions[-1]
    assert d.actor == "human"
    assert "untracked build output" in d.reason
    assert d.payload["was_blocked_for"].startswith("accepted but")


def test_unblock_verweigert_was_nicht_blockiert_ist(tmp_path):
    """Unblocking something that is not blocked would hide whatever it is
    actually doing."""
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)]
    s.create(st)
    with pytest.raises(StoreError) as exc:
        unblock(s, "a", "because I said so")
    assert "not BLOCKED" in str(exc.value)


def test_unblock_kennt_unbekannte_knoten_nicht(tmp_path):
    from hoh.projectstore import unblock

    s = ProjectStore(tmp_path, "p")
    s.create(projekt(tmp_path))
    with pytest.raises(StoreError):
        unblock(s, "gibtesnicht", "x")


def test_cli_unblock_hat_einen_eigenen_exitcode_fuer_verweigerung(tmp_path, capsys):
    from hoh.cli import main

    s = ProjectStore(tmp_path, "p")
    st = projekt(tmp_path)
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.BLOCKED, note="stuck")]
    s.create(st)

    assert main(["--root", str(tmp_path), "project", "unblock", "p", "a",
                 "--reason", "the obstruction is gone"]) == 0
    assert s.read_state().node("a").lifecycle is Lifecycle.READY
    # Doing it twice is refused, and not with 0.
    assert main(["--root", str(tmp_path), "project", "unblock", "p", "a",
                 "--reason", "again"]) == 4
