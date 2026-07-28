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


# ---- reap classification (worker crash recovery) -----------------------------

class _DeadChild:
    def __init__(self, code=1):
        self.code = code
        self.pid = 99999

    def poll(self):
        return self.code


def _sending_round(daemon, rid):
    s = daemon.store_mod.Store()
    s.create_round(rid, "submit", prompt="p")
    s.set_state(rid, daemon.store_mod.READY)
    s.begin_send(rid, "p", daemon.store_mod.sha256("p"), daemon_instance_id="d1")
    return s


def test_reap_classifies_dead_sending_worker_as_possibly_accepted(daemon):
    """A worker that dies mid-send leaves its round `sending` with no owner. The still-live daemon
    must classify it AT REAP TIME — possibly_accepted within one loop — not leave it `sending`
    until the awaiter's full timeout (the liveness half of the recovery promise)."""
    rid = _rid("00d001")
    s = _sending_round(daemon, rid)
    children = {rid: _DeadChild(code=137)}
    daemon._reap_children(children)
    assert children == {}, "the dead child must be reaped"
    assert s.get_round(rid)["state"] == daemon.store_mod.POSSIBLY_ACCEPTED
    assert "worker exited" in (s.get_round(rid)["error_code"] or "")
    s.close()


def test_reap_leaves_non_sending_states_untouched(daemon):
    """ready stays dispatchable (the ready-orphan path re-runs it: pre-send, safe); waiting stays
    reattachable (read-only resume). Only `sending` is uncertain."""
    s = daemon.store_mod.Store()
    r_ready, r_wait = _rid("00d002"), _rid("00d003")
    s.create_round(r_ready, "submit", prompt="p")
    s.set_state(r_ready, daemon.store_mod.READY)
    s.create_round(r_wait, "submit", thread_id="t-w", prompt="p")
    s.set_state(r_wait, daemon.store_mod.READY)
    s.begin_send(r_wait, "p", daemon.store_mod.sha256("p"), daemon_instance_id="d1")
    a = s.get_round(r_wait)["current_attempt_id"]
    s.mark_accepted(a, "conv-w")
    s.mark_waiting(r_wait)
    children = {r_ready: _DeadChild(), r_wait: _DeadChild()}
    daemon._reap_children(children)
    assert s.get_round(r_ready)["state"] == daemon.store_mod.READY
    assert s.get_round(r_wait)["state"] == daemon.store_mod.WAITING
    s.close()


def test_reap_of_lease_refused_duplicate_leaves_winners_sending_untouched(daemon):
    """S3 losing-duplicate race. Daemon A spawns worker A and dies before A takes its rid lease;
    daemon B sees the lease free and spawns worker B; worker A then WINS the lease and reaches
    `sending`, worker B loses and exits EXIT_LEASE_REFUSED. When daemon B reaps its own losing
    worker B, it must NOT move worker A's live `sending` round to possibly_accepted — a lease-refused
    death is inert. We model 'worker A alive and owning the round' by holding the rid lease here."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00d010")
    s = _sending_round(daemon, rid)                     # round is `sending`
    winner = daemon.spool.acquire_rid_lease(rid)        # worker A owns it
    children = {rid: _DeadChild(code=daemon.EXIT_LEASE_REFUSED)}  # worker B (the loser) exits
    daemon._reap_children(children)
    assert children == {}, "the reaped duplicate is removed from the child map"
    assert s.get_round(rid)["state"] == daemon.store_mod.SENDING, \
        "the winner's live sending round must be untouched by the loser's reap"
    winner.close()
    s.close()


def test_reap_of_crashed_worker_skips_promotion_when_another_process_owns_the_lease(daemon):
    """Even a NON-refusal death (a crash exit code) must not promote a `sending` round while another
    live process holds the rid lease — that process is the real owner. Only the lease holder may
    reclassify; a reaper that cannot take the lease leaves the round `sending`."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00d011")
    s = _sending_round(daemon, rid)
    owner = daemon.spool.acquire_rid_lease(rid)         # a live process owns the round
    children = {rid: _DeadChild(code=139)}              # a crash, not a lease refusal
    daemon._reap_children(children)
    assert s.get_round(rid)["state"] == daemon.store_mod.SENDING, \
        "no promotion while another live process holds the lease"
    owner.close()
    s.close()


# ---- periodic ownerless-sending sweep ----------------------------------------

def test_periodic_sweep_promotes_a_dead_prior_generation_owners_sending_round(daemon):
    """S3 second half: a prior-generation worker owned a `sending` round at daemon-B startup and then
    died. It is not in daemon B's children, so the reaper never sees it; without a periodic sweep the
    row sits `sending` until the full await timeout. One sweep pass — rid not in children, lease free
    (owner dead) — must promote it to possibly_accepted, no daemon restart needed."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00d012")
    s = _sending_round(daemon, rid)                     # `sending`, no live owner (lease free)
    promoted = daemon._sweep_ownerless_sending({}, reason="periodic sweep test")
    assert rid in promoted
    assert s.get_round(rid)["state"] == daemon.store_mod.POSSIBLY_ACCEPTED
    s.close()


def test_periodic_sweep_leaves_a_live_owners_sending_round_untouched(daemon):
    """The sweep's fence is the per-rid lease: a `sending` round whose owner is still alive (lease
    held) is skipped, never promoted. A round in `children` is skipped without even probing."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00d013")
    s = _sending_round(daemon, rid)
    owner = daemon.spool.acquire_rid_lease(rid)         # a live prior-generation worker owns it
    promoted = daemon._sweep_ownerless_sending({}, reason="periodic sweep test")
    assert rid not in promoted
    assert s.get_round(rid)["state"] == daemon.store_mod.SENDING
    owner.close()
    s.close()


# ---- worker requires BOTH leases (rid + shared browser) ----------------------

def test_worker_refuses_when_browser_is_under_exclusive_maintenance(daemon):
    """A worker must hold the rid lease AND the shared browser lease. If browser-global maintenance
    holds the browser EXCLUSIVE, the shared acquire returns None; the worker must release the rid
    lease it just took and exit EXIT_LEASE_REFUSED WITHOUT touching the round (it stays ready)."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00d014")
    s = daemon.store_mod.Store()
    s.create_round(rid, "submit", prompt="p")
    s.set_state(rid, daemon.store_mod.READY)            # dispatchable, pre-send
    excl = daemon.spool.acquire_browser_lease(shared=False)  # maintenance holds the browser
    assert excl is not None
    assert daemon.run_worker_store(rid) == daemon.EXIT_LEASE_REFUSED
    assert s.get_round(rid)["state"] == daemon.store_mod.READY, "the round is untouched, still ready"
    assert daemon.spool.rid_lease_free(rid) is True, "the rid lease it briefly took was released"
    excl.close()
    s.close()


# ---- per-conversation exclusive mutator lease (same-thread followup send) ----

def test_worker_refuses_second_same_conversation_followup(daemon, monkeypatch):
    """S3: two follow-ups pinned to the SAME conversation must not both mutate its one shared
    composer. With the conversation lease already held (a stand-in for the first worker), the second
    worker exits EXIT_LEASE_REFUSED WITHOUT invoking any CDP send and WITHOUT touching the round (it
    stays ready); its rid+browser leases are released; and reaping its EXIT_LEASE_REFUSED death is
    inert (never a possibly_accepted promotion)."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    # any real CDP send would go through _run — assert it is never reached
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: pytest.fail("no CDP while refused"))
    rid = _rid("00c001")
    s = daemon.store_mod.Store()
    s.create_round(rid, "followup", prompt="continuing this consult",
                   spec_json=json.dumps({"conversation": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}))
    s.set_state(rid, daemon.store_mod.READY)
    held = daemon.spool.acquire_conversation_lease("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")  # the first worker owns the composer
    assert held is not None
    try:
        assert daemon.run_worker_store(rid) == daemon.EXIT_LEASE_REFUSED
        assert s.get_round(rid)["state"] == daemon.store_mod.READY, "the round is untouched, still ready"
        assert s.get_round(rid)["current_attempt_id"] is None, "no send attempt was begun"
        assert daemon.spool.rid_lease_free(rid) is True, "the rid lease it briefly took was released"
        # the reaper must treat this refusal death as inert
        children = {rid: _DeadChild(code=daemon.EXIT_LEASE_REFUSED)}
        daemon._reap_children(children)
        assert children == {}
        assert s.get_round(rid)["state"] == daemon.store_mod.READY, "reaping a refusal never promotes"
    finally:
        held.close()
    s.close()


def test_conversation_lease_is_exclusive_and_per_conversation(daemon):
    """The fence is exclusive AND scoped to one conversation: a held lease blocks a second acquire on
    the SAME conversation but not on a DIFFERENT one (no false serialization of distinct threads)."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    a = daemon.spool.acquire_conversation_lease("11111111-1111-4111-8111-111111111111")
    assert a is not None
    assert daemon.spool.acquire_conversation_lease("11111111-1111-4111-8111-111111111111") is None, "same conversation is exclusive"
    b = daemon.spool.acquire_conversation_lease("22222222-2222-4222-8222-222222222222")
    assert b is not None, "a different conversation is not blocked"
    a.close()
    b.close()
    assert daemon.spool.acquire_conversation_lease("11111111-1111-4111-8111-111111111111") is not None, "freed on release"


# ---- Fix S3-A: live-lease fd registry + inheritance into mutating CDP subprocesses ----

def test_live_lease_fds_registers_on_acquire_and_deregisters_on_close(daemon):
    """The registry is the source of truth for which fds a mutating subprocess must inherit: every
    lease-acquire helper registers its fd, and .close() de-registers it (before closing, so a reused
    fd number is never left falsely marked inheritable)."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    spool = daemon.spool
    assert spool.live_lease_fds() == (), "no leases held yet"
    rid_l = spool.acquire_rid_lease(_rid("00d001"))
    brow_l = spool.acquire_browser_lease(shared=True)
    conv_l = spool.acquire_conversation_lease("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    rid_fd, brow_fd, conv_fd = rid_l.fileno(), brow_l.fileno(), conv_l.fileno()
    assert set(spool.live_lease_fds()) == {rid_fd, brow_fd, conv_fd}, "acquire registers each fd"
    conv_l.close()
    assert conv_fd not in spool.live_lease_fds(), "close de-registers the fd"
    assert set(spool.live_lease_fds()) == {rid_fd, brow_fd}
    rid_l.close()
    brow_l.close()
    assert spool.live_lease_fds() == (), "every lease de-registered on close"


def test_mutating_cdp_subprocess_inherits_exactly_the_live_lease_fds(daemon, monkeypatch):
    """S3-A: the daemon hands every browser-mutating cdp_consult subprocess the live lease fds via
    subprocess pass_fds, so the inherited open file descriptions keep the rid/browser/conversation
    flocks held until the CDP child also exits — the backend dying mid-click can no longer release
    ownership out from under the running mutation."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    spool = daemon.spool
    rid = _rid("00d101")
    # a worker holds its rid + shared-browser leases, plus (followup mutating region) the conv lease
    rid_l = spool.acquire_rid_lease(rid)
    brow_l = spool.acquire_browser_lease(shared=True)
    conv_l = spool.acquire_conversation_lease("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    expected = set(spool.live_lease_fds())
    captured = {}

    class _R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        captured["pass_fds"] = kw.get("pass_fds")
        return _R()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    daemon._make_run_cdp()("followup", rid=rid,
                           conversation="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                           prompt="continuing this consult")
    assert set(captured["pass_fds"]) == expected, \
        "the followup subprocess must inherit exactly the held rid+browser+conversation lease fds"
    for lease in (conv_l, brow_l, rid_l):
        lease.close()


# ---- cross-generation leases (worker/browser fencing) ------------------------

def test_rid_lease_is_exclusive_and_freed_on_release(daemon):
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00e001")
    held = daemon.spool.acquire_rid_lease(rid)
    assert held is not None
    assert daemon.spool.rid_lease_free(rid) is False, "a held lease must read as owned"
    held.close()
    assert daemon.spool.rid_lease_free(rid) is True


def test_worker_refuses_a_round_owned_by_a_live_worker(daemon):
    """A replacement daemon may spawn a worker for a round an OLD-generation worker still owns.
    The new worker's first act is taking the per-RID lease; refused means exit WITHOUT touching
    the round — the fence against duplicate waiters."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00e002")
    held = daemon.spool.acquire_rid_lease(rid)
    assert daemon.run_worker_store(rid) == daemon.EXIT_LEASE_REFUSED
    assert daemon.run_worker_store_resume(rid) == daemon.EXIT_LEASE_REFUSED
    held.close()


def test_browser_exclusive_is_unobtainable_while_a_worker_holds_shared(daemon):
    """Tab sweep / Chrome restart are browser-global; the exclusive browser lease must be refused
    while ANY worker (any daemon generation) holds its shared lease."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    worker = daemon.spool.acquire_browser_lease(shared=True)
    assert worker is not None
    assert daemon.spool.acquire_browser_lease(shared=False) is None
    worker.close()
    excl = daemon.spool.acquire_browser_lease(shared=False)
    assert excl is not None
    excl.close()


def test_dispatch_skips_rounds_whose_lease_is_held(daemon, monkeypatch):
    """An accepted/waiting round owned by a surviving prior-generation worker must NOT get a second
    (duplicate) waiter from the new daemon."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    rid = _rid("00e003")
    s = daemon.store_mod.Store()
    s.create_round(rid, "submit", thread_id="t-l", prompt="p")
    s.set_state(rid, daemon.store_mod.READY)
    s.begin_send(rid, "p", daemon.store_mod.sha256("p"), daemon_instance_id="d1")
    a = s.get_round(rid)["current_attempt_id"]
    s.mark_accepted(a, "conv-l")
    s.mark_waiting(rid)
    s.close()
    spawned = []
    monkeypatch.setattr(daemon.subprocess, "Popen",
                        lambda cmd, **kw: spawned.append(cmd) or _DeadChild())
    held = daemon.spool.acquire_rid_lease(rid)
    children = {}
    daemon._dispatch_store(children, 3, "gen2")
    assert not any(rid in " ".join(map(str, c)) for c in spawned), \
        "a leased round must not be re-dispatched"
    held.close()
    daemon._dispatch_store(children, 3, "gen2")
    assert any(rid in " ".join(map(str, c)) for c in spawned), \
        "once the lease frees, the round is reattached"


# ---- lock namespace: durable data dir + fail-closed fcntl --------------------

def test_lock_files_live_under_the_data_dir_not_the_spool_dir(daemon, tmp_path, monkeypatch):
    """S2: coordination files (daemon singleton, browser lock, per-rid leases, heartbeat) must live
    beside control.db in the durable data dir, NEVER under SPOOL_DIR — which config documents as
    deletable scratch and which is overridable per generation. The lock dir must not move when
    CGC_SPOOL_DIR is overridden."""
    if daemon.spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    data_dir = daemon.store_mod.data_dir()
    spool_dir = daemon.spool.SPOOL_DIR
    lock_dir = daemon.spool._lock_dir()
    assert lock_dir == os.path.join(os.path.dirname(daemon.store_mod.db_path()), "locks"), \
        "the lock dir sits beside control.db"
    assert not lock_dir.startswith(spool_dir), "locks must not live under the spool/scratch dir"

    rid = _rid("00f0a1")
    lease = daemon.spool.acquire_rid_lease(rid)
    daemon.spool.heartbeat_write(pid=os.getpid())
    singleton = daemon.spool.acquire_daemon_singleton(timeout=0)
    browser = daemon.spool.acquire_browser_lease(shared=True)
    try:
        for p in (daemon.spool._lease_path(rid), daemon.spool.daemon_path(),
                  daemon.spool.daemon_lock_path(), daemon.spool.browser_lock_path()):
            assert p.startswith(data_dir), f"{p} must live under the durable data dir"
            assert os.path.exists(p), f"{p} was created under the lock dir"
            assert not p.startswith(spool_dir), f"{p} must not live under the spool dir"
    finally:
        lease.close()
        singleton.close()
        browser.close()


def test_flock_fails_closed_when_fcntl_is_unavailable(daemon, monkeypatch):
    """On a platform without fcntl there is no advisory lock; granting a fake lease would silently
    defeat every fence. The lease primitives must raise a clear error, not return a fake handle."""
    monkeypatch.setattr(daemon.spool, "fcntl", None)
    with pytest.raises(RuntimeError, match="fcntl"):
        daemon.spool.acquire_rid_lease(_rid("00f0b1"))
    with pytest.raises(RuntimeError, match="fcntl"):
        daemon.spool.acquire_browser_lease(shared=True)
    with pytest.raises(RuntimeError, match="fcntl"):
        daemon.spool.acquire_daemon_singleton(timeout=0)


def _pages(*urls):
    import io, json as _j
    return lambda *a, **k: io.BytesIO(_j.dumps(
        [{"type": "page", "id": f"T{i}", "url": u} for i, u in enumerate(urls)]).encode())


def test_tab_sweep_holds_for_an_uncertain_round_with_no_conversation(daemon, monkeypatch):
    """The evidence contradiction: `unknown_send` leaves its tab open ON PURPOSE — it is the only
    record of whether the prompt went — and the sweep, whose lease only proves 'no live worker',
    used to close it a minute later. With no conversation, nothing identifies that tab, so the only
    way to keep it is to keep them all."""
    monkeypatch.setattr(daemon.urllib.request, "urlopen",
                        _pages("https://chatgpt.com/", "https://chatgpt.com/c/aaa"))
    assert daemon._sweep_tabs(set(), ["REQ-20260707-120000-00u001"]) == 0


def test_the_hold_is_bounded_so_one_stranded_round_cannot_disable_all_consults(daemon):
    """possibly_accepted has no automatic exit, so an unbounded hold would let a single unreconciled
    round stop every future sweep — and a browser with enough tabs cannot open new ones, which is the
    failure that ends every consult. The hold expires; per-conversation protection does not."""
    import io, json as _j
    assert daemon._EVIDENCE_HOLD_S <= 24 * 3600
    old = {"updated_at": "2020-01-01T00:00:00+00:00"}
    assert daemon._age_s(old) > daemon._EVIDENCE_HOLD_S
    assert daemon._age_s({"updated_at": "not a timestamp"}) == 0.0, "unparseable reads as fresh"


def test_sweep_spares_only_the_tab_of_an_uncertain_rounds_conversation(daemon, monkeypatch):
    """A known conversation is precisely addressable, so keeping it costs ONE tab and blocks nothing.
    Everything else is litter and still goes."""
    closed = []

    class FakeWS:
        def send(self, m): closed.append(__import__("json").loads(m)["params"]["targetId"])
        def close(self): pass

    import io, json as _j

    def urlopen(url, *a, **k):
        if url.endswith("/json/version"):
            return io.BytesIO(_j.dumps({"webSocketDebuggerUrl": "ws://x"}).encode())
        return _pages("https://chatgpt.com/",
                      "https://chatgpt.com/c/6a684b38-bef4-83ea-83d4-134bf9610e05",
                      "https://chatgpt.com/c/6a630a18-cbcc-83ea-a985-2fcd42a0a173",
                      "about:blank")(url)

    monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(daemon.websocket, "create_connection", lambda *a, **k: FakeWS())
    assert daemon._sweep_tabs({"6a684b38-bef4-83ea-83d4-134bf9610e05"}, []) == 2
    assert closed == ["T2", "T3"], "the protected conversation's tab survives"


def test_a_tab_whose_url_cannot_be_read_is_protected_not_swept(daemon, monkeypatch):
    """Unidentifiable is not unowned: a protected tab caught mid-navigation reports no URL, and
    sweeping it destroys the evidence the protection exists to keep."""
    import io, json as _j

    def urlopen(url, *a, **k):
        if url.endswith("/json/version"):
            return io.BytesIO(_j.dumps({"webSocketDebuggerUrl": "ws://x"}).encode())
        return _pages("https://chatgpt.com/", "")(url)

    monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(daemon.websocket, "create_connection",
                        lambda *a, **k: pytest.fail("nothing may be closed"))
    assert daemon._sweep_tabs({"6a684b38-bef4-83ea-83d4-134bf9610e05"}, []) == 0


def test_evidence_fails_closed_when_the_store_cannot_be_read(daemon, monkeypatch):
    """Unable to read the store is unable to prove a tab is not evidence."""
    class Boom:
        def __enter__(self): raise RuntimeError("db gone")
        def __exit__(self, *a): return False
    monkeypatch.setattr(daemon.store_mod, "Store", lambda *a, **k: Boom())
    convs, hold = daemon._evidence()
    assert hold, "must hold the sweep"


def test_maintenance_sweep_is_passed_the_evidence(daemon):
    import inspect
    assert "_sweep_tabs(*_evidence())" in inspect.getsource(daemon.run_loop)
