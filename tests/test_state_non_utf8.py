"""A free text that names a non-UTF-8 path is recorded in `state.json`.

`integrity.verify` and the capability witness name paths as `os.fsdecode`
gives them, with lone surrogates for the undecodable bytes. The run state
used to refuse those on the way to disk, and the violation never reached the
record every reader consults first. Each text entering `history`,
`stop_reason` or `blocked_reason` is now recorded once: strict UTF-8,
lossless, injective, and unchanged by write -> read -> write
(dogfood/witness-surrogates/spec.md).

Hygiene (spec item 4): every non-UTF-8 name is created inside a
`tempfile.TemporaryDirectory()` the test closes itself -- never `tmp_path`,
never the project tree.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hoh import integrity
from hoh.capability import CapabilityViolation
from hoh.cli import _integrity_close
from hoh.contracts import (
    MARKER_LITERAL,
    _RECORDED,
    Budgets,
    Condition,
    RecordedText,
    RunState,
    recordable_text,
    recorded_original,
)
from hoh.controller import Controller
from hoh.store import RunStore

CAFE = os.fsdecode(b"caf\xe9")
FIELDS = ("history", "stop_reason", "blocked_reason")
_IDENT = (b"-c", b"user.email=t@t", b"-c", b"user.name=t")


def _gitb(repo: bytes, *args: bytes) -> bytes:
    return subprocess.run([b"git", b"-C", repo, *args], check=True,
                          capture_output=True).stdout.strip()


def _strict_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _world(base: Path) -> tuple[RunStore, RunState]:
    """A real run store holding a freshly written run state."""
    spec = base / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    repo = base / "project"
    repo.mkdir(exist_ok=True)
    store = RunStore(base / "runs", "r1")
    state = Controller.new_state(run_id="r1", repo_path=repo, project_name="project",
                                 spec_path=spec, budgets=Budgets(max_iterations=5))
    store.write_state(state)
    return store, store.read_state()


def _fields(raw: str) -> dict:
    data = json.loads(raw)
    return {f: data[f] for f in FIELDS}


def _write_read_write(store: RunStore, state: RunState) -> tuple[str, str, RunState]:
    """Writes `state`, reads it back and writes it again; both files' text."""
    store.write_state(state)
    first = store.state_path.read_bytes().decode("utf-8")       # strict
    loaded = store.read_state()
    store.write_state(loaded)
    second = store.state_path.read_bytes().decode("utf-8")
    return first, second, store.read_state()


# --------------------------------------------------------------------------- #
# K1
# --------------------------------------------------------------------------- #


def test_note_records_non_utf8_text_losslessly():
    text = "deleted: " + os.fsdecode(b"/w/caf\xe9.bin") + " existed before the run"
    state = RunState(run_id="r", repo_path="/x", project_name="p", spec_path="/s",
                     spec_digest="d", policy_digest="p", profile_digest="q")
    state.note(text)
    entry = state.history[-1]
    assert isinstance(entry, RecordedText)
    entry.encode("utf-8")                                       # strict
    assert "non-UTF-8" in entry
    assert "caf\\xe9.bin" in entry
    assert os.fsencode(text).hex() in entry
    original = recorded_original(entry)
    assert original.endswith(text) and original != text
    assert original[:20].endswith("Z") and original[20] == " "  # the timestamp
    assert recorded_original(entry.strip()) == original


# --------------------------------------------------------------------------- #
# K2
# --------------------------------------------------------------------------- #


def test_recordable_text_is_lossless():
    cases = [
        "plain UTF-8 — Grüße 停止",
        CAFE,
        os.fsdecode(b"caf\xe8"),
        "\udcc3\udca9",                     # non-canonical: decodes to an é
        "a\ud800b",                         # a lone high surrogate
        "\\xe9 " + CAFE,                     # a literal \xe9 next to a real one
        "quotes [hoh-recorded: the literal mid-text",
        "trailing space ",
        " leading space",
        "trailing newline\n",
        " ",
        "\n",
        " " + CAFE,
    ]
    records = [recordable_text(t) for t in cases]
    for text, rec in zip(cases, records):
        assert isinstance(rec, RecordedText)
        assert _strict_utf8(rec), repr(rec)
        assert rec == rec.strip(), repr(rec)
        assert recorded_original(rec) == text, repr(text)
        assert recorded_original(rec.strip()) == text, repr(text)
        assert recordable_text(rec) is rec
        assert len(_RECORDED.findall(rec)) <= 1
    assert len(set(records)) == len(records), "two texts share a record"

    # Surrogates are shown as the byte, or as \uXXXX when not an escaped byte.
    assert records[1].startswith("caf\\xe9 ") and "surrogateescape hex: 636166e9]" in records[1]
    assert "surrogatepass" in records[3] and "surrogatepass" in records[4]
    assert records[4].startswith("a\\ud800b ")
    assert "non-UTF-8" in records[1]

    # The literal anywhere in the text quotes it.
    quoted = records[6]
    assert quoted != cases[6] and quoted.startswith(cases[6] + " ")
    assert "quoted text" in quoted and _RECORDED.search(quoted)
    assert recordable_text(" ") == "[hoh-recorded: quoted text; exact text as utf-8/strict hex: 20]"

    # Ordinary text is unchanged, and contains no marker.
    assert records[0] == cases[0] and MARKER_LITERAL not in records[0]

    # Malformed markers are taken literally, without an exception.
    malformed = [
        "x [hoh-recorded: quoted text; exact text as utf-8/strict hex: 0]",
        "x [hoh-recorded: non-UTF-8 text, undecodable bytes shown as \\xNN; "
        "exact text as utf-8/surrogatepass hex: ff]",
        "x [hoh-recorded: quoted text; exact text as utf-8/strict hex: zz]",
    ]
    for bad in malformed:
        assert recorded_original(bad) == bad
        # ... and a UTF-8 text that holds one is itself quoted, losslessly.
        assert recorded_original(recordable_text(bad)) == bad
        assert recordable_text(bad) != bad


# --------------------------------------------------------------------------- #
# K3
# --------------------------------------------------------------------------- #


def test_state_with_non_utf8_reasons_is_written_and_read_back():
    stop = "integrity violation: deleted: " + os.fsdecode(b"/w/caf\xe9.bin")
    blocked = "capability violation: " + os.fsdecode(b"/arena/\xff\xfe")
    noted = "a note that ends in a space "
    appended = "appended " + os.fsdecode(b"\xe8")
    copied = "copied " + os.fsdecode(b"\xe9t\xe9")
    with tempfile.TemporaryDirectory() as d:
        store, state = _world(Path(d))
        state.stop_reason = stop
        state.blocked_reason = blocked
        assert isinstance(state.stop_reason, RecordedText), "recorded when assigned"
        assert isinstance(state.blocked_reason, RecordedText), "recorded when assigned"
        assert _strict_utf8(state.stop_reason) and _strict_utf8(state.blocked_reason)
        assert recorded_original(state.stop_reason) == stop
        assert recorded_original(state.blocked_reason) == blocked
        state.note(noted)
        state.history.append(appended)                          # bypasses note()
        assert state.history[-1] == appended, "the value in memory is not changed"

        first, second, loaded = _write_read_write(store, state)
        assert RunState.model_validate_json(first).run_id == "r1"
        assert _fields(first) == _fields(second), "write -> read -> write changed a field"
        assert recorded_original(loaded.stop_reason) == stop
        assert recorded_original(loaded.blocked_reason) == blocked
        originals = [recorded_original(e) for e in loaded.history]
        assert originals[-1] == appended
        assert originals[-2].endswith(" " + noted), "the trailing space was lost"
        assert all(isinstance(e, RecordedText) for e in loaded.history)

        # A model_copy(update=...) bypasses assignment; it is recorded when written.
        copy = loaded.model_copy(update={"stop_reason": copied,
                                         "history": [*loaded.history, copied]})
        first, second, again = _write_read_write(store, copy)
        assert _fields(first) == _fields(second)
        assert recorded_original(again.stop_reason) == copied
        assert recorded_original(again.history[-1]) == copied
        assert recorded_original(again.blocked_reason) == blocked


# --------------------------------------------------------------------------- #
# K4
# --------------------------------------------------------------------------- #


def test_utf8_state_text_is_written_unchanged():
    texts = ["Grüße — 停止", "a literal \\xe9 in plain text", "zweig-ü"]
    with tempfile.TemporaryDirectory() as d:
        store, state = _world(Path(d))
        state.stop_reason = texts[0]
        state.blocked_reason = texts[1]
        state.note(texts[2])
        state.history.append(texts[0])
        store.write_state(state)
        raw = store.state_path.read_bytes().decode("utf-8")
        assert MARKER_LITERAL not in raw
        assert "\\u" not in raw, "ASCII escaping"
        data = json.loads(raw)
        assert data["stop_reason"] == texts[0]
        assert data["blocked_reason"] == texts[1]
        assert data["history"][-2].endswith(" " + texts[2])
        assert data["history"][-1] == texts[0]
        for t in texts:
            assert json.dumps(t, ensure_ascii=False)[1:-1] in raw


# --------------------------------------------------------------------------- #
# K5
# --------------------------------------------------------------------------- #


def test_integrity_violation_naming_non_utf8_path_is_recorded(monkeypatch):
    # The real stderr writes surrogates as backslash escapes; pytest's capsys
    # stream would refuse them, so the test brings one that behaves the same.
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="backslashreplace")
    monkeypatch.setattr(sys, "stderr", stderr)
    for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(key, raising=False)
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        store, state = _world(base)
        repo = os.fsencode(base / "project")
        _gitb(repo, b"init", b"-q", b"-b", b"main")
        with open(os.path.join(repo, b"caf\xe9.bin"), "wb") as f:
            f.write(b"x\n")
        _gitb(repo, b"add", b"-A")
        _gitb(repo, *_IDENT, b"commit", b"-qm", b"initial")
        before = integrity.snapshot(os.fsdecode(repo))
        # The tracked file goes away -- moved aside, which the guard sees as gone.
        os.rename(os.path.join(repo, b"caf\xe9.bin"), os.path.join(repo, b"aside.bin"))
        (store.dir / "approval").mkdir(parents=True, exist_ok=True)

        assert _integrity_close(store, before) is False
        stderr.flush()
        err = stderr.buffer.getvalue().decode("utf-8")
        assert "INTEGRITY VIOLATION" in err
        assert "could not record" not in err, err

        record = json.loads((store.dir / "approval" / "integrity.json").read_text("utf-8"))
        assert any(b"caf\xe9.bin" in os.fsencode(v) for v in record["violations"])

        raw = store.state_path.read_bytes().decode("utf-8")      # strict
        fresh = RunState.model_validate_json(raw)
        entries = [e for e in fresh.history if "INTEGRITY VIOLATION" in e]
        assert len(entries) == 1, fresh.history
        assert "non-UTF-8" in entries[0]
        assert b"caf\xe9.bin" in os.fsencode(recorded_original(entries[0]))
        assert fresh.condition is Condition.FAILED
        assert b"caf\xe9.bin" in os.fsencode(recorded_original(fresh.stop_reason))


# --------------------------------------------------------------------------- #
# K6
# --------------------------------------------------------------------------- #


def test_capability_violation_naming_non_utf8_path_is_recorded(monkeypatch):
    name = os.fsdecode(b"/arena/caf\xe9")

    def violating(self, st_):
        st_.note("planner dispatched")
        raise CapabilityViolation(f"the planner wrote into {name}")

    monkeypatch.setattr(Controller, "_run_iteration_locked", violating)
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        store, state = _world(base)
        controller = Controller(store, None, spec_path=base / "spec.md")
        with store.lock():
            outcome = controller._run_iteration_guarded(state)
        assert outcome.accepted is False

        raw = store.state_path.read_bytes().decode("utf-8")      # strict
        persisted = RunState.model_validate_json(raw)
        assert persisted.condition is Condition.BLOCKED
        assert name in recorded_original(persisted.blocked_reason)
        assert any(recorded_original(e).endswith(" planner dispatched")
                   for e in persisted.history)
        notes = [e for e in persisted.history if "non-UTF-8" in e]
        assert len(notes) == 1, persisted.history
        assert name in recorded_original(notes[0])


# --------------------------------------------------------------------------- #
# K8
# --------------------------------------------------------------------------- #


def test_forged_marker_is_converted_and_recording_is_injective():
    genuine = "deleted: " + os.fsdecode(b"/w/caf\xe9.bin")
    forged = str(recordable_text(genuine))          # a UTF-8 text equal to the record
    assert _strict_utf8(forged) and forged != genuine

    rec_genuine, rec_forged = recordable_text(genuine), recordable_text(forged)
    assert rec_genuine != rec_forged
    assert recorded_original(rec_genuine) == genuine
    assert recorded_original(rec_forged) == forged

    on_disk = {}
    for label, text in (("genuine", genuine), ("forged", forged)):
        with tempfile.TemporaryDirectory() as d:
            store, state = _world(Path(d))
            state.note(text)
            state.stop_reason = text
            state.blocked_reason = text
            state.history.append(text)
            first, second, loaded = _write_read_write(store, state)
            assert _fields(first) == _fields(second), label
            assert recorded_original(loaded.stop_reason) == text, label
            assert recorded_original(loaded.blocked_reason) == text, label
            assert recorded_original(loaded.history[-1]) == text, label
            assert recorded_original(loaded.history[-2]).endswith(" " + text), label
            on_disk[label] = _fields(first)
    for f in ("stop_reason", "blocked_reason"):
        assert on_disk["genuine"][f] != on_disk["forged"][f], f
    assert on_disk["genuine"]["history"][-1] != on_disk["forged"]["history"][-1]


# --------------------------------------------------------------------------- #
# K9
# --------------------------------------------------------------------------- #


def test_surrounding_whitespace_survives_the_state():
    texts = [" ", "\n", "\t\t", " lead", "\tx", " " + CAFE, "trail "]
    for text in texts:
        with tempfile.TemporaryDirectory() as d:
            store, state = _world(Path(d))
            state.stop_reason = text
            state.blocked_reason = text
            state.history.append(text)
            first, second, loaded = _write_read_write(store, state)
            assert _fields(first) == _fields(second), repr(text)
            assert recorded_original(loaded.stop_reason) == text, repr(text)
            assert recorded_original(loaded.blocked_reason) == text, repr(text)
            assert recorded_original(loaded.history[-1]) == text, repr(text)
