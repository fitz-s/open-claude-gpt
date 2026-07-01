#!/usr/bin/env python3
# Created: 2026-07-01
# Last audited: 2026-07-01
# Authority basis: refactor task — replace the single-active + recent[] state model in
#   skill/scripts/cdp_consult.py with a per-rid job registry ({"jobs": {"<rid>": {...}}}).
#   Deep review flagged the old active.json shape as "cleaner if it were a per-rid job
#   registry" — these tests lock in the new on-disk shape + every externally-observable
#   behavior it must preserve (atomic write, flock locking, auto-resolve ambiguity refusal,
#   explicit --rid/--conversation resolution, tolerance of stale/old-format/corrupt state).
"""
Offline tests for cdp_consult.py's per-rid job registry (_read_state/_write_state/
_resolve_conv) — no browser, no network. Run: python3 -m pytest tests/test_state.py -q
(also runnable plain: python3 tests/test_state.py)

Each test points CGC_STATE_DIR at a fresh tmp directory (tempfile.mkdtemp), never the
real /tmp/cgc, so tests never interfere with each other or with a live consult.
"""
import concurrent.futures
import importlib.util
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
CDP_CONSULT_PATH = os.path.join(SCRIPTS, "cdp_consult.py")


def _load(env=None):
    """Import cdp_consult.py as an isolated module, optionally with env overrides.
    Mirrors tests/test_smoke.py's `_load` helper (same import pattern)."""
    old = dict(os.environ)
    try:
        if env:
            os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location("_cdp_consult_state_under_test", CDP_CONSULT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


def _fresh_state_dir():
    """A brand-new tmp dir per call — never the real /tmp/cgc."""
    return tempfile.mkdtemp(prefix="cgc-test-state-")


def _cdp(state_dir):
    return _load(env={"CGC_STATE_DIR": state_dir})


# ---- basic round-trip ---------------------------------------------------------------

def test_write_read_round_trip_single_job():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        st = m._read_state()
        assert "jobs" in st and isinstance(st["jobs"], dict)
        job = st["jobs"]["REQ-1"]
        assert job["conversation_id"] == "conv-1"
        assert job["status"] == "submitted"
        assert "ts" in job
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_multiple_jobs_coexist_keyed_by_rid():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        m._write_state(conversation="conv-2", rid="REQ-2")
        m._write_state(conversation="conv-3", rid="REQ-3")
        st = m._read_state()
        assert set(st["jobs"].keys()) == {"REQ-1", "REQ-2", "REQ-3"}
        assert st["jobs"]["REQ-1"]["conversation_id"] == "conv-1"
        assert st["jobs"]["REQ-2"]["conversation_id"] == "conv-2"
        assert st["jobs"]["REQ-3"]["conversation_id"] == "conv-3"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_status_transition_submitted_to_answered():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        assert m._read_state()["jobs"]["REQ-1"]["status"] == "submitted"
        # wait-done refreshes the SAME job (by conversation, rid=None) and marks it answered —
        # mirrors cmd_wait's `_write_state(conversation=conv, status="answered")` call site.
        m._write_state(conversation="conv-1", status="answered")
        job = m._read_state()["jobs"]["REQ-1"]
        assert job["status"] == "answered"
        assert job["conversation_id"] == "conv-1"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- auto-resolution ------------------------------------------------------------------

def test_auto_resolve_single_active_job():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        assert m._resolve_conv("auto") == "conv-1"
        assert m._resolve_conv(None) == "conv-1"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_auto_resolve_no_jobs_returns_none():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        assert m._resolve_conv("auto") is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_auto_resolve_ambiguous_raises_same_error():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        m._write_state(conversation="conv-2", rid="REQ-2")
        try:
            m._resolve_conv("auto")
            assert False, "expected SystemExit on ambiguous auto-resolve"
        except SystemExit as e:
            msg = str(e)
            assert "CGC_ERROR ambiguous_followup" in msg
            assert "conv-1" in msg and "conv-2" in msg
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_auto_resolve_ambiguity_deduped_by_conversation():
    # two rids pointing at the SAME conversation_id (e.g. a followup refresh under a new rid)
    # must count as ONE active thread, not two — ambiguity is measured in threads.
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        m._write_state(conversation="conv-1", rid="REQ-2")
        assert m._resolve_conv("auto") == "conv-1"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- explicit --rid / --conversation resolution ----------------------------------------

def test_explicit_conversation_id_passthrough():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        m._write_state(conversation="conv-2", rid="REQ-2")
        # explicit --conversation always wins, even under ambiguity
        assert m._resolve_conv("conv-2") == "conv-2"
        # an id with no matching job just passes through unchanged
        assert m._resolve_conv("conv-unknown") == "conv-unknown"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_explicit_rid_resolves_to_its_conversation():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-1", rid="REQ-1")
        m._write_state(conversation="conv-2", rid="REQ-2")
        # pinning by rid (not "auto") must resolve to that job's conversation, bypassing ambiguity
        assert m._resolve_conv("REQ-1") == "conv-1"
        assert m._resolve_conv("REQ-2") == "conv-2"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- atomicity / locking ----------------------------------------------------------------

def test_sequential_writes_under_lock_preserve_prior_jobs():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        for i in range(20):
            m._write_state(conversation=f"conv-{i}", rid=f"REQ-{i}")
        st = m._read_state()
        assert len(st["jobs"]) == 20
        for i in range(20):
            assert st["jobs"][f"REQ-{i}"]["conversation_id"] == f"conv-{i}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_concurrent_writes_do_not_corrupt_state():
    # Concurrent-ish writers (thread pool, real fcntl.flock contention within one process)
    # must never truncate/corrupt active.json, and every job must survive.
    d = _fresh_state_dir()
    try:
        m = _cdp(d)

        def _write(i):
            m._write_state(conversation=f"conv-{i}", rid=f"REQ-{i}")
            return i

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_write, range(30)))

        # file must be valid JSON with the registry shape (never partially written)
        with open(m.STATE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        assert isinstance(raw, dict) and isinstance(raw.get("jobs"), dict)
        assert len(raw["jobs"]) == 30
        for i in range(30):
            assert raw["jobs"][f"REQ-{i}"]["conversation_id"] == f"conv-{i}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_second_write_preserves_first_job():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        m._write_state(conversation="conv-A", rid="REQ-A")
        m._write_state(conversation="conv-B", rid="REQ-B")
        st = m._read_state()
        assert st["jobs"]["REQ-A"]["conversation_id"] == "conv-A"
        assert st["jobs"]["REQ-B"]["conversation_id"] == "conv-B"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- stale / old-format / corrupt state tolerance --------------------------------------

def test_missing_state_file_treated_as_empty():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        assert not os.path.exists(m.STATE_PATH)
        st = m._read_state()
        assert st == {"jobs": {}}
        assert m._resolve_conv("auto") is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_old_format_active_json_tolerated_no_crash():
    # OLD single-active shape: {"conversation": "...", "recent": [...]} — must be treated as
    # empty (ignore-and-overwrite), never crash.
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        os.makedirs(d, exist_ok=True)
        with open(m.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"conversation": "conv-old", "recent": [
                {"conv": "conv-old", "rid": "REQ-old", "ts": 0}]}, f)
        st = m._read_state()
        assert st == {"jobs": {}}
        assert m._resolve_conv("auto") is None
        # writing after an old-format file on disk must succeed and produce the new shape
        m._write_state(conversation="conv-new", rid="REQ-new")
        st2 = m._read_state()
        assert st2["jobs"]["REQ-new"]["conversation_id"] == "conv-new"
        assert "conversation" not in st2 and "recent" not in st2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_corrupt_json_state_file_tolerated_no_crash():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        os.makedirs(d, exist_ok=True)
        with open(m.STATE_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json::: [[[")
        st = m._read_state()
        assert st == {"jobs": {}}
        assert m._resolve_conv("auto") is None
        m._write_state(conversation="conv-x", rid="REQ-x")
        assert m._read_state()["jobs"]["REQ-x"]["conversation_id"] == "conv-x"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_jobs_not_a_dict_tolerated_no_crash():
    d = _fresh_state_dir()
    try:
        m = _cdp(d)
        os.makedirs(d, exist_ok=True)
        with open(m.STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"jobs": "not-a-dict"}, f)
        st = m._read_state()
        assert st == {"jobs": {}}
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'PASS' if not fails else 'FAIL'} — {fails} failure(s)")
    sys.exit(1 if fails else 0)
