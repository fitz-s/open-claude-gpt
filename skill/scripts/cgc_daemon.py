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
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgc_spool as spool  # noqa: E402  (shares config load, paths, gate)

_HERE = os.path.dirname(os.path.abspath(__file__))
_CDP = os.path.join(_HERE, "cdp_consult.py")

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


def _run(cmd, timeout, rid=None):
    """Run a cdp_consult.py subcommand; return (exit, stdout, stderr). Never raises on non-zero.

    stderr streams LIVE into the job's log instead of being captured and discarded. Both halves of
    that matter. It used to be captured and thrown away except for a 240-char tail folded into the
    status message — so when a consult burned its whole 25-minute budget and produced nothing, the
    waiter's per-poll heartbeat (`CGC_WAIT alive: gen/len/done/begin/end/ac`), which says exactly
    which failure happened, was gone. And buffering it to write at exit would still leave the log
    empty for the 25 minutes you actually want to watch it. stdout is still captured, because the
    daemon parses submit's conversation id out of it."""
    log = None
    if rid:
        try:
            os.makedirs(os.path.dirname(spool.log_path(rid)), exist_ok=True)
            log = open(spool.log_path(rid), "a", encoding="utf-8", buffering=1)
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {cmd[2] if len(cmd) > 2 else '?'} "
                      f"{rid} =====\n")
        except OSError:
            log = None
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=(log or subprocess.PIPE),
                           text=True, timeout=timeout, env=dict(os.environ))
        code, so, se = r.returncode, r.stdout, (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        code, so, se = 124, "", f"subprocess timeout: {e}"
    except Exception as e:
        code, so, se = 1, "", f"subprocess error: {e}"
    if log:
        try:
            if so:
                log.write(so if so.endswith("\n") else so + "\n")
            if se:
                log.write(se if se.endswith("\n") else se + "\n")
            log.write(f"----- exit={code} -----\n")
            log.close()
        except OSError:
            pass  # logging must never break a consult
        # stderr went to the file, so recover the tail the status message needs from there.
        se = se or _tail_file(spool.log_path(rid))
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
    timeout = int(job.get("timeout", spool.CONSULT_TIMEOUT_S))  # enqueue always writes it

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
    ok, reason = spool.validate_prompt(prompt)
    if not ok:
        sys.stderr.write(f"CGC_DAEMON GATE REFUSED {rid}: {reason}\n")
        spool.finish_job(rid, state="error", exit=2, out=out, msg=reason)
        return 2
    sys.stderr.write(f"CGC_DAEMON gate ok {rid}: {reason}\n")
    spool.write_status(rid, "processing", out=out, msg=reason)

    # subprocess wall-clock budget: mirror the old `timeout 899` outer wrapper (inner --timeout
    # clears it by ~29s so the salvage grab runs) — here the daemon is the supervisor, so give the
    # child timeout+40 and let cdp_consult's own --timeout do the graceful salvage.
    child_budget = timeout + 40

    if kind == "followup":
        conv = job.get("conversation") or "auto"
        cmd = [sys.executable, _CDP, "followup",
               "--conversation", conv, "--prompt-file", job["prompt_file"],
               "--rid", rid, "--model", job.get("model", "Pro"),
               "--watch", "--out", out, "--poll", poll, "--timeout", str(timeout)]
        code, so, se = _run(cmd, child_budget, rid)
        return _finish_from_wait(rid, code, out, se, conv)

    # kind == submit: send, capture the conversation id, then wait.
    cmd = [sys.executable, _CDP, "submit",
           "--rid", rid, "--prompt-file", job["prompt_file"],
           "--project-url", job.get("project_url", "https://chatgpt.com/"),
           "--model", job.get("model", "Pro")]
    code, so, se = _run(cmd, 240, rid)
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

def _requeue_orphans() -> int:
    """A job sitting in processing/ when the daemon starts has no worker. Workers are children of
    the daemon process, so a crash or a launchd restart leaves the job claimed but unowned — and
    nothing ever scans processing/ again, so the consult is lost silently while `await` keeps
    reporting it as still running. Move those back to pending/.

    Only jobs older than a full consult budget are touched. A worker's hard ceiling is
    CONSULT_TIMEOUT_S + 40 (child_budget), so anything past CONSULT_TIMEOUT_S + 120 provably has no
    live worker and cannot be double-sent — and a double send costs real ChatGPT quota."""
    cutoff = time.time() - (spool.CONSULT_TIMEOUT_S + 120)
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
        try:
            if os.path.getmtime(src) > cutoff:
                continue
            os.rename(src, spool.pending_path(rid))
        except OSError:
            continue
        spool.write_status(rid, "queued", msg="requeued: no worker owned it after a daemon restart")
        n += 1
    if n:
        sys.stderr.write(f"CGC_DAEMON requeued {n} orphaned job(s) from a previous run\n")
    return n


def run_loop(poll: float, concurrency: int, once: bool) -> int:
    spool.ensure_dirs()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    spool.heartbeat_write(os.getpid())
    sys.stderr.write(
        f"CGC_DAEMON up (pid {os.getpid()}) — spool {spool.SPOOL_DIR}, concurrency {concurrency}, "
        f"poll {poll}s. This is the user-owned egress gate; the agent only reads/writes local files.\n")
    children = {}  # rid -> Popen
    next_orphan_scan = 0.0
    try:
        while _running:
            spool.heartbeat_write(os.getpid())
            # Rescan periodically, not only at startup. A job becomes requeue-eligible only once it
            # is older than a worker's hard ceiling, so a daemon that restarts EARLY in a job's life
            # scans while that job is still ineligible and then never looks again — the consult sits
            # in processing/ forever. (Observed: daemon restarted 415s into a job whose window opens
            # at 1620s.) This also covers a worker that died without writing a terminal status while
            # the daemon itself stayed up.
            if time.time() >= next_orphan_scan:
                _requeue_orphans()
                next_orphan_scan = time.time() + 60
            # reap
            for rid in list(children):
                if children[rid].poll() is not None:
                    del children[rid]
            # dispatch
            for pf in spool.list_pending():
                if len(children) >= concurrency:
                    break
                claimed = spool.claim(pf)
                if not claimed:
                    continue  # another worker took it
                rid = os.path.basename(claimed)[:-5]
                p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", claimed],
                                     env=dict(os.environ))
                children[rid] = p
                sys.stderr.write(f"CGC_DAEMON dispatched {rid} ({len(children)}/{concurrency} busy)\n")
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
    a = p.parse_args()
    if a.worker:
        return run_worker(a.worker)
    return run_loop(a.poll, max(1, a.concurrency), a.once)


if __name__ == "__main__":
    raise SystemExit(main())
