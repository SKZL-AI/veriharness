"""The product controller reaches the prototype's decisions, and then some.

`~/hoh-operator-tools/autopilot.py` is the loop that actually drove this
project's release campaign. It is the oracle: whatever else the product
control plane does, it must not decide *differently* from the thing that has
already been used in anger.

So this module runs both against the same synthetic scenarios and requires
their halt classes to agree, under an explicit mapping. Where they cannot
agree, the divergence is asserted deliberately and its reason stated -- a
parity suite that quietly tolerates a difference is worth nothing.

The prototype lives outside the package and does not ship. In the export
regime it is simply absent, and the parity tests skip rather than fail: an
oracle you do not have is not a failure, it is a missing oracle. The
product-only tests below do not skip, because they are about the shipped code.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from hoh.orchestrator import (
    GateRunner,
    HaltClass,
    ProjectController,
    RunLauncher,
    RunOutcome,
    RunVerdict,
)
from hoh.project import (
    ActionClass,
    GateOutcome,
    GateResult,
    Lifecycle,
    ProjectState,
    TaskNode,
)
from hoh.projectstore import ProjectStore

PROTOTYP = Path(os.path.expanduser("~/hoh-operator-tools/autopilot.py"))

#: The prototype's vocabulary, in the product's terms. Written out rather than
#: inferred: an implicit mapping is where a parity claim quietly stops meaning
#: anything.
ENTSPRECHUNG = {
    "FIXPUNKT": HaltClass.CLOSED,
    "HALT_EXTERN": HaltClass.BLOCKED_EXTERNAL,
    "HALT_BUDGET": HaltClass.BLOCKED_PROVIDER,
    "HALT_RUNDEN": HaltClass.ROUND_LIMIT,
    "HALT_TROCKEN": HaltClass.NOT_RUN,
    "HALT_UNKLAR": HaltClass.AMBIGUOUS,
}


def prototyp():
    if not PROTOTYP.exists():
        pytest.skip(f"prototype oracle not present at {PROTOTYP} (export regime)")
    spec = importlib.util.spec_from_file_location("autopilot_oracle", PROTOTYP)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


# --------------------------------------------------------------------------- #
# Fixtures shared by both sides
# --------------------------------------------------------------------------- #

class Start(RunLauncher):
    """A scripted outside world, identical in shape to the prototype's."""

    def __init__(self, verdicts=None, classes=None, depends=False, merge_lands=True,
                 evaluations=None):
        self.verdicts = dict(verdicts or {})
        self.classes = dict(classes or {})
        self.depends = depends
        self.merge_lands = merge_lands
        # What an already-dispatched run is found to have done. The default is
        # UNDETERMINED: a fixture that has dispatched nothing genuinely cannot
        # say, and defaulting to anything else would let the resume path look
        # more decisive than it is.
        self.evaluations = dict(evaluations or {})
        self.launched: list[str] = []
        self.merged: list[str] = []
        self.evaluated: list[str] = []

    def action_class(self, node: TaskNode) -> ActionClass:
        return self.classes.get(node.id, ActionClass.INTERNAL)

    def depends_on(self, a: TaskNode, b: TaskNode) -> bool:
        return self.depends

    def launch(self, node: TaskNode) -> RunOutcome:
        self.launched.append(node.id)
        v = self.verdicts.get(node.id, RunVerdict.ACCEPTED)
        if isinstance(v, list):
            v = v.pop(0) if v else RunVerdict.ACCEPTED
        return RunOutcome(verdict=v, candidate=f"{node.id}-cand")

    def accepted_baseline(self, node: TaskNode) -> str | None:
        return None

    def evaluate(self, node: TaskNode) -> RunOutcome:
        self.evaluated.append(node.id)
        v = self.evaluations.get(node.id, RunVerdict.UNDETERMINED)
        return RunOutcome(verdict=v, detail="from the recorded run state",
                          candidate=f"{node.id}-cand")

    def merge(self, node: TaskNode, outcome: RunOutcome) -> bool:
        if not self.merge_lands:
            return False
        self.merged.append(node.id)
        return True


class Gates(GateRunner):
    def __init__(self, folge=None, subject="abc1234"):
        self.folge = list(folge or [GateOutcome.GREEN])
        self._subject = subject
        self.calls = 0

    def subject(self) -> str:
        return self._subject

    def run(self, subject: str) -> list[GateResult]:
        i = min(self.calls, len(self.folge) - 1)
        self.calls += 1
        ausgang = self.folge[i]
        return [GateResult(name="union", outcome=ausgang, subject=subject,
                           exit_code=0 if ausgang is GateOutcome.GREEN else 1,
                           detail="" if ausgang is GateOutcome.GREEN else "composition defect")]


def projekt(tmp_path, nodes, pid="p") -> ProjectStore:
    s = ProjectStore(tmp_path, pid)
    st = ProjectState(project_id=pid, repo_path=str(tmp_path))
    st.nodes = nodes
    s.create(st)
    return s


def fahre(tmp_path, nodes, *, verdicts=None, classes=None, gates=None,
          max_rounds=40, max_repairs=5, merge_lands=True, evaluations=None):
    s = projekt(tmp_path, nodes)
    start = Start(verdicts, classes, merge_lands=merge_lands, evaluations=evaluations)
    g = Gates(gates)
    c = ProjectController(s, start, g, max_rounds=max_rounds, max_repairs=max_repairs)
    return c.run(), start, s


# --------------------------------------------------------------------------- #
# Parity: the eight scenarios the prototype is exercised on
# --------------------------------------------------------------------------- #

def _prototyp_faehrt(modul, knoten, *, verdikte=None, gate_folge=None, klassen=None,
                     max_runden=40):
    class Sim(modul.Ausfuehrer):
        def __init__(self):
            self.verdikte = dict(verdikte or {})
            self.gate_folge = list(gate_folge or [0])
            self.klassen = dict(klassen or {})
            self.gestartet = []
            self.gate_aufrufe = 0

        def klassifiziere(self, k):
            return self.klassen.get(k["id"], "INTERN")

        def semantische_abhaengigkeit(self, a, b):
            return False

        def starte_lauf(self, k):
            self.gestartet.append(k["id"])
            v = self.verdikte.get(k["id"], "ACCEPTED")
            if isinstance(v, list):
                v = v.pop(0) if v else "ACCEPTED"
            return {"verdikt": v}

        def globale_gates(self):
            i = min(self.gate_aufrufe, len(self.gate_folge) - 1)
            self.gate_aufrufe += 1
            return self.gate_folge[i], "simuliert"

    sim = Sim()
    pilot = modul.Autopilot({"nodes": [dict(k) for k in knoten]}, sim,
                            modul.Protokoll(None), max_runden=max_runden)
    return pilot.fahre(), sim


@pytest.mark.parametrize(
    "name,knoten,verdikte,gates_proto,gates_prod,klassen,erwartet",
    [
        ("S1 durchlauf",
         [{"id": "a", "status": "READY", "dependencies": []},
          {"id": "b", "status": "READY", "dependencies": ["a"]}],
         None, [0], [GateOutcome.GREEN], None, "FIXPUNKT"),
        ("S2 rotes gate erzeugt reparatur, danach gruen",
         [{"id": "a", "status": "READY", "dependencies": []}],
         None, [1, 0], [GateOutcome.RED, GateOutcome.GREEN], None, "FIXPUNKT"),
        ("S3 externe aktion",
         [{"id": "push", "status": "READY", "dependencies": []}],
         None, [0], [GateOutcome.GREEN], {"push": "EXTERN"}, "HALT_EXTERN"),
        ("S5 dauerhaft rotes gate",
         [{"id": "a", "status": "READY", "dependencies": []}],
         None, [1], [GateOutcome.RED], None, "HALT_RUNDEN"),
        ("S8 provider weg",
         [{"id": "a", "status": "READY", "dependencies": []}],
         {"a": "BUDGET"}, [0], [GateOutcome.GREEN], None, "HALT_BUDGET"),
    ],
)
def test_paritaet_mit_dem_prototyp(tmp_path, name, knoten, verdikte, gates_proto,
                                   gates_prod, klassen, erwartet):
    """Both loops reach the same halt class on the same scenario."""
    modul = prototyp()
    proto_ende, proto_sim = _prototyp_faehrt(
        modul, knoten, verdikte=verdikte, gate_folge=gates_proto, klassen=klassen
    )
    assert proto_ende == erwartet, f"oracle itself changed: {name} -> {proto_ende}"

    produkt_verdikte = None
    if verdikte:
        produkt_verdikte = {
            k: (RunVerdict.PROVIDER_UNAVAILABLE if v == "BUDGET" else RunVerdict.ACCEPTED)
            for k, v in verdikte.items()
        }
    produkt_klassen = None
    if klassen:
        produkt_klassen = {
            k: (ActionClass.EXTERNAL if v == "EXTERN" else ActionClass.INTERNAL)
            for k, v in klassen.items()
        }
    ergebnis, start, _ = fahre(
        tmp_path,
        [TaskNode(id=k["id"], dependencies=k["dependencies"]) for k in knoten],
        verdicts=produkt_verdikte, classes=produkt_klassen, gates=gates_prod,
    )
    assert ergebnis.halt == ENTSPRECHUNG[proto_ende], (
        f"{name}: oracle says {proto_ende} -> {ENTSPRECHUNG[proto_ende]}, "
        f"product says {ergebnis.halt} ({ergebnis.reason})"
    )


def test_paritaet_ablehnung_ist_keine_sackgasse(tmp_path):
    """S4: two rejections then acceptance, both loops close.

    Split out because the scripted verdict list is consumed by the run, so the
    two sides need their own copies.
    """
    modul = prototyp()
    proto_ende, proto_sim = _prototyp_faehrt(
        modul, [{"id": "a", "status": "READY", "dependencies": []}],
        verdikte={"a": ["REJECTED", "REJECTED", "ACCEPTED"]}, gate_folge=[0],
    )
    assert proto_ende == "FIXPUNKT"
    assert proto_sim.gestartet == ["a", "a", "a"]

    ergebnis, start, _ = fahre(
        tmp_path, [TaskNode(id="a")],
        verdicts={"a": [RunVerdict.REJECTED, RunVerdict.REJECTED, RunVerdict.ACCEPTED]},
        gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.CLOSED, ergebnis.reason
    assert start.launched == ["a", "a", "a"], "a rejection is input to the next iteration"


def test_paritaet_externe_aktion_startet_nachweislich_nichts(tmp_path):
    """S3, the half that matters: neither loop dispatches before the gate."""
    modul = prototyp()
    proto_ende, proto_sim = _prototyp_faehrt(
        modul, [{"id": "push", "status": "READY", "dependencies": []}],
        klassen={"push": "EXTERN"}, gate_folge=[0],
    )
    assert proto_ende == "HALT_EXTERN"
    assert proto_sim.gestartet == []

    ergebnis, start, _ = fahre(
        tmp_path, [TaskNode(id="push")],
        classes={"push": ActionClass.EXTERNAL}, gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.BLOCKED_EXTERNAL
    assert start.launched == [], "nothing may be dispatched before a captain gate"


def test_divergenz_unbekannter_zustand_wird_verhindert_statt_erkannt(tmp_path):
    """S6 is the one scenario where the two sides *must* differ, and why.

    The prototype reads node status from JSON, so a state it does not know --
    `"VIELLEICHT"` -- is a runtime discovery, and it halts AMBIGUOUS. The
    product types the lifecycle as an enum, so such a state cannot be
    constructed at all: it is refused at the boundary, and the halt never
    happens because the state never exists.

    That is a strict improvement, and it is asserted here rather than papered
    over, because "the two agree everywhere" would otherwise be false.
    """
    modul = prototyp()
    proto_ende, _ = _prototyp_faehrt(
        modul, [{"id": "a", "status": "VIELLEICHT", "dependencies": []}], gate_folge=[0]
    )
    assert proto_ende == "HALT_UNKLAR", "oracle should detect the unknown state at runtime"

    with pytest.raises(Exception) as exc:
        TaskNode(id="a", lifecycle="VIELLEICHT")  # type: ignore[arg-type]
    assert "lifecycle" in str(exc.value).lower() or "VIELLEICHT" in str(exc.value)


def test_paritaet_unbekanntes_verdikt_haelt_beide_an(tmp_path):
    """S7: a verdict neither loop can classify halts it, in both."""
    modul = prototyp()
    proto_ende, _ = _prototyp_faehrt(
        modul, [{"id": "a", "status": "READY", "dependencies": []}],
        verdikte={"a": "FERTIG?"}, gate_folge=[0],
    )
    assert proto_ende == "HALT_UNKLAR"

    ergebnis, _, store = fahre(
        tmp_path, [TaskNode(id="a")],
        verdicts={"a": RunVerdict.UNDETERMINED}, gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.AMBIGUOUS, ergebnis.reason
    # And the product records why, where the next session will read it.
    assert store.read_state().node("a").lifecycle is Lifecycle.BLOCKED


# --------------------------------------------------------------------------- #
# Product-only: what the prototype never had, because it had no durable state
# --------------------------------------------------------------------------- #

def test_stale_writer_haelt_die_schleife_an(tmp_path):
    """A second orchestrator that advanced the state fences this one out."""
    s = projekt(tmp_path, [TaskNode(id="a")])

    class Dazwischen(Start):
        def launch(self, node):
            # Somebody else writes while this round is in flight.
            fremd = ProjectStore(tmp_path, "p")
            st = fremd.read_state()
            st.nodes.append(TaskNode(id="fremd"))
            fremd.write_state(st)
            return super().launch(node)

    c = ProjectController(s, Dazwischen(), Gates([GateOutcome.GREEN]))
    ergebnis = c.run()
    assert ergebnis.halt is HaltClass.AMBIGUOUS
    assert "advanced this project" in ergebnis.reason


def test_doppelter_orchestratorstart_wird_abgewiesen(tmp_path):
    s = projekt(tmp_path, [TaskNode(id="a")])
    with s.lock():
        zweiter = ProjectController(ProjectStore(tmp_path, "p"),
                                    Start(), Gates([GateOutcome.GREEN]))
        ergebnis = zweiter.run()
    assert ergebnis.halt is HaltClass.BLOCKED_DEPENDENCY
    assert "another orchestrator" in ergebnis.reason


def test_absturz_vor_dem_statuswechsel_verliert_nichts(tmp_path):
    """Killed before the lifecycle was written: the node is still READY."""
    s = projekt(tmp_path, [TaskNode(id="a")])

    class Stirbt(Start):
        def launch(self, node):
            raise KeyboardInterrupt("killed mid-dispatch")

    c = ProjectController(s, Stirbt(), Gates([GateOutcome.GREEN]))
    with pytest.raises(KeyboardInterrupt):
        c.run()
    # RUNNING was persisted before the dispatch, which is the point: the next
    # session sees an in-flight node and evaluates rather than re-dispatching.
    assert s.read_state().node("a").lifecycle is Lifecycle.RUNNING
    weiter = ProjectController(ProjectStore(tmp_path, "p"), Start(), Gates([GateOutcome.GREEN]))
    ergebnis = weiter.run()
    assert ergebnis.halt is HaltClass.AMBIGUOUS
    assert "RUNNING" in ergebnis.reason


def test_absturz_nach_dem_merge_wird_nicht_doppelt_gemergt(tmp_path):
    """Killed after MERGED was persisted: the next session does not re-merge."""
    s = projekt(tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.MERGED)])
    start = Start()
    c = ProjectController(s, start, Gates([GateOutcome.GREEN]))
    ergebnis = c.run()
    assert ergebnis.halt is HaltClass.CLOSED
    assert start.launched == [] and start.merged == [], "already merged work is not redone"


def test_akzeptiert_aber_nicht_gelandet_wird_nicht_geraten(tmp_path):
    """Accepted, merge did not land: nothing is resolved automatically.

    This fixture's launcher returns a bare `False`, the way an older launcher
    would, and says nothing about why. That is the one case that genuinely
    stays AMBIGUOUS -- the halt is honest about the tool having been silent,
    rather than inventing a category for it.
    """
    ergebnis, start, store = fahre(
        tmp_path, [TaskNode(id="a")], gates=[GateOutcome.GREEN], merge_lands=False
    )
    assert ergebnis.halt is HaltClass.AMBIGUOUS
    assert "did not say why" in ergebnis.reason
    assert store.read_state().node("a").lifecycle is Lifecycle.BLOCKED


def test_terminaler_dag_mit_roter_closure_erzeugt_reparatur(tmp_path):
    ergebnis, _, store = fahre(
        tmp_path, [TaskNode(id="a")],
        gates=[GateOutcome.RED, GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.CLOSED, ergebnis.reason
    st = store.read_state()
    rep = [n for n in st.nodes if n.repair_of]
    assert len(rep) == 1, [n.id for n in st.nodes]
    assert rep[0].lifecycle is Lifecycle.MERGED
    assert st.closure_generation == 2, "closure ran twice: once red, once green"


def test_reparatur_wird_als_entscheidung_festgehalten(tmp_path):
    """Why a repair node exists survives the session that created it."""
    _, _, store = fahre(tmp_path, [TaskNode(id="a")],
                        gates=[GateOutcome.RED, GateOutcome.GREEN])
    st = store.read_state()
    gruende = [d for d in st.decisions if d.kind.value == "CREATE_REPAIR_NODE"]
    assert gruende, [d.kind for d in st.decisions]
    assert "union" in gruende[0].reason
    assert gruende[0].evidence and gruende[0].evidence[0].startswith("gate:union@")


def test_echter_fixpunkt_nennt_seinen_gegenstand(tmp_path):
    ergebnis, _, store = fahre(tmp_path, [TaskNode(id="a")], gates=[GateOutcome.GREEN])
    assert ergebnis.halt is HaltClass.CLOSED
    st = store.read_state()
    assert st.measurement_head == "abc1234"
    assert st.rc_closed()
    assert "abc1234" in ergebnis.reason


def test_nicht_ausgefuehrte_gates_sind_kein_fixpunkt(tmp_path):
    ergebnis, _, store = fahre(tmp_path, [TaskNode(id="a")], gates=[GateOutcome.NOT_RUN])
    assert ergebnis.halt is HaltClass.NOT_RUN
    assert not store.read_state().rc_closed()


def test_beschaedigter_zustand_haelt_an_statt_zu_ueberschreiben(tmp_path):
    s = projekt(tmp_path, [TaskNode(id="a")])
    st = s.read_state()
    st.nodes = [TaskNode(id="a", lifecycle=Lifecycle.MERGED)]
    s.write_state(st)
    s.state_path.write_text('{"project_id": "p", "nodes": [', encoding="utf-8")
    c = ProjectController(ProjectStore(tmp_path, "p"), Start(), Gates([GateOutcome.GREEN]))
    ergebnis = c.run()
    assert ergebnis.halt is HaltClass.CORRUPT_STATE
    assert s.parked_states(), "the way back is still on disk"


def test_unaufloesbare_abhaengigkeit_startet_nichts(tmp_path):
    ergebnis, start, _ = fahre(
        tmp_path, [TaskNode(id="b", dependencies=["gibtesnicht"])], gates=[GateOutcome.GREEN]
    )
    assert ergebnis.halt is HaltClass.BLOCKED_DEPENDENCY
    assert "gibtesnicht" in ergebnis.reason
    assert start.launched == []


def test_jeder_halt_traegt_eine_klasse(tmp_path):
    """No exit leaves the loop without a name -- the measure of how far the
    automation reaches depends on it."""
    faelle = [
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.GREEN]), HaltClass.CLOSED),
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.NOT_RUN]), HaltClass.NOT_RUN),
        (dict(nodes=[TaskNode(id="a")], gates=[GateOutcome.RED], max_repairs=0),
         HaltClass.ROUND_LIMIT),
        (dict(nodes=[TaskNode(id="b", dependencies=["x"])], gates=[GateOutcome.GREEN]),
         HaltClass.BLOCKED_DEPENDENCY),
    ]
    for i, (kw, erwartet) in enumerate(faelle):
        knoten = kw.pop("nodes")
        s = ProjectStore(tmp_path, f"f{i}")
        st = ProjectState(project_id=f"f{i}", repo_path=str(tmp_path))
        st.nodes = knoten
        s.create(st)
        c = ProjectController(s, Start(), Gates(kw.pop("gates")), **kw)
        ergebnis = c.run()
        assert ergebnis.halt is erwartet, f"case {i}: {ergebnis.halt} {ergebnis.reason}"
        assert ergebnis.reason, f"case {i} halted without a reason"


# --------------------------------------------------------------------------- #
# Resume: a RUNNING node is read, not re-dispatched
# --------------------------------------------------------------------------- #

def test_laufender_knoten_wird_gelesen_nicht_neu_gestartet(tmp_path):
    """The heart of exactly-once.

    A fresh process finding a node marked RUNNING must not re-dispatch it to
    find out what happened -- that would repeat an acceptance that may already
    have landed. It reads the run's own recorded state instead, which costs
    nothing and is decisive when the run finished.
    """
    ergebnis, start, store = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        evaluations={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.CLOSED, ergebnis.reason
    assert start.evaluated == ["a"], "the run state must be read"
    assert start.launched == [], "and the run must not be dispatched again"
    assert start.merged == ["a"], "exactly one merge"
    assert store.read_state().node("a").lifecycle is Lifecycle.MERGED


def test_laufender_knoten_ohne_erkennbares_ergebnis_haelt_an(tmp_path):
    """When the run state cannot say, the loop stops rather than guessing."""
    ergebnis, start, _ = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.AMBIGUOUS
    assert "could repeat an acceptance" in ergebnis.reason
    assert start.launched == []


def test_laufender_knoten_mit_ablehnung_geht_zurueck_in_die_schleife(tmp_path):
    """A crashed dispatch whose run was rejected resumes as ordinary work."""
    ergebnis, start, store = fahre(
        tmp_path, [TaskNode(id="a", lifecycle=Lifecycle.RUNNING)],
        evaluations={"a": RunVerdict.REJECTED},
        verdicts={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.CLOSED, ergebnis.reason
    # Evaluated twice: once on resume, and once again before the dispatch. The
    # second read is what stops the loop paying for work a previous dispatch
    # already had accepted -- it costs nothing and it is the whole mechanism
    # behind exactly-once.
    assert start.evaluated == ["a", "a"], start.evaluated
    assert start.launched == ["a"], "after the rejection it is dispatched once, normally"
    assert store.read_state().node("a").rejections == 1


def test_basislinie_wird_vor_dem_dispatch_persistiert(tmp_path):
    """What was accepted *before* a dispatch goes into state, not memory.

    A process that dies during the dispatch is exactly when it is needed: it is
    the only thing that lets the next process tell an acceptance from a run
    that ended where it started.
    """
    s = projekt(tmp_path, [TaskNode(id="a")])

    class MerktBasislinie(Start):
        def accepted_baseline(self, node):
            return "a-i1"

        def launch(self, node):
            # What is on disk at the moment of dispatch is what a crash leaves.
            self.gesehen = ProjectStore(tmp_path, "p").read_state().node("a").accepted_before
            return super().launch(node)

    start = MerktBasislinie()
    c = ProjectController(s, start, Gates([GateOutcome.GREEN]))
    ergebnis = c.run()
    assert ergebnis.halt is HaltClass.CLOSED
    assert start.gesehen == "a-i1", "the baseline must be durable before the dispatch"


def test_bereits_angenommener_kandidat_wird_nicht_neu_dispatcht(tmp_path):
    """A node whose run already accepted something is merged, not re-run.

    The state this covers is real and was found by the first end-to-end run: a
    dispatch succeeded, the merge was blocked by untracked build output, the
    obstruction was cleared, and the node came back READY with its acceptance
    still sitting in the run record. Dispatching again would pay a second time
    for verified work -- and could accept a second, different candidate for the
    same node.
    """
    ergebnis, start, store = fahre(
        tmp_path, [TaskNode(id="a")],
        evaluations={"a": RunVerdict.ACCEPTED}, gates=[GateOutcome.GREEN],
    )
    assert ergebnis.halt is HaltClass.CLOSED, ergebnis.reason
    assert start.launched == [], "nothing may be dispatched when the work is already accepted"
    assert start.merged == ["a"], "exactly one merge"
    assert any(s.kind == "ALREADY_ACCEPTED" for s in ergebnis.steps), \
        [s.kind for s in ergebnis.steps]
