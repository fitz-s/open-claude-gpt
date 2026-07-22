# Tests for the one-shot spool -> store migration (skill/scripts/cgc_migrate.py), phase 3.
# Builds a SYNTHETIC spool on disk (never the live one) and asserts the mapping — above all that a
# processing job with no conversation lands as possibly_accepted and is never dispatchable.
import importlib.util
import json
import os

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(name, db_path=None):
    import sys
    if db_path is not None:
        os.environ["CGC_STORE_DB"] = str(db_path)
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _job(spool, sub, rid, **extra):
    d = os.path.join(spool, sub)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, rid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": extra.get("kind", "submit")}, f)


def _status(spool, rid, **fields):
    d = os.path.join(spool, "status")
    os.makedirs(d, exist_ok=True)
    fields["rid"] = rid
    with open(os.path.join(d, rid + ".json"), "w", encoding="utf-8") as f:
        json.dump(fields, f)


@pytest.fixture
def spool(tmp_path):
    """A synthetic spool covering every mapping branch."""
    sp = tmp_path / "spool"
    ans = tmp_path / "answers"
    ans.mkdir()
    # A: pending -> queued
    _job(sp, "pending", "REQ-20260721-000000-00000a")
    _status(sp, "REQ-20260721-000000-00000a", state="queued")
    # B: processing WITH conversation -> waiting
    _job(sp, "processing", "REQ-20260721-000000-00000b")
    _status(sp, "REQ-20260721-000000-00000b", state="processing", conversation="conv-b")
    # C: processing WITHOUT conversation -> possibly_accepted (THE rule)
    _job(sp, "processing", "REQ-20260721-000000-00000c")
    _status(sp, "REQ-20260721-000000-00000c", state="processing", msg="gate ok; sent")
    # D: done + non-empty answer -> completed_verified
    outd = str(ans / "d.txt"); open(outd, "w").write("the answer D")
    _job(sp, "done", "REQ-20260721-000000-00000d")
    _status(sp, "REQ-20260721-000000-00000d", state="done", out=outd)
    # E: done + answer + .raw salvage sibling -> completed_unverified
    oute = str(ans / "e.txt"); open(oute, "w").write("salvaged E"); open(oute + ".raw", "w").write("salvaged E")
    _job(sp, "done", "REQ-20260721-000000-00000e")
    _status(sp, "REQ-20260721-000000-00000e", state="done", out=oute)
    # F: blocker -> blocked
    _job(sp, "done", "REQ-20260721-000000-00000f")
    _status(sp, "REQ-20260721-000000-00000f", state="blocker", msg="login")
    # G: error -> failed
    _job(sp, "done", "REQ-20260721-000000-00000g")
    _status(sp, "REQ-20260721-000000-00000g", state="error", msg="submit failed")
    # H: done but empty/missing answer -> failed
    _job(sp, "done", "REQ-20260721-000000-00000h")
    _status(sp, "REQ-20260721-000000-00000h", state="done", out=str(ans / "missing.txt"))
    return str(sp)


@pytest.fixture
def store(tmp_path):
    m = _load("cgc_store", tmp_path / "control.db")
    s = m.Store(str(tmp_path / "control.db"))
    yield m, s
    s.close()


def test_drain_only_refuses_active_legacy_work(spool, store):
    """Production migration is DRAIN-ONLY: with pending/processing legacy jobs present, it refuses
    (import_round cannot carry their prompt/spec, so it would create unrunnable rows) — the operator
    must drain the old daemon first. The `spool` fixture has active work, so the default aborts."""
    m, s = store
    mig = _load("cgc_migrate")
    with pytest.raises(mig.MigrationBlocked):
        mig.migrate_spool_to_store(spool, s)          # default require_drained=True
    assert s.recover() == {"dispatchable": [], "reattach": [], "uncertain": [], "retrievable": []}, \
        "aborts before writing ANY store state"


def test_mapping_states(spool, store):
    m, s = store
    mig = _load("cgc_migrate")
    summary = mig.migrate_spool_to_store(spool, s, require_drained=False)
    assert summary["imported"] == 8
    want = {
        "REQ-20260721-000000-00000a": "queued",
        "REQ-20260721-000000-00000b": "waiting",
        "REQ-20260721-000000-00000c": "possibly_accepted",
        "REQ-20260721-000000-00000d": "completed_verified",
        "REQ-20260721-000000-00000e": "completed_unverified",
        "REQ-20260721-000000-00000f": "blocked",
        "REQ-20260721-000000-00000g": "failed",
        "REQ-20260721-000000-00000h": "failed",
    }
    for rid, st in want.items():
        assert s.get_round(rid)["state"] == st, f"{rid} expected {st}"


def test_sent_without_conversation_is_never_dispatchable(spool, store):
    """The whole point of the migration's caution: a processing-without-conv job must not be
    re-sent. It lands possibly_accepted and recover() classifies it uncertain, never dispatchable."""
    m, s = store
    mig = _load("cgc_migrate")
    mig.migrate_spool_to_store(spool, s, require_drained=False)
    rec = s.recover()
    c = "REQ-20260721-000000-00000c"
    assert c in rec["uncertain"]
    assert c not in rec["dispatchable"], "a possibly-sent consult must NEVER be auto-resent"
    # and the pending one IS dispatchable, the conv'd one reattaches
    assert "REQ-20260721-000000-00000a" in rec["dispatchable"]
    assert "REQ-20260721-000000-00000b" in rec["reattach"]


def test_completed_answer_text_is_carried_over(spool, store):
    m, s = store
    mig = _load("cgc_migrate")
    mig.migrate_spool_to_store(spool, s, require_drained=False)
    d = s.get_round("REQ-20260721-000000-00000d")
    assert d["result_text"] == "the answer D" and d["completion_confidence"] == "verified"
    e = s.get_round("REQ-20260721-000000-00000e")
    assert e["completion_confidence"] == "unverified"


def test_migration_is_idempotent(spool, store):
    m, s = store
    mig = _load("cgc_migrate")
    first = mig.migrate_spool_to_store(spool, s, require_drained=False)
    second = mig.migrate_spool_to_store(spool, s, require_drained=False)
    assert first["imported"] == 8 and second["imported"] == 0 and second["skipped"] == 8


def test_migration_does_not_mutate_the_spool(spool, store):
    m, s = store
    mig = _load("cgc_migrate")
    before = {sub: sorted(os.listdir(os.path.join(spool, sub)))
              for sub in ("pending", "processing", "done", "status")}
    mig.migrate_spool_to_store(spool, s, require_drained=False)
    after = {sub: sorted(os.listdir(os.path.join(spool, sub)))
             for sub in ("pending", "processing", "done", "status")}
    assert before == after, "migration must read the spool, never modify it"
