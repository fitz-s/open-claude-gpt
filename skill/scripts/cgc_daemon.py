#!/usr/bin/env python3
# Created: 2026-07-07
# Authority basis: open-claude-gpt skill v2 (CDP backend). The USER-STARTED egress daemon that
#   makes a consult work under Claude Code's auto-mode data-exfiltration classifier without any
#   per-user settings edit (see cgc_spool.py header for the full rationale). The agent only writes
#   a local job file (`enqueue`) and reads a local answer file (`await`); THIS process — started
#   once by the user, exactly like the debug Chrome — is what actually talks to chatgpt.com. Its
#   network egress is therefore never an agent tool call, so the classifier never gates it.
#
#   It is a VALIDATING gate, not a blind relay: every job is re-checked (public-repo verification +
#   secret scan, in cgc_spool.validate_prompt) BEFORE the send, fail-closed. A prompt-injected agent
#   can only enqueue a job the gate will reject unless it references genuinely public code.
"""
The consult egress daemon. Start it ONCE (the user, not the agent):

  cgc watch                 # foreground loop (Ctrl-C to stop) — good for watching it work
  cgc up                    # convenience: start the debug Chrome AND this daemon detached
  python3 cgc_daemon.py     # equivalent to `cgc watch`

It polls $CGC_SPOOL_DIR/pending for jobs written by `cgc enqueue`, validates each through the
egress gate, then drives the existing `cdp_consult.py` submit/followup + wait against the debug
Chrome, writing the answer to the job's --out (which `cgc await` is polling locally).

Flags:
  --poll N          seconds between spool scans (default 2)
  --concurrency N   max jobs sent in parallel (default 3; each gets its own Chrome tab)
  --once            process whatever is pending, then exit (for tests / cron-style runs)
  --worker PATH     internal: process a single claimed job file, then exit
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
    """The injected CDP driver for the store worker: drives the SAME cdp_consult submit/wait
    subprocesses the file-spool worker uses (so the proven browser path is unchanged) and returns
    the small dict cgc_backend.process_round maps to round states."""
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


def run_worker_store(rid: str) -> int:
    """Process ONE claimed (`ready`) store round to a terminal/uncertain state, then exit — the
    store-backed peer of run_worker(). Isolated in its own process so a hung/crashed send can't take
    the daemon down; the risky browser work is still in cdp_consult subprocesses."""
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

    # Reattach first: an in-flight round that lost its worker is more urgent than a new send.
    with store_mod.Store() as s:
        rec = s.recover()
        reattach = [rid for rid in rec["reattach"] if rid not in children]
        # possibly_accepted + conversation → one-shot read-only auto-retrieve (never re-sends).
        retrievable = [rid for rid in rec["retrievable"] if rid not in children]
        # Ready-orphans: a `ready` round with no live worker — its worker's Popen failed, or the
        # worker/daemon died before begin_send. claim_ready only picks `queued`, and recover() reports
        # these as dispatchable but nothing acted on them, so the round sat `ready` forever. A `ready`
        # round is pre-send (begin_send has not run), so re-running process_round from it is safe.
        ready_orphans = [r["rid"] for r in
                         (s.get_round(rid) for rid in rec["dispatchable"])
                         if r and r["state"] == store_mod.READY and r["rid"] not in children]
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


def _other_live_jobs(rid) -> list:
    """Other rids currently being worked. Restarting Chrome is GLOBAL — it takes every tab with it —
    so it must not be done while someone else's consult is mid-flight."""
    out = []
    try:
        names = os.listdir(spool._p("processing"))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json") or n[:-5] == rid:
            continue
        if spool._live_owner(n[:-5]) is not None:
            out.append(n[:-5])
    return out


def _sweep_tabs() -> int:
    """Close leftover ChatGPT tabs when nothing is running.

    Tabs accumulate. Fixing the leaks in submit's failure paths removes the known source, but not
    the one no code path can cover: a worker killed mid-send — by a daemon restart, say — never runs
    its cleanup at all, and its tab stays. Each one holds a renderer, and a browser carrying enough
    of them stops being able to start new ones, which is the failure that ends every consult (a
    freshly created tab that never answers Runtime.enable).

    Guarded by the same rule as the restart: only when NO job is live, so a tab this sweep sees is
    by definition unowned. One tab is kept, because the profile with zero windows is a worse state
    to leave the browser in than one with a spare."""
    if _other_live_jobs(None):
        return 0
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


def _restart_chrome(rid=None):
    """Replace the debug Chrome. Call inside spool.lifecycle_lock(). The daemon owns the browser's lifecycle, so a browser that can no
    longer open a usable tab is the daemon's problem to fix, not an errand for a human — the whole
    point of running it under launchd was to stop consults stalling on people. Safe to do: the
    profile keeps the login, and a consult whose tab is lost is recoverable via `--kind retrieve`.

    Not safe to do BLINDLY, though: the restart is global. Doing it for one job while others are
    sending or waiting would tear their tabs away too, which is how a healthy consult ends up
    reported as broken.

    Returns (ok, reason): ok True only on a verified-working restart. On failure, reason is the
    ACTIONABLE explanation for the caller to persist to job status — some causes are transient
    (neighbours live), so the message tells the caller to re-enqueue rather than reporting the
    opaque attach failure that triggered the repair."""
    # Held across the scan AND the restart: without it the dispatcher can admit a worker between
    # the two, and that worker's tab is destroyed by a restart that believed it was alone.
    others = _other_live_jobs(rid)
    if others:
        why = (f"chrome restart suppressed: {len(others)} other consult(s) live "
               f"({', '.join(others[:3])}) — a restart is global and would break them; "
               f"re-enqueue once they finish")
        sys.stderr.write(f"CGC_DAEMON {rid}: NOT restarting Chrome — {why}\n")
        return False, why
    code, _so, _se = _run(["bash", _LAUNCH], 120, rid,
                          env_extra={"CGC_RESTART": "1", "CGC_GATE": "1"})
    if code != 0:
        return False, f"chrome restart failed: launcher exit {code}"
    # The launcher proves the PORT is up and the session is logged in. Neither is the thing that
    # broke: what fails is opening a working tab, and a browser that has just restarted also needs a
    # moment before its target list is stable — a retry 3s after a "successful" restart failed with
    # `No such target id`. So confirm the actual capability before handing the job back.
    for _ in range(15):
        if _new_tab_healthy():
            return True, ""
        time.sleep(2.0)
    sys.stderr.write(f"CGC_DAEMON {rid}: Chrome restarted but still cannot open a working tab\n")
    return False, "chrome restarted but still cannot open a working tab"

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


def run_worker(processing_file: str) -> int:
    job = spool._read_json(processing_file)
    if not isinstance(job, dict) or not job.get("rid"):
        sys.stderr.write(f"CGC_DAEMON bad job file {processing_file}\n")
        return 2
    rid = job["rid"]
    out = job.get("out") or os.path.join(spool.CGC_STATE_DIR, f"answer_{rid}.txt")
    kind = job.get("kind", "submit")
    poll = str(job.get("poll", 20))
    timeout = int(job.get("timeout", spool.STUCK_AFTER_S))  # enqueue always writes it

    if kind == "retrieve":
        # Attach to an existing conversation and read its answer. NOTHING is sent, so there is no
        # payload for the gate to validate. That is enforced structurally rather than trusted: a
        # retrieve job that carries a prompt is refused outright, so this branch can never become a
        # way to send unvalidated content. It exists so that recovering a consult whose waiter died
        # stays on the daemon path instead of forcing the agent onto the direct one.
        if job.get("prompt_file"):
            spool.finish_job(rid, state="error", exit=2, out=out,
                             msg="refused: a retrieve job must carry no prompt (it sends nothing)")
            return 2
        conv = job.get("conversation") or ""
        if not conv or conv == "auto":
            spool.finish_job(rid, state="error", exit=2, out=out,
                             msg="refused: retrieve needs an explicit conversation id")
            return 2
        spool.write_status(rid, "processing", out=out, conversation=conv,
                           msg=f"attaching to {conv} to read its answer (nothing sent)")
        rcmd = [sys.executable, _CDP, "wait", "--rid", rid, "--conversation", conv,
                "--out", out, "--poll", poll, "--timeout", str(timeout)]
        rcode, _rso, rse = _run(rcmd, timeout + 40, rid)
        return _finish_from_wait(rid, rcode, out, rse, conv)

    # --- the gate: re-validate independently before ANY external send --------
    try:
        prompt = open(job["prompt_file"], encoding="utf-8").read()
    except OSError as e:
        spool.finish_job(rid, state="error", exit=2, out=out, msg=f"prompt unreadable: {e}")
        return 2
    ok, reason = spool.validate_prompt(prompt)  # `prompt` is now the ONLY copy that matters
    if not ok:
        sys.stderr.write(f"CGC_DAEMON GATE REFUSED {rid}: {reason}\n")
        spool.finish_job(rid, state="error", exit=2, out=out, msg=reason)
        return 2
    sys.stderr.write(f"CGC_DAEMON gate ok {rid}: {reason}\n")
    spool.write_status(rid, "processing", out=out, msg=reason)

    # The daemon is the supervisor, so give the child a slightly wider wall-clock budget than its
    # own --timeout and let cdp_consult's timeout do the graceful salvage first.
    child_budget = timeout + 40

    if kind == "followup":
        conv = job.get("conversation") or "auto"
        # Same rule as submit: the validated bytes go over stdin, never a reopenable path.
        cmd = [sys.executable, _CDP, "followup",
               "--conversation", conv, "--prompt-file", "-",
               "--rid", rid, "--model", job.get("model", "Pro"),
               "--watch", "--out", out, "--poll", poll, "--timeout", str(timeout)]
        code, so, se = _run(cmd, child_budget, rid, stdin_text=prompt)
        return _finish_from_wait(rid, code, out, se, conv)

    # kind == submit: send, capture the conversation id, then wait.
    # Hand the CDP child the VALIDATED BYTES over stdin, not the pathname. Passing the path let the
    # child reopen a file any same-user process could have rewritten after validation, so what got
    # sent to ChatGPT need not be what passed the public-repo and secret checks. The gate is the
    # justification for this whole egress design; it has to cover the bytes that actually leave.
    _sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cmd = [sys.executable, _CDP, "submit",
           "--rid", rid, "--prompt-file", "-",
           "--project-url", job.get("project_url", "https://chatgpt.com/"),
           "--model", job.get("model", "Pro")]
    code, so, se = _run(cmd, 240, rid, stdin_text=prompt)
    # Match THIS invocation's stderr (`se`), never the whole-file tail. `_run` now returns the full
    # current-invocation stderr (log_start..EOF), so the START-of-message token is present AND the
    # match is isolated to the attempt that just ran. Using _tail_file here would reintroduce the
    # exact cross-attempt leak the diff-review flagged S0: a stale `cdp_attach_failed` from an
    # EARLIER attempt could match after THIS attempt clicked-then-failed, restarting Chrome and
    # automatically re-running submit — a resend of a possibly-clicked send.
    if code != 0 and "cdp_attach_failed" in se:
        # The browser can still serve its existing tabs but cannot produce a working new one, and
        # every send needs a new one. Retrying the job changes nothing; replacing Chrome does.
        sys.stderr.write(f"CGC_DAEMON {rid}: debug Chrome cannot open a usable tab — restarting it\n")
        with spool.lifecycle_lock() as lk:
            if lk.acquired:
                repaired, why = _restart_chrome(rid)
            else:
                # Fail closed: a peer admission/restart holds the lock; a global restart now could
                # tear tabs from workers being admitted under it. Skip the repair (job re-enqueues).
                repaired, why = False, ("chrome restart suppressed: browser lifecycle busy "
                                        "(a peer admission or restart holds the lock) — re-enqueue")
        if repaired:
            code, so, se = _run(cmd, 240, rid, stdin_text=prompt)
        else:
            # Persist the ACTIONABLE reason, not the opaque attach failure that triggered the repair.
            # Nothing was sent; several causes are transient, so the message says to re-enqueue.
            spool.finish_job(rid, state="error", exit=2, out=out,
                             msg=f"{why}; log {spool.log_path(rid)}")
            return 2
    spool.write_status(rid, "processing", msg=f"{reason}; sent sha256={_sha[:16]}")
    conv = ""
    try:
        conv = (json.loads(so.strip().splitlines()[-1]) if so.strip() else {}).get("conversation_id", "") or ""
    except Exception:
        conv = ""
    if code == 3:
        spool.finish_job(rid, state="blocker", exit=3, out=out, msg=_tail(se) or "login/model blocker at submit")
        return 3
    if code != 0 or not conv:
        spool.finish_job(rid, state="error", exit=2, out=out,
                         msg=_tail(se) or f"submit failed (exit {code}, conv {conv or 'none'})")
        return 2
    spool.write_status(rid, "processing", out=out, conversation=conv, msg="sent; waiting for answer")
    wcmd = [sys.executable, _CDP, "wait", "--rid", rid, "--conversation", conv,
            "--out", out, "--poll", poll, "--timeout", str(timeout)]
    wcode, wso, wse = _run(wcmd, child_budget, rid)
    return _finish_from_wait(rid, wcode, out, wse, conv)


def _finish_from_wait(rid, code, out, stderr, conv):
    """Map a cdp_consult.py wait/followup exit code to a terminal spool status."""
    if code == 0:
        spool.finish_job(rid, state="done", exit=0, out=out, conversation=conv,
                         msg="answer retrieved")
        return 0
    if code == 3:
        spool.finish_job(rid, state="blocker", exit=3, out=out, conversation=conv,
                         msg=_tail(stderr) or "login/captcha/rate-limit")
        return 3
    if code == 4:
        spool.finish_job(rid, state="no_answer", exit=4, out=out, conversation=conv,
                         msg=_tail(stderr) or "timeout with no usable answer")
        return 4
    spool.finish_job(rid, state="error", exit=2, out=out, conversation=conv,
                     msg=_tail(stderr) or f"wait failed (exit {code})")
    return 2


# ---- the loop ---------------------------------------------------------------

def _recover_orphans() -> int:
    """Recover jobs left in processing/ with no worker running them.

    Workers are children of the daemon, so a crash or a launchd restart leaves the job claimed but
    unowned, and nothing ever re-scans processing/ — the consult is lost silently while `await`
    still reports it healthy.

    Orphanhood is DETERMINED, not guessed. The dispatcher records the worker's pid, so the question
    "is anyone working this?" is answered exactly by asking whether that pid is alive. The previous
    age heuristic — requeue once the job outlives a worker's hard ceiling — had to wait longer than
    any possible worker, which after the deadline became one number meant 62 minutes of a consult
    sitting dead before anything noticed. A job with no recorded pid predates this and still falls
    back to the age rule.

    Recovery prefers RETRIEVE over re-sending. The waiter dying does not stop ChatGPT: the
    conversation is usually still generating, so re-sending would open a second conversation, redo
    the round and bill the quota twice. Only a job that never got far enough to have a conversation
    is genuinely re-sent."""
    age_cutoff = time.time() - (spool.STUCK_AFTER_S + 120)
    n = 0
    try:
        names = sorted(os.listdir(spool._p("processing")))
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue
        src = spool._p("processing", name)
        rid = name[:-5]
        st = spool.read_status(rid) or {}
        pid = st.get("worker_pid")
        if pid is not None:
            if spool._pid_alive(pid):
                continue                      # genuinely being worked
        elif os.path.getmtime(src) > age_cutoff:
            continue                          # pre-pid job, not yet provably unowned
        job = spool._read_json(src) or {}
        conv = st.get("conversation") or job.get("conversation")
        try:
            if conv and conv != "auto":
                # Its ChatGPT conversation outlived the waiter — attach and read, do not re-ask.
                job = {"rid": rid, "kind": "retrieve", "conversation": conv,
                       "out": job.get("out") or os.path.join(spool.CGC_STATE_DIR, f"answer_{rid}.txt"),
                       "poll": job.get("poll", spool.POLL_S), "timeout": job.get("timeout", spool.STUCK_AFTER_S)}
                os.remove(src)
                spool.enqueue_job(job)
                spool.write_status(rid, "queued",
                                   msg=f"worker died; re-attaching to {conv} to read its answer")
            else:
                # No conversation recorded → the send MAY already have reached ChatGPT before the
                # worker died (an orphan can be post-click). Re-sending would duplicate a possible
                # send — the same at-most-once violation the store forbids. NEVER auto-resend; mark
                # it for human reconciliation. This also closes the rollback path the diff-review
                # flagged S0: a store round imported as possibly_accepted leaves a legacy processing
                # record, and a later CGC_STORE_BACKEND=0 rollback must not turn it back into an
                # automatic send.
                spool.finish_job(
                    rid, state="blocker", exit=3,
                    out=job.get("out") or os.path.join(spool.CGC_STATE_DIR, f"answer_{rid}.txt"),
                    msg="worker died before a conversation was recorded — the send may have happened; "
                        "NOT auto-resent (at-most-once). Retrieve by conversation or reconcile by hand.")
        except (OSError, ValueError):
            continue
        n += 1
    if n:
        sys.stderr.write(f"CGC_DAEMON recovered {n} orphaned job(s)\n")
    return n


def run_loop(poll: float, concurrency: int, once: bool) -> int:
    spool.ensure_dirs()
    # Enforce a single daemon per spool: two supervisors (launchd + `cgc watch` + an ad-hoc start)
    # would each keep their own concurrency count and child map, silently doubling the global cap
    # and racing on the same browser. Hold the lock for this process's whole lifetime.
    _singleton = spool.acquire_daemon_singleton()  # noqa: F841 — kept alive for the daemon's lifetime
    if _singleton is None:
        sys.stderr.write("CGC_DAEMON already running (another process holds the daemon singleton) — "
                         "this one exits so two daemons don't share one spool with independent "
                         "concurrency limits.\n")
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    spool.heartbeat_write(os.getpid())
    sys.stderr.write(
        f"CGC_DAEMON up (pid {os.getpid()}) — spool {spool.SPOOL_DIR}, concurrency {concurrency}, "
        f"poll {poll}s. This is the user-owned egress gate; the agent only reads/writes local files.\n")
    children = {}  # rid -> Popen
    next_orphan_scan = 0.0
    _store_mode = cgc_backend.store_enabled()
    daemon_instance_id = store_mod.new_daemon_instance_id()
    if _store_mode:
        # Recovery: a round left `sending` when the daemon died is uncertain, not resendable.
        with store_mod.Store() as _s:
            moved = _s.promote_sending_to_uncertain()
        sys.stderr.write(f"CGC_DAEMON store backend (instance {daemon_instance_id[:8]}); "
                         f"{len(moved)} interrupted send(s) marked possibly_accepted.\n")
    try:
        while _running:
            spool.heartbeat_write(os.getpid())
            # Rescan periodically, not only at startup. A job becomes requeue-eligible only once it
            # is older than a worker's hard ceiling, so a daemon that restarts EARLY in a job's life
            # scans while that job is still ineligible and then never looks again — the consult sits
            # in processing/ forever. (Observed: daemon restarted 415s into a job whose window opens
            # at 1620s.) This also covers a worker that died without writing a terminal status while
            # the daemon itself stayed up.
            # Legacy file-spool maintenance — ONLY in spool mode. In store mode both are wrong:
            # _recover_orphans rewrites the rollback spool (it can flip an old processing job back to
            # `pending` "re-sending", priming a future CGC_STORE_BACKEND=0 rollback to duplicate an
            # uncertain send), and _sweep_tabs decides liveness from file-spool PIDs alone — it knows
            # nothing about store rounds, so it can close the tab of an active store send/wait. Store
            # mode has its own recovery (startup promote_sending_to_uncertain + _dispatch_store
            # reattach) and no tab sweep yet, which is safer than a sweep that guesses ownership.
            if not _store_mode and time.time() >= next_orphan_scan:
                _recover_orphans()
                if websocket is not None:
                    _sweep_tabs()
                next_orphan_scan = time.time() + 60
            # reap
            for rid in list(children):
                if children[rid].poll() is not None:
                    del children[rid]
            # dispatch
            if _store_mode:
                _dispatch_store(children, concurrency, daemon_instance_id)
                if once and not children:
                    with store_mod.Store() as _s:
                        if not _s.recover()["dispatchable"]:
                            break
                time.sleep(poll)
                continue
            for pf in spool.list_pending():
                if len(children) >= concurrency:
                    break
                # Admission is serialised against browser restart: claim, spawn and publish the
                # worker pid atomically, so a worker is never invisible to a neighbour scan while it
                # is being admitted.
                with spool.lifecycle_lock() as lk:
                    if not lk.acquired:
                        # A browser restart holds the lock. Admitting now would hand a worker a tab
                        # the in-progress restart destroys — the reported "my consult was killed"
                        # bug. Admit nothing this cycle; pending jobs stay pending and retry next
                        # loop, once the restart releases the lock (fail closed, never unserialised).
                        break
                    claimed = spool.claim(pf)
                    if not claimed:
                        continue  # another worker took it
                    rid = os.path.basename(claimed)[:-5]
                    p = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                                          "--worker", claimed], env=dict(os.environ))
                    children[rid] = p
                    spool.write_status(rid, "processing", worker_pid=p.pid)
                sys.stderr.write(f"CGC_DAEMON dispatched {rid} pid={p.pid} "
                                 f"({len(children)}/{concurrency} busy)\n")
            if once and not children and not spool.list_pending():
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
    p.add_argument("--worker", help="internal: process one claimed job file then exit")
    p.add_argument("--worker-store", help="internal: process one claimed store round then exit")
    p.add_argument("--worker-store-resume", help="internal: reattach one accepted/waiting round then exit")
    a = p.parse_args()
    if a.worker_store_resume:
        return run_worker_store_resume(a.worker_store_resume)
    if a.worker_store:
        return run_worker_store(a.worker_store)
    if a.worker:
        return run_worker(a.worker)
    return run_loop(a.poll, max(1, a.concurrency), a.once)


if __name__ == "__main__":
    raise SystemExit(main())
