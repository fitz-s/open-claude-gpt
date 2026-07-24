# Tests for the consult egress daemon (cgc_daemon.py) over the SQLite store — dispatch, reattach
# priority, browser maintenance (sweep/repair), the per-attempt stderr scoping of _run, and the
# daemon singleton. No network, no real Chrome: subprocess spawns and _run are always monkeypatched.
#
# cgc_daemon.py does `import cgc_spool as spool` internally after inserting its own directory on
# sys.path. Both modules read env (SPOOL_DIR etc.) at import time, so we load cgc_spool FIRST
# under the test's tmp_path env, register it in sys.modules under the name "cgc_spool", then load
# cgc_daemon by path so its `import cgc_spool as spool` resolves to that same already-configured
# instance instead of re-importing (and re-reading env) on its own.
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "state" / "spool"))
    monkeypatch.delenv("CGC_GATE_ALLOW_GIST", raising=False)

    spool = _load_module("cgc_spool", "cgc_spool.py")
    sys.modules["cgc_spool"] = spool  # so cgc_daemon's `import cgc_spool as spool` reuses this instance

    d = _load_module("cgc_daemon", "cgc_daemon.py")
    d.spool.ensure_dirs()
    # db_path() resolves from the environment at call time; the conftest _isolate_durable_state
    # fixture already pins CGC_STORE_DB into this test's tmp dir, so nothing bleeds across tests.
    return d


def _rid(suffix="000001"):
    return f"REQ-20260707-120000-{suffix}"


# ---- _run: per-attempt stderr scoping ----------------------------------------

def test_run_stderr_is_scoped_to_the_current_attempt_not_the_cumulative_log(daemon):
    """Regression (S0 auto-duplicate): the per-rid log is append-only across every attempt, so a
    whole-file tail could carry an EARLIER attempt's `composer_not_ready` into the CURRENT attempt's
    returned stderr. The store worker reads that stale marker as proof THIS attempt did not send and
    re-queues a send that may already have happened. `_run` must return only log_start..EOF — the
    child that just ran — so a proven-not-sent verdict is always the current attempt's own."""
    rid = _rid("00c001")
    # attempt 1: a pre-click fail-closed marker, written to the shared per-rid log.
    c1 = [sys.executable, "-c", "import sys; sys.stderr.write('CGC_ERROR composer_not_ready: no input box\\n')"]
    _code1, _so1, se1 = daemon._run(c1, 30, rid)
    assert "composer_not_ready" in se1, "the current attempt's own marker must be returned"
    # attempt 2 on the SAME rid: clicks, then exits ambiguously with a SHORT stderr and no marker.
    c2 = [sys.executable, "-c", "import sys; sys.stderr.write('clicked send; turn schema ambiguous\\n')"]
    _code2, _so2, se2 = daemon._run(c2, 30, rid)
    assert "composer_not_ready" not in se2, \
        "attempt 1's marker must NOT leak into attempt 2 — that leak is the auto-resend duplicate bug"
    assert "ambiguous" in se2, "attempt 2's own stderr must be what is returned"


# ---- _dispatch_store ---------------------------------------------------------

def test_ready_orphan_is_redispatched_not_stranded(daemon, monkeypatch):
    """A `ready` round with no live worker (its Popen failed, or the worker died before begin_send)
    was stranded forever: claim_ready only picks `queued`, and recover() reported it as dispatchable
    but nothing acted on it. _dispatch_store must re-dispatch it — a `ready` round is pre-send, so
    re-running process_round from it is safe."""
    spawned = []

    class _P:
        pid = 4321
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda argv, **k: spawned.append(argv) or _P())

    with daemon.store_mod.Store() as s:
        s.create_round("REQ-20260707-120000-00d001", "submit")
        s.set_state("REQ-20260707-120000-00d001", daemon.store_mod.READY)  # claimed, worker never ran

    daemon._dispatch_store({}, concurrency=3, daemon_instance_id="d1")
    worker_argvs = [a for a in spawned if "--worker-store" in a]
    assert any("REQ-20260707-120000-00d001" in a for a in worker_argvs), \
        "the orphaned ready round must be re-dispatched"


def test_dispatch_store_respects_concurrency_and_claims_distinct_rounds(daemon, monkeypatch):
    """Parallel consults: _dispatch_store fills up to `concurrency` worker slots with DISTINCT rounds
    (claim_ready is atomic, so no two workers get the same one), and holds the rest back until a slot
    frees. Each worker occupies its slot for the whole consult (submit + the ~25-min wait), so this
    cap is the real parallel-consult limit."""
    spawned = []

    class _P:
        pid = 1000
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda argv, **k: spawned.append(argv[-1]) or _P())

    with daemon.store_mod.Store() as s:
        for i in range(5):
            s.create_round(f"REQ-20260707-120000-00e0{i:02d}", "submit")  # 5 queued

    children = {}
    daemon._dispatch_store(children, concurrency=3, daemon_instance_id="d1")
    assert len(children) == 3, "must fill exactly the 3 available slots"
    assert len(set(spawned)) == 3, "each slot got a DISTINCT round — no double-claim"
    # the two extra rounds stay queued for a later loop (nothing claimed them)
    with daemon.store_mod.Store() as s:
        queued = [r["rid"] for r in s.db.execute(
            "SELECT rid FROM rounds WHERE state='queued'")]
    assert len(queued) == 2, "surplus rounds wait for a free slot"


def test_dispatch_store_reattach_takes_priority_over_new_sends(daemon, monkeypatch):
    """Under a full-ish cap, an in-flight round that lost its worker (reattach) is dispatched BEFORE a
    new queued send — resuming a paid, possibly-generating consult matters more than starting a new
    one."""
    spawned = []

    class _P:
        pid = 2000
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda argv, **k: spawned.append((argv[-2], argv[-1])) or _P())

    with daemon.store_mod.Store() as s:
        # one waiting round (reattach) + two queued (new sends)
        s.create_round("REQ-20260707-120000-00f000", "submit"); s.set_state("REQ-20260707-120000-00f000", daemon.store_mod.READY)
        aid = s.begin_send("REQ-20260707-120000-00f000", "b", "h" * 64, daemon_instance_id="d0")
        s.mark_accepted(aid, "conv-live"); s.mark_waiting("REQ-20260707-120000-00f000")
        s.create_round("REQ-20260707-120000-00f001", "submit")
        s.create_round("REQ-20260707-120000-00f002", "submit")

    daemon._dispatch_store({}, concurrency=1, daemon_instance_id="d1")
    assert spawned == [("--worker-store-resume", "REQ-20260707-120000-00f000")], \
        "the single slot went to the reattach, not a new send"


# ---- browser maintenance: sweep + repair -------------------------------------

def test_tab_sweep_is_a_noop_with_one_tab(daemon, monkeypatch):
    """The browser is left with a tab on purpose — a profile with zero windows is a worse state."""
    import io

    monkeypatch.setattr(daemon.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps([{"type": "page", "id": "A"}]).encode()))
    assert daemon._sweep_tabs() == 0


def test_maintenance_only_runs_with_no_live_children(daemon):
    """Sweep and repair are browser-GLOBAL (they can close/restart the tab a live worker is using).
    The loop must gate them on an empty child map — the child map is the daemon's complete knowledge
    of live store workers."""
    import inspect
    loop = inspect.getsource(daemon.run_loop)
    assert "if not children and time.time() >= next_maintenance" in loop, \
        "maintenance must be guarded by an empty child map"


def test_repair_triggers_on_tab_family_queued_rounds(daemon, monkeypatch):
    """A round whose submit failed pre-click with a tab-family error is requeued (safe — provably
    unsent). But a browser PERMANENTLY unable to open tabs turns that into an infinite silent loop:
    dispatched, attach fails, requeued, forever. The daemon must break the loop by restarting Chrome
    when it sees tab-family errors on queued rounds and has no live children."""
    with daemon.store_mod.Store() as s:
        s.create_round(_rid("00r001"), "submit")
        s.set_state(_rid("00r001"), daemon.store_mod.READY)
        s.begin_send(_rid("00r001"), "b", "h" * 64, daemon_instance_id="d0")
        s.set_state(_rid("00r001"), daemon.store_mod.QUEUED,
                    expect=daemon.store_mod.SENDING, error_code="attach_failed")

    restarts = []
    monkeypatch.setattr(daemon, "_restart_chrome", lambda: restarts.append(1) or (True, ""))
    t = daemon._maybe_repair_browser({}, last_repair=0.0)
    assert restarts == [1], "tab-family queued rounds with no children must trigger a restart"
    assert t > 0.0, "the repair timestamp must advance (cooldown)"


def test_repair_respects_children_and_cooldown(daemon, monkeypatch):
    import time as _time
    with daemon.store_mod.Store() as s:
        s.create_round(_rid("00r002"), "submit")
        s.set_state(_rid("00r002"), daemon.store_mod.READY)
        s.begin_send(_rid("00r002"), "b", "h" * 64, daemon_instance_id="d0")
        s.set_state(_rid("00r002"), daemon.store_mod.QUEUED,
                    expect=daemon.store_mod.SENDING, error_code="attach_failed")

    monkeypatch.setattr(daemon, "_restart_chrome",
                        lambda: (_ for _ in ()).throw(AssertionError("must not restart")))
    # live children → no-op (a restart is global and would break them)
    assert daemon._maybe_repair_browser({"x": object()}, last_repair=0.0) == 0.0
    # cooldown not yet elapsed → no-op (a restart that doesn't cure must not loop hot)
    now = _time.time()
    assert daemon._maybe_repair_browser({}, last_repair=now) == now


def test_repair_is_a_noop_without_tab_family_errors(daemon, monkeypatch):
    with daemon.store_mod.Store() as s:
        s.create_round(_rid("00r003"), "submit")   # plain queued, no error
    monkeypatch.setattr(daemon, "_restart_chrome",
                        lambda: (_ for _ in ()).throw(AssertionError("must not restart")))
    assert daemon._maybe_repair_browser({}, last_repair=0.0) == 0.0


def test_restart_is_not_reported_successful_until_a_tab_actually_works(daemon, monkeypatch):
    """The launcher proves the port is up and the session is logged in. Neither is what broke: an
    unwell Chrome keeps serving existing tabs while new ones never answer Runtime.enable, so both
    those checks stay true while every consult dies. A retry 3s after a 'successful' restart failed
    with `No such target id`, because the browser was still settling."""
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(daemon.time, "sleep", lambda n: None)
    monkeypatch.setattr(daemon, "_new_tab_healthy", lambda: False)
    assert daemon._restart_chrome()[0] is False, \
        "a browser that still cannot open a tab is not a successful restart"

    monkeypatch.setattr(daemon, "_new_tab_healthy", lambda: True)
    assert daemon._restart_chrome()[0] is True


def test_health_check_is_the_real_capability_not_a_ping(daemon):
    """It must create a tab and drive it, because 'the port answers' was already true when the
    browser was broken."""
    import inspect
    src = inspect.getsource(daemon._new_tab_healthy)
    assert "Target.createTarget" in src and "Runtime.enable" in src
    assert "Target.closeTarget" in src, "the probe must not leak the tab it opens"


def test_health_probe_keeps_the_last_tab(daemon):
    """A browser with zero pages has nothing for the login probe to evaluate on, so the gate
    degrades to 'login unverified' and the login check fails OPEN. Observed exactly that."""
    import inspect
    src = inspect.getsource(daemon._new_tab_healthy)
    assert "if others:" in src, "the probe must not close the only remaining tab"


# ---- the daemon singleton ----------------------------------------------------

def test_daemon_singleton_refuses_a_second_daemon(daemon):
    """Two daemons on one store would each keep their own concurrency count and child map, doubling
    the global cap and racing on the browser. The singleton lock refuses the second and frees on
    release."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    first = daemon.spool.acquire_daemon_singleton()
    assert first is not None, "first daemon must acquire the singleton"
    # timeout=0 → immediate, non-blocking check (the default WAITS ~40s for a draining daemon)
    assert daemon.spool.acquire_daemon_singleton(timeout=0) is None, "a second daemon must be refused"
    first.close()  # release
    second = daemon.spool.acquire_daemon_singleton(timeout=0)
    assert second is not None, "singleton must be re-acquirable once released"
    second.close()


def test_run_loop_takes_the_singleton_and_exits_if_held(daemon):
    """run_loop must acquire the singleton before doing any work and exit cleanly if another daemon
    already holds it — the enforcement, not just the primitive."""
    import inspect
    loop = inspect.getsource(daemon.run_loop)
    assert "acquire_daemon_singleton()" in loop, "run_loop must acquire the daemon singleton"
    assert "return 0" in loop and "already running" in loop, "and exit if another daemon holds it"
