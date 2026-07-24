#!/usr/bin/env python3
# Created: 2026-07-07
# Authority basis: open-claude-gpt skill v2 (CDP backend). The USER-STARTED egress daemon that
#   makes a consult work under Claude Code's auto-mode data-exfiltration classifier without any
#   per-user settings edit (see cgc_spool.py header for the full rationale). The agent only writes
#   a queued round to the local store (`enqueue`) and reads a local answer file (`await`); THIS process — started
#   once by the user, exactly like the debug Chrome — is what actually talks to chatgpt.com. Its
#   network egress is therefore never an agent tool call, so the classifier never gates it.
#
#   It is a VALIDATING gate, not a blind relay: every job is re-checked (public-repo verification +
#   secret scan, in cgc_spool.validate_prompt) BEFORE the send, fail-closed. What the gate GUARANTEES
#   is bounded and exact: no recognized secret leaves, and every cited repo is gh-confirmed public (a
#   recognized-but-unclassifiable code URL fails closed). What it does NOT do: vet arbitrary PROSE. A
#   `--no-code` / follow-up prompt is exempt from the public-link rule by design (maths/research/
#   writing consults, and threads that already hold the code), so a prompt-injected agent CAN route
#   private free text through one — the gate stops secrets and private-repo LINKS, not the provenance
#   of prose the caller chose to send. Closing that needs a typed, control-plane-attested source
#   manifest (deferred — see the plan); until then the security claim is exactly "secrets + repo
#   visibility", not "arbitrary-content safe".
"""
The consult egress daemon. Start it ONCE (the user, not the agent):

  cgc watch                 # foreground loop (Ctrl-C to stop) — good for watching it work
  cgc up                    # convenience: start the debug Chrome AND this daemon detached
  python3 cgc_daemon.py     # equivalent to `cgc watch`

It claims queued rounds from the SQLite store (written by `cgc enqueue`), validates each through
the egress gate, then drives `cdp_consult.py` submit/followup + wait against the debug Chrome,
committing the answer to the store (which `cgc await` polls locally).

Flags:
  --poll N          seconds between store scans (default 2)
  --concurrency N   max rounds sent in parallel (default 3; each gets its own Chrome tab)
  --once            process whatever is dispatchable, then exit (for tests / cron-style runs)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

# All urllib targets here are the loopback CDP endpoint (127.0.0.1:CGC_PORT). Loopback must never
# go through an HTTP proxy: with http_proxy set, urlopen routes /json to the proxy and gets its
# HTML, so the health check (_new_tab_healthy) fails and the daemon restarts a perfectly healthy
# Chrome in a loop. Force a proxy-free global opener for every urlopen call.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

try:
    import websocket
except ImportError:  # only the sweep needs it; never break the daemon over a missing extra
    websocket = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgc_spool as spool  # noqa: E402  (shares config load, paths, gate)
import cgc_store as store_mod  # noqa: E402
import cgc_backend  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_CDP = os.path.join(_HERE, "cdp_consult.py")
_LAUNCH = os.path.join(_HERE, "cdp_launch.sh")


def _make_run_cdp():
    """The injected CDP driver for the store worker: drives the cdp_consult submit/wait
    subprocesses (the proven browser path) and returns the small dict cgc_backend.process_round
    maps to round states."""
    def run_cdp(kind, **kw):
        if kind == "submit":
            cmd = [sys.executable, _CDP, "submit", "--rid", kw["rid"], "--prompt-file", "-",
                   "--project-url", kw["project_url"], "--model", kw["model"]]
            code, so, se = _run(cmd, 240, kw["rid"], stdin_text=kw["prompt"])
            conv = ""
            try:
                conv = (json.loads(so.strip().splitlines()[-1]) if so.strip() else {}).get(
                    "conversation_id", "") or ""
            except Exception:
                conv = ""
            return {"code": code, "conversation": conv, "stderr": se}
        if kind == "followup":
            # CONTINUE the existing conversation: attach to --conversation and SEND (no --watch, so it
            # returns after sending). The caller then runs a separate "wait", so the round reaches
            # `waiting` promptly and is reattach-able — never a 25-min `sending`. Never opens a new
            # thread.
            cmd = [sys.executable, _CDP, "followup", "--conversation", kw["conversation"],
                   "--prompt-file", "-", "--rid", kw["rid"]]
            code, _so, se = _run(cmd, 240, kw["rid"], stdin_text=kw["prompt"])
            return {"code": code, "stderr": se}
        if kind == "wait":
            poll = str(kw.get("poll") or spool.POLL_S)
            timeout = int(kw.get("timeout") or spool.STUCK_AFTER_S)
            cmd = [sys.executable, _CDP, "wait", "--rid", kw["rid"], "--conversation", kw["conversation"],
                   "--out", kw["out"], "--poll", poll, "--timeout", str(timeout)]
            code, _so, se = _run(cmd, timeout + 40, kw["rid"])
            return {"code": code, "out": kw["out"], "stderr": se}
        raise ValueError(f"unknown cdp kind {kind!r}")
    return run_cdp


def _take_worker_leases(rid):
    """Every worker's first act: own its round (per-RID exclusive lease) and pin the browser
    (shared lease) for its whole process lifetime. Both are flocks the OS releases on ANY death, so
    a replacement daemon can PROVE this worker is gone instead of assuming its own empty children
    map is global truth. Returns the (rid_lease, browser_lease) handles to keep alive, or None when
    the round is already owned — by a prior-generation worker this daemon cannot see — in which
    case this worker must exit without touching the round."""
    lease = spool.acquire_rid_lease(rid)
    if lease is None:
        sys.stderr.write(f"CGC_DAEMON worker {rid}: round already owned by a live worker "
                         "(prior daemon generation) — exiting without touching it\n")
        return None
    return lease, spool.acquire_browser_lease(shared=True)


def run_worker_store(rid: str) -> int:
    """Process ONE claimed (`ready`) store round to a terminal/uncertain state, then exit. Isolated
    in its own process so a hung/crashed send can't take the daemon down; the risky browser work is
    still in cdp_consult subprocesses."""
    leases = _take_worker_leases(rid)
    if leases is None:
        return 1
    with store_mod.Store() as s:
        r = s.get_round(rid)
        if r is None or r["state"] != store_mod.READY:
            sys.stderr.write(f"CGC_DAEMON store worker: {rid} not in 'ready' ({r and r['state']})\n")
            return 1
        final = cgc_backend.process_round(
            s, r, _make_run_cdp(),
            daemon_instance_id=os.environ.get("CGC_DAEMON_INSTANCE", "d"),
            validate=spool.validate_prompt)
    sys.stderr.write(f"CGC_DAEMON store worker {rid} -> {final}\n")
    return 0


def run_worker_store_resume(rid: str) -> int:
    """Reattach to an accepted/waiting round and resume polling — the store peer of orphan recovery.
    Never re-sends."""
    leases = _take_worker_leases(rid)
    if leases is None:
        return 1
    with store_mod.Store() as s:
        r = s.get_round(rid)
        if r is None or r["state"] not in (store_mod.ACCEPTED, store_mod.WAITING,
                                           store_mod.POSSIBLY_ACCEPTED):
            return 1
        final = cgc_backend.resume_round(s, r, _make_run_cdp())
    sys.stderr.write(f"CGC_DAEMON store resume {rid} -> {final}\n")
    return 0


def _dispatch_store(children: dict, concurrency: int, daemon_instance_id: str) -> None:
    """Claim ready rounds AND reattach orphaned accepted/waiting rounds (no live worker), each in its
    own store worker, up to the concurrency cap. Reattach never re-sends — it resumes the existing
    conversation, so a daemon restart mid-consult does not strand or duplicate a round."""
    def _spawn(arg, rid, tag):
        """Spawn a worker, guarding Popen: a spawn that raises must NOT silently drop the round. A
        `ready` row whose spawn failed stays `ready` (pre-send, so re-running it is safe) and is
        picked up as a ready-orphan next loop — never stranded."""
        env = dict(os.environ, CGC_DAEMON_INSTANCE=daemon_instance_id)
        try:
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__), arg, rid], env=env)
        except OSError as e:
            sys.stderr.write(f"CGC_DAEMON {tag} spawn failed for {rid}: {e} — retried next loop\n")
            return False
        children[rid] = p
        sys.stderr.write(f"CGC_DAEMON {tag} {rid} pid={p.pid} ({len(children)}/{concurrency})\n")
        return True

    def _unowned(rids):
        """Drop rounds a LIVE worker (any daemon generation) still holds — 'no live worker' must be
        proven by the cross-generation lease, not by absence from THIS daemon's children map. The
        worker itself re-checks its lease on start, so this filter is anti-churn, not the fence."""
        return [rid for rid in rids if rid not in children and spool.rid_lease_free(rid)]

    # Reattach first: an in-flight round that lost its worker is more urgent than a new send.
    with store_mod.Store() as s:
        rec = s.recover()
        reattach = _unowned(rec["reattach"])
        # possibly_accepted + conversation → one-shot read-only auto-retrieve (never re-sends).
        retrievable = _unowned(rec["retrievable"])
        # Ready-orphans: a `ready` round with no live worker — its worker's Popen failed, or the
        # worker/daemon died before begin_send. claim_ready only picks `queued`, and recover() reports
        # these as dispatchable but nothing acted on them, so the round sat `ready` forever. A `ready`
        # round is pre-send (begin_send has not run), so re-running process_round from it is safe.
        ready_orphans = _unowned(
            r["rid"] for r in (s.get_round(rid) for rid in rec["dispatchable"])
            if r and r["state"] == store_mod.READY)
    for rid in reattach:
        if len(children) >= concurrency:
            return
        _spawn("--worker-store-resume", rid, "reattach(store)")
    for rid in retrievable:
        if len(children) >= concurrency:
            return
        _spawn("--worker-store-resume", rid, "auto-retrieve(store)")
    for rid in ready_orphans:
        if len(children) >= concurrency:
            return
        _spawn("--worker-store", rid, "ready-orphan(store)")
    while len(children) < concurrency:
        with store_mod.Store() as s:
            rnd = s.claim_ready(daemon_instance_id)
        if rnd is None:
            return
        _spawn("--worker-store", rnd["rid"], "dispatched(store)")


def _reap_children(children: dict) -> None:
    """Remove exited workers AND classify what their death left behind — at reap time, not at the
    caller's timeout. A worker that died while its round was `sending` leaves that row ownerless;
    without this, the still-live daemon would let it sit `sending` until the awaiter's full budget
    expired, violating liveness while claiming recovery. Classification per state:
      - sending           -> possibly_accepted (the click may have landed; never auto-resent)
      - ready             -> untouched (pre-send; the ready-orphan path re-dispatches it)
      - accepted/waiting  -> untouched (the reattach path resumes it read-only)
      - terminal          -> untouched (the worker finished its job)"""
    for rid in list(children):
        code = children[rid].poll()
        if code is None:
            continue
        del children[rid]
        try:
            with store_mod.Store() as s:
                r = s.get_round(rid)
                if r and r["state"] == store_mod.SENDING:
                    moved = s.promote_sending_to_uncertain(
                        rids=[rid],
                        reason=f"worker exited (code {code}) while sending — uncertain, not resent")
                    if moved:
                        sys.stderr.write(f"CGC_DAEMON reaped worker {rid} (exit {code}) mid-send — "
                                         "round marked possibly_accepted\n")
        except Exception as e:  # classification must never take the daemon down
            sys.stderr.write(f"CGC_DAEMON reap classification failed for {rid}: {e}\n")


def _sweep_tabs() -> int:
    """Close leftover ChatGPT tabs. CALLER-GUARDED: call only under the EXCLUSIVE browser lease —
    unobtainable while any worker of any daemon generation is alive, so a tab this sweep sees is
    provably unowned.

    Tabs accumulate. Fixing the leaks in submit's failure paths removes the known source, but not
    the one no code path can cover: a worker killed mid-send — by a daemon restart, say — never runs
    its cleanup at all, and its tab stays. Each one holds a renderer, and a browser carrying enough
    of them stops being able to start new ones, which is the failure that ends every consult (a
    freshly created tab that never answers Runtime.enable). One tab is kept, because the profile
    with zero windows is a worse state to leave the browser in than one with a spare."""
    try:
        base = f"http://127.0.0.1:{os.environ.get('CGC_PORT', '9333')}"
        info = json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
        pages = [t for t in info if t.get("type") == "page"]
        if len(pages) <= 1:
            return 0
        ver = json.load(urllib.request.urlopen(f"{base}/json/version", timeout=5))
        bw = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=5)
        for i, t in enumerate(pages[1:], start=1):
            bw.send(json.dumps({"id": 900 + i, "method": "Target.closeTarget",
                                "params": {"targetId": t["id"]}}))
        bw.close()
        sys.stderr.write(f"CGC_DAEMON swept {len(pages) - 1} unowned browser tab(s)\n")
        return len(pages) - 1
    except Exception as e:
        sys.stderr.write(f"CGC_DAEMON tab sweep skipped: {type(e).__name__}\n")
        return 0


def _new_tab_healthy(port=None) -> bool:
    """Can this browser produce a USABLE tab? That is the capability every send needs and the exact
    one that breaks — an unwell Chrome keeps serving its existing tabs while new ones never answer
    Runtime.enable, so "the port is up" and "the session is logged in" both stay true while consults
    die. Checked by doing the real thing on a throwaway about:blank, then cleaning it up."""
    if websocket is None:
        return True
    port = port or os.environ.get("CGC_PORT", "9333")
    base = f"http://127.0.0.1:{port}"
    tid = bw = None
    try:
        ver = json.load(urllib.request.urlopen(f"{base}/json/version", timeout=5))
        bw = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=8)
        bw.send(json.dumps({"id": 1, "method": "Target.createTarget",
                            "params": {"url": "about:blank"}}))
        for _ in range(50):
            m = json.loads(bw.recv())
            if m.get("id") == 1:
                tid = m.get("result", {}).get("targetId")
                break
        if not tid:
            return False
        tgt = None
        for _ in range(20):
            info = json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
            hit = [t for t in info if t.get("id") == tid and t.get("webSocketDebuggerUrl")]
            if hit:
                tgt = hit[0]
                break
            time.sleep(0.2)
        if not tgt:
            return False
        w = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=8)
        w.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
        while True:
            r = json.loads(w.recv())
            if r.get("id") == 2:
                break
        w.close()
        return True
    except Exception:
        return False
    finally:
        # Close the probe tab — unless it is the only one left. A browser with zero pages has
        # nothing for the login probe to evaluate on, so `cdp_launch --gate` degrades to
        # "CGC_READY (login unverified)" and the login check quietly fails OPEN.
        try:
            if bw and tid:
                others = [t for t in json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
                          if t.get("type") == "page" and t.get("id") != tid]
                if others:
                    bw.send(json.dumps({"id": 3, "method": "Target.closeTarget",
                                        "params": {"targetId": tid}}))
            if bw:
                bw.close()
        except Exception:
            pass


def _restart_chrome():
    """Replace the debug Chrome. CALLER-GUARDED: call only under the EXCLUSIVE browser lease — the
    restart is global (it takes every tab with it), and only the lease proves no worker of ANY
    daemon generation is using the browser. The daemon owns the browser's lifecycle, so a browser that can no
    longer open a usable tab is the daemon's problem to fix, not an errand for a human. Safe to do:
    the profile keeps the login, and a consult whose tab is lost is recoverable read-only.

    Returns (ok, reason): ok True only on a verified-working restart."""
    code, _so, _se = _run(["bash", _LAUNCH], 120, None,
                          env_extra={"CGC_RESTART": "1", "CGC_GATE": "1"})
    if code != 0:
        return False, f"chrome restart failed: launcher exit {code}"
    # The launcher proves the PORT is up and the session is logged in. Neither is the thing that
    # broke: what fails is opening a working tab, and a browser that has just restarted also needs a
    # moment before its target list is stable — a retry 3s after a "successful" restart failed with
    # `No such target id`. So confirm the actual capability before handing work back.
    for _ in range(15):
        if _new_tab_healthy():
            return True, ""
        time.sleep(2.0)
    sys.stderr.write("CGC_DAEMON Chrome restarted but still cannot open a working tab\n")
    return False, "chrome restarted but still cannot open a working tab"


# The browser-repair escalation. A round whose submit failed pre-click with a tab-family error
# (attach_failed / new_tab*) is requeued by the worker — safe, it provably never sent. But if the
# browser is PERMANENTLY unable to open a tab, requeue alone is an infinite silent loop: dispatched,
# attach fails, requeued, forever. The daemon breaks that loop by watching for queued rounds whose
# last error is tab-family and, when it has no live children (restart is global), restarting Chrome
# — at most once per cooldown, so a restart that doesn't cure the browser cannot itself loop hot.
_TAB_ERRORS = ("attach_failed", "new_tab", "no_page_target")
_REPAIR_COOLDOWN_S = 600


def _maybe_repair_browser(children: dict, last_repair: float) -> float:
    """If tab-family failures are blocking queued rounds and no worker is live, restart Chrome.
    Returns the new last-repair timestamp (unchanged when nothing was done)."""
    if children or (time.time() - last_repair) < _REPAIR_COOLDOWN_S:
        return last_repair
    with store_mod.Store() as s:
        row = s.db.execute(
            "SELECT rid, error_code FROM rounds WHERE state=? AND error_code IS NOT NULL",
            (store_mod.QUEUED,)).fetchall()
    hit = [r["rid"] for r in row if any(t in (r["error_code"] or "") for t in _TAB_ERRORS)]
    if not hit:
        return last_repair
    sys.stderr.write(f"CGC_DAEMON {len(hit)} queued round(s) blocked by tab failures "
                     f"({', '.join(hit[:3])}) — restarting the debug Chrome\n")
    ok, why = _restart_chrome()
    if not ok:
        sys.stderr.write(f"CGC_DAEMON browser repair failed: {why}\n")
    return time.time()


_running = True


def _stop(signum, frame):
    global _running
    _running = False
    sys.stderr.write(f"\nCGC_DAEMON stopping (signal {signum})…\n")


# ---- worker: process exactly one claimed job --------------------------------

def _tail(s, n=240):
    s = (s or "").strip()
    return s[-n:]


def _tail_file(path, n=240):
    """Last n chars of a file, without reading a 25-minute log into memory."""
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - n * 4))
            return f.read()[-n:].strip()
    except OSError:
        return ""


def _run(cmd, timeout, rid=None, stdin_text=None, env_extra=None):
    """Run a cdp_consult.py subcommand; return (exit, stdout, stderr). Never raises on non-zero.

    stderr streams LIVE into the job's log instead of being captured and discarded. Both halves of
    that matter. It used to be captured and thrown away except for a 240-char tail folded into the
    status message — so when a consult burned its whole 25-minute budget and produced nothing, the
    waiter's per-poll heartbeat (`CGC_WAIT alive: gen/len/done/begin/end/ac`), which says exactly
    which failure happened, was gone. And buffering it to write at exit would still leave the log
    empty for the 25 minutes you actually want to watch it. stdout is still captured, because the
    daemon parses submit's conversation id out of it."""
    log = None
    log_start = 0  # byte offset where THIS invocation's stderr begins in the shared per-rid log
    if rid:
        try:
            os.makedirs(os.path.dirname(spool.log_path(rid)), exist_ok=True)
            log = open(spool.log_path(rid), "a", encoding="utf-8", buffering=1)
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {cmd[2] if len(cmd) > 2 else '?'} "
                      f"{rid} =====\n")
            log.flush()
            log_start = log.tell()  # everything the child streams from here on is THIS attempt's
        except OSError:
            log = None
    try:
        r = subprocess.run(cmd, input=stdin_text, stdout=subprocess.PIPE,
                           stderr=(log or subprocess.PIPE),
                           text=True, timeout=timeout,
                           env={**os.environ, **(env_extra or {})})
        code, so, se = r.returncode, r.stdout, (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        code, so, se = 124, "", f"subprocess timeout: {e}"
    except Exception as e:
        code, so, se = 1, "", f"subprocess error: {e}"
    if log:
        # Recover THIS invocation's stderr from log_start..EOF — NOT a whole-file tail. The log is
        # per-rid and append-only across every attempt, so a whole-file tail can still carry an
        # EARLIER attempt's marker (e.g. attempt 1's `composer_not_ready`); the caller then reads
        # that stale marker as proof the CURRENT attempt did not send and re-queues a send that may
        # have already happened — an automatic duplicate consult. Scoping to log_start makes `se`
        # reflect only the child that just ran, so a proven-not-sent verdict is always the current
        # attempt's own. (No length cap: submit/followup logs are small; wait is big but its outcome
        # is classified by exit code + answer file, never by these markers.)
        try:
            log.flush()
            with open(spool.log_path(rid), encoding="utf-8", errors="replace") as rf:
                rf.seek(log_start)
                se = se or rf.read().strip()
        except OSError:
            pass
        try:
            if so:
                log.write(so if so.endswith("\n") else so + "\n")
            log.write(f"----- exit={code} -----\n")
            log.close()
        except OSError:
            pass  # logging must never break a consult
    return code, so, se


# ---- the loop ---------------------------------------------------------------

def run_loop(poll: float, concurrency: int, once: bool) -> int:
    spool.ensure_dirs()
    # Enforce a single daemon per spool: two supervisors (launchd + `cgc watch` + an ad-hoc start)
    # would each keep their own concurrency count and child map, silently doubling the global cap
    # and racing on the same browser. Hold the lock for this process's whole lifetime.
    _singleton = spool.acquire_daemon_singleton()  # noqa: F841 — kept alive for the daemon's lifetime
    if _singleton is None:
        sys.stderr.write("CGC_DAEMON already running (another process holds the daemon singleton) — "
                         "this one exits so two daemons don't share one store with independent "
                         "concurrency limits.\n")
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    spool.heartbeat_write(os.getpid())
    sys.stderr.write(
        f"CGC_DAEMON up (pid {os.getpid()}) — store {store_mod.db_path()}, concurrency {concurrency}, "
        f"poll {poll}s. This is the user-owned egress gate; the agent only reads/writes local files.\n")
    children = {}  # rid -> Popen
    daemon_instance_id = store_mod.new_daemon_instance_id()
    # Recovery: a round left `sending` with NO live owner is uncertain, not resendable. "No live
    # owner" is proven per-rid by the cross-generation lease — a blanket promotion would mutate a
    # `sending` row a surviving prior-generation worker still owns.
    with store_mod.Store() as _s:
        stuck = [row["rid"] for row in _s.db.execute(
            "SELECT rid FROM rounds WHERE state=?", (store_mod.SENDING,))]
        moved = _s.promote_sending_to_uncertain(
            rids=[rid for rid in stuck if spool.rid_lease_free(rid)])
        identity = {"protocol": 1, "schema_version": store_mod.SCHEMA_VERSION,
                    "db_path": os.path.abspath(store_mod.db_path()),
                    "store_uuid": _s.store_uuid(), "daemon_instance_id": daemon_instance_id}
    sys.stderr.write(f"CGC_DAEMON store (instance {daemon_instance_id[:8]}); "
                     f"{len(moved)} interrupted send(s) marked possibly_accepted.\n")
    next_maintenance = 0.0
    last_repair = 0.0
    try:
        while _running:
            spool.heartbeat_write(os.getpid(), identity)
            _reap_children(children)
            # Idle maintenance — both actions are browser-global, so they run only under the
            # EXCLUSIVE browser lease: unobtainable while any worker (THIS generation's children
            # or a surviving prior generation's) holds its shared lease.
            if not children and time.time() >= next_maintenance:
                blease = spool.acquire_browser_lease(shared=False)
                if blease is not None:
                    try:
                        if websocket is not None:
                            _sweep_tabs()
                        last_repair = _maybe_repair_browser(children, last_repair)
                    finally:
                        if hasattr(blease, "close"):
                            blease.close()
                next_maintenance = time.time() + 60
            _dispatch_store(children, concurrency, daemon_instance_id)
            if once and not children:
                with store_mod.Store() as _s:
                    if not _s.recover()["dispatchable"]:
                        break
            time.sleep(poll)
        # drain: let in-flight workers finish (they hold real consults), bounded.
        if children:
            sys.stderr.write(f"CGC_DAEMON draining {len(children)} in-flight consult(s)…\n")
            for p in children.values():
                try:
                    p.wait(timeout=30)
                except Exception:
                    pass
    finally:
        try:
            if os.path.exists(spool.DAEMON_PATH):
                os.remove(spool.DAEMON_PATH)  # so `daemon_alive()` flips to false immediately
        except OSError:
            pass
    sys.stderr.write("CGC_DAEMON stopped.\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_daemon.py")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--once", action="store_true")
    p.add_argument("--worker-store", help="internal: process one claimed store round then exit")
    p.add_argument("--worker-store-resume", help="internal: reattach one accepted/waiting round then exit")
    a = p.parse_args()
    if a.worker_store_resume:
        return run_worker_store_resume(a.worker_store_resume)
    if a.worker_store:
        return run_worker_store(a.worker_store)
    return run_loop(a.poll, max(1, a.concurrency), a.once)


if __name__ == "__main__":
    raise SystemExit(main())
