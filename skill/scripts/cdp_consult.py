#!/usr/bin/env python3
# Created: 2026-06-11
# Last reused or audited: 2026-06-15
# Authority basis: open-claude-gpt skill v2 (CDP backend — external DevTools client).
#   Adds `followup` (continue an existing conversation) so a consult is a multi-round
#   thread, not a one-shot, and an automated Step-0 gate (_ensure_chrome) so submit/
#   followup self-start the debug Chrome + check login with no LLM step. Answer
#   detect/extract read via textContent + a DOM walk (NOT innerText, which collapses on
#   a backgrounded tab — the cause of "1-char answer / waiter only returns on timeout").
"""
Pure-CDP backend for the open-claude-gpt skill.

WHY THIS EXISTS
---------------
The Claude-in-Chrome MCP path works with zero setup but pays three taxes:
javascript_tool RETURN values are privacy-scanned (URLs blocked), get_page_text
is hard-capped at 50000 chars (forcing DOM windowing), and every ScheduleWakeup
poll reloads the whole main-agent context (cache miss). An *external* Chrome
DevTools Protocol client is bound by none of these: the page CSP only constrains
the page's own JS, not a DevTools client; there is no MCP scanner and no 50k cap;
and the wait-loop runs in a detached shell process holding no agent context.

The cost is a one-time setup: a DEDICATED Chrome profile launched with a remote
debugging port, logged into ChatGPT Pro once. CDP is disallowed on Chrome's
default profile (anti-cookie-theft, Chrome 136+), so a separate --user-data-dir
is mandatory anyway. Use this profile ONLY for consults.

Launch (the user does this once; the agent never types credentials):
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9333 "--remote-allow-origins=http://127.0.0.1:9333" \
    --user-data-dir="$HOME/.cgc-chrome" --no-first-run --no-default-browser-check \
    "$CGC_PROJECT_URL"   # e.g. https://chatgpt.com/g/g-p-<id>-<slug>/project, or plain https://chatgpt.com/
Then log into ChatGPT Pro in that window. Leave it open.

SECURITY: this is ordinary browser automation against the user's own logged-in
session, in a profile they set up, writing the answer to a local file they own.
It reads only the ChatGPT answer text — never cookies, never cross-site data.

Subcommands:
  submit   --rid R --prompt-file F [--port P] [--project-url U]
           open a fresh project chat, type the prompt, submit. (control plane)
  followup --conversation C --prompt-file F [--port P] [--rid R]
           send a follow-up into an existing conversation (continues the thread,
           keeping its context + model). Attaches to an existing tab if one is still
           open, or reopens the conversation at /c/<conversation_id> otherwise —
           --keep-tab is an optional convenience, not a requirement.
  Both submit and followup share one PRE-CLICK paste boundary (_paste_prompt): a CAUGHT failure
  there exits EXIT_NOT_SENT_PRECLICK (6), proving the send click was never issued. An uncontrolled
  process death (SIGKILL, uncatchable crash) produces no exit 6 and stays classified uncertain.
  wait    --rid R --out F [--port P] [--poll S] [--timeout S]
          poll until the answer is complete, extract it between the bare-line BEGIN/END
          sentinels, write to --out, exit 0. Run as a detached background Bash;
          its exit re-invokes the agent = the wake. Exit codes:
            0 done — either a properly WRAPPED answer was extracted and written to --out,
              OR (at timeout, no wrapper) a substantial last-assistant message existed and
              was salvaged: written to --out, ALSO to a sibling "<out>.raw", with a
              CGC_UNWRAPPED log line (best-effort — never lose a present answer)
            3 blocker (login/captcha/rate-limit)
            4 timeout with NO usable answer at all (empty / only short streaming stubs) —
              a genuine no-answer timeout, distinct from the exit-0 salvage case above
            2 usage error, or (a concrete, non-'auto' --rid) rid_absent: that turn was never
              found among the conversation's user turns within the grace window
            5 rid_superseded/ambiguous: a concrete --rid's turn exists but is no longer the
              conversation's latest (a later turn landed before/during the wait) and no
              sentinel-wrapped answer anchors it — refused rather than guessing
  status  --rid R [--port P]   one-shot JSON {generating,done,blocker,len}
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# Every urllib target in this script is the loopback CDP endpoint (127.0.0.1:<debug-port>).
# Loopback must never traverse an HTTP proxy: with http_proxy set (e.g. a local LLM router),
# urlopen would route /json to the proxy and get its HTML back, so json.load fails and the
# debug Chrome looks dead. Force a proxy-free global opener for all urlopen calls here.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

try:
    import fcntl  # advisory file locking (POSIX only) — degrade gracefully if unavailable
except ImportError:
    fcntl = None

try:
    import websocket  # websocket-client
except ImportError:
    sys.stderr.write("CGC_ERROR missing_dep: pip install websocket-client\n")
    raise SystemExit(2)

# ---- configuration (all overridable via environment) ------------------------
# Everything personal/host-specific is read from the environment so the public
# skill ships no hard-coded identity. See docs/CONFIGURATION.md and .env.example.
#   CGC_PORT         remote-debugging port of the dedicated Chrome  (default 9333)
#   CGC_STATE_DIR    scratch dir for state + answer files           (default /tmp/cgc)
#   CGC_AUTO_MODEL   auto-pick the model tier before sending? 1/0    (default 1 = on)
#   CGC_MODEL        which tier to pick when auto-model is on        (default "Pro")
#   CGC_MODEL_FAMILY which MODEL to pin in the same picker menu      (default "Latest")
#                    — new on the GPT-6 composer (2026-09-03); "skip" disables the check
#   CGC_PROJECT_URL  ChatGPT URL a fresh consult opens; set this to YOUR project
#                    (…/g/g-p-<id>-<slug>/project) to keep consults in one project,
#                    or leave default to open a plain new chat.     (default new chat)
# Load persisted CGC_* settings from the user's config file (env still wins) — see
# cgc_config.py and `cgc set-project`. Makes the script's own dir importable first, so
# this works however the script is launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cgc_config import load_config as _cgc_load_config
    _cgc_load_config()
except Exception:
    pass

CGC_PORT = int(os.environ.get("CGC_PORT", "9333"))
CGC_STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")
# Auto model-selection is a toggle. ON (default): pick CGC_MODEL in the composer
# and fail closed if it can't be selected. OFF (CGC_AUTO_MODEL=0/false/no): don't
# touch the model picker — send on whatever the composer currently shows. This is
# the same effect as `--model skip`, exposed as a global switch.
CGC_AUTO_MODEL = os.environ.get("CGC_AUTO_MODEL", "1").strip().lower() not in ("0", "false", "no", "off", "")
CGC_MODEL = os.environ.get("CGC_MODEL", "Pro") if CGC_AUTO_MODEL else "skip"
# The MODEL, as distinct from the tier — a second dimension the composer only started exposing
# with GPT-6 (2026-09-03). Same toggle, same "skip" escape hatch as CGC_MODEL: auto-model OFF
# means the picker is not touched at all, so neither dimension is enforced.
CGC_MODEL_FAMILY = os.environ.get("CGC_MODEL_FAMILY", "Latest") if CGC_AUTO_MODEL else "skip"
CGC_PROJECT_URL = os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/")

# ---- per-rid job registry (makes follow-up zero-bookkeeping) ----------------
# submit/followup record the live conversation here, keyed by rid, so a follow-up never has to
# track the conversation_id across turns/background tasks: `--conversation auto` (the default)
# reads it back. This is THE thing that makes follow-up a habit instead of a chore. Multiple
# concurrent consults are addressable by their own rid; pass an explicit --conversation/--rid
# to pin one when juggling several at once.
#
# On-disk shape (registry keyed by rid — one entry per consult):
#   {"jobs": {"<rid>": {"conversation_id": "<conv>", "title": null,
#                        "ts": 1234.5, "status": "submitted"}, ...}}
# `status`: "submitted" (submit/followup sent) -> "answered" (wait wrote the answer).
# "active"/"recent" are VIEWS over jobs (most-recent-by-ts within the window), not separate
# stored fields — so there is nothing but the jobs dict to keep consistent.
STATE_PATH = os.path.join(CGC_STATE_DIR, "active.json")


STATE_LOCK_PATH = STATE_PATH + ".lock"

# How long a consult takes: a GPT-6 Astra Pro round reasons ~25 min typically, but a deep one (esp. a
# follow-up that re-reasons from scratch) was observed at ~62 min. Same number as
# cgc_spool.STUCK_AFTER_S — keep them in sync. This is NOT a budget for the consult and must never
# kill a healthy one for thinking; it is the point past which waiting is no longer explained by the
# work. 3600 stranded a 62-min answer minutes before it landed, so 5400 (90 min) covers the long
# tail; the read-only auto-retrieve backstops anything beyond it.
STUCK_AFTER_S = 5400

# READ-side stale-tab recovery (see cmd_wait). A CDP-attached ChatGPT tab can keep serving a DOM
# that has fallen behind the server-side conversation — observed 2026-09-06 on
# REQ-20260906-025243-c975c2, where the tab reported 3 assistant turns for a conversation that had
# 4 and never re-rendered; the answer existed the whole time. A waiter pinned to such a tab burns
# its entire timeout (90 min there) and returns a false stuck/timeout_no_answer on a round that
# actually succeeded. The signal the waiter already has is that the round's OWN source turn is not
# in the DOM: if it is still missing after this window, the tab is stale, not the send slow.
STALE_TURN_AFTER_S = 60
# Post-reload settle before the next probe. ChatGPT rebuilds the turn list asynchronously; probing
# immediately reads an empty <main> and wastes the one recovery on a page that had not painted yet.
STALE_RELOAD_SETTLE_S = 8
# The SECOND stale trigger (see cmd_wait's watch loop). The same dead DOM also strands a waiter
# AFTER the source turn resolved, so the missing-turn window above never fires: on
# REQ-20260906-025243-c975c2 generation began normally (gen=True, ac=4), the tab then dropped an
# assistant turn (ac 4->3) mid-stream and froze at 539 bytes for the remaining ~5200s, exiting 4
# with timeout_no_answer while a finished 36446-char answer sat on the server — a later wait that
# re-opened the conversation read it in 17s. The waiter already NAMES that state once per settle
# window ("stub-stable"); it fired ~18 times per dead wait and meant nothing. So the unit here is
# settle CYCLES, not polls: 3 x --settle-seconds (300s in production) = 15 minutes in which the tab
# reported neither generation nor one new byte. Astra's classifier pauses are seconds and its
# reasoning gaps are minutes, and both keep gen=True or the byte count moving — either of which
# resets the streak before it can reach 3 — so 15 idle minutes sits far outside a live round while
# costing ~1/6 of the 5400s actually lost.
STALE_STUB_CYCLES = 3


@contextlib.contextmanager
def _state_lock():
    """Advisory exclusive lock (fcntl.flock) around a read-modify-write of active.json, so 2-3
    concurrent submits/followups never interleave and truncate or lose each other's records.
    Degrades gracefully to a no-op if fcntl is unavailable (non-POSIX) — best-effort, not a hard
    requirement for correctness of a single writer."""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(STATE_LOCK_PATH), exist_ok=True)
    lf = open(STATE_LOCK_PATH, "a+")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


def _read_state():
    """Tolerant read: missing file, corrupt JSON, or an OLD (pre-registry) shape all degrade to
    an empty registry rather than crashing — a stale active.json on disk must never break a
    consult. A dict without a "jobs" key (old single-active shape, or garbage) is treated as
    having no jobs; it gets overwritten wholesale on the next write."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {"jobs": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), dict):
        return {"jobs": {}}  # old shape ({"conversation":...,"recent":[...]}) or corrupt — ignore
    return raw


_RECENT_WINDOW_S = 2 * 3600  # a consult counts as "active" for ambiguity for this long


def _active_jobs(state):
    """VIEW over state["jobs"]: (rid, job) pairs within the ambiguity window, newest first."""
    cutoff = time.time() - _RECENT_WINDOW_S
    jobs = [(rid, j) for rid, j in state.get("jobs", {}).items()
            if isinstance(j, dict) and j.get("ts", 0) >= cutoff and j.get("conversation_id")]
    jobs.sort(key=lambda kv: kv[1].get("ts", 0), reverse=True)
    return jobs


def _write_state(*, conversation=None, rid=None, status=None, title=None):
    """Read-modify-write active.json under an advisory exclusive lock (see _state_lock), so 2-3
    concurrent submits/followups never race each other's read-modify-write. The write itself is
    atomic: a temp file in the same directory is written then os.replace()'d over the target, so
    a concurrent reader never observes a truncated/partial file even without the lock.

    Upserts ONE job keyed by `rid`. If `rid` is None (the wait-done conversation-refresh call),
    the job matching `conversation` is refreshed in place instead (keeps its original rid) — this
    is the per-rid equivalent of the old "wait-done refreshes a conv without a rid" behavior."""
    if not conversation:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with _state_lock():
            cur = _read_state()
            jobs = cur.setdefault("jobs", {})
            key = rid
            if key is None:
                # find the existing job for this conversation to refresh (preserve its rid)
                key = next((r for r, j in jobs.items()
                           if isinstance(j, dict) and j.get("conversation_id") == conversation), None)
            if key is None:
                return  # nothing to key this job by — no-op rather than inventing a rid
            job = dict(jobs.get(key) or {})
            job["conversation_id"] = conversation
            job["ts"] = time.time()
            if status is not None:
                job["status"] = status
            elif "status" not in job:
                job["status"] = "submitted"
            if title is not None:
                job["title"] = title
            elif "title" not in job:
                job["title"] = None
            jobs[key] = job
            tmp = STATE_PATH + f".tmp-{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f)
            os.replace(tmp, STATE_PATH)
    except OSError:
        sys.stderr.write(f"CGC_WARNING state_not_written {STATE_PATH}\n")


def _resolve_conv(conv):
    """Explicit --conversation always wins. `auto`/None resolves to the active thread, but ONLY
    when it is unambiguous: if 2+ consults have been active within the window, `auto` would
    silently target the most RECENT one (so an earlier consult's follow-up lands on the wrong
    thread) — refuse and make the caller pin the one it means."""
    if conv and conv != "auto":
        # Accept the ORIGINAL consult's rid as a handle too — jobs are keyed by rid, and a rid
        # (REQ-…) never collides with a conversation uuid, so an agent can pin a specific consult
        # by either its conv id or the rid it already has from submit / the answer file.
        job = _read_state().get("jobs", {}).get(conv)
        if isinstance(job, dict) and job.get("conversation_id"):
            return job["conversation_id"]
        return conv
    st = _read_state()
    distinct = _active_jobs(st)
    # de-dup by conversation_id (two rids could in principle point at the same conv, e.g. a
    # followup refresh) so ambiguity is measured in THREADS, not job records.
    seen, uniq = set(), []
    for rid, j in distinct:
        cid = j["conversation_id"]
        if cid not in seen:
            seen.add(cid); uniq.append((rid, j))
    if len(uniq) > 1:
        lines = "\n".join(f"    --conversation {j['conversation_id']}   (rid {rid or '?'})"
                          for rid, j in uniq)
        raise SystemExit(
            "CGC_ERROR ambiguous_followup: %d consults are active — `--conversation auto` would "
            "silently continue the most RECENT one, so an earlier consult's follow-up would land "
            "on the wrong thread. Pin the one you mean (the conversation_id submit printed):\n%s"
            % (len(uniq), lines))
    if uniq:
        return uniq[0][1]["conversation_id"]
    return None


# ---- automated Step-0 gate (no LLM) -----------------------------------------

def _ensure_chrome(port: int) -> None:
    """Self-heal Step 0 before a consult: start the debug Chrome if it's down and
    probe login — all deterministic, no LLM. Runs cdp_launch.sh in CGC_GATE mode.
    Aborts with a clear alert ONLY when the user must act (login needed) or Chrome
    can't start; otherwise returns and lets the caller proceed (submit's own
    composer/login checks catch anything the gate couldn't verify). This is what
    makes Step 0 fire automatically on every consult instead of being a manual step."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_launch.sh")
    if not os.path.exists(script):
        return
    try:
        r = subprocess.run(["bash", script], text=True, capture_output=True, timeout=45,
                           env={**os.environ, "CGC_GATE": "1", "CGC_PORT": str(port)})
    except Exception as e:
        sys.stderr.write(f"CGC_WARN gate_skipped: {e}\n")
        return
    if r.returncode == 2:
        sys.stderr.write((r.stderr or "CGC_LOGIN needed").strip() + "\n")
        raise SystemExit("CGC_ERROR login_needed: ask the user to log into ChatGPT Pro in the "
                         "debug Chrome window that is open, then retry the consult.")
    if r.returncode == 1:
        sys.stderr.write((r.stderr or "CGC_ERROR chrome").strip() + "\n")
        raise SystemExit("CGC_ERROR chrome_unavailable: the dedicated debug Chrome could not be "
                         "started — check the Chrome path / port and retry.")
    # returncode 0 (ready, possibly login-unverified) → proceed


# ---- minimal CDP client -----------------------------------------------------

class CDP:
    def __init__(self, port: int, timeout: float = 10.0, match=None, create_url=None):
        """Attach to a ChatGPT page target.
        - create_url: open a NEW tab at this URL and attach to it (isolates a consult
          so concurrent consults never clobber each other's conversation).
        - match: attach only to the page whose parsed URL pathname is /c/<id> (or
          /c/<id>/...) for this conversation id — matched on pathname + hostname, never a
          full-URL substring — so it pins wait/status to the exact conversation.
        - neither: attach to the single ChatGPT page; ERROR if there are several
          (ambiguous — caller must pin with a conversation id).
        """
        self.port = port
        self._id = 0
        base = f"http://127.0.0.1:{port}"
        target = None
        if create_url:
            ver = json.load(urllib.request.urlopen(f"{base}/json/version", timeout=5))
            bw = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=timeout)
            # background:true — create the consult tab WITHOUT activating it, so a consult never
            # yanks Chrome (or the whole desktop) to the foreground and interrupts the user's work.
            # The waiter drives and reads the tab over CDP, which does not need it focused.
            bw.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                "params": {"url": create_url, "background": True}}))
            tid = None
            for _ in range(50):
                m = json.loads(bw.recv())
                if m.get("id") == 1:
                    tid = m.get("result", {}).get("targetId"); break
            bw.close()
            if not tid:
                raise SystemExit("CGC_ERROR new_tab_failed")
            for _ in range(20):
                info = json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
                hit = [t for t in info if t.get("id") == tid and t.get("webSocketDebuggerUrl")]
                if hit:
                    target = hit[0]; break
                time.sleep(0.2)
            if not target:
                raise SystemExit("CGC_ERROR new_tab_no_ws")
        else:
            info = json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
            # Hostname-exact (not a substring like "chatgpt.com" anywhere in the URL) —
            # defense-in-depth so a page at e.g. /c/<id>?x=chatgpt.com is never attachable.
            def _is_chatgpt(u):
                h = (urllib.parse.urlparse(u or "").hostname or "").lower()
                return h == "chatgpt.com" or h.endswith(".chatgpt.com")
            pages = [t for t in info if t.get("type") == "page" and _is_chatgpt(t.get("url"))]
            if match:
                # Match the URL PATH ONLY (via urlparse), never the query/hash — a tab whose URL
                # merely contains the id in a query string or hash (e.g. ?ref=/c/<id> or #/c/<id>)
                # must NOT count as the same conversation. /c/<match> may sit at the ROOT
                # (/c/<id>) or inside a PROJECT (/g/g-p-<pid>/c/<id>), so match it as a path
                # SEGMENT anywhere in the pathname — still pathname-only, so a query spoof can't hit.
                def _is_conv_url(u):
                    path = urllib.parse.urlparse(u or "").path
                    return re.search(r"(?:^|/)c/" + re.escape(match) + r"(?:/|$)", path) is not None
                hit = [t for t in pages if _is_conv_url(t.get("url"))]
                if not hit:
                    raise SystemExit(f"CGC_ERROR conversation_not_found: no ChatGPT tab whose URL "
                                     f"path is /c/{match} — the tab may have been closed/navigated")
                target = hit[0]
            else:
                if not pages:
                    pages = [t for t in info if t.get("type") == "page"]
                if not pages:
                    raise SystemExit("CGC_ERROR no_page_target: open the ChatGPT tab in the debug profile")
                if len(pages) > 1:
                    raise SystemExit("CGC_ERROR ambiguous_target: %d ChatGPT tabs open — pass "
                                     "--conversation <id> to pin the right one" % len(pages))
                target = pages[0]
        self.target_id = target.get("id")
        self.target_url = target.get("url", "")
        # Attaching can time out transiently — a tab that is mid-navigation, or a Chrome busy
        # rendering a long conversation, simply does not answer within the window. That is a retry,
        # not a failure. Letting it escape produced a 30-line websocket traceback in the job log,
        # which tells the caller nothing it can act on and looks like a crash rather than a blip.
        last = None
        created_tid = tid if create_url else None
        for attempt in range(3):
            try:
                self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=timeout)
                self.call("Runtime.enable")
                self.call("Page.enable")
                break
            except Exception as e:
                last = e
                try:
                    if getattr(self, "ws", None):
                        self.ws.close()
                except Exception:
                    pass
                self.ws = None
                if attempt < 2:
                    time.sleep(2.0)
        else:
            # Close the tab we opened. Every failed attach used to leak one, and they pile up as
            # blank targets in a browser that is already unwell.
            if created_tid:
                try:
                    _b = websocket.create_connection(
                        json.load(urllib.request.urlopen(f"{base}/json/version", timeout=5))
                        ["webSocketDebuggerUrl"], timeout=5)
                    _b.send(json.dumps({"id": 99, "method": "Target.closeTarget",
                                        "params": {"targetId": created_tid}}))
                    _b.close()
                except Exception:
                    pass
            raise SystemExit(
                f"CGC_ERROR cdp_attach_failed: opened a tab on port {port} but it never answered "
                f"Runtime.enable in 3 tries ({type(last).__name__}: {last}). Measured cause: this "
                f"Chrome can still serve its EXISTING tabs while every NEWLY created tab stays "
                f"unresponsive — an about:blank tab reproduces it — so the browser needs "
                f"restarting, not the job retrying. Nothing was sent.")

    def conversation_id(self):
        """The /c/<id> conversation id of the attached tab, or '' if not in a conversation yet.
        Reads location.PATHNAME only — a /c/<id> that appears in the query string or hash of
        some other page must never be mistaken for the attached conversation."""
        path = self.eval("location.pathname") or ""
        # /c/<id> can sit at the ROOT (/c/<id>) OR inside a PROJECT (/g/g-p-<pid>/c/<id>) —
        # match it as a path SEGMENT anywhere in the PATHNAME (never the query/hash, so a
        # spoofed ?x=/c/<id> can't be mistaken for the attached conversation).
        m = re.search(r"(?:^|/)c/([0-9a-f-]+)(?:/|$)", path)
        return m.group(1) if m else ""

    def call(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline_s = timeout or 30
        deadline = time.time() + deadline_s
        # The socket's own recv() timeout was fixed at connect time (__init__'s `timeout`, default
        # 10s) and does not track THIS call's requested deadline — the field incident: a 45s
        # composer eval (_clear_composer_js/_paste_chunk_js/_composer_text_js) died with a raw
        # WebSocketTimeoutException around the 10s mark, long before the Python-level loop below
        # would have given up. Make the requested timeout authoritative by stretching the socket
        # timeout to cover it, and restore the prior value in `finally` so an exception here can
        # never leave a later call running on a silently widened socket.
        prev_timeout = self.ws.gettimeout()
        if prev_timeout is not None and prev_timeout < deadline_s:
            self.ws.settimeout(deadline_s)
        try:
            while time.time() < deadline:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"CDP {method} error: {msg['error']}")
                    return msg.get("result", {})
            raise RuntimeError(f"CDP {method} timeout")
        finally:
            if prev_timeout is not None and prev_timeout < deadline_s:
                self.ws.settimeout(prev_timeout)

    def eval(self, expr, timeout=None):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        return r.get("result", {}).get("value")

    def key(self, key_name, code, keycode):
        for t in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent",
                      {"type": t, "key": key_name, "code": code,
                       "windowsVirtualKeyCode": keycode, "nativeVirtualKeyCode": keycode})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def close_tab(self):
        """Close the attached browser tab (Target.closeTarget via the browser endpoint).
        Used to clean up a consult's dedicated tab after its answer is retrieved, so
        concurrent consults stay isolated without accumulating tabs."""
        tid = getattr(self, "target_id", None)
        if not tid:
            return
        try:
            ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5))
            bw = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=5)
            bw.send(json.dumps({"id": 1, "method": "Target.closeTarget", "params": {"targetId": tid}}))
            bw.recv()
            bw.close()
        except Exception:
            pass


# ---- page logic (textContent-based, layout-INDEPENDENT) ---------------------
# CRITICAL: read the answer via textContent + a DOM walk, NOT innerText. innerText depends on
# layout/rendering, which Chrome throttles for a BACKGROUND tab — and the detached waiter polls
# while the user's foreground tab is elsewhere, so innerText there collapses to ~empty: the
# answer reads as "1 char", `done` never fires, and wait only returns on timeout. textContent
# and childNodes are populated regardless of tab visibility. (_last_user_text_js already used
# textContent, which is exactly why rid-resolution worked while answer-extraction silently
# failed on the same backgrounded tab.)

# Pick the assistant node that actually CONTAINS our BEGIN sentinel (by textContent), not blindly
# the last node — a trailing empty/streaming assistant node would otherwise read as ~1 char.
# ChatGPT's turn markup changed shape. Older builds tag each MESSAGE node
# data-message-author-role="assistant"|"user"; the current build tags the TURN
# data-turn="assistant"|"user" and leaves data-message-author-role on the user node only.
# Selecting only the old attribute made querySelectorAll return ZERO assistant nodes, so the
# sentinel could never be found and EVERY consult ran its full 25-minute budget before reporting
# "no answer" with ac=0 — the tool looked alive and produced nothing, twice, before this was found.
# Match either shape. This is the single fact about ChatGPT's DOM that the whole file rests on, so
# it is named once here instead of being retyped inside six JS string literals where a future
# rename would again have to be found six times.
_SEL_A = '[data-message-author-role="assistant"],[data-turn="assistant"]'
_SEL_U = '[data-message-author-role="user"],[data-turn="user"]'
_SEL_ANY = '[data-message-author-role],[data-turn]'
_JS_A, _JS_U, _JS_ANY = json.dumps(_SEL_A), json.dumps(_SEL_U), json.dumps(_SEL_ANY)

# Whichever attribute this build uses, the role reads the same way.
_ROLE_FN = ("function __cgcRole(el){return el.getAttribute('data-message-author-role')||"
            "el.getAttribute('data-turn')||'';}")

_NODE_FN = (
    _ROLE_FN +
    # __cgcNode(BG, turnIndex): turnIndex is OPTIONAL. Omitted/undefined/negative -> UNSCOPED,
    # the legacy 'auto' behavior (step 1 searches the whole document; step 2 falls back to only
    # AFTER the last user node). Given (0-based ordinal of a user turn, DOM order, among
    # querySelectorAll(_JS_ANY)'s user-role matches) -> SCOPED to the interval strictly between
    # that turn and the NEXT user turn (or end of conversation): this is the [S3] fix — a
    # source-pinned retrieve's answer may come ONLY from assistant nodes inside its own turn's
    # interval, never from a later round's nodes, even when they happen to contain the same BEGIN
    # token (e.g. the model echoing/quoting it). Returns null when turnIndex is scoped but that
    # ordinal's user node is not currently in the DOM (virtualized away) — the caller must fail
    # closed rather than search outside the (unresolvable) interval.
    "function __cgcNode(BG,turnIndex){"
    "var nx=document.querySelectorAll(" + _JS_ANY + ");"
    "var scoped=(turnIndex!==undefined&&turnIndex!==null&&turnIndex>=0);"
    "var lo=-1,hi=nx.length;"
    "if(scoped){"
    "var seen=-1;lo=-1;hi=nx.length;"
    "for(var j=0;j<nx.length;j++){if(__cgcRole(nx[j])==='user'){seen++;"
    "if(seen===turnIndex){lo=j;}else if(seen===turnIndex+1){hi=j;break;}}}"
    "if(lo<0)return null;"
    "}"
    # 1) exact: any assistant node containing our (unique) BEGIN sentinel, newest-first. Scoped ->
    #    within [lo,hi) only. Unscoped -> the whole document (lo=-1,hi=nx.length), matching the
    #    original global search.
    "for(var i=hi-1;i>lo;i--){if(__cgcRole(nx[i])==='assistant'&&(nx[i].textContent||'').indexOf(BG)>=0)return nx[i];}"
    # 2) fallback (no sentinel yet): the LARGEST assistant node in the interval. Scoped -> the same
    #    [lo,hi). Unscoped -> only AFTER the last user node (NOT a[last], which is often a 1-char
    #    trailing streaming placeholder; NOT a global max, which would read a PRIOR round's answer).
    "var flo=lo,fhi=hi;"
    "if(!scoped){flo=-1;for(var m=0;m<nx.length;m++){if(__cgcRole(nx[m])==='user')flo=m;}}"
    "var best=null,bl=-1;"
    "for(var k=flo+1;k<fhi;k++){if(__cgcRole(nx[k])==='assistant'){"
    "var L=(nx[k].textContent||'').length;if(L>bl){bl=L;best=nx[k];}}}"
    "return best;}"
)

# Layout-independent innerText approximation: a textContent walk that re-inserts newlines at block
# boundaries, so the extracted answer keeps its line structure WITHOUT needing the tab rendered.
_TEXT_FN = ("function __cgcText(el){if(!el)return '';"
            "var BLOCK=/^(P|DIV|LI|UL|OL|H1|H2|H3|H4|H5|H6|PRE|BLOCKQUOTE|TABLE|TR|THEAD|TBODY|SECTION|ARTICLE|HR)$/;"
            "var out='';(function w(n){for(var i=0;i<n.childNodes.length;i++){var c=n.childNodes[i];"
            "if(c.nodeType===3){out+=c.nodeValue;}else if(c.nodeType===1){"
            "if(c.tagName==='BR'){out+='\\n';continue;}var b=BLOCK.test(c.tagName);if(b)out+='\\n';w(c);if(b)out+='\\n';}}})(el);"
            "return out.replace(/[ \\t]+\\n/g,'\\n').replace(/\\n{3,}/g,'\\n\\n');}")


# ---- SHARED CONTRACT #1: fence-aware line-anchored sentinel parser (v3) -----
# The ONLY completion/extraction rule, in Python AND in the JS this file builds. A line merely
# CONTAINING the sentinel text (quoted in prose, inside a fenced code block, etc.) must NOT match —
# only a bare standalone line equal to the sentinel after trim() matches. v2 additionally ignores a
# bare sentinel line that sits INSIDE a ``` / ~~~ fenced code block (v1 only ignored non-bare
# occurrences; a bare sentinel-lookalike inside a fence used to false-match). v3 requires the END to
# be the LAST NON-BLANK line, not merely the first END after BEGIN: because consults deliberately
# point the model at PUBLIC repos it did not author, a repo can prompt-inject an early bare
# END_RESPONSE:<rid> to truncate the answer, and taking the first END would honour it. Taking the
# last non-blank line means an early injected END is ignored while real answer text still follows.
# This is what lets the model safely quote BEGIN_RESPONSE:/END_RESPONSE: tokens — even as a bare line
# inside its own fenced code — without those being mistaken for the real wrapper. Canonical Python
# implementation below; the JS builder (_sentinel_js) mirrors it exactly so detect/extract/status/
# timeout-rescue never drift apart.

def _sentinel_parse(text: str, rid: str):
    """Fence-aware, line-anchored sentinel parser (SHARED CONTRACT #1, v2).
    Scans lines tracking a boolean in_fence: a line whose trimmed value STARTS WITH ``` or ~~~ is a
    fence toggle — it flips in_fence and is never itself treated as a sentinel line.
    begin = index of the FIRST line that is NOT in_fence AND whose trimmed value == 'BEGIN_RESPONSE:<rid>'
    end   = the LAST NON-BLANK line, which must itself be a bare unfenced 'END_RESPONSE:<rid>'. Taking
            the last non-blank line (NOT the first END after begin) is a security property: a browsed
            PUBLIC repo can prompt-inject the model into emitting an early bare END_RESPONSE:<rid>
            mid-answer to truncate it. An early END is not the terminal line while real answer text
            still follows, so it is ignored; only a genuine terminal END closes extraction. Trailing
            content after the END therefore means NOT done (still streaming, or injected) and the
            waiter keeps polling.
    done  = begin found AND the last non-blank line is that bare unfenced END AND the body is non-empty.
    Returns (done: bool, body: str) — body is '' when not done. The body itself MAY contain fenced
    sentinel-lookalikes, and even a bare early END line as literal text; only the terminal boundary
    must be bare AND outside any fence.
    """
    norm = (text or "").replace("\r\n", "\n")
    lines = norm.split("\n")
    begin_tok = f"BEGIN_RESPONSE:{rid}"
    end_tok = f"END_RESPONSE:{rid}"

    def _is_fence_toggle(line: str) -> bool:
        s = line.strip()
        return s.startswith("```") or s.startswith("~~~")

    in_fence = False
    i = None
    for idx, line in enumerate(lines):
        if _is_fence_toggle(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.strip() == begin_tok:
            i = idx
            break
    if i is None:
        return False, ""
    # END must be the LAST NON-BLANK line, bare and unfenced (see docstring: the anti-injection
    # property — an early bare END is ignored while real answer text still follows it).
    last = None
    for idx in range(len(lines) - 1, i, -1):
        if lines[idx].strip() != "":
            last = idx
            break
    if last is None or lines[last].strip() != end_tok:
        return False, ""
    in_fence = False
    for idx in range(i + 1, last):
        if _is_fence_toggle(lines[idx]):
            in_fence = not in_fence
    if in_fence:  # terminal END sits inside an unclosed fence → it is answer text, not a wrapper
        return False, ""
    body = "\n".join(lines[i + 1:last]).strip()
    if not body:
        return False, ""
    return True, body


def _sentinel_js(rid: str, text_expr: str) -> str:
    """JS mirror of _sentinel_parse (v2, fence-aware), operating on the LINE-STRUCTURED text produced
    by `text_expr` (a JS expression evaluating to a string — pass __cgcText(node) output, not raw
    .textContent, so block-element boundaries survive as line breaks and bare-line matching is
    meaningful). Returns {done, body} as a JS object literal — embed via `(function(){...return
    X;})()`. This is the ONE canonical fence-aware JS implementation; detect, extract, status and the
    timeout-rescue path all call it instead of re-deriving the rule."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    end = json.dumps(f"END_RESPONSE:{rid}")
    return (
        "(function(){"
        "var __t=((" + text_expr + ")||'').replace(/\\r\\n/g,'\\n');"
        "var __lines=__t.split('\\n');var __BG=" + begin + ",__EN=" + end + ";"
        "function __fence(l){var s=l.trim();return s.indexOf('```')===0||s.indexOf('~~~')===0;}"
        "var __inFence=false;var __i=-1;"
        "for(var __a=0;__a<__lines.length;__a++){"
        "if(__fence(__lines[__a])){__inFence=!__inFence;continue;}"
        "if(!__inFence&&__lines[__a].trim()===__BG){__i=__a;break;}}"
        "if(__i<0)return {done:false,body:''};"
        # END must be the LAST NON-BLANK line, bare and unfenced — mirrors _sentinel_parse's
        # anti-injection rule: an early bare END is ignored while real answer text still follows.
        "var __last=-1;"
        "for(var __c=__lines.length-1;__c>__i;__c--){"
        "if(__lines[__c].trim()!==''){__last=__c;break;}}"
        "if(__last<0||__lines[__last].trim()!==__EN)return {done:false,body:''};"
        "__inFence=false;"
        "for(var __b=__i+1;__b<__last;__b++){"
        "if(__fence(__lines[__b]))__inFence=!__inFence;}"
        "if(__inFence)return {done:false,body:''};"
        "var __body=__lines.slice(__i+1,__last).join('\\n').trim();"
        "if(!__body)return {done:false,body:''};"
        "return {done:true,body:__body};})()"
    )


def _detect_js(rid: str, turn_index=None) -> str:
    """Returns JSON {generating,done,blocker,len,begin,end,ac} for the answer node.
    done comes from the canonical bare-line _sentinel_js parser (SHARED CONTRACT #1) — a line
    merely CONTAINING BEGIN/END_RESPONSE:<rid> (quoted in prose or a code block) never counts,
    only a bare standalone sentinel line does. This is NOT stop-button driven: the model writes
    END_RESPONSE only as its final line, so its presence == complete + extractable. Depending on
    the stop-button was fragile — if that UI selector ever persists, done would never fire and
    wait would only return on timeout. begin/end/ac are diagnostics for the heartbeat.

    turn_index: when given (a source-pinned wait — see cmd_wait/_locate_source_turn), the answer
    node is looked up SCOPED to that turn's interval via __cgcNode's turnIndex ([S3] fix: an
    answer may only come from its own source turn's interval, never a later round's). Omitted
    (None) keeps the legacy unscoped 'auto' lookup."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    end = json.dumps(f"END_RESPONSE:{rid}")
    sentinel = _sentinel_js(rid, "__cgcText(node)")
    node_call = f"__cgcNode({begin})" if turn_index is None else f"__cgcNode({begin},{int(turn_index)})"
    return (
        "(function(){" + _NODE_FN + _TEXT_FN +
        "var a=document.querySelectorAll(" + _JS_A + ");"
        "var BG=" + begin + ",EN=" + end + ";"
        "var node=" + node_call + ";"
        "var rawT=((node?node.textContent:'')||'').replace(/\\r\\n/g,'\\n');"
        "var res=" + sentinel + ";"
        "var hasB=rawT.indexOf(BG)>=0,hasE=rawT.indexOf(EN)>=0;"
        "var stop=!!document.querySelector('[data-testid=\"stop-button\"],button[aria-label*=\"Stop\"],button[aria-label*=\"\\u505c\\u6b62\"]');"
        "var blocker=null;"
        "if(document.querySelector('input[type=\"password\"]')||/^\\/(auth|login)(\\/|$)/i.test(location.pathname))blocker='login';"
        "else if(document.querySelector('iframe[src*=\"captcha\" i],iframe[title*=\"captcha\" i],[id*=\"challenge\"]'))blocker='captcha';"
        "else{var al=document.querySelector('[role=\"alert\"]');if(al&&/rate limit|too many requests|usage limit/i.test(al.textContent||''))blocker='rate_limit';}"
        "var done=(res.done&&a.length>0);"
        "return JSON.stringify({generating:stop,done:done,blocker:blocker,len:rawT.length,begin:hasB,end:hasE,ac:a.length});})()"
    )


# ---- SHARED CONTRACT #3: canonical per-turn request-rid parser ([S3] fix) --------------------
# The rid regex shape (REQ-YYYYMMDD-HHMMSS-hhhhhh) shared by every rid-reading regex in this file.
_RID_RE_SRC = r"REQ-\d{8}-\d{6}-[0-9a-f]{6}"
_TURN_BEGIN_LINE_RE = re.compile(rf"^BEGIN_RESPONSE:({_RID_RE_SRC})$")
_TURN_END_LINE_RE = re.compile(rf"^END_RESPONSE:({_RID_RE_SRC})$")


def _turn_canonical_rid(turn_text):
    """The ONE canonical parser for a user turn's OWN request rid ([S3] audit fix). Returns the
    rid string, or None if the turn carries no complete pair.

    Template-order evidence (consult.py): PROMPT_TEMPLATE embeds caller-supplied task text early
    ('# Goal\\n{task}') and FOLLOWUP_TEMPLATE likewise ('# What I'm asking now\\n{task}'); BOTH
    templates then emit their own BEGIN_RESPONSE:{rid} / END_RESPONSE:{rid} pair together, as the
    LAST thing in the render ('# Final output format', the fixed closing block). So any
    sentinel-shaped text a caller's task happens to quote or discuss — in prose, inside a code
    fence, or even as a fully quoted BEGIN+END pair for some OTHER rid — necessarily sits BEFORE
    the template's genuine pair. The canonical rid is therefore the rid of the LAST complete,
    in-order BEGIN_RESPONSE:<rid>/END_RESPONSE:<rid> pair in the turn text (same rid on both
    lines, BEGIN before END): a lone BEGIN with no matching END never wins, and an earlier pair
    never beats a later one — exactly the [S3] fixture (an R2 turn that quotes 'BEGIN_RESPONSE:R1'
    before its own footer must still resolve to R2, never R1).

    Matched as whole (trimmed) lines — 'bare line' here means line-anchored after trim() on the
    text split by '\\n', the same convention _sentinel_parse uses for the assistant-side wrapper
    (DOM textContent flattens markdown, so this is the only structure available to match on;
    fence-awareness is unnecessary here because picking the LAST pair already defeats an earlier
    fenced/prose quote regardless of whether it's inside a fence).
    """
    canonical = None
    open_begin = set()
    for line in (turn_text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        mb = _TURN_BEGIN_LINE_RE.match(s)
        if mb:
            open_begin.add(mb.group(1))
            continue
        me = _TURN_END_LINE_RE.match(s)
        if me and me.group(1) in open_begin:
            canonical = me.group(1)  # scanning in order: the last completed pair wins
    return canonical


def _last_user_text_js() -> str:
    """Block-aware text of the LAST user turn only (__cgcText, NOT raw textContent: the composer
    pastes the prompt as one <p> per line, and raw textContent concatenates those paragraphs with
    NO newlines — the line-anchored canonical parser then sees one giant line and finds nothing;
    observed live: a landed send classified possibly_accepted because its own echo was unparseable).
    DOM extraction stays JS's only job here — parsing the rid out of it is Python's (via
    _turn_canonical_rid, pure and unit-tested)."""
    return ("(function(){" + _TEXT_FN + "var u=document.querySelectorAll(" + _JS_U + ");"
            "var n=u[u.length-1];return n?__cgcText(n):'';})()")


def _resolve_rid(c, rid):
    """Resolve + VERIFY the rid against the attached conversation, via the canonical per-turn
    parser (SHARED CONTRACT #3) — never a first-regex-match/substring read.
    - rid=='auto': read it from the page's last user message.
    - explicit rid: confirm the page actually holds THAT request — if the page's rid
      differs, the wrong tab/conversation is attached → fail loudly (prevents one
      request from receiving another's answer)."""
    page_rid = _turn_canonical_rid(c.eval(_last_user_text_js()) or "") or ""
    if rid == "auto":
        if not page_rid:
            raise SystemExit("CGC_ERROR rid_autodetect_failed: no BEGIN_RESPONSE:<rid> in the last "
                             "user message — wrong/empty conversation attached?")
        sys.stderr.write(f"CGC_RID resolved {page_rid}\n")
        return page_rid
    if page_rid and page_rid != rid:
        raise SystemExit(f"CGC_ERROR rid_mismatch: attached conversation is for '{page_rid}', "
                         f"not the expected '{rid}' — wrong tab. Pin with --conversation <id>.")
    return rid


def _all_user_texts_js() -> str:
    """Block-aware text (__cgcText — see _last_user_text_js for why raw textContent breaks the
    line-anchored parser) of EVERY user turn, DOM order — unlike _last_user_text_js (last turn
    only), this lets a retrieve locate a SOURCE rid's own turn anywhere in the conversation, even
    when later turns were sent after it."""
    return ("(function(){" + _TEXT_FN + "var u=document.querySelectorAll(" + _JS_U + ");"
            "return Array.prototype.map.call(u, function(n){return __cgcText(n);});})()")


def _locate_source_turn(user_texts, source_rid):
    """Pure, CDP-free turn-matching helper (unit-tested without a browser — see tests/
    test_store_backend.py): given every user turn's textContent in DOM order and the rid a retrieve
    is recovering, find whether/where its own turn lives — using _turn_canonical_rid (SHARED
    CONTRACT #3), never substring containment of 'BEGIN_RESPONSE:<rid>'.

    [S3] fix: substring containment used to let a LATER turn that merely quotes/discusses an OLDER
    rid (in prose, a code fence, or even a fully quoted BEGIN+END pair) be mistaken for that rid's
    own turn — and, if it also looked 'latest', for the SOURCE turn's identity, which then let
    unwrapped salvage adopt the later turn's answer under the earlier rid's name. Matching on each
    turn's OWN canonical rid closes this: only the turn whose LAST complete sentinel pair equals
    source_rid can ever match.

    present=False: the source turn is nowhere in the conversation -> rid_absent, refuse outright.
    present=True, is_last=True: no user turn was sent after it -> the conversation has not advanced
      past it, so the existing unwrapped-salvage fallback (largest assistant node in this turn's
      interval, no sentinel) is as safe here as it is for a normal submit/followup wait.
    present=True, is_last=False: at least one later turn exists -> only a sentinel-WRAPPED answer
      (its own END_RESPONSE:<rid> anchor, extracted via the rid-and-interval-scoped
      _detect_js/_extract_js) can still be safely attributed to the source turn; unwrapped salvage
      must be refused (rid_superseded/ambiguous) since a later turn's own interval now exists.

    turn_index: the 0-based ordinal (DOM order) of the located turn among ALL user turns, or None
    when absent. The caller threads this into __cgcNode's turnIndex so assistant-answer selection
    is scoped to the exact interval between this turn and the next user turn — an answer may never
    come from outside it (see _detect_js/_extract_js/_last_assistant_js).

    observed_rid is the CANONICAL rid of the LAST user turn, if any — 'what the conversation is
    actually on now', surfaced in error text and the outcome envelope so a refusal is legible, not
    just silent.
    """
    idx = None
    for i, t in enumerate(user_texts):
        if _turn_canonical_rid(t) == source_rid:
            idx = i  # keep the LAST turn whose OWN canonical rid is source_rid (a legitimate resend)
    observed = _turn_canonical_rid(user_texts[-1]) if user_texts else None
    if idx is None:
        return {"present": False, "is_last": False, "observed_rid": observed, "turn_index": None}
    return {"present": True, "is_last": idx == len(user_texts) - 1,
            "observed_rid": observed, "turn_index": idx}


def _salvage_allowed(c, rid):
    """Re-check, AT THE MOMENT unwrapped salvage is considered (not just once at wait-start),
    whether rid's own turn is still the conversation's LAST — a later turn can land WHILE the wait
    is in progress, which is exactly the race a retrieve recovery must close. Returns
    (allowed: bool, observed_rid, turn_index, present). Cheap (one DOM query), so re-checking on
    every salvage attempt costs nothing against a poll loop measured in seconds. turn_index/present
    are the fresh (this-instant) values from _locate_source_turn — the caller should use THIS
    turn_index (not a cached one) for the salvage extraction that immediately follows, since the
    DOM composition may have shifted since the wait started."""
    loc = _locate_source_turn(c.eval(_all_user_texts_js()) or [], rid)
    return loc["is_last"] and loc["present"], loc["observed_rid"], loc["turn_index"], loc["present"]


def _last_assistant_js(rid: str, turn_index=None) -> str:
    """Reconstructed text of the answer node (no sentinel slicing) — for the stall raw dump.
    turn_index: see _detect_js — scopes the lookup to the source turn's interval when given."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    node_call = f"__cgcNode({begin})" if turn_index is None else f"__cgcNode({begin},{int(turn_index)})"
    return ("(function(){" + _NODE_FN + _TEXT_FN +
            "return __cgcText(" + node_call + ");})()")


def _extract_js(rid: str, turn_index=None) -> str:
    """Returns the answer text BETWEEN the bare-line sentinels of the answer node (SHARED
    CONTRACT #1, via the canonical _sentinel_js parser), or '' if no valid wrapper is present.
    turn_index: see _detect_js — scopes the lookup to the source turn's interval when given."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    node_call = f"__cgcNode({begin})" if turn_index is None else f"__cgcNode({begin},{int(turn_index)})"
    sentinel = _sentinel_js(rid, f"__cgcText({node_call})")
    return (
        "(function(){" + _NODE_FN + _TEXT_FN +
        "var res=" + sentinel + ";"
        "return res.done?res.body:'';})()"
    )


# ---- SHARED CONTRACT #4: producer attribution (which model served THIS answer) ---------------
# THE THIRD PROPERTY. Selecting a model in the composer and being served by that model are two
# different facts, and this file could only ever read the first one. `modelBadge` is a PRE-SEND
# read of the switcher: it is evidence of what was REQUESTED. It cannot attest to what answered —
# OpenAI's release notes document a Thinking rate-limit fallback onto a model that is not even a
# picker option, so a correct badge and a different serving model coexist by design.
#
# A completed assistant turn carries the provider's own answer: data-message-model-slug. Measured
# live on 2026-09-06, three conversations on the same build, same page load:
#   consult 6a9d16b7-bb9c-83ea-8665-400615e369bd -> "gpt-6-pro"        (32254-char node)
#   an earlier consult                           -> "gpt-5-6-pro"
#   a hand-typed chat                            -> "gpt-5-6-thinking"
# So it is per-conversation, not a build-wide constant, and it survives a reload.
#
# ABSENT is a real, expected third outcome (reported as null), never an error and never a
# substitute for the badge. Measured on that same thread while a consult was mid-flight: three
# assistant TURN nodes existed, and exactly ONE carried the attribute — the finished 32155-char
# answer. The still-streaming turn had none. So the attribute arrives with the completed message
# node, not with the turn wrapper, and reading it early legitimately yields nothing.
#
# Read off the SAME node __cgcNode already resolved for this rid, never "the last assistant
# message". A stale tab under-reports how many assistant turns exist, so a last-node read can
# attribute a different turn's producer to this answer — and on that live thread two user turns
# carried the SAME rid (a resend), with only the second one's interval holding the answer. The
# rid-and-interval scoping picked it; an unscoped "latest" read would have been a coin flip.
# The attribute lives on the MESSAGE node while __cgcNode may legitimately resolve the TURN
# wrapper (_SEL_A matches either shape) — hence self -> descendant -> ancestor, all three confined
# to that one resolved node.
def _model_slug_js(rid: str, turn_index=None) -> str:
    """JS returning the producing model's slug for the rid's OWN answer node, or '' when the
    provider has not written one (a legitimate outcome — see SHARED CONTRACT #4)."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    node_call = f"__cgcNode({begin})" if turn_index is None else f"__cgcNode({begin},{int(turn_index)})"
    return ("(function(){" + _NODE_FN + "var A='data-message-model-slug';"
            "var n=" + node_call + ";if(!n)return '';"
            "var v=n.getAttribute(A);if(v)return v;"
            "var d=n.querySelector('['+A+']');if(d){v=d.getAttribute(A);if(v)return v;}"
            "var p=n.closest?n.closest('['+A+']'):null;"
            "return (p&&p.getAttribute(A))||'';})()")


def _read_attribution(c, rid, turn_index=None):
    """The producing model's slug for this rid's answer, or None when absent/unreadable. Never
    raises: attribution is evidence ABOUT an answer, so failing to read it must not fail a consult
    that otherwise succeeded — it degrades to 'unknown', reported honestly as null."""
    try:
        return (c.eval(_model_slug_js(rid, turn_index)) or "").strip() or None
    except Exception:
        return None


def _wait_receipt(rid, out, slug):
    """The ONE stdout line `wait` prints, mirroring what `submit` already does: the daemon parses
    it for attribution. modelSlug is the provider's own producer attribution (SHARED CONTRACT #4);
    null means the attribute was absent, which is NOT a failure and NOT a mismatch."""
    print(json.dumps({"rid": rid, "out": out, "modelSlug": slug}))


def _reload_stale_tab(c, reason):
    """Reload the attached conversation ONCE because its DOM has stopped tracking the server-side
    thread. `reason` is the caller's diagnosis — two independent symptoms reach the same tab
    (the round's own user turn never rendering, and a frozen post-generation stub), and they share
    this one mechanism and, in cmd_wait, one budget.

    READ-SIDE ONLY, and structurally so: Page.reload re-fetches the conversation the tab is already
    on. It does not touch the composer, does not paste, and cannot send — the at-most-once send
    invariant is untouched, because nothing here can produce a send at all. The caller owns the
    at-most-once budget for the reload itself, so a genuinely absent turn still reaches rid_absent
    or the normal timeout instead of reloading in a loop."""
    sys.stderr.write(
        f"CGC_WAIT stale-tab: {reason} — reloading the conversation once (read-only; nothing is "
        "re-sent) before committing to the rest of the watch.\n")
    try:
        c.call("Page.reload", {"ignoreCache": False})
    except Exception as e:
        sys.stderr.write(f"CGC_WAIT stale-tab: reload failed ({e}); continuing on the current DOM\n")
        return
    time.sleep(STALE_RELOAD_SETTLE_S)


def _scoped_raw(c, rid, strict, turn_index):
    """Fetch unwrapped-salvage raw text via _last_assistant_js, honoring the [S3] interval
    guarantee. STRICT (source-pinned) mode: the answer may come ONLY from the source turn's own
    interval — if turn_index is None (that turn is no longer resolvable in the current DOM, e.g.
    virtualized away), refuse outright rather than falling back to an unscoped 'current turn'
    search that could read a LATER round's answer. Non-strict ('auto') mode keeps the legacy
    unscoped lookup, since there is no pinned source turn to violate."""
    if strict:
        if turn_index is None:
            return ""
        return c.eval(_last_assistant_js(rid, turn_index)) or ""
    return c.eval(_last_assistant_js(rid)) or ""


# ChatGPT VIRTUALIZES message nodes on a backgrounded/inactive tab: the completed answer node
# can be absent from the DOM (its textContent unreadable) while the tab is in the background, so
# a passive CDP read sees only short thinking/streaming stubs, `done` never fires, and the waiter
# times out on an answer that is actually PRESENT (the "no answer" exit-4 on a finished consult).
# Scrolling the list to the bottom fires the virtualizer's scroll handler synchronously, which
# renders+commits the final node so textContent becomes readable — no foregrounding needed.
_FORCE_RENDER_JS = ("(function(){try{var sc=document.querySelector('main')||document.scrollingElement;"
                    "if(sc)sc.scrollTop=sc.scrollHeight;"
                    "var a=document.querySelectorAll(" + _JS_A + ");"
                    "if(a.length)a[a.length-1].scrollIntoView(false);}catch(e){}return 1;})()")


# ---- subcommands ------------------------------------------------------------

def cmd_status(a) -> int:
    # A self-check AFTER the waiter retrieved + auto-closed the tab must NOT look like a
    # failure: if --out holds a non-empty answer, report retrieved/done even though the
    # tab (and thus the live conversation) is gone.
    out_chars = 0
    if a.out:
        try:
            with open(a.out, encoding="utf-8") as f:
                out_chars = len(f.read().strip())
        except OSError:
            out_chars = 0
    try:
        c = CDP(a.port, match=_resolve_conv(a.conversation))
    except SystemExit:
        if out_chars > 0:
            print(json.dumps({"done": True, "retrieved": True, "out": a.out,
                              "len": out_chars, "note": "tab closed; answer already retrieved"}))
            return 0
        raise
    try:
        rid = _resolve_rid(c, a.rid)
        print(c.eval(_detect_js(rid)))
    finally:
        c.close()
    return 0


# ---- model selection: TWO dimensions now, both pinned, both fail-closed ------
# THE LAW CHANGED ON 2026-09-03. This block used to say "Model must never be touched (it picks
# the model family, not the tier)". That was correct only while the composer offered no way to
# pick the model at all — the single selectable thing was the reasoning tier, so touching
# anything model-shaped could only be a mistake. OpenAI shipped GPT-6 ("GPT-6 Astra") on
# 2026-09-03 and the picker now carries BOTH dimensions in ONE menu. Under the old law the
# family would sit unguarded: "Pro tier on GPT-5.5" satisfies every tier check this file makes
# while being a DIFFERENT MODEL than the receipt claims answered. A consult that silently runs
# weaker than it says is precisely the failure this whole guard exists to prevent, so the family
# is pinned too now — CGC_MODEL_FAMILY, default "Latest" — with the tier's own discipline: fail
# closed, never send on an unconfirmed model.
#
# Live readings from a logged-in Pro tab, 2026-09-06 (verbatim, this is the build being fixed):
#   _cand_labels_js()                                  -> ["6"]   (the switcher's label is now
#                                                                  the MODEL BADGE, not a tier)
#   that same button's innerText while its menu is OPEN -> "Thinking effort"
#   [data-testid="composer-intelligence-picker-content"] innerText lines:
#     0 "6"  1 "Pro"  2 "Pro, 5 of 5."  3 "Use Left and Right arrow keys to adjust power."
#     4 "Latest" (menuitemradio, aria-checked=true)  5 "GPT-5.6 Sol"  6 "GPT-5.5"
#   span[role=slider]: aria-valuemin=0 aria-valuemax=4 aria-valuenow=4, textContent "", NO
#     aria-valuetext. Its [role=menuitem] ancestor is aria-label="Power" with EMPTY innerText
#     AND textContent, carrying aria-describedby="_r_cn_ _r_co_" whose first id reads
#     "Pro, 5 of 5." and whose second reads the arrow-keys instruction.
#   _submenu_count_js()                                -> 0       (no haspopup trigger survives)
#
# WHY THE TIER IS NOW READ BY IDENTITY, NEVER BY LINE POSITION: the picker group's line 0 used
# to BE the tier announcement ("Pro, 5 of 5.") and is now the model badge ("6"). Reading line 0
# is exactly what broke every consult on this build — _slider_label("6") == "6",
# _matches("6", "Pro") is false, so the walk matched at no slider position, the flat-click and
# submenu fallbacks found nothing, and _select_model(c, "Pro") returned (False, '6') in 4.0s: a
# total refusal to send, measured live. The tier is therefore read from the slider control's OWN
# accessible description first, and only ever from text that is SHAPE-VALIDATED as the tier
# announcement — see _slider_state_label's source chain.
#
# ORDER OF OPERATIONS: family FIRST, then tier. Changing the family re-renders the picker and can
# reset the slider, so a tier chosen before a family change is not a tier that survives it.
#
# The composer has more than one switcher button. Which one carries the target tier is
# NOT fixed — a build seen 2026-08-18 puts Instant/Medium/High/Extra High/Pro entirely
# inside the reasoning-effort/power control (see _slider_set below); an older build
# still served a flat 'Pro' menuitem directly. (An earlier version of this comment
# claimed the effort menu never contains 'Pro' — that was true of the flat-menu build
# only and is now the stale case, not the current one.) So we must try EACH candidate
# switcher, open its menu, and pick the one whose menu actually contains the target.
# Detection is by short button label, not a fixed whitelist, so it survives ChatGPT
# renaming the tiers.
# Candidate = a COMPOSER model/effort switcher only. Scoped tightly so a stray
# 'Pro'-reading button elsewhere (e.g. 'Upgrade to Pro', a plan badge, account chrome)
# can NEVER false-confirm the model. A real switcher: short label, opens a menu
# (aria-haspopup) or is aria-labelled as a model picker; and is NOT an action/nav button.
_CAND_JS = ("[].slice.call(document.querySelectorAll('button')).filter(function(b){"
            "var t=(b.innerText||'').trim().split('\\n')[0];"
            "if(!t||t.length>28||/^(projects?|share|copy|send|search|attach|new chat|cancel|stop|upgrade|get |settings|log ?in|sign|account)/i.test(t))return false;"
            "var hp=(b.getAttribute('aria-haspopup')||'');"
            "var al=(b.getAttribute('aria-label')||'');"
            "var menuish=/menu|listbox|dialog|true/i.test(hp)||/model/i.test(al);"
            "var labelish=/^(Instant|Medium|High|Extra High|Pro|Auto|Thinking|GPT|ChatGPT|o[0-9]|[0-9]\\.[0-9])/.test(t);"
            "return menuish||labelish;})")

# The composer's OWN switcher, scoped to its <form>: on the build live-repro'd 2026-08-28 this
# is exactly the tier control (a button[aria-haspopup=menu] under the form, never the '+'
# attach button) and it is what actually works — the wide, document-wide _CAND_JS scan above
# can (and on this build did) resolve its first entry to the Chat/Agent MODE TOGGLE instead of
# the tier switcher. Tried FIRST for that reason; _CAND_JS remains as the fallback for a build
# whose switcher sits outside the form (do not delete it). aria-haspopup is read via
# getAttribute rather than a CSS attribute selector purely so this stays a plain equality check
# alongside _CAND_JS's own such check above.
_FORM_CAND_JS = (
    "[].slice.call((document.querySelector('form')||document.documentElement)"
    ".querySelectorAll('button')).filter(function(b){"
    "return b.getAttribute('aria-haspopup')==='menu'&&b.id!=='composer-plus-btn';})"
)

# Every candidate this run considers, form-scoped ones FIRST (see _FORM_CAND_JS) followed by
# whatever the wide scan adds that isn't already in that list. A single expression so
# _cand_labels_js / _open_cand_by_label_js each get one internally consistent DOM snapshot per
# Runtime.evaluate call — never a snapshot stitched together from two separate round-trips.
_ALL_CAND_JS = (
    "(function(){var f=%s;var w=%s;"
    "return f.concat(w.filter(function(b){return f.indexOf(b)<0;}));})()"
    % (_FORM_CAND_JS, _CAND_JS)
)


# ChatGPT's model pill is a Radix popover: it opens ONLY on a real pointer-event
# sequence, NOT on a bare .click(). A plain click left the menu closed, so no
# menuitems ever appeared and selection silently fell back to the project default
# (looked fine only because the default was already Pro). Dispatch the full gesture.
_GESTURE = ("['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){"
            "EL.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));});")


def _open_cand_by_label_js(label):
    """Atomically FIND the candidate whose first-line label is exactly `label` and dispatch the
    open gesture to it — all inside ONE Runtime.evaluate call. Returns the label it actually
    opened (None if `label` is not present in THIS snapshot).

    This replaces addressing-by-index. The previous version (`_open_cand_js(i)`) read every
    candidate's label in one round-trip, then in a SECOND, separate round-trip re-ran the
    candidate query and opened whatever sat at position `i` — but that query rebuilds a live
    NodeList, and between the two round-trips React can re-render the composer and change which
    button occupies position `i`, or how many candidates exist at all. Live-verified 2026-08-28
    on a FRESH tab at the project URL: the candidate set itself flapped between one and two
    entries across reads two seconds apart. One run's index-1 open resolved to nothing (that
    read only had one candidate); the next opened index 0, which resolved to the composer's
    Chat/Agent MODE TOGGLE, not the tier switcher — its menu has no slider and no tier items, so
    the walk below never even started, and the consult refused to send with the tier button
    sitting right there the whole time. Searching for `label` INSIDE the same evaluation that
    also builds the candidate list and dispatches the gesture means there is only ever one live
    snapshot in play; nothing about the addressing can survive — or be invalidated by — a round
    trip, so it can never land on a button other than the one actually named `label` right now.

    The echo is captured BEFORE the gesture, not after. Live 2026-09-06: this build's switcher
    RENAMES ITSELF on open — closed it reads the model badge "6", open it reads "Thinking effort" —
    and React had already committed that rename by the time a post-gesture read ran in the same
    evaluation. The echo then disagreed with `label` for the correct button, the caller discarded
    its own successful open as an addressing miss, and _select_model refused with the picker it had
    just opened sitting right there ("the composer's picker menu never opened", measured live).
    A post-gesture read cannot say anything the pre-gesture one cannot: the element gestured is the
    element found, inside one evaluation. So the echo answers the only question it ever really
    answered — was a button named `label` found — and no longer doubles as a claim about what that
    button reads after being clicked."""
    t = json.dumps(label)
    return (
        "(function(){var c=%s;var want=%s;var idx=-1;"
        "for(var k=0;k<c.length;k++){"
        "if(((c[k].innerText||'').trim().split('\\n')[0])===want){idx=k;break;}}"
        "var EL=c[idx];if(!EL)return null;"
        "var got=(EL.innerText||'').trim().split('\\n')[0];%s"
        "return got;})()"
        % (_ALL_CAND_JS, t, _GESTURE)
    )


def _click_item_js(target):
    # Fallback #2 (after the slider, below): the flat-menu layout, where a tier sits directly
    # as a [role=menuitem] in the open menu. Superseded as the PRIMARY path by _slider_set on
    # the build probed 2026-08-18 (the top-level menu there holds no item at all — the tiers
    # live on a slider), but kept because ChatGPT is mid-rollout and older accounts still serve
    # this flat form; also still needed for the reasoning-effort SUBMENU (see _submenu_try),
    # which reintroduces a flat item list one level down.
    #
    # Match the menuitem's FIRST LINE, Pro-family aware — MUST mirror _matches():
    # a 'Pro' target is satisfied by any Pro tier the menu offers (ChatGPT's effort menu
    # labels the top tier 'Pro Extended', there is no bare 'Pro' item), so an exact-only
    # match could never CLICK 'Pro Extended' from a 'Pro' target even though confirm()
    # accepts it — the two matchers would disagree and selection would fail-closed on a
    # switcher sitting on Medium. A non-Pro target still needs an exact first-line match
    # (never a loose contains, which would click a description item). Same Radix gesture as
    # opening (a bare .click() misses). Verified live: Medium -> 'Pro' selects 'Pro Extended'.
    t = json.dumps(target.lower())
    return ("(function(){var T=%s;var ms=[].slice.call(document.querySelectorAll("
            "'[role=\"menuitem\"],[role=\"option\"],[role=\"menuitemradio\"]'));"
            "var EL=ms.find(function(x){var f=(x.innerText||'').trim().toLowerCase().split('\\n')[0];"
            "return f===T||(T.indexOf('pro')===0&&f.indexOf('pro')===0);});"
            "if(EL){%s return true;}return false;})()" % (t, _GESTURE))


# ---- slider path (primary on the build probed 2026-08-18) ----
# That build's tier control is a Radix slider, not a menuitem list: opening a switcher's menu
# shows a group (data-testid="composer-intelligence-picker-content") whose first line is e.g.
# "Pro, 5 of 5." followed by a span[role="slider"][aria-valuemin=0][aria-valuemax=4] that
# saturates at both ends (no wraparound). valuenow 0..4 maps onto Instant/Medium/High/Extra
# High/Pro in that order — verified live by walking it with ArrowLeft/ArrowRight via CDP
# Input.dispatchKeyEvent after focusing the slider's [role=menuitem] ancestor (the slider
# itself is not focusable; .focus() on a bare <span role=slider> does nothing in this build).
_SLIDER_ANNOUNCE_RE = re.compile(r",\s*\d+\s+of\s+\d+\.?\s*$", re.IGNORECASE)
# The SAME announcement, anchored whole rather than as a trailing strip. This is the shape test
# that lets the tier be found by identity instead of by line number: any line of the picker
# group — or any element the slider points at with aria-describedby — that reads "<tier>, N of
# M." IS the tier announcement, wherever it sits. An unanchored strip cannot say "this text is
# not an announcement at all", which is what made "6" parse as a tier on the GPT-6 build.
_TIER_ANNOUNCE_RE = re.compile(r"^(.+?),\s*\d+\s+of\s+\d+\.?$", re.IGNORECASE)


def _slider_label(first_line):
    """Parse the group's first line into a tier label. Two shapes are both real, live-verified
    2026-08-28: a control that has been driven by keyboard announces 'Pro, 5 of 5.' (an
    accessibility 'N of M' suffix — the Power menuitem carries data-keyboard-interaction-active,
    which stays false, and the suffix absent, until the control has actually been driven by
    keyboard); a fresh tab whose menu was only just opened renders the bare word 'Pro' with no
    suffix at all. Strip the suffix when present; otherwise trust the line as-is.

    (The previous version returned '' whenever there was no comma, on the theory that a line
    with no 'N of M' delimiter was not this group's format at all. That was the live bug: it
    silently discarded the bare form too, so _matches() compared every read against '' and a
    walk that landed exactly on Pro still reported failure. The theory does not survive contact
    with the real DOM — a fresh tab serves the bare form as its FIRST and only shape until
    keyboard interaction flips the flag.)

    A short, single-line sanity bound still guards the result: _matches() demands an exact
    label match (or the Pro-family prefix) anyway, so a long non-label line (e.g. 'Use Left and
    Right arrow keys to adjust power.') could never have produced a false MATCH — but an
    unbounded return would still let such prose leak into the reported 'shown' value on
    failure, which is worth keeping out."""
    s = (first_line or "").strip()
    if not s:
        return ""
    s = _SLIDER_ANNOUNCE_RE.sub("", s).strip()
    if not s or len(s) > 24 or "\n" in s:
        return ""
    return s


def _announced_tier(text):
    """The tier out of a slider announcement — 'Pro, 5 of 5.' -> 'Pro' — or '' when `text` is not
    that shape at all. Anchored on purpose (see _TIER_ANNOUNCE_RE): the two highest-priority
    sources below trust this result BY SHAPE rather than by where it was found, so it must be
    able to REJECT, not merely strip. The captured tier still goes through _slider_label's
    single-line/24-char sanity bound — an announcement is not a licence to return prose."""
    m = _TIER_ANNOUNCE_RE.match((text or "").strip())
    return _slider_label(m.group(1)) if m else ""


def _slider_state_label(st):
    """The tier the slider is currently on, by IDENTITY. Ordered source chain — each source is
    tried only because the one above it was unavailable on some real, live-observed build:

      1. `descs` — the textContent of every id in the slider's [role=menuitem] ancestor's OWN
         aria-describedby. This is the control describing itself, so it cannot be displaced by a
         re-layout of the menu around it. On the GPT-6 build (live 2026-09-06) it is the only
         place the tier is written down at all: that wrapper's innerText AND textContent are both
         empty, and aria-describedby="_r_cn_ _r_co_" points at "Pro, 5 of 5.".
      2. any `lines` entry of the picker group matching the same ", N of M." shape — the
         pre-GPT-6 build, where line 0 was itself "Pro, 5 of 5.". Scanned rather than indexed
         because on the GPT-6 build the same announcement moved to line 2.
      3. the group's first line, permissively (see _slider_label) — the legacy BARE form: a fresh
         pre-GPT-6 tab rendered "Pro" with no announcement suffix until the control had been
         driven by keyboard.
      4. `btnLabel`, the open switcher's own label — LAST RESORT ONLY. It used to be a trustworthy
         independent read of the same fact; on the GPT-6 build that button reads "Thinking effort"
         while its menu is open (live 2026-09-06), i.e. this source can now yield a string that is
         not a tier at all. That is why it ranks below everything else and why sources 1-2 are
         shape-validated instead of trusted for their position."""
    for desc in st.get("descs") or []:
        lab = _announced_tier(desc)
        if lab:
            return lab
    for line in st.get("lines") or []:
        lab = _announced_tier(line)
        if lab:
            return lab
    lab = _slider_label(st.get("first") or "")
    if lab:
        return lab
    return _slider_label(st.get("btnLabel") or "")


# ONE snapshot carrying BOTH dimensions the guard pins — the tier sources _slider_state_label
# needs AND the model-family radio group — because they must be judged as a TUPLE. Two separate
# Runtime.evaluate calls can only ever produce two observations that were never simultaneously
# true, and React re-renders this picker between round-trips (live-verified 2026-08-28: the
# candidate set itself flapped across reads two seconds apart).
#
# Reading them together also removes a whole fail-open by construction: the family read used to be
# able to come back null/empty *while the slider read fine*, and an empty family read was taken as
# "this build has no family control". In one snapshot that combination cannot arise — the slider
# lives INSIDE the picker, so a readable slider always yields an owning picker (see `own` below).
#
# Tier fields (unchanged meanings):
#   descs     textContent of each aria-describedby target of the slider's [role=menuitem] wrapper
#   lines     every non-empty line of the picker group (the announcement can sit at any of them)
#   first     that group's first line — the legacy bare form, and on GPT-6 the model badge "6"
#   btnLabel  the open switcher's label ("Thinking effort" while open, on GPT-6)
#
# Family field `fam`, deliberately NOT a bare list (see _family_obs for why an empty list is not a
# usable signal):
#   owners    how many elements are the composer's intelligence PICKER right now. The family is
#             read out of that one element only — never out of "every open menu's radios" — so an
#             unrelated menu (the Chat/Agent mode toggle) that happens to contain no radios can no
#             longer be mistaken for "the picker, inspected, has no family control".
#   menus     how many [role=menu] are open at all, for the ambiguity report.
#   radios    {label, checked} per [role=menuitemradio] INSIDE the owner — null when there is no
#             unique owner to read them out of, which is a different fact from "there are none".
#   scaffold  radio-group chrome ([role=radiogroup] / [role=radio]) inside the owner. A picker
#             with scaffolding but zero readable radios is a half-rendered group, not a build
#             that has no such control.
_PICKER_STATE_JS = (
    "(function(){"
    "var own=[].slice.call(document.querySelectorAll("
    "'[data-testid=\"composer-intelligence-picker-content\"]'));"
    "if(!own.length){own=[].slice.call(document.querySelectorAll('[role=\"menu\"]'))"
    ".filter(function(m){return !!m.querySelector('[role=\"slider\"]');});}"
    "var fam={owners:own.length,menus:document.querySelectorAll('[role=\"menu\"]').length,"
    "radios:null,scaffold:0};"
    "if(own.length===1){var o=own[0];"
    "fam.radios=[].slice.call(o.querySelectorAll('[role=\"menuitemradio\"]')).map(function(r){"
    "return {label:((r.innerText||'').trim().split('\\n')[0]),"
    "checked:r.getAttribute('aria-checked')==='true'};});"
    "fam.scaffold=o.querySelectorAll('[role=\"radiogroup\"],[role=\"radio\"]').length;}"
    "var s=document.querySelector('[role=\"menu\"] [role=\"slider\"]');"
    "if(!s)return {fam:fam,first:'',lines:[],descs:[],btnLabel:'',now:null,min:null,max:null};"
    "var mi=s.closest('[role=\"menuitem\"]');"
    "var grp=document.querySelector('[data-testid=\"composer-intelligence-picker-content\"]');"
    "var src=grp||mi;"
    "var lines=((src&&src.innerText)||'').split('\\n').map(function(x){return x.trim();})"
    ".filter(function(x){return !!x;});"
    "var descs=[];((mi&&mi.getAttribute('aria-describedby'))||'').split(/\\s+/)"
    ".forEach(function(id){var e=id?document.getElementById(id):null;"
    "if(e)descs.push((e.textContent||'').trim());});"
    "var trig=(document.querySelector('form')||document).querySelector('button[aria-expanded=\"true\"]');"
    "var btnLabel=((trig&&trig.innerText)||'').trim().split('\\n')[0];"
    "return {fam:fam,first:lines[0]||'',lines:lines,descs:descs,btnLabel:btnLabel,"
    "now:parseInt(s.getAttribute('aria-valuenow'),10),"
    "min:parseInt(s.getAttribute('aria-valuemin'),10),max:parseInt(s.getAttribute('aria-valuemax'),10)};"
    "})()")

_SLIDER_FOCUS_JS = (
    "(function(){var s=document.querySelector('[role=\"menu\"] [role=\"slider\"]');"
    "if(!s)return false;var mi=s.closest('[role=\"menuitem\"]');"
    "if(!mi)return false;mi.focus();return true;})()")


# A restore/goto walk must END, and "loop until the value equals the target" is not a bound. Live
# reasoning behind the three bounds in _slider_goto: a control that ALTERNATES (0 -> 2 -> 0 -> 2
# while the destination is 1) satisfies neither "arrived" nor "did not move", so the old
# `while pos != entry_now` loop had no exit at all — only the outer subprocess timeout ended it,
# which is a process being killed, not an algorithm terminating.
_SLIDER_WALK_SECONDS = 20.0


def _press_slider(c, key, prev_now):
    """One arrow press plus the bounded settle poll. Returns (now, label, state) — (None,None,None)
    when the control stopped reading at all.

    Re-read up to 3 times at ~0.2s and take the first read that actually differs from prev_now.
    Only 3 unchanged reads in a row count as no-move: a live probe found single presses settle in
    0.35-0.45s, and one flat 0.3s sleep read that as "stuck keys" on a merely-slow frame."""
    key_name, code, keycode = key
    c.key(key_name, code, keycode)
    st = None
    for _ in range(3):
        time.sleep(0.2)
        st = c.eval(_PICKER_STATE_JS)
        if isinstance(st, dict) and st.get("now") is not None and st["now"] != prev_now:
            break
    if not isinstance(st, dict) or st.get("now") is None:
        return None, None, None
    return st["now"], _slider_state_label(st), st


_SliderWalk = collections.namedtuple("_SliderWalk", "pos label proven")
"""Where a bounded walk actually ENDED, as an observation rather than a hope:

  pos     the last position genuinely OBSERVED (None when the control stopped reading)
  label   the tier observed at `pos` — None when unproven or unreadable. NEVER a remembered
          entry label: presenting historical evidence as an observation of what is selected now
          is how a restore that did not happen reads as one that did.
  proven  whether `pos == dest` was actually witnessed. Anything else is dirty/unknown.
"""


def _slider_goto(c, dest, deadline=None):
    """Walk the OPEN picker's slider to the ABSOLUTE position `dest`. Bounded by construction, on
    three independent axes — any one of them alone is insufficient:

      * an OPERATION budget: at most one press per position between here and `dest`, plus two
        presses of slack for a press that lands late;
      * a wall-clock DEADLINE, so a control that answers every read but never settles still ends;
      * MONOTONIC PROGRESS IN RANGE: every observed position must lie inside [min,max] and be
        strictly closer to `dest` than the one before it. This is the one that terminates the
        alternating control described above — 0 -> 2 with dest 1 is not progress, so the walk
        stops and reports `proven=False` instead of pressing forever.

    Reports UNKNOWN rather than guessing. A caller that cannot prove the restoration must say the
    composer is dirty; "probably back where it was" is exactly the claim this whole guard exists
    to refuse to make."""
    st = c.eval(_PICKER_STATE_JS)
    if not isinstance(st, dict) or st.get("now") is None:
        return _SliderWalk(None, None, False)
    lo, hi, pos = st.get("min"), st.get("max"), st["now"]
    label = _slider_state_label(st)
    if pos == dest:
        return _SliderWalk(pos, label, True)
    if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi or not lo <= dest <= hi:
        return _SliderWalk(pos, None, False)  # the destination is not on this control's scale
    if deadline is None:
        deadline = time.time() + _SLIDER_WALK_SECONDS
    if not c.eval(_SLIDER_FOCUS_JS):
        return _SliderWalk(pos, label, False)  # cannot drive it — nothing pressed, nothing moved
    budget = abs(dest - pos) + 2
    while budget > 0 and time.time() < deadline:
        budget -= 1
        gap = dest - pos
        key = ("ArrowRight", "ArrowRight", 39) if gap > 0 else ("ArrowLeft", "ArrowLeft", 37)
        new_pos, new_label, _st = _press_slider(c, key, pos)
        if new_pos is None:
            return _SliderWalk(pos, None, False)          # stopped reading — position unknown now
        if not lo <= new_pos <= hi:
            return _SliderWalk(new_pos, None, False)      # off its own scale — not this control
        if abs(dest - new_pos) >= abs(gap):
            return _SliderWalk(new_pos, new_label, False)  # no progress: stuck, or alternating
        pos, label = new_pos, new_label
        if pos == dest:
            return _SliderWalk(pos, label, True)
    return _SliderWalk(pos, label, pos == dest)


_TierPick = collections.namedtuple("_TierPick", "ok label pos dirty")
"""One tier-walk attempt. `pos` is the slider position last observed (None when unreadable) and
`dirty` says the walk moved the control and could NOT prove it put it back — a property the caller
must be able to report separately from "nothing was sent"."""


def _slider_set(c, target):
    """Primary path: walk the power/effort slider to `target` inside the currently-open menu.
    Deterministic by construction — always saturate LEFT first (valuemax-valuemin presses; a
    press at the floor is a verified no-op, not a wrap) so the walk starts from a known state
    regardless of where the tier began, then step RIGHT one position at a time, re-reading
    (label, valuenow) after every press and stopping the instant _matches() is satisfied.

    Bails the moment a press that SHOULD have moved valuenow (i.e. we were not already sitting
    on the boundary it presses toward) does not move it, per a short bounded poll rather than a
    single fixed sleep — see _press_slider.

    FAIL-CLOSED MUST BE SIDE-EFFECT-FREE. Live-verified: a target this account does not have
    (walk saturates left, then right to the ceiling, never matches) used to leave the slider
    wherever the walk gave up — the composer silently ended up retargeted to the HIGHEST tier
    it happened to pass through, even though the caller's own message says "could not be
    changed". So every failure return that happens after at least one press actually landed
    now walks back to the ENTRY valuenow via _slider_goto, which is bounded on three axes and
    reports whether it ARRIVED. An unproven restore returns label=None and dirty=True: the
    caller then says the composer is dirty instead of quoting a label nobody observed. The
    success path never restores — there is nothing to undo.
    """
    st = c.eval(_PICKER_STATE_JS)
    if not isinstance(st, dict) or st.get("now") is None:
        return _TierPick(False, None, None, False)
    lo, hi, now = st.get("min"), st.get("max"), st["now"]
    if not isinstance(lo, int) or not isinstance(hi, int) or lo > hi:
        return _TierPick(False, None, now, False)
    entry_now = now
    label = _slider_state_label(st)
    if _matches(label, target):
        return _TierPick(True, label, now, False)
    if not c.eval(_SLIDER_FOCUS_JS):
        return _TierPick(False, label, now, False)  # nothing pressed yet — nothing to restore
    deadline = time.time() + _SLIDER_WALK_SECONDS

    def _give_up():
        back = _slider_goto(c, entry_now)
        return _TierPick(False, back.label if back.proven else None, back.pos, not back.proven)

    span = hi - lo
    for _ in range(span):
        if time.time() >= deadline:
            return _give_up()
        expect_move = now > lo
        new_now, new_label, _st = _press_slider(c, ("ArrowLeft", "ArrowLeft", 37), now)
        if new_now is None or (expect_move and new_now == now):
            return _give_up()
        now, label = new_now, new_label
    if _matches(label, target):
        return _TierPick(True, label, now, False)

    for _ in range(span):
        if time.time() >= deadline:
            return _give_up()
        expect_move = now < hi
        new_now, new_label, _st = _press_slider(c, ("ArrowRight", "ArrowRight", 39), now)
        if new_now is None or (expect_move and new_now == now):
            return _give_up()
        now, label = new_now, new_label
        if _matches(label, target):
            return _TierPick(True, label, now, False)
    return _give_up()


# ---- submenu path (fallback #3) ----
# The Effort/Model rows inside the open menu are themselves [role=menuitem][aria-haspopup=menu]
# triggers that open a SECOND [role=menu]; on the probed build the Effort submenu duplicates the
# slider's tiers as flat, checkable items (Instant/Medium/High/Extra High/Pro), reachable by
# _click_item_js once open. Never hardcode which trigger by name ('Effort') — a future build
# could relocate it — so every haspopup trigger is tried and left to the item-match to decide.
# (This comment used to add "and Model must never be touched (it picks the family, not the
# tier)". Dead as of 2026-09-03: the family is pinned deliberately now, by _apply_family, which
# addresses the radios directly and never goes through this submenu walk. The item-match here
# still decides on TIER text alone, so a Model submenu opened first remains harmless.)
# Verified live: hover
# events alone (pointerover/pointerenter/mouseover/mousemove) did NOT open the submenu; only
# following them with the same pointer-down/up gesture used to open the top-level menu (_GESTURE)
# did.
def _submenu_count_js():
    return ("(function(){return [].slice.call(document.querySelectorAll("
            "'[role=\"menu\"] [role=\"menuitem\"][aria-haspopup=\"menu\"]')).length;})()")


def _open_submenu_js(i):
    return ("(function(){var els=[].slice.call(document.querySelectorAll("
            "'[role=\"menu\"] [role=\"menuitem\"][aria-haspopup=\"menu\"]'));"
            "var EL=els[%d];if(!EL)return false;"
            "['pointerover','pointerenter','mouseover','mousemove'].forEach(function(t){"
            "EL.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));});"
            "%s return true;})()" % (i, _GESTURE))


def _submenu_try(c, target):
    """Open each haspopup trigger in the currently-open menu, in DOM order, and retry
    _click_item_js inside it. Escapes out of a submenu that did not contain the target before
    trying the next, so a Model submenu opened first never blocks reaching Effort second."""
    n = c.eval(_submenu_count_js()) or 0
    for i in range(min(int(n), 6)):
        if not c.eval(_open_submenu_js(i)):
            continue
        time.sleep(0.6)  # second Radix menu also renders async
        if c.eval(_click_item_js(target)):
            time.sleep(0.5)
            return True
        c.key("Escape", "Escape", 27)
        time.sleep(0.25)
    return False


# ---- model FAMILY path (new dimension; see the law at the top of this section) ----
# Live 2026-09-06: the open picker carries the MODEL itself as [role="menuitemradio"] items —
# "Latest" (aria-checked=true), "GPT-5.6 Sol", "GPT-5.5" — in the SAME menu as the power slider.
# Read as {label, checked} pairs so the "already correct" case can be distinguished from the
# "must click" case without a click: this is the one control in the picker where a redundant
# gesture is not free (a Radix radio click also closes the menu, which would drop the slider out
# from under the tier walk that runs next).
# The read itself lives in _PICKER_STATE_JS (one snapshot, both dimensions). `var FAMS=` survives
# only in _click_family_js below, and is deliberately distinctive: it is how the offline fakes in
# tests/ tell the family CLICK apart from _click_item_js, which queries menuitemradio too.


def _click_family_js(target):
    """Select the radio whose FIRST LINE is exactly `target` (case-insensitive; never a loose
    contains — 'GPT-5.5' must not be able to match 'GPT-5.6 Sol' or a description line). Scoped to
    the OWNING picker by the same rule _PICKER_STATE_JS reads it with, so the element clicked is
    the element observed; a radio in some other open menu is not this control. Same _GESTURE as
    everything else in this menu: these are Radix items and a bare .click() does not select them."""
    t = json.dumps((target or "").strip().lower())
    return ("(function(){var T=%s;"
            "var own=[].slice.call(document.querySelectorAll("
            "'[data-testid=\"composer-intelligence-picker-content\"]'));"
            "if(!own.length){own=[].slice.call(document.querySelectorAll('[role=\"menu\"]'))"
            ".filter(function(m){return !!m.querySelector('[role=\"slider\"]');});}"
            "if(own.length!==1)return false;"
            "var FAMS=[].slice.call(own[0].querySelectorAll('[role=\"menuitemradio\"]'));"
            "var EL=FAMS.find(function(r){"
            "return ((r.innerText||'').trim().toLowerCase().split('\\n')[0])===T;});"
            "if(!EL)return false;%s return true;})()" % (t, _GESTURE))


_FamilyObs = collections.namedtuple("_FamilyObs", "status entries detail")
"""A TYPED observation of the family dimension, replacing the old list-or-empty-list signal.

The old signal was a fail-open: the list-shaped family read turned a null read, a non-list read
and a malformed read all into [], and [] meant "this build has no family control — exempt".
The reachable counterexample: a composer sitting on GPT-5.5 whose family probe returns null during
a React render transition while the Pro slider reads fine. Nothing established `Latest`, and the
guard reported the model confirmed. An unreadable control is not evidence that the control is
right; it is the absence of evidence, which is the one thing a fail-closed guard must never treat
as a pass.

  family      a unique owning picker with a COHERENT radio group (every item labelled, at most one
              checked). `entries` is ((label, checked), ...). The only status that can be acted on.
  legacy      a unique owning picker that positively renders NO family control at all: zero radios
              AND zero radio-group scaffolding. The pre-GPT-6 composer. The ONLY status that earns
              the silent no-op exemption on its own.
  absent      no element is the composer's intelligence picker right now — this open menu is not
              it (the Chat/Agent mode toggle), or the whole read came back null. NOT an exemption
              by itself: it earns one only from a build that then proves it predates the picker
              entirely by serving the tier as a flat menuitem (see _select_model).
  ambiguous   more than one picker is open; none of them uniquely owns the radios.
  failed      the picker IS open and its radio group did not read back coherently — null radios, an
              unlabelled item, two items checked at once, or scaffolding with no items. Refuse.
"""


def _family_obs(st):
    """Read the family dimension out of ONE _PICKER_STATE_JS snapshot. Pure — no round-trip of its
    own, so the family it reports and the tier `st` reports were true at the same instant."""
    fam = st.get("fam") if isinstance(st, dict) else None
    if not isinstance(fam, dict):
        return _FamilyObs("absent", (), None)
    owners = fam.get("owners")
    if not isinstance(owners, int) or owners < 0:
        return _FamilyObs("failed", (), "the composer picker probe returned no owner count")
    if owners == 0:
        return _FamilyObs("absent", (), None)
    if owners > 1:
        return _FamilyObs("ambiguous", (), (
            "%d composer pickers are open at once, so none of them uniquely owns the model radios "
            "— the model cannot be established from an ambiguous picker" % owners))
    radios = fam.get("radios")
    if not isinstance(radios, list):
        return _FamilyObs("failed", (), (
            "the composer picker is open but its model radio group did not read back"))
    entries = []
    for r in radios:
        if not isinstance(r, dict) or not str(r.get("label") or "").strip():
            return _FamilyObs("failed", (), (
                "the composer picker's model radio group read back malformed (an item with no "
                "label) — an unreadable model control is not evidence that the model is right"))
        entries.append((str(r["label"]).strip(), bool(r.get("checked"))))
    if entries:
        checked = sum(1 for _lab, chk in entries if chk)
        if checked > 1:
            return _FamilyObs("failed", (), (
                "the composer picker's model radio group is incoherent: %d items read "
                "aria-checked=true at once" % checked))
        return _FamilyObs("family", tuple(entries), None)
    scaffold = fam.get("scaffold")
    if not isinstance(scaffold, int) or scaffold > 0:
        return _FamilyObs("failed", (), (
            "the composer picker renders radio-group scaffolding but no readable radio — a "
            "half-rendered group is not a build that has no model control"))
    return _FamilyObs("legacy", (), None)


def _family_checked(obs):
    """The label the radio group currently reads as checked, or None (including when the group
    offers no checked item at all, which is itself a state we cannot restore TO)."""
    return next((lab for lab, chk in obs.entries if chk), None)


def _reopen_picker(c, lab):
    """Reopen the composer's picker and return a fresh snapshot, or None.

    Radix commits a radio selection by CLOSING the menu, so every read that must happen after a
    click needs this. The old code re-read the radios without reopening, saw none (the menu was
    gone), and reported the click had failed AFTER its mutation had actually landed — a false
    failure that then left the composer on a family nobody restored.

    `lab` is tried first, then whatever the composer's switchers read RIGHT NOW, because the very
    commit being confirmed can RENAME the button that owns the picker: the GPT-6 badge is the model
    family (live 2026-09-06 it reads "6"), and the pre-GPT-6 switcher reads the tier it is on. An
    open that lands on a menu with no picker in it (the Chat/Agent mode toggle) is closed again and
    does not count."""
    tried = []
    for cand in [lab] + [x for x in (c.eval(_cand_labels_js()) or []) if x]:
        if not cand or cand in tried or len(tried) >= 3:
            continue
        tried.append(cand)
        if c.eval(_open_cand_by_label_js(cand)) != cand:
            continue                    # addressing miss — nothing opened, nothing to close
        time.sleep(1.0)                 # Radix menu renders async after the gesture
        st = c.eval(_PICKER_STATE_JS)
        if isinstance(st, dict) and (st.get("now") is not None
                                     or _family_obs(st).status != "absent"):
            return st
        _close_menus(c)
    return None


_FamilyPick = collections.namedtuple("_FamilyPick", "ok label detail touched")
"""`touched` means a gesture was DISPATCHED at the radio group — whether or not it was confirmed.
Anything touched must be restored on a later failure, because a click whose confirmation could not
be read is not a click that provably did nothing."""


def _apply_family(c, lab, obs, target):
    """Pin the model FAMILY inside the currently-open picker, given its already-taken observation.

    An already-checked target is left ALONE — no click at all. Clicking a checked Radix radio is
    not a guaranteed no-op (it commits the menu closed), and there is nothing to change.

    A real change returns only after re-reading aria-checked and seeing it flip; when the click
    committed the menu closed, the menu is REOPENED first so that re-read can happen at all."""
    want = target.strip().lower()
    hit = next(((l, chk) for l, chk in obs.entries if l.lower() == want), None)
    if hit is None:
        return _FamilyPick(False, None, (
            "model family '%s' is not offered in this picker (offered: %s)"
            % (target, ", ".join(l for l, _ in obs.entries))), False)
    if hit[1]:
        return _FamilyPick(True, hit[0], None, False)
    if not c.eval(_click_family_js(target)):
        return _FamilyPick(False, None,
                           "model family '%s' is listed but its radio could not be clicked" % target,
                           False)
    # A family change re-renders the whole picker, slider included; the tier walk that follows
    # re-reads _PICKER_STATE_JS from scratch, so it sees the NEW slider, not a stale position.
    time.sleep(0.6)
    obs2 = _family_obs(c.eval(_PICKER_STATE_JS))
    if obs2.status != "family":
        obs2 = _family_obs(_reopen_picker(c, lab))
    if obs2.status != "family":
        return _FamilyPick(False, None, (
            "model family '%s' was clicked but the picker could not be re-read to confirm it"
            % target), True)
    cur = _family_checked(obs2)
    if cur is not None and cur.lower() == want:
        return _FamilyPick(True, cur, None, True)
    return _FamilyPick(False, cur, (
        "model family '%s' did not take — aria-checked never flipped after the click" % target),
        True)


def _restore_family(c, lab, orig):
    """Put the radio group back on `orig` after a LATER step failed. Returns True only when the
    restoration was WITNESSED (aria-checked back on `orig`).

    This is the missing half of the transaction. `_slider_set` restores only its own entry
    position, and it captures that position AFTER the family change — so a run that switched the
    family and then failed the tier used to return "could not be changed" while leaving the
    composer on the new family. Two properties, not one: "no send occurred" and "no state
    changed"."""
    if not orig:
        return False  # never knew what it was — a restoration cannot be proven to a place unknown
    obs = _family_obs(c.eval(_PICKER_STATE_JS))
    if obs.status != "family":
        obs = _family_obs(_reopen_picker(c, lab))
    if obs.status != "family":
        return False
    cur = _family_checked(obs)
    if cur is not None and cur.lower() == orig.lower():
        return True
    if not c.eval(_click_family_js(orig)):
        return False
    time.sleep(0.6)
    obs = _family_obs(c.eval(_PICKER_STATE_JS))
    if obs.status != "family":
        obs = _family_obs(_reopen_picker(c, lab))
    if obs.status != "family":
        return False
    cur = _family_checked(obs)
    return bool(cur and cur.lower() == orig.lower())


def _restore_original(c, lab, orig_family, orig_tier_pos, restore_family):
    """Put the composer back on the (family, tier) tuple it was FOUND on. Returns (proven, label):
    `proven` only when every half attempted was witnessed, `label` the tier observed at the end (or
    None when that could not be established).

    FAMILY FIRST, then the tier — a family change re-renders the picker and rebuilds the slider, so
    restoring the tier before the family restores it onto a control that is about to be replaced.
    This is also why the tier is always restored to the ORIGINAL absolute position rather than left
    to _slider_set's own undo: _slider_set captures its entry position AFTER the family change, so
    on a build where the family reset the slider its "restore" faithfully returns the control to a
    position the composer was never on."""
    proven = True
    if restore_family:
        proven = _restore_family(c, lab, orig_family) and proven
    if orig_tier_pos is None:
        return proven, None
    st = c.eval(_PICKER_STATE_JS)
    if not isinstance(st, dict) or st.get("now") is None:
        st = _reopen_picker(c, lab)
    if not isinstance(st, dict) or st.get("now") is None:
        return False, None
    back = _slider_goto(c, orig_tier_pos)
    return (back.proven and proven), (back.label if back.proven else None)


def _cand_labels_js():
    """Every switcher-ish button's first-line label, in DOM order — composer/form-scoped
    candidates FIRST (see _FORM_CAND_JS), then whatever the wide, document-wide scan adds.
    c[0] is therefore the composer's own switcher when the build serves one there; later
    entries are other switchers (ChatGPT splits model and reasoning effort) or modes."""
    return ("(function(){return %s.map(function(b){"
            "return (b.innerText||'').trim().split('\\n')[0];});})()" % _ALL_CAND_JS)


def _matches(label, target):
    """Pro-family aware match, mirroring _click_item_js: a 'Pro' target is satisfied by any Pro tier
    the switcher offers ('Pro' / 'Pro Extended'), since ChatGPT ships no bare 'Pro' item. Any other
    target must match the first line exactly — never a loose contains, which would match a
    description line."""
    if not label:
        return False
    f = label.strip().lower()
    t = (target or "").strip().lower()
    return f == t or (t.startswith("pro") and f.startswith("pro"))


def _model_verdict(labels, target):
    """(confirmed, shown). `shown` is the label of the switcher that actually carries the target, so
    the verdict and the reported model can no longer contradict each other.

    Confirmation scans every switcher — correctly, because ChatGPT splits model and reasoning effort
    across two menus and the Pro tier lives in whichever one this build puts it in. Reporting,
    however, used to take labels[0] unconditionally, and labels[0] is NOT the model pill: the
    composer's first switcher in DOM order is the MODE toggle (Chat / Agent / …). A correctly
    pinned Pro consult therefore printed {"model": "Chat", "modelConfirmed": true} — a record that
    contradicts itself, makes a healthy run look broken, and would disguise a genuinely wrong tier
    exactly as well. Verified live: the project page shows ['Chat', 'Pro'], the conversation ['Pro']."""
    labels = [x for x in (labels or []) if x]
    for lab in labels:
        if _matches(lab, target):
            return True, lab
    return False, (labels[0] if labels else None)


_Confirm = collections.namedtuple("_Confirm", "verdict tier detail")
"""The post-commit joint read. `verdict` is one of:

  ok             the authoritative controls, observed TOGETHER after the selection settled, carry
                 the (family, tier) tuple that was asked for.
  contradiction  they carry something else. A VETO — never a tie an earlier observation wins.
  unreadable     they could not be read at all; the caller falls back to the resting composer's
                 own labels, which is the authoritative surface on the builds that have one.
"""


def _confirm_pinned(c, lab, target, family, want_family):
    """Reopen the picker after the commit/settle transition and read (family, tier) ONCE, together.

    This replaces `if not ok and slid_ok: ok = True`. That line could not tell "the resting button
    carries no tier on this schema, as expected" (GPT-6: the button reads the model badge "6")
    apart from "the resting surface actively CONTRADICTS the tier we just set" — so a slider that
    announced Pro mid-walk and reverted to High on commit still passed, on the strength of an
    observation that was no longer true. It also never re-read the FAMILY after the tier walk,
    so a tier change that reset the family passed on two observations that were never
    simultaneously true.

    Restoring "the closed button must read Pro" is NOT the fix — that requirement is what took the
    tool down when the badge replaced the tier on that button. The fix is to go back to the
    controls that actually hold the state, after they have settled, and read both at once.

    Read-only: it opens, reads, and closes. `lab` is the label the selection was made through; the
    resting labels are tried after it because pinning a tier can rename that very button (the
    pre-GPT-6 switcher shows the tier it is on)."""
    # Read the controls WHERE THEY ARE first. Live 2026-09-06: Radix leaves this picker MOUNTED
    # after dismissal (data-state="closed", contents still queryable and still tracking the real
    # state — a closed read returned checked=Latest, valuenow=4, "Pro, 5 of 5.", matching the
    # resting badge "6"). When the controls can be read without touching anything, reopening them
    # is not a stronger observation, only a more disruptive one — and reopening is where the flake
    # lives, because the trigger renames itself on open. Reopen only when there is nothing to read.
    st = c.eval(_PICKER_STATE_JS)
    if not (isinstance(st, dict)
            and (st.get("now") is not None or _family_obs(st).status != "absent")):
        st = _reopen_picker(c, lab)
        if st is None:
            return _Confirm("unreadable", None, None)
    obs = _family_obs(st)
    tier = _slider_state_label(st) if st.get("now") is not None else None
    _close_menus(c)
    if obs.status in ("failed", "ambiguous"):
        return _Confirm("unreadable", tier, obs.detail)
    if want_family and obs.status == "family":
        chk = _family_checked(obs)
        if not chk or chk.strip().lower() != family.strip().lower():
            return _Confirm("contradiction", tier, (
                "the composer settled on model family '%s', not '%s' — the tier walk did not "
                "leave the model where it was pinned" % (chk or "nothing", family)))
    if tier is None:
        return _Confirm("unreadable", None, None)
    if not _matches(tier, target):
        return _Confirm("contradiction", tier, (
            "the composer settled on tier '%s', not '%s' — the selection did not survive the menu "
            "closing" % (tier, target)))
    return _Confirm("ok", tier, None)


# Dispatch Escape AT the open menu, and report how many switchers still read aria-expanded="true".
# Two dispatch targets because Radix's dismissable layer listens on the layer node, not the
# document: the currently focused element (which IS that layer while the menu is open — live
# 2026-09-06 activeElement was the picker's own popover div) and every open [role=menu].
_DISMISS_JS = (
    "(function(){var E={key:'Escape',code:'Escape',keyCode:27,which:27,"
    "bubbles:true,cancelable:true};"
    "[document.activeElement].concat([].slice.call(document.querySelectorAll("
    "'[role=\"menu\"][data-state=\"open\"],[role=\"menu\"]:not([data-state])')))"
    ".forEach(function(t){if(t)t.dispatchEvent(new KeyboardEvent('keydown',E));});"
    # COMPOSER-SCOPED, exactly like _PICKER_STATE_JS's own btnLabel read. A document-wide count is
    # useless: live 2026-09-06 a RESTING ChatGPT page already carries four button[aria-expanded=
    # "true"] (sidebar and account disclosures), so "did the composer's menu close" measured
    # page-wide never reads zero and the dismissal loop can never see success.
    "return (document.querySelector('form')||document)"
    ".querySelectorAll('button[aria-expanded=\"true\"]').length;})()")


def _close_menus(c):
    """Leave the composer RESTING, and say whether it got there.

    Two dismissal mechanisms, not one. Live-measured 2026-09-06 on a logged-in Pro tab: a CDP
    Input.dispatchKeyEvent Escape does NOT dismiss this build's picker — the trigger stayed
    aria-expanded="true" across two of them — while a KeyboardEvent('keydown',{key:'Escape'})
    dispatched at the focused dismissable layer closed it on the first try. The CDP key is kept
    because it is what works on the pre-GPT-6 builds, where the JS form is a harmless extra event.

    This matters far more than tidiness now: the verdict is taken from the RESTING composer (with
    the menu open, this build's switcher reads "Thinking effort", which is neither a model nor a
    tier), so a menu that never closes makes every confirming read describe a transient. The old
    `if not ok and slid_ok: ok = True` line hid exactly that — it confirmed on a mid-walk
    observation and never noticed the composer had not actually settled.

    Repeated because the submenu path can stack a second [role=menu], and because dismissal is a
    React state update that is not visible in the same tick as the event that causes it."""
    for _ in range(3):
        c.key("Escape", "Escape", 27)
        time.sleep(0.15)
        if c.eval(_DISMISS_JS) == 0:
            return True
        time.sleep(0.35)
    return c.eval(_DISMISS_JS) == 0


_ModelPick = collections.namedtuple("_ModelPick", "confirmed shown badge error dirty")
"""The whole verdict of one selection attempt, in one value, because it is now more than one fact:

  confirmed  the TIER is pinned to the target (and no family miss vetoed the run)
  shown      the tier label the composer is actually left on
  badge      what the composer's own switcher reads with its menus CLOSED. On the GPT-6 build
             (live 2026-09-06) that is the model badge "6", which is the receipt this whole
             change exists to produce: it says WHICH MODEL answered, not merely how hard it
             thought. On older builds the same button showed a tier, or the Chat/Agent mode
             toggle; it is recorded verbatim either way, because a receipt that invents a model
             name is strictly worse than one that quotes the button.
  error      a human sentence when a FAMILY miss, an unreadable model control or a post-commit
             contradiction is what failed the run (a plain tier miss is already fully described by
             `shown` + the caller's target). Kept on the same value rather than a second channel
             so a caller cannot report one and miss the other.
  dirty      the composer's model controls were MUTATED and could not be proven restored. "No send
             occurred" and "no state changed" are separate properties: a caller that only reports
             the first leaves a human believing an untouched tab. Never a receipt field — it is
             appended to the refusal sentence, because the receipt schema belongs to another
             change.
"""


def _select_model(c, target, family=None):
    """Switch the composer to `target` (tier) and `family` (model) by trying each switcher menu;
    within each open menu pin the FAMILY first, then try the slider, the flat item, and each
    submenu trigger in that order — the three forms ChatGPT is known to serve for the tier
    depending on rollout stage. Returns a _ModelPick. Fully automated — no human step.

    FAMILY FIRST is not a preference: changing the model re-renders the picker and can reset the
    slider, so a tier pinned before a family change is not a tier that survives it. A family miss
    fails the whole selection immediately — it is the same class of harm as a tier miss (the
    consult would answer on a model the receipt does not name), so it must not be recoverable by
    trying another switcher, and the caller must not send.

    When a family IS being enforced the "composer already shows the target tier" shortcut is
    skipped: the radios are only readable inside an OPEN menu, so a tier-only early return would
    be exactly the unguarded-family hole this change closes.

    CONFIRMATION AUTHORITY moved with the DOM. The switcher label used to carry the tier, so
    _model_verdict over the closed composer's labels could confirm one. On the GPT-6 build that
    button shows the MODEL BADGE instead ("6" live 2026-09-06) and no button anywhere reads the
    tier, so the label verdict alone can never confirm and a correctly pinned Pro would be
    refused forever. The authority is therefore the CONTROLS, re-read together after they settle
    (see _confirm_pinned) — not the mid-walk observation, which is exactly as stale as the button
    was wrong. A contradiction there VETOES; only an unreadable confirming read falls back to the
    resting labels, and only when the family was positively settled first.

    TRANSACTIONAL FAILURE. Everything mutated inside a menu is snapshotted before it is touched —
    the checked family AND the slider position, from one snapshot — and a failure restores the
    family first (a family change re-renders the slider, so the reverse order restores a tier onto
    a control that is about to be rebuilt), then the original tier, then verifies both. What
    cannot be verified is reported as `dirty` rather than assumed.

    Candidates are read as LABELS, not positions, and each is opened by identity in a single
    evaluation (see _open_cand_by_label_js) — never by an index into a NodeList read in an
    earlier round-trip. If a candidate's open does not land on the label it was asked for, that
    open is discarded and nothing inside whatever it actually opened is acted on: an addressing
    miss (the composer re-rendered between the labels read and this open) is not evidence about
    the menu's contents, so it must not be treated as one.

    `exhausted` remembers a label whose menu WAS opened as asked and searched (slider, flat
    item, submenus) without finding target, and skips it on the next pass — the composer
    genuinely does re-render, so a bounded retry is kept, but a candidate already proven wrong
    must not be what the whole retry budget gets spent re-opening. A label that merely failed to
    OPEN (addressing miss, not content miss) is never added, so it stays eligible next pass.

    On a final failure, `shown` prefers the last real tier label the slider path observed over
    _model_verdict's labels[0] fallback: labels[0] is the composer's MODE toggle (Chat/Agent),
    not a tier (see _model_verdict), so on an unreachable target the caller's own error message
    ("switcher shows '{model_now}'") would otherwise report the mode toggle — useless for
    diagnosing a tier problem, and live-verified to happen. With the slider's own fail-closed
    restore (see _slider_set) that label is also the tier the composer is actually left on."""
    want_family = bool(family) and family.strip().lower() != "skip"
    labels = [x for x in (c.eval(_cand_labels_js()) or []) if x]
    badge = labels[0] if labels else None
    ok, shown = _model_verdict(labels, target)
    if ok and not want_family:
        return _ModelPick(True, shown, badge, None, False)
    last_slider_label = None
    # Whether the family dimension was actually ESTABLISHED (inside a menu that really opened, on a
    # schema whose family control was positively recognized), as opposed to merely not having
    # failed. Without this, a composer already sitting on the target tier whose picker never opens
    # at all would return confirmed=True with the model unverified — a fail-OPEN in the middle of a
    # fail-closed guard, and the exact hole this guard exists to close.
    family_checked = not want_family
    fam_detail = None      # the last reason the family could not be established, for the refusal
    dirty = False          # the composer was mutated and the mutation could not be proven undone
    exhausted = set()
    for _ in range(2):
        labels = [x for x in (c.eval(_cand_labels_js()) or []) if x]
        if labels:
            badge = labels[0]
        for lab in labels:
            if lab in exhausted:
                continue
            opened = c.eval(_open_cand_by_label_js(lab))
            if opened != lab:
                continue  # addressing miss — nothing opened is trustworthy, try the next label
            time.sleep(1.0)  # Radix menu renders async after the gesture
            # THE TRANSACTION'S OPENING SNAPSHOT: the family and the tier as they were found, read
            # together, before anything is touched. Restoration has to aim at the ORIGINAL tuple —
            # _slider_set's own entry position is captured after any family change, so it alone
            # cannot undo a run that changed the family and then failed the tier.
            st0 = c.eval(_PICKER_STATE_JS)
            obs0 = _family_obs(st0)
            orig_tier_pos = st0.get("now") if isinstance(st0, dict) else None
            orig_family = _family_checked(obs0)
            if want_family and obs0.status in ("failed", "ambiguous"):
                # The picker is open and its model control did not read back coherently. That is
                # not a build without one, and it is not a licence to drive the tier behind it:
                # touch nothing here, keep the reason, and let another candidate or pass try.
                fam_detail = obs0.detail
                _close_menus(c)
                continue
            fam = _FamilyPick(True, None, None, False)
            if want_family and obs0.status == "family":
                fam = _apply_family(c, lab, obs0, family)
                if not fam.ok:
                    # This picker's radio group EXISTS and could not be put on the family we
                    # promised. No other switcher can undo that, so stop — but first undo whatever
                    # this attempt already did.
                    if fam.touched:
                        proven, _lab = _restore_original(c, lab, orig_family, orig_tier_pos, True)
                        dirty = dirty or not proven
                    _close_menus(c)
                    return _ModelPick(False, last_slider_label or shown, badge, fam.detail, dirty)
            tier = _slider_set(c, target)
            if tier.label:
                last_slider_label = tier.label
            hit = tier.ok or c.eval(_click_item_js(target)) or _submenu_try(c, target)
            # What positively settles the family for THIS candidate:
            #   family  the radios were read and put (or found) on the target;
            #   legacy  a picker that renders no family control at all — the pre-GPT-6 composer;
            #   absent  no picker element exists here AND the tier was reached by the FLAT/submenu
            #           path, which is the pre-picker composer generation: a build that serves the
            #           tier as a plain menuitem and renders no intelligence picker predates the
            #           family control entirely. `absent` alone never settles anything — that is
            #           what let an unrelated menu with no radios satisfy the bookkeeping.
            settled = ((not want_family)
                       or (obs0.status == "family" and fam.ok)
                       or obs0.status == "legacy"
                       or (obs0.status == "absent" and hit and not tier.ok))
            family_checked = family_checked or settled
            if hit:
                time.sleep(0.5)
                # Leave the composer resting BEFORE any verdict: with the menu still open the
                # GPT-6 composer button reads "Thinking effort" (live 2026-09-06), so a verdict
                # taken mid-menu would judge the composer by a transient label that is not a model
                # or a tier at all.
                rested = _close_menus(c)
                conf = _confirm_pinned(c, lab, target, family, want_family)
                post = [x for x in (c.eval(_cand_labels_js()) or []) if x]
                if post:
                    badge = post[0]
                ok_rest, shown_rest = _model_verdict(post, target)
                if conf.verdict == "contradiction":
                    # A veto, not a tie the earlier observation wins. Retrying cannot make an
                    # observed contradiction untrue, so this ends the run — and because the
                    # composer is provably NOT where the walk thought it left it, the whole
                    # transaction is rolled back to the tuple it was found on. The family half is
                    # restored whether or not this attempt is what moved it: a family that drifted
                    # under the tier walk is still a family this run is responsible for.
                    proven, back_label = _restore_original(
                        c, lab, orig_family, orig_tier_pos, bool(orig_family))
                    _close_menus(c)
                    return _ModelPick(False, conf.tier or back_label or shown_rest, badge,
                                      conf.detail, dirty or not proven)
                if settled and conf.verdict == "ok":
                    return _ModelPick(True, conf.tier, badge, None, dirty)
                if settled and ok_rest and rested:
                    # The confirming read could not reach the controls; the RESTING composer is the
                    # authoritative surface on every build whose button carries the tier (the flat
                    # and pre-GPT-6 slider composers). Never the mid-walk slider observation — and
                    # only when the composer IS resting: `post` describes an open menu otherwise,
                    # and on this build an open switcher reads "Thinking effort".
                    return _ModelPick(True, shown_rest, badge, None, dirty)
                # The tier landed, but this attempt cannot be CONFIRMED: either the family behind
                # it was never established, or the authoritative controls could not be re-read and
                # the resting composer carries no tier to fall back on. Either way the attempt is
                # not a success, so the tier it moved goes back — a refusal that silently
                # retargets the composer is the same harm as the tier walk's own abandoned state.
                fam_detail = fam_detail or (None if settled else conf.detail)
                proven, back_label = _restore_original(
                    c, lab, orig_family, orig_tier_pos, fam.touched)
                dirty = dirty or not proven
                last_slider_label = back_label or last_slider_label
                _close_menus(c)
                continue
            # this WAS the candidate asked for, and it genuinely does not carry target — budget
            # must not be spent reopening it. Undo the whole transaction: the family this attempt
            # changed AND the tier, back to the ORIGINAL position rather than to _slider_set's own
            # entry, which was captured after the family change rebuilt the slider.
            if fam.touched or tier.dirty:
                proven, back_label = _restore_original(
                    c, lab, orig_family, orig_tier_pos, fam.touched)
                dirty = dirty or not proven
                last_slider_label = back_label or last_slider_label
            exhausted.add(lab)
            _close_menus(c)
        time.sleep(0.4)
    final = [x for x in (c.eval(_cand_labels_js()) or []) if x]
    if final:
        badge = final[0]
    ok, shown = _model_verdict(final, target)
    if not ok and last_slider_label:
        shown = last_slider_label
    if not family_checked:
        # The family reason outranks the tier's here even when the tier ALSO failed: an unreadable
        # or unlocatable model control means the tier was deliberately never driven behind it, so
        # "wanted Pro, switcher shows X" would describe a walk that never happened.
        return _ModelPick(False, shown, badge,
                          fam_detail or
                          "model family '%s' could not be verified — the composer's picker menu "
                          "never opened, so the radios were never readable" % family, dirty)
    return _ModelPick(bool(ok), shown, badge, None, dirty)


# A real code-source URL: https:// on github.com / gist.github.com / raw.githubusercontent.com,
# with at least one path segment after the host (a bare domain isn't a link to anything).
# Deliberately does NOT match the bare word "github" — that substring alone used to fail-open
# the no_code_source gate (spoofable by prose that merely mentions "GitHub").
_CODE_URL_RE = re.compile(
    r"https://(?:www\.)?(?:github\.com|gist\.github\.com|raw\.githubusercontent\.com)/\S+",
    re.IGNORECASE)

# Two sentence-sentinels prep stamps into the rendered prompt to declare a LEGITIMATE reason it
# carries no code LINK: a follow-up (the thread already holds the code) or a self-contained no-code
# question (maths/research/writing). Every source gate MUST honor BOTH — cgc_spool.validate_prompt
# does. Honoring only one is not "stricter", it is WRONG: it refuses a prompt prep authorized, at
# the moment of send. A prior version of the backstop below checked only the follow-up sentinel, so
# every `prep --no-code` consult passed the spool gate and was then refused here with
# no_code_source. Keep this predicate in lockstep with cgc_spool.validate_prompt's rule 1.
_FOLLOWUP_SENTINEL = "continuing this consult"
_NO_CODE_SENTINEL = "references no code"


def _has_sendable_source(prompt: str) -> bool:
    """The send-time mirror of cgc_spool.validate_prompt rule 1: a prompt is sendable iff it carries
    a real code LINK, or a sentinel declaring it legitimately needs none. A prose Context section is
    NOT the code — ChatGPT can't read the repo from a description and answers blind."""
    low = prompt.lower()
    return (bool(_CODE_URL_RE.search(prompt))
            or _FOLLOWUP_SENTINEL in low
            or _NO_CODE_SENTINEL in low)


def _egress_gate(prompt: str):
    """Run the full egress gate (public-repo verification + secret scan) over the exact bytes about
    to be sent. Delegates to cgc_spool.validate_prompt — the SAME validator the daemon uses — so the
    click owner enforces the boundary itself and a direct send cannot bypass it. Imported lazily to
    keep cdp_consult standalone-runnable; a gate that cannot load must FAIL CLOSED, never send."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import cgc_spool as _spool
        return _spool.validate_prompt(prompt)
    except Exception as e:  # noqa: BLE001 — a gate that errors must refuse, not wave the send through
        return False, f"refused: egress gate could not run ({type(e).__name__}: {e})"


# ChatGPT has shipped two turn schemas. Keep them as SEPARATE adapters rather than unioning the
# selectors: when both attributes are present at different levels of the DOM, one union query
# double-counts wrapper and content nodes and interleaves their order. Pick the family whose USER
# turns contain this request's rid, then read the page through that family alone.
_ADAPTERS = (
    ("data-turn-v1", '[data-turn="user"]', '[data-turn="assistant"]'),
    ("legacy-author-role-v1", '[data-message-author-role="user"]', '[data-message-author-role="assistant"]'),
)

# How long after the click the request's own user turn must appear, and how long after that some
# sign of generation must appear. These are failure DETECTORS, not deadlines — they never extend or
# replace STUCK_AFTER_S. Their whole purpose is to turn a DOM-contract break into a named failure in
# under a minute instead of a full silent hour, which is exactly what cost two complete consults.
_RID_LANDED_S = 10
_GENERATION_SIGNAL_S = 45


def _contract_js(rid):
    """Report, per adapter family: does a USER turn carry this rid, and is the assistant side alive?

    Counting user turns (what this used to do) only proves the page grew a message. It cannot tell
    'my prompt was sent' from 'something else appeared', and it says nothing about whether we can
    still READ the reply — the failure that burned two 25-minute rounds."""
    tag = json.dumps(f"BEGIN_RESPONSE:{rid}")
    fams = json.dumps([{"name": n, "u": u, "a": a} for n, u, a in _ADAPTERS])
    return ("(function(){var TAG=%s,F=%s,out=[];"
            "for(var i=0;i<F.length;i++){"
            "var us=document.querySelectorAll(F[i].u),as=document.querySelectorAll(F[i].a),hit=false;"
            "for(var j=0;j<us.length;j++){if((us[j].textContent||'').indexOf(TAG)>=0){hit=true;break;}}"
            "out.push({adapter:F[i].name,ridLanded:hit,users:us.length,assistants:as.length});}"
            "var gen=!!document.querySelector('[data-testid=\"stop-button\"],button[aria-label*=\"Stop\"]');"
            "return JSON.stringify({families:out,generating:gen});})()" % (tag, fams))


def _await_contract(c, rid):
    """After the click, hold the send to a contract instead of hoping.

    Returns (verdict, adapter, detail). Verdicts:
      "ok"            a user turn carries this rid AND the assistant side is observable
      "unknown_send"  no adapter can find the rid — this does NOT prove nothing was sent, so a
                      human must look; auto-resending could duplicate a 25-minute consult
      "selector_drift" the rid landed (so the send definitely happened) but no assistant turn or
                      generating indicator is recognizable — the send worked and our READER is
                      broken, which is a tool defect, not a missing answer
    """
    adapter, detail = None, {}
    end = time.time() + _RID_LANDED_S
    while time.time() < end:
        time.sleep(0.5)
        detail = json.loads(c.eval(_contract_js(rid)) or "{}")
        for fam in detail.get("families", []):
            if fam.get("ridLanded"):
                adapter = fam["adapter"]
                break
        if adapter:
            break
    if not adapter:
        return "unknown_send", None, detail
    end = time.time() + _GENERATION_SIGNAL_S
    while time.time() < end:
        detail = json.loads(c.eval(_contract_js(rid)) or "{}")
        fam = next((f for f in detail.get("families", []) if f["adapter"] == adapter), {})
        if fam.get("assistants", 0) > 0 or detail.get("generating"):
            return "ok", adapter, detail
        time.sleep(1.0)
    return "selector_drift", adapter, detail


# No <main>, no proof. A body-wide fallback would fire in exactly the situation this exists for —
# the adapters are blind because ChatGPT's DOM moved, and the same move could drop this element —
# silently restoring the whole-body scan that matches our rid in the sidebar of tabs that never held
# it. An unreadable page is an unproven one.
_JS_RID_IN_MAIN = """(function(r){var m=document.querySelector('main');
  return !!m && (m.innerText||'').indexOf(r)>=0;})(%s)"""


def _rid_in_main(c, rid) -> bool:
    """Is this round's rid in the CONVERSATION region of the page?

    Schema-independent on purpose: it is used exactly when the turn adapters failed, so it must not
    depend on them. Scoped to <main> because ChatGPT renders the thread LIST in every tab's nav, and
    a title derived from our own first message would otherwise match in tabs that never held it."""
    try:
        return bool(c.eval(_JS_RID_IN_MAIN % json.dumps(rid)))
    except Exception:
        return False


def _send_proven_by_landing(before_conv, after_conv, rid_in_page) -> bool:
    """Did this click provably send OUR prompt, without the turn adapters?

    Two independent facts, both required. The tab entered a thread it did not hold before — ChatGPT
    mints a conversation on send — AND our rid is rendered in that thread. The URL alone is not
    enough and naming it proof was wrong: `location.pathname` moves on plain NAVIGATION into an
    existing thread just as readily as on minting, so a no-op click plus any unrelated navigation
    inside the poll window would have been read as a successful send, silencing the one failure that
    is supposed to summon a human and pinning the global active-thread to a foreign conversation.
    The rid is what makes the landing OURS. A reused tab (before_conv set) proves nothing either way
    — sending does not change the id — so it stays uncertain."""
    return bool(after_conv) and not before_conv and bool(rid_in_page)


def _poll_conversation(c, seconds=20.0):
    """The attached tab's /c/<id>, waiting out the post-send URL transition (it lands a beat after
    the message). '' if the tab never entered a conversation."""
    end = time.time() + seconds
    while True:
        conv = c.conversation_id()
        if conv or time.time() >= end:
            return conv
        time.sleep(0.5)


def _write_private(path, text):
    """Write an answer/salvage file readable only by this user.

    The PROMPT is public by construction — the egress gate enforces that. The ANSWER is not: a reply
    can quote private context, and follow-up rounds carry local results outright. These land under a
    world-traversable /tmp, so leaving them at the umask's mercy made confidentiality a property of
    the host's configuration rather than of this tool."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# The composer is the input side of the same UI contract the post-send checks cover. It gets its own
# helper because the failure it used to produce — a bare `composer_not_ready` — told the caller
# nothing about WHY, and the caller's only guess was "tab not on ChatGPT or login lapsed". That sent
# a human off to re-log-in while they were already logged in and the page had merely not finished
# loading. A failure has to name which of those it actually is.
_COMPOSER_SEL = 'div[role="textbox"][contenteditable="true"],#prompt-textarea'


def _composer_state_js():
    js = """(function(){return JSON.stringify({
      composer: !!document.querySelector(__SEL__),
      onChatGPT: /(^|\\.)chatgpt\\.com$/.test(location.hostname),
      loginWall: !!document.querySelector('input[type="password"]')
                 || /^\\/(auth|login)(\\/|$)/i.test(location.pathname),
      captcha: !!document.querySelector('iframe[src*="captcha" i],iframe[title*="captcha" i]'),
      readyState: document.readyState,
      path: location.pathname.slice(0,60)
    });})()"""
    return js.replace("__SEL__", json.dumps(_COMPOSER_SEL))


def _await_composer(c, seconds=60):
    """Wait for the composer, and on failure say which thing is actually wrong.

    Chrome is often started by this very command, so the project page can still be loading when the
    first probe runs; 15s was too tight for a cold profile under a launchd-spawned browser."""
    st = {}
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        try:
            st = json.loads(c.eval(_composer_state_js()) or "{}")
        except Exception:
            st = {}
        if st.get("composer"):
            return True, st
        if st.get("loginWall") or st.get("captcha"):
            return False, st
    return False, st


def _composer_failure(st):
    """(exit_code, message). Login/captcha need a human; anything else is ours to retry."""
    if st.get("loginWall"):
        return 3, ("CGC_ERROR login_needed: the debug Chrome is showing a login page. A human must "
                   "log into ChatGPT in that window — the agent never types credentials.")
    if st.get("captcha"):
        return 3, ("CGC_ERROR captcha: ChatGPT is showing a challenge. A human must clear it in the "
                   "debug Chrome window.")
    if not st.get("onChatGPT", True):
        return 1, (f"CGC_ERROR wrong_page: the tab is not on chatgpt.com (path {st.get('path')!r}). "
                   "Nothing was sent.")
    return 1, ("CGC_ERROR composer_not_ready: the ChatGPT page never finished producing its input "
               f"box (readyState={st.get('readyState')!r}, path={st.get('path')!r}). "
               "**Login is NOT the problem** — do not ask anyone to re-log-in on account of this. "
               "The page did not load in time; re-enqueue and it will retry.")


def _read_prompt(path):
    """Read the prompt to send. `-` means stdin, which is how the daemon hands over the EXACT bytes
    its egress gate validated.

    Reopening the path here would reintroduce a TOCTOU hole in that gate: the daemon reads the file
    to validate it, and any same-user process could rewrite the file before this process opened it,
    so the bytes sent to ChatGPT need not be the bytes that passed the public-repo and secret checks.
    The gate is the whole justification for this tool's egress design, so it must cover the bytes
    that actually leave, not a filename that once contained them."""
    if path == "-":
        return sys.stdin.read()
    return open(path, encoding="utf-8").read()


# ---- paste + submit (shared by cmd_submit and cmd_followup) -----------------
# FIELD INCIDENT (recorded twice): a single-shot Runtime.evaluate pasting a multi-KB prompt into a
# composer sitting atop a heavy, freshly-rehydrated DOM (~30KB of prior turns) can have its CDP reply
# outrun the websocket read timeout in call() even though the page-side execCommand('insertText')
# itself completed — the exception then killed the worker BEFORE any click code ran, yet the round
# was classified possibly_accepted (uncertain), forcing a manual reconcile even though the send
# provably never happened. Fix: paste in bounded CHUNKS (no single reply is at the mercy of a heavy-
# DOM stall) inside one explicit PRE-CLICK region (_paste_prompt) whose only failure mode is
# _PreClickFailure — the caller's unambiguous signal that the click was never issued.
EXIT_NOT_SENT_PRECLICK = 6  # cdp_consult's own pre-click boundary proved the send click never fired
_PASTE_CHUNK_CHARS = 2000
_WS_RUN_RE = re.compile(r"\s+")


class _PreClickFailure(Exception):
    """Raised only for a failure PROVEN to precede the send click — never after it."""


def _composer_get_js():
    return "document.querySelector(" + json.dumps(_COMPOSER_SEL) + ")"


def _settle_composer(c, seconds=5):
    """Best-effort bounded wait past the composer's first appearance: on a server-side reopen of a
    heavy conversation the composer node can exist before the surrounding page finishes hydrating, so
    give it a beat to report ready before the first paste chunk races it. Timing out here is not
    itself a failure — the chunked paste + length verification below is the real backstop."""
    js = ("(function(){var d=" + _composer_get_js() + ";return !!d && !d.disabled && "
          "!d.getAttribute('aria-disabled') && document.readyState==='complete';})()")
    end = time.time() + seconds
    while time.time() < end:
        try:
            if c.eval(js):
                return
        except Exception:
            pass
        time.sleep(0.3)


def _clear_composer_js():
    # Count only meaningful residue: an EMPTY ProseMirror composer keeps a placeholder paragraph
    # whose innerText is "\n" (observed live — a raw length check misread it as a leftover draft).
    return ("(function(){var d=" + _composer_get_js() + ";if(!d)return -1;d.focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);"
            "return (d.innerText||'').replace(/[\\s\\u200b\\u200c\\ufeff]/g,'').length;})()")


def _paste_chunk_js(chunk):
    return ("(function(){var d=" + _composer_get_js() + ";if(!d)return -1;d.focus();"
            "document.execCommand('insertText',false," + json.dumps(chunk) + ");"
            "return (d.innerText||'').length;})()")


def _composer_text_js():
    return "(function(){var d=" + _composer_get_js() + ";return d?(d.innerText||''):null;})()"


def _paste_prompt(c, prompt: str) -> None:
    """Clear the composer and paste `prompt` in bounded chunks via execCommand('insertText') — kept
    (never a raw textContent/value assignment, which would not drive ChatGPT's React composer state)
    so a leftover draft from a previous failed attempt can never be PREPENDED to this paste. Every
    step here runs strictly BEFORE the send click; any failure — a raised exception or a failed
    length verification — is raised as _PreClickFailure so the caller can report the distinct
    provably-not-sent exit code instead of the uncertain unknown_send/crash path."""
    try:
        _settle_composer(c)
        cleared = c.eval(_clear_composer_js(), timeout=45)
        if cleared is None or cleared < 0:
            raise _PreClickFailure("composer not found while clearing it")
        if cleared != 0:
            raise _PreClickFailure(f"composer still holds {cleared} chars after clear — refusing to "
                                   "paste onto a leftover draft")
        for i in range(0, len(prompt), _PASTE_CHUNK_CHARS):
            chunk = prompt[i:i + _PASTE_CHUNK_CHARS]
            got = c.eval(_paste_chunk_js(chunk), timeout=45)
            if got is None or got < 0:
                raise _PreClickFailure(f"composer disappeared mid-paste (chunk offset {i})")
        got_text = c.eval(_composer_text_js(), timeout=45)
    except _PreClickFailure:
        raise
    except Exception as e:
        # Anything else here — including the recorded websocket read TimeoutError — is still, by
        # construction, strictly pre-click: no code past this function has run yet.
        raise _PreClickFailure(f"{type(e).__name__} during paste: {e}") from e
    if got_text is None:
        raise _PreClickFailure("composer not found for post-paste verification")
    # Length, not equality: ChatGPT's contenteditable composer normalizes newline runs (observed to
    # collapse/expand \n differently than a plain textarea would), so collapse all whitespace on both
    # sides and require the composer to hold AT LEAST as much content as the prompt — a shortfall is
    # exactly the truncated-by-a-DOM-stall signature this whole helper exists to catch.
    want_len = len(_WS_RUN_RE.sub(" ", prompt).strip())
    got_len = len(_WS_RUN_RE.sub(" ", got_text).strip())
    if got_len < want_len:
        raise _PreClickFailure(
            f"composer holds {got_len} normalized chars, prompt needs >= {want_len} — paste "
            "looks truncated by a DOM stall")


def cmd_submit(a) -> int:
    prompt = _read_prompt(a.prompt_file)
    # Backstop (prep already hard-blocks at render; the spool gate re-checks): refuse to send a
    # prompt that has neither a real CODE LINK nor a sentinel declaring it legitimately needs none.
    # A prose Context section is NOT the code — ChatGPT can't read the repo from a description and
    # answers blind ("no file access / can't cite file:line"). PROVENANCE: a real https URL on
    # github.com/gist.github.com/raw.githubusercontent.com, NOT the bare substring "github" (which a
    # prose mention used to spoof under the old `"github" in low` gate). No override — fail closed.
    if not _has_sendable_source(prompt):
        sys.stderr.write(
            "CGC_ERROR no_code_source: this prompt has no code link (no github/gist URL) — ChatGPT "
            "cannot read your repo and would answer BLIND (the recurring 'no file access' failure; a "
            "prose Context section is NOT the code). Deliver the link first: consult.py deliver "
            "--repo <owner/repo> --ref <sha> (whole-repo /tree link) → pass its refs_file to prep "
            "--refs-file. If the question has NO code subject at all (maths/research/writing), render "
            "it with `prep --no-code`. NOT submitting.\n")
        return 2
    # EGRESS GATE (defense in depth). The click owner independently re-validates the EXACT bytes it
    # is about to send — public-repo verification + secret scan, fail-closed — so a DIRECT
    # cdp_consult invocation cannot bypass the daemon's gate and exfiltrate a private-repo URL or
    # secret-bearing text. On the daemon path this re-checks what process_round already validated;
    # one extra gh call is negligible against a consult, and "the daemon already checked" cannot be
    # trusted from an untrusted direct caller (diff-review S0). No skip flag — a forgeable "already
    # gated" bit would reopen the hole.
    _ok, _reason = _egress_gate(prompt)
    if not _ok:
        sys.stderr.write(f"CGC_ERROR gate_refused: {_reason} — NOT submitting.\n")
        return 2
    if not a.no_gate:
        _ensure_chrome(a.port)  # automated Step 0: start debug Chrome if down + check login
    # Tab policy: each consult gets its OWN dedicated tab by default, so multiple
    # consults run concurrently without clobbering each other's conversation. The
    # waiter pins to this tab's conversation id and (by default) CLOSES it after
    # retrieving the answer, so tabs don't accumulate. Pass --reuse-tab to instead
    # navigate the existing single tab (only safe when no other consult is in flight).
    if a.reuse_tab:
        c = CDP(a.port)
        c.call("Page.navigate", {"url": a.project_url})
    else:
        c = CDP(a.port, create_url=a.project_url)
    try:
        # (The old reuse-tab user-message COUNT guard lived here. It became dead when the post-send
        # check moved to _await_contract, which matches THIS request's rid inside a user turn — an
        # identity check that stale history cannot satisfy, so counting is no longer load-bearing.
        # It was left computing a value nobody read, next to a comment describing a guard that no
        # longer existed.)
        ready, _cstate = _await_composer(c)
        if not ready:
            _code, _msg = _composer_failure(_cstate)
            c.close_tab()   # nothing was sent and this page never loaded — do not leak it
            sys.stderr.write(_msg + "\n")
            return _code
        # Model selection — fully automated (must run AFTER navigate, which resets the
        # model to the project default). Tries each switcher menu until the target tier
        # is selected. FAIL-CLOSED: if it genuinely cannot select the target, do NOT send.
        model_confirmed = None
        model_now = None
        model_badge = None
        if a.model and a.model.lower() != "skip":
            target = a.model
            pick = _select_model(c, target, a.model_family)
            model_confirmed, model_now, model_badge = pick.confirmed, pick.shown, pick.badge
            if not model_confirmed and not a.allow_model_mismatch:
                if not a.reuse_tab:
                    c.close_tab()  # don't orphan the dedicated tab we opened for this submit
                c.close()
                print(json.dumps({"ok": False, "submitted": False,
                                  "modelConfirmed": False, "model": model_now,
                                  "modelBadge": model_badge, "wanted": target,
                                  "wantedFamily": a.model_family, "detail": pick.error}))
                # pick.error is set only when the MODEL FAMILY is what failed — a different
                # problem with a different fix than a tier miss, so it gets its own sentence
                # instead of being flattened into the tier wording.
                why = pick.error or (f"wanted '{target}', switcher shows '{model_now}' and it "
                                     f"could not be changed")
                if pick.dirty:
                    # "Nothing was sent" and "nothing was changed" are separate properties. A
                    # refusal that only reports the first leaves a human believing an untouched
                    # tab, and the next run inherits a composer nobody knows the state of.
                    why += (" (and the composer's model controls could NOT be proven restored to "
                            "the state they were found in — the tab is left dirty)")
                fix = ("set CGC_MODEL_FAMILY to a family this account offers (or 'skip')"
                       if pick.error else f"set a '{target}' tier in the ChatGPT window")
                sys.stderr.write(
                    f"CGC_ERROR model_not_selectable: {why} — NOT submitting. Fix: {fix}, or "
                    f"pass --allow-model-mismatch.\n")
                return 3
            if not model_confirmed:
                sys.stderr.write(f"CGC_WARN proceeding on '{model_now}' not '{target}' "
                                 f"(--allow-model-mismatch)\n")
            time.sleep(0.3)
        # PRE-CLICK boundary: everything up to and including paste verification is provably pre-send.
        # Any failure here — including a crash — proves the click was never issued, so it is caught
        # and reported via the distinct not-sent exit code, never the uncertain unknown_send path (the
        # recorded field incident: an insertText's CDP reply outran the websocket read timeout and the
        # resulting exception used to be indistinguishable from a post-click crash).
        try:
            _paste_prompt(c, prompt)
        except _PreClickFailure as e:
            c.close_tab()   # nothing was sent; don't leak the tab
            sys.stderr.write(f"CGC_ERROR not_sent_preclick: {e} — NOT submitted; safe to retry with "
                             "the same --request-key.\n")
            return EXIT_NOT_SENT_PRECLICK
        time.sleep(0.3)
        # Pre-click thread identity. A fresh tab sits on /project with no /c/<id>; a reused tab may
        # already be inside one. ChatGPT mints a conversation when a message is SENT and at no other
        # time, so ""->/c/<id> across the click is send evidence that lives in the URL rather than in
        # the DOM — the one witness that survives when the turn schema moves and the rid echo becomes
        # unreadable. Captured here because after the click it is no longer distinguishable from a
        # thread the tab was already in.
        # Caught, not raised: this read sits in the pre-click window, where an escaping CDP timeout
        # would exit without the not_sent_preclick marker and turn a provably re-queueable round into
        # an uncertain one. An unknown before-state costs only the URL upgrade below (which requires
        # it to be empty), so failing it closed to "unknown" is strictly the safe direction.
        try:
            before_conv = c.conversation_id()
        except Exception:
            before_conv = "?unknown"
        # Submit. Prefer the send button; fall back to Enter. (POST-click from here: a failure past
        # this point is uncertain, never provably-not-sent.)
        clicked = c.eval(
            "(function(){var b=document.querySelector('button[data-testid=\"send-button\"],"
            "button[aria-label*=\"Send\" i],button[aria-label*=\"\\u53d1\\u9001\"]');"
            "if(b&&!b.disabled){b.click();return true;}return false;})()")
        if not clicked:
            c.key("Enter", "Enter", 13)
        # Confirm a user message actually landed (poll — render lags the click). On a reused
        # tab, requiring merely "count > 0" would false-positive on OLD messages already in
        # the thread if THIS send's composer insert/click silently failed — so require the
        # count to have grown past before_user_count there. A fresh tab always starts at 0,
        # so ">0" and "> before_user_count" are equivalent and new-tab behavior is unchanged.
        verdict, adapter, contract = _await_contract(c, a.rid)
        # Post-click, the conversation id is the round's ADDRESS: every recovery path (retrieve,
        # auto-retrieve, followup) is addressed by conversation, so a round that may have sent but
        # carries no conversation is unrecoverable by construction — that is exactly how a live,
        # still-generating answer once became a manual copy-paste. So resolve it on EVERY post-click
        # outcome, not just the clean one, and report it in every envelope below.
        conv = _poll_conversation(c)
        if verdict == "unknown_send" and _send_proven_by_landing(
                before_conv, conv, _rid_in_main(c, a.rid)):
            # The turn adapters are blind, but the thread is new AND our rid is rendered in it. That
            # is the send, read without them; the reader, not the send, is what failed. Downgrading
            # it to `unknown_send` would strand a confirmed consult as uncertain — and the waiter
            # re-verifies END_RESPONSE:<rid> anyway, so nothing here is taken on trust that the
            # answer itself won't have to prove.
            sys.stderr.write(
                f"CGC_WARN rid_echo_unreadable: no turn schema showed BEGIN_RESPONSE:{a.rid}, but "
                f"the tab entered conversation {conv} across the click (it held none before) and "
                f"{a.rid} is rendered in it. Treating the send as landed; the waiter still verifies "
                f"END_RESPONSE:{a.rid}. The turn adapters need updating.\n")
            verdict, adapter = "ok", adapter or "url_transition"
        if verdict == "unknown_send":
            # Deliberately NOT close_tab(): this outcome exists because a human has to look
            # at this window to see whether the prompt actually went. Closing it destroys
            # the only evidence. It is the one failure allowed to leave a tab behind.
            c.close()
            print(json.dumps({"ok": False, "submitted": None, "rid": a.rid,
                              "reason": "unknown_send", "contract": contract,
                              "conversation_id": conv}))
            sys.stderr.write(
                "CGC_ERROR unknown_send: clicked send, but no known turn schema shows a user turn "
                f"carrying BEGIN_RESPONSE:{a.rid} within {_RID_LANDED_S}s. This does NOT prove the "
                "prompt was not sent, so it will NOT be resent automatically — a duplicate would "
                "cost another full round. A human should look at the ChatGPT window.\n"
                + (f"The tab is in conversation {conv} (it was already there before the click, so "
                   "this is an address, not proof) — retrieve from it before considering a resend.\n"
                   if conv else "")
                + f"observed: {json.dumps(contract)}\n")
            return 3
        if verdict == "selector_drift":
            c.close_tab()   # the send is confirmed and the conversation persists server-side
            print(json.dumps({"ok": False, "submitted": True, "rid": a.rid,
                              "reason": "selector_drift", "adapter": adapter, "contract": contract,
                              "conversation_id": conv}))
            sys.stderr.write(
                f"CGC_ERROR selector_drift: the prompt WAS sent (its rid is in a {adapter} user "
                f"turn), but no assistant turn or generating indicator is recognizable within "
                f"{_GENERATION_SIGNAL_S}s. ChatGPT's DOM has moved and this tool can no longer read "
                "replies — a tool defect, not a missing answer. Do not resend; fix the adapter.\n"
                f"observed: {json.dumps(contract)}\n")
            return 1
        # SUBMIT-RACE: a submit that reports ok=true with no captured conversation id is worse than a
        # clean failure — a later `wait`/`followup --conversation auto` would resolve to whatever
        # OTHER tab/thread is active and silently answer the wrong request. So if the URL never
        # transitioned, fail closed: do NOT write active-thread state (that would point `auto` at a
        # request with no known tab) and do NOT return success.
        n = 1
        if not conv:
            print(json.dumps({"ok": False, "userMsgs": n, "model": model_now,
                              "modelBadge": model_badge,
                              "modelConfirmed": model_confirmed, "conversation_id": ""}))
            sys.stderr.write(
                "CGC_ERROR no_conversation_id: the message sent but the tab never transitioned to "
                "/c/<id> within the grace window — NOT recording active-thread state (a follow-up/"
                "wait using --conversation auto would otherwise silently target the wrong thread). "
                "Recovery: retry submit, or attach manually with `status --conversation <id>` once "
                "the tab's URL shows a /c/<id>.\n")
            return 2
        _write_state(conversation=conv, rid=a.rid)  # so `followup`/`wait` can auto-resolve
        print(json.dumps({"ok": True, "userMsgs": n, "model": model_now, "adapter": adapter,
                          "modelBadge": model_badge,
                          "modelConfirmed": model_confirmed, "conversation_id": conv}))
        out = os.path.join(CGC_STATE_DIR, f"answer_{a.rid}.txt")
        # Hand the agent the exact waiter to run (run_in_background:true). It holds the whole
        # consult itself — no wrapper, no slicing. The 899/870 pair this used to print existed for
        # a 900s background-task cap that was measured not to exist.
        sys.stderr.write(
            "CGC_SUBMITTED. Now run the detached waiter (run_in_background:true) — copy verbatim:\n"
            f"  python3 {os.path.abspath(__file__)} wait --rid {a.rid} "
            f"--conversation {conv} --out {out}\n"
            "NEVER `nohup … & disown`: an untracked process's exit does NOT wake you, so the "
            "answer lands silently and you wait forever.\n")
        return 0
    finally:
        c.close()


def _render_followup(a):
    """One-shot ergonomics: if the caller gave --task instead of a pre-rendered
    --prompt-file, render the follow-up prompt here by shelling out to
    `consult.py prep --followup`. Returns (prompt_file, rid)."""
    consult = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consult.py")
    cmd = [sys.executable, consult, "prep", "--followup", "--task", a.task]
    if a.title:
        cmd += ["--title", a.title]
    if a.context_file:
        cmd += ["--context-file", a.context_file]
    if a.refs_file:
        cmd += ["--refs-file", a.refs_file]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"CGC_ERROR followup_prep_failed: consult.py prep --followup exited "
                         f"{r.returncode}: {r.stderr.strip()}")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise SystemExit(f"CGC_ERROR followup_prep_unparseable: {r.stdout[:400]}")
    return out["prompt_file"], out.get("request_id")


_PROMPT_FILE_RID_RE = re.compile(r"^BEGIN_RESPONSE:(REQ-\d{8}-\d{6}-[0-9a-f]{6})\s*$", re.MULTILINE)


def _extract_rid_from_prompt_file(prompt_file: str) -> str:
    """LEGACY-PATH RID GUARD: when `followup --prompt-file` is given with no `--rid`, this is the
    ONLY place that rid comes from. Scan the prompt file for lines matching
    'BEGIN_RESPONSE:REQ-...'. Exactly ONE distinct rid must be present — zero or more-than-one is a
    hard failure BEFORE sending, since the caller has no way to know which round the prompt actually
    belongs to and a wrong guess would let `wait --rid auto` watch a stale round."""
    try:
        text = _read_prompt(prompt_file)
    except OSError as e:
        raise SystemExit(f"CGC_ERROR prompt_file_unreadable: {prompt_file}: {e}")
    rids = sorted(set(m.group(1) for m in _PROMPT_FILE_RID_RE.finditer(text)))
    if len(rids) == 0:
        raise SystemExit(
            f"CGC_ERROR rid_not_found_in_prompt_file: {prompt_file} has no bare "
            "'BEGIN_RESPONSE:REQ-...' line — pass --rid explicitly or use a prompt file rendered "
            "by consult.py prep --followup.")
    if len(rids) > 1:
        raise SystemExit(
            f"CGC_ERROR ambiguous_rid_in_prompt_file: {prompt_file} contains {len(rids)} distinct "
            f"rids ({', '.join(rids)}) — pass --rid explicitly to disambiguate.")
    return rids[0]


def cmd_followup(a) -> int:
    """Continue a consult THREAD (ChatGPT keeps the conversation's full context + model).
    Two ways to call it, both zero-bookkeeping:
      • One-shot:  followup --task "<results + next ask>" [--title ..] [--context-file ..] [--refs-file ..]
                   (renders the follow-up prompt for you — no separate `prep` step)
      • Explicit:  followup --prompt-file <from consult.py prep --followup>
    --conversation defaults to the active thread (the last submit/followup); pass it
    explicitly only when juggling several consults. If the tab was closed, the
    conversation is RE-OPENED server-side at /c/<id> — --keep-tab is never required.
    After sending, prints the ready-to-run `wait` command (conv + rid pre-filled)."""
    if not a.no_gate:
        _ensure_chrome(a.port)  # automated Step 0: ensure debug Chrome is up + logged in
    conv = _resolve_conv(a.conversation)
    if not conv:
        raise SystemExit("CGC_ERROR no_active_thread: no --conversation given and no active "
                         "consult on record. Pass --conversation <id> from the original submit.")
    if not a.prompt_file and not a.task:
        raise SystemExit("CGC_ERROR followup_no_input: give either --task \"<results + next ask>\" "
                         "(one-shot, renders for you) or --prompt-file <pre-rendered>.")
    rid = a.rid
    if a.prompt_file:
        prompt_file = a.prompt_file
        if not rid:
            # LEGACY PATH: --prompt-file with no --rid used to skip the rid-echo check entirely
            # and hand `wait --rid auto` a rid resolved AFTER send (which can watch a stale round).
            # Parse the one rid the prompt file itself declares so the SAME echo check the
            # one-shot path gets is enforced here too.
            rid = _extract_rid_from_prompt_file(prompt_file)
            sys.stderr.write(f"CGC_FOLLOWUP rid parsed from prompt file: {rid}\n")
    else:
        prompt_file, rid = _render_followup(a)
        sys.stderr.write(f"CGC_FOLLOWUP rendered {prompt_file} (rid {rid})\n")
    prompt = _read_prompt(prompt_file)
    # EGRESS GATE (defense in depth) — same as submit: the click owner re-validates the exact bytes,
    # so a direct followup cannot bypass the daemon's gate. A followup prompt is rule-1-exempt (the
    # thread already holds the code) but still secret-scanned and public-repo-checked for any NEW
    # link it introduces (diff-review S0).
    _ok, _reason = _egress_gate(prompt)
    if not _ok:
        sys.stderr.write(f"CGC_ERROR gate_refused: {_reason} — NOT sending the follow-up.\n")
        return 2
    # A consult is a THREAD: the conversation persists server-side at /c/<id> even after
    # its tab is closed (default), so follow-up must NOT depend on round-1 having kept the
    # tab. Attach to a live tab if one exists; otherwise RE-OPEN the conversation by URL.
    reopened = False
    try:
        c = CDP(a.port, match=conv)
    except SystemExit as e:
        if "conversation_not_found" in str(e):
            sys.stderr.write(f"CGC_FOLLOWUP tab gone — re-opening conversation {conv} "
                             f"server-side (thread persists; --keep-tab was not required)\n")
            c = CDP(a.port, create_url=f"https://chatgpt.com/c/{conv}")
            reopened = True
        else:
            raise
    try:
        # composer should already be present at the bottom of an existing conversation
        # (re-opened tabs need a beat longer to hydrate the thread + composer)
        ready, _cstate = _await_composer(c)
        if not ready:
            _code, _msg = _composer_failure(_cstate)
            sys.stderr.write(_msg + "\n")
            return _code
        # CONVERSATION-INTEGRITY GUARD (before any model-selection/insert): after attaching to a
        # live tab or re-opening the closed thread above, the page's URL can lag a beat behind
        # the target /c/<conv> (freshly-created tab still on about:blank/transitional URL, or a
        # re-open that briefly redirects). Poll for EXACT equality — not substring — before doing
        # anything that could land on the WRONG thread. Bounded wait; fail closed without sending.
        gdl = time.time() + 20
        while c.conversation_id() != conv and time.time() < gdl:
            time.sleep(0.5)
        if c.conversation_id() != conv:
            print(json.dumps({"ok": False, "followup": True, "conversation_id": c.conversation_id(),
                              "rid": rid, "wanted_conversation": conv}))
            sys.stderr.write(
                f"CGC_ERROR conversation_mismatch: attached tab is at conversation "
                f"'{c.conversation_id() or '(none)'}', not the expected '{conv}' — NOT sending "
                f"(would land the follow-up on the wrong thread).\n")
            return 2
        # MODEL GATE (mirrors submit; fail-closed). A thread can silently downgrade to a
        # non-Pro tier mid-session — Pro quota exhausted, ChatGPT auto-falls back to Instant —
        # and a follow-up used to INHERIT that blindly ("keep the thread's model"), answering
        # on Instant with no check. That is the degraded-consult hole. Re-assert the target
        # tier on the thread BEFORE inserting the prompt; refuse to send on the wrong model
        # unless --allow-model-mismatch. Runs before insert so the menu clicks can't clobber
        # composer text.
        model_badge = None
        if a.model and a.model.lower() != "skip":
            target = a.model
            if reopened:
                # ROBUSTNESS: a freshly re-opened composer reports the model-tier switcher as
                # None on the first probes (the switcher button hasn't hydrated yet) — reading
                # that as "unselectable" is the recurring false refusal on reopen. Wait up to
                # ~8s for the switcher to hydrate before letting _select_model act; only if it
                # NEVER resolves do we fall through to the fail-closed path below. (_select_model
                # itself then opens the picker and selects — this settle just gives it a live DOM
                # to act on; the fail-closed guarantee is unchanged.)
                _settle = time.time() + 8
                while not (c.eval(_cand_labels_js()) or []) and time.time() < _settle:
                    time.sleep(0.5)
            pick = _select_model(c, target, a.model_family)
            _fu_ok, model_now, model_badge = pick.confirmed, pick.shown, pick.badge
            if not _fu_ok and not a.allow_model_mismatch:
                print(json.dumps({"ok": False, "followup": True, "conversation_id": conv,
                                  "rid": rid, "modelConfirmed": False, "model": model_now,
                                  "modelBadge": pick.badge, "wanted": target,
                                  "wantedFamily": a.model_family, "detail": pick.error}))
                why = pick.error or (f"thread offers '{model_now}', not '{target}'")
                if pick.dirty:
                    why += (" (and the composer's model controls could NOT be proven restored to "
                            "the state they were found in — the tab is left dirty)")
                fix = ("set CGC_MODEL_FAMILY to a family this account offers (or 'skip')"
                       if pick.error else f"restore the '{target}' tier in the ChatGPT window")
                sys.stderr.write(
                    f"CGC_ERROR model_not_selectable: {why} — NOT sending this follow-up (it "
                    f"would answer on a DEGRADED model, the Instant-stall failure). Fix: {fix}, "
                    f"or pass --allow-model-mismatch to override.\n")
                return 2
        # Count user messages BEFORE sending so we can confirm a NEW one landed (the
        # thread already has >=1 user message, so an absolute >0 check would false-pass).
        u_before = c.eval("document.querySelectorAll(" + _JS_U + ").length") or 0
        # PRE-CLICK boundary (see _paste_prompt) — a failure here is provable proof the follow-up was
        # never sent, so it is reported via the distinct not-sent exit code instead of falling into
        # possibly_accepted (the recorded field incident happened on exactly this path: re-opening a
        # heavy conversation server-side, then a paste whose CDP reply outran the read timeout).
        # Model already gated above.
        try:
            _paste_prompt(c, prompt)
        except _PreClickFailure as e:
            sys.stderr.write(f"CGC_ERROR not_sent_preclick: {e} — NOT sent; safe to retry with the "
                             "same --request-key.\n")
            return EXIT_NOT_SENT_PRECLICK
        time.sleep(0.3)
        clicked = c.eval(
            "(function(){var b=document.querySelector('button[data-testid=\"send-button\"],"
            "button[aria-label*=\"Send\" i],button[aria-label*=\"\\u53d1\\u9001\"]');"
            "if(b&&!b.disabled){b.click();return true;}return false;})()")
        if not clicked:
            c.key("Enter", "Enter", 13)
        # Landed-confirmation: on a long thread the DOM virtualizes older turns, so the rendered
        # user-turn COUNT can stay flat forever after a real send (observed live: a landed follow-up
        # returned ok:false because the render window kept its size). Count-increase is only the
        # fast path; the authoritative signal is the LAST rendered user turn echoing OUR rid.
        n = u_before
        echoed = ""
        grew = False
        _sdl = time.time() + 30
        while time.time() < _sdl:
            time.sleep(0.5)
            n = c.eval("document.querySelectorAll(" + _JS_U + ").length") or 0
            grew = grew or n > u_before
            if not rid:
                if grew:        # nothing to verify against; the count is all the evidence there is
                    break
                continue
            echoed = _turn_canonical_rid(c.eval(_last_user_text_js()) or "") or ""
            if echoed == rid:
                break
        # Count growth is a HINT, not an exit: the node can appear a beat before its text hydrates,
        # and breaking on it left the authoritative rid read below as a single shot against an empty
        # turn — a landed follow-up reported as rid_echo_mismatch, i.e. a false uncertain on a send
        # that did happen. Keep polling for the canonical echo and let the deadline end the loop.
        ok = grew or bool(rid and echoed == rid)
        # CONVERSATION-INTEGRITY GUARD (after send): require the EXACT same conversation still
        # holds — do NOT silently adopt whatever conv the page now reports (that would let a
        # mid-send navigation/redirect to a DIFFERENT thread pass unnoticed, with the follow-up
        # believed sent into `conv` while it actually landed elsewhere, or vice versa).
        # SETTLE: right after send the SPA can briefly report location.pathname as a transitional
        # value (conversation_id() == '') for a beat before it re-settles on /c/<conv>. Reading
        # once there false-positived a mismatch on a send that actually stayed on the right thread,
        # so poll for the id to come back to `conv` (or to a genuinely DIFFERENT non-empty id)
        # before deciding — a real thread switch resolves to another id and still fails closed.
        after_conv = c.conversation_id()
        _adl = time.time() + 6
        while after_conv != conv and after_conv == "" and time.time() < _adl:
            time.sleep(0.5)
            after_conv = c.conversation_id()
        if after_conv != conv:
            print(json.dumps({"ok": False, "userMsgs": n, "conversation_id": after_conv,
                              "rid": rid, "followup": True, "wanted_conversation": conv}))
            sys.stderr.write(
                f"CGC_ERROR conversation_mismatch: after sending, the page is at conversation "
                f"'{after_conv or '(none)'}', not the expected '{conv}' — the thread changed "
                f"mid-send. NOT adopting the new id.\n")
            return 2
        if not ok:
            print(json.dumps({"ok": False, "userMsgs": n, "conversation_id": conv,
                              "rid": rid, "followup": True}))
            return 2
        # RID-ECHO VERIFICATION: before proceeding to watch, confirm the NEW last-user-message
        # actually contains the expected BEGIN_RESPONSE:<rid> echo. Without this, a race (the
        # click landed but the composer text was stale, or a concurrent consult's message
        # interleaved) would have the waiter watch a STALE/OLD round and either time out or
        # (worse) extract a previous answer under this rid. `rid` is always known here: one-shot
        # rendering yields one, and the explicit --prompt-file path without --rid now parses it
        # from the prompt file itself (_extract_rid_from_prompt_file, which fails closed before
        # send if the file has zero or multiple distinct rids) — so this check always runs.
        if rid and echoed != rid:
            # Only re-read when the loop never saw our echo — a match it already polled is the same
            # fact, and re-reading it can only lose to a turn that arrived in between.
            echoed = _turn_canonical_rid(c.eval(_last_user_text_js()) or "") or ""
        if rid:
            if echoed != rid:
                print(json.dumps({"ok": False, "userMsgs": n, "conversation_id": conv,
                                  "rid": rid, "followup": True, "echoedRid": echoed}))
                sys.stderr.write(
                    f"CGC_ERROR rid_echo_mismatch: sent follow-up for rid '{rid}' but the last user "
                    f"message on the page echoes '{echoed or '(none)'}' — this looks like a stale/old "
                    f"round, not the one just sent. NOT watching. Retry the follow-up, or verify no "
                    f"other consult is concurrently writing into this same conversation.\n")
                return 2
        _write_state(conversation=conv, rid=rid)  # keep the active thread current
        default_out = os.path.join(CGC_STATE_DIR, f"answer_{rid}.txt" if rid else "answer_followup.txt")
        # modelBadge here is the same PRE-SEND selection evidence submit reports (SHARED CONTRACT
        # #4): a follow-up re-asserts the tier/family on the thread, so it has its own selection
        # evidence and its own round row to carry it. It is NOT attribution — `wait`'s modelSlug is.
        print(json.dumps({"ok": True, "userMsgs": n, "conversation_id": conv, "rid": rid,
                          "followup": True, "modelBadge": model_badge,
                          "wait_out": default_out, "watching": bool(a.watch)}))
    finally:
        c.close()
    if a.watch:
        # Same process now waits for the answer → the agent runs ONE backgrounded command for
        # the whole round (send+wait) and its exit IS the wake. No separate `wait` to mis-arm.
        a.conversation = conv
        a.rid = rid or "auto"
        if not a.out:
            a.out = default_out
        sys.stderr.write(f"CGC_FOLLOWUP sent — now watching inline (out {a.out})\n")
        return cmd_wait(a)
    sys.stderr.write(
        "CGC_FOLLOWUP sent. Run the detached waiter (run_in_background:true) — conv+rid pre-filled:\n"
        f"  python3 {os.path.abspath(__file__)} wait --rid {rid or 'auto'} "
        f"--conversation {conv} --out {default_out}\n"
        "(Or skip this: pass --watch --out <file> to followup so send+wait is ONE backgrounded command.)\n")
    return 0


def cmd_wait(a) -> int:
    deadline = time.time() + a.timeout
    conv = _resolve_conv(a.conversation)  # 'auto'/None → the active thread
    # The detached waiter can launch a beat before the tab transitions to /c/<id> (submit
    # warns it may not capture the conversation id instantly), so RETRY the attach instead of
    # dying at birth on that race. If the tab was closed (follow-up flow), re-open it.
    c = None
    adl = time.time() + min(120, a.timeout)
    while time.time() < adl:
        try:
            c = CDP(a.port, match=conv)
            break
        except SystemExit as e:
            if conv and "conversation_not_found" in str(e):
                try:
                    c = CDP(a.port, create_url=f"https://chatgpt.com/c/{conv}")
                    sys.stderr.write(f"CGC_WAIT re-opened conversation {conv} (tab was closed)\n")
                    break
                except SystemExit:
                    pass
            sys.stderr.write(f"CGC_WAIT attaching… ({e})\n")
            time.sleep(5)
    if c is None:
        sys.stderr.write("CGC_ERROR attach_failed: no matching ChatGPT tab within the grace window — "
                         "pass --conversation <id> from submit and ensure the debug Chrome is up.\n")
        return 2
    try:
        # Ensure the answer's directory exists so a completed answer is never lost on write.
        os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        # Tolerant rid resolution: right after submit the conversation/sentinel echo may
        # still be settling, so RETRY instead of dying. A patient waiter must reach its
        # poll loop — never hard-exit at startup over a transient. 600s, not 120: a
        # followup on a long thread was observed to take >2 min before the just-sent user
        # message committed to the DOM (the page still resolved to the PREVIOUS round's
        # rid), and the 120s window expired exactly there — burning the round's one-shot
        # auto-retrieve on a render lag. Waiting longer on a wrong tab costs nothing
        # (read-only); giving up early costs the recovery.
        # 'auto' means "adopt whatever the conversation's latest turn is" — a legitimate mode for a
        # freshly-sent submit/followup (this round's own turn IS the latest one) or an explicitly
        # named debug lookup (`status --rid auto`). A CONCRETE rid, however, names one turn among
        # possibly several — most notably a retrieve's --parent source_rid, which must be located
        # and verified, never assumed to be the latest (that is the causal-substitution bug this
        # closes: a later same-thread send must never be mistaken for an earlier round's answer).
        strict = a.rid != "auto"
        rid = None
        observed_rid = None
        # Set only in the strict branch, from _locate_source_turn's turn_index: the ordinal (DOM
        # order) of the source turn among user turns, threaded into every _detect_js/_extract_js/
        # _last_assistant_js call below so the answer can only come from THAT turn's interval
        # ([S3] fix). Stays None for 'auto' — the legacy unscoped lookup.
        turn_index = None
        rdl = time.time() + min(600, a.timeout)
        # At most ONE stale-tab reload per WAIT (see _reload_stale_tab) — one budget shared by
        # both triggers, the missing-source-turn one below and the frozen-stub one in the watch
        # loop, whichever claims it first. Bounded deliberately: a turn that is genuinely absent —
        # wrong --conversation, a round never sent here — must still fall through to rid_absent,
        # and a round the model never answered must still reach timeout_no_answer, rather than
        # reload-looping until the deadline.
        stale_deadline = time.time() + STALE_TURN_AFTER_S
        reloaded = False
        while time.time() < rdl:
            if strict:
                try:
                    texts = c.eval(_all_user_texts_js()) or []
                except Exception as e:
                    sys.stderr.write(f"CGC_WAIT resolving source turn… ({e})\n")
                    time.sleep(5)
                    continue
                loc = _locate_source_turn(texts, a.rid)
                observed_rid = loc["observed_rid"]
                if loc["present"]:
                    rid = a.rid
                    turn_index = loc["turn_index"]
                    break
                if not reloaded and time.time() >= stale_deadline:
                    reloaded = True
                    _reload_stale_tab(
                        c, f"source_rid={a.rid} is still not in this tab's DOM (observed latest "
                           f"{observed_rid or 'none'}) after {STALE_TURN_AFTER_S}s")
                    continue
                sys.stderr.write(f"CGC_WAIT resolving source turn… (not yet visible; observed "
                                 f"latest {observed_rid or 'none'})\n")
                time.sleep(5)
                continue
            try:
                rid = _resolve_rid(c, a.rid)
                break
            except SystemExit as e:
                sys.stderr.write(f"CGC_WAIT resolving rid… ({e})\n")
                time.sleep(5)
        if rid is None:
            if strict:
                sys.stderr.write(
                    f"CGC_ERROR rid_absent: source_rid={a.rid} was never found among the "
                    f"conversation's user turns within the grace window observed_rid={observed_rid or 'none'} "
                    "— wrong --conversation, or the source round was never sent here. The recovery "
                    "path never falls back to the conversation's latest turn.\n")
                return 2
            sys.stderr.write("CGC_ERROR rid_unresolved: could not resolve/verify the rid in the "
                             "attached conversation within the grace window — wrong tab or no "
                             "submitted prompt. Pass --conversation <id> from submit.\n")
            return 2
        sys.stderr.write(f"CGC_WAIT watching {rid} (poll {a.poll}s, timeout {a.timeout}s, "
                         f"settle {a.settle_seconds}s)\n")
        last_len = -1
        settle_start = None  # wall-clock when the answer FIRST became non-generating + byte-stable
        # Consecutive stub-stable verdicts and the byte count all of them were frozen at — the
        # second stale-tab trigger (see STALE_STUB_CYCLES). Any movement resets the streak.
        stub_cycles = 0
        stub_len = None
        ticks = 0
        while time.time() < deadline:
            try:
                c.eval(_FORCE_RENDER_JS)  # materialize the virtualized answer node before reading
            except Exception:
                pass
            try:
                st = json.loads(c.eval(_detect_js(rid, turn_index)) or "{}")
            except Exception as e:
                # transient CDP/eval hiccup — log and keep waiting, don't die
                sys.stderr.write(f"CGC_WAIT eval-retry: {e}\n")
                time.sleep(a.poll)
                continue
            ticks += 1
            if ticks == 1 or ticks % 5 == 0:  # heartbeat so the .output shows it's alive + diagnosable
                sys.stderr.write(f"CGC_WAIT alive: gen={st.get('generating')} len={st.get('len')} "
                                 f"done={st.get('done')} begin={st.get('begin')} end={st.get('end')} "
                                 f"ac={st.get('ac')} t+{int(time.time()-(deadline-a.timeout))}s\n")
            if st.get("blocker"):
                sys.stderr.write(f"CGC_BLOCKER {st['blocker']}\n")
                return 3
            if st.get("done"):
                ans = c.eval(_extract_js(rid, turn_index)) or ""
                _write_private(a.out, ans)  # answer file stays PURE — the follow-up recipe goes to stderr only
                # Attribution is read from the SAME turn_index that produced `ans`, and BEFORE the
                # tab is closed below — afterwards the node is gone and the fact is unrecoverable.
                _wait_receipt(rid, a.out, _read_attribution(c, rid, turn_index))
                sys.stderr.write(f"CGC_DONE wrote {len(ans)} chars to {a.out}\n")
                conv = conv or c.conversation_id()
                _write_state(conversation=conv, status="answered")  # mark this job answered
                if not a.keep_tab:
                    c.close_tab()  # free the tab; the conversation still PERSISTS at /c/<id>
                # On wake the agent sees this on the task output. To CONTINUE the thread (feed
                # local results back / next round) ALWAYS use `followup` on THIS conversation —
                # NEVER `prep` (no --followup) + `submit`, which starts a BRAND-NEW thread and
                # throws away ChatGPT's context. Follow-up works even though the tab auto-closed:
                # followup re-opens /c/<id> server-side (the thread persists; --keep-tab not needed).
                sys.stderr.write(
                    "CGC_NEXT to CONTINUE this consult as a thread (do NOT start a new prep+submit — "
                    "that opens a new conversation and loses ChatGPT's context, and do NOT arm a bare "
                    "`until [ -s file ]` watcher — nothing writes that file). It's ONE backgrounded "
                    "command that sends AND waits (its exit is the wake — no separate step to forget):\n"
                    f"  python3 {os.path.abspath(__file__)} followup "
                    "--task \"<local results + next question>\" --title \"<what's new>\" "
                    f"--watch --out {os.path.join(CGC_STATE_DIR, 'answer_<r2>.txt')}\n"
                    "  (run_in_background:true; --conversation defaults to this thread; read --out on wake.)\n"
                    "Do this each time local verification raises a question/disagreement; when the user "
                    "says 'follow up till it flags nothing', loop until the answer is clean.\n")
                return 0
            # The model SHOULD wrap its answer in BEGIN/END_RESPONSE:<rid> (done fires on that).
            # But it sometimes skips the wrapper — especially on a short follow-up. So when
            # generation looks finished AND the text is byte-stable, decide: present sentinel →
            # clean extract (done above); a SUBSTANTIAL unwrapped message (>= --min-unwrapped) →
            # take it whole with a warning; a TINY stable message → a thinking/streaming stub or a
            # gap before the real answer, NOT a stall → keep waiting (timeout is the backstop).
            # NOTE: thinking-summary stub nodes can be 100-250 chars, so the threshold must be well
            # above that — a real deep answer is many KB.
            cur_len = st.get("len", 0)
            if not st.get("generating") and cur_len > 0 and cur_len == last_len:
                if settle_start is None:
                    settle_start = time.time()
                elif time.time() - settle_start >= a.settle_seconds:
                    ans = c.eval(_extract_js(rid, turn_index)) or ""
                    if ans:
                        _write_private(a.out, ans)
                        _wait_receipt(rid, a.out, _read_attribution(c, rid, turn_index))
                        sys.stderr.write(f"CGC_DONE wrote {len(ans)} chars to {a.out} (recovered at settle)\n")
                        if not a.keep_tab:
                            c.close_tab()
                        return 0
                    # No sentinel wrapper yet. Unwrapped salvage grabs the largest assistant node in
                    # the source turn's interval with no rid anchor at all — re-check RIGHT NOW
                    # whether rid's turn is still the conversation's latest (a later turn may have
                    # landed while we waited): if not, that later turn now owns its own interval, so
                    # salvage must be refused. Use the FRESH turn_index from this check (DOM
                    # composition may have shifted since the wait started), not the cached one.
                    allowed, observed_rid, fresh_turn_index, _present = _salvage_allowed(c, rid)
                    raw = _scoped_raw(c, rid, strict, fresh_turn_index)
                    if allowed and len(raw) >= a.min_unwrapped:
                        _write_private(a.out, raw)
                        _write_private(a.out + ".raw", raw)
                        _wait_receipt(rid, a.out, _read_attribution(c, rid, fresh_turn_index))
                        sys.stderr.write(
                            f"CGC_UNWRAPPED wrote {len(raw)} chars to {a.out} (also saved to {a.out}.raw): "
                            f"the model did NOT emit BEGIN/END_RESPONSE:{rid}, so the whole last message "
                            f"was taken — verify it is complete (not cut off) before trusting it.\n")
                        if not a.keep_tab:
                            c.close_tab()
                        return 0
                    if not allowed and len(raw) >= a.min_unwrapped:
                        _write_private(a.out + ".raw", raw)
                        sys.stderr.write(
                            f"CGC_WAIT ambiguous-stable: source_rid={rid} is no longer the "
                            f"conversation's latest turn (observed_rid={observed_rid or 'none'}) and no "
                            "sentinel-wrapped answer for it was found — refusing to attribute the "
                            "latest message to it; still waiting for a properly wrapped answer.\n")
                        settle_start = None
                    else:
                        # tiny + stable + no sentinel → stub/gap, not a dead answer. Keep polling.
                        _write_private(a.out + ".raw", raw)
                        sys.stderr.write(
                            f"CGC_WAIT stub-stable: last assistant only {len(raw)} chars, no sentinel — "
                            f"likely a thinking/streaming gap; still waiting (raw saved to {a.out}.raw).\n")
                        settle_start = None
                        # …unless the gap never closes. A streak of these, all at the SAME byte
                        # count, is no longer a gap in a live round — it is a DOM that stopped
                        # tracking the thread (the else-branch below resets the streak the instant
                        # generation resumes or one byte moves, so a real pause cannot reach here).
                        # Spend the wait's one reload on it instead of watching a dead tab out.
                        stub_cycles = stub_cycles + 1 if len(raw) == stub_len else 1
                        stub_len = len(raw)
                        if not reloaded and stub_cycles >= STALE_STUB_CYCLES:
                            reloaded = True
                            _reload_stale_tab(
                                c, f"{stub_cycles} consecutive stub-stable reads for {rid}, all "
                                   f"frozen at {stub_len} chars with no generation "
                                   f"(~{int(stub_cycles * a.settle_seconds)}s of a motionless DOM)")
                            # The turn ordinal was read off the DOM we just declared untrustworthy;
                            # re-locate the source turn on the rebuilt page rather than keep scoping
                            # the answer by an ordinal that may have shifted.
                            if strict:
                                _ok, _obs, fresh_idx, fresh_present = _salvage_allowed(c, rid)
                                if fresh_present:
                                    turn_index = fresh_idx
            else:
                # Generating, or the byte count moved: the round is alive. This is the reset that
                # keeps the frozen-stub trigger off every legitimate thinking/streaming pause.
                settle_start = None
                last_len = cur_len
                stub_cycles = 0
            time.sleep(a.poll)
        # Timeout: the answer is usually PRESENT but was virtualized out of the inactive tab's
        # DOM (the failure that returned "no answer" on a completed consult). Force-render by
        # SCROLLING the node into view so React commits it — WITHOUT Page.bringToFront, which would
        # yank the tab to the foreground and interrupt the user. The answer is read via textContent,
        # which survives a backgrounded/virtualized tab, so focus is not required to extract it.
        try:
            c.eval(_FORCE_RENDER_JS)
        except Exception:
            pass
        time.sleep(2)
        # After force-render the answer node is (usually) materialized. SHARED CONTRACT #2:
        # - a properly WRAPPED answer (bare-line sentinels, non-empty body) → exit 0, clean extract.
        # - no wrapper but a SUBSTANTIAL last-assistant message (>= --min-unwrapped) → best-effort
        #   salvage: write --out AND a sibling --out.raw, log CGC_UNWRAPPED, exit 0 (never lose a
        #   present answer).
        # - empty / only short streaming stubs → exit 4, a genuine no-answer timeout, DISTINCT from
        #   the salvage case above (they do not conflict).
        rescue = c.eval(_extract_js(rid, turn_index)) or ""
        if rescue:
            _write_private(a.out, rescue)
            _wait_receipt(rid, a.out, _read_attribution(c, rid, turn_index))
            sys.stderr.write(f"CGC_DONE wrote {len(rescue)} chars to {a.out} (rescued at timeout)\n")
            if not a.keep_tab:
                c.close_tab()
            return 0
        # No sentinel wrapper. Re-check (see the settle-time comment above) whether rid's turn is
        # still the conversation's latest before considering unwrapped salvage — a later turn may
        # have landed anywhere during the whole wait, not just at the settle instant.
        allowed, observed_rid, fresh_turn_index, present = _salvage_allowed(c, rid)
        raw = _scoped_raw(c, rid, strict, fresh_turn_index)
        if not present and strict:
            sys.stderr.write(
                f"CGC_WAIT interval_unresolved: source_rid={rid}'s own turn could no longer be "
                f"located in the DOM at timeout (observed_rid={observed_rid or 'none'}) — its "
                "answer interval cannot be identified; not falling back outside it.\n")
        if allowed and len(raw) >= a.min_unwrapped:
            _write_private(a.out, raw)
            _write_private(a.out + ".raw", raw)
            _wait_receipt(rid, a.out, _read_attribution(c, rid, fresh_turn_index))
            sys.stderr.write(
                f"CGC_UNWRAPPED wrote {len(raw)} chars to {a.out} (also saved to {a.out}.raw): the "
                f"model did NOT emit BEGIN/END_RESPONSE:{rid} — best-effort salvage at timeout; "
                f"verify it is complete (not cut off) before trusting it.\n")
            if not a.keep_tab:
                c.close_tab()
            return 0
        if not allowed and len(raw) >= a.min_unwrapped:
            _write_private(a.out + ".raw", raw)
            sys.stderr.write(
                f"CGC_ERROR rid_superseded: source_rid={rid} is not the conversation's latest turn "
                f"observed_rid={observed_rid or 'none'} and no sentinel-wrapped answer for it was "
                "found — refusing to attribute the latest message to it.\n")
            if not a.keep_tab:
                c.close_tab()
            return 5
        # Genuinely empty / only short streaming stubs — no usable answer at all.
        _write_private(a.out + ".raw", raw)
        sys.stderr.write(
            f"CGC_ERROR timeout_no_answer: no wrapped answer and no substantial unwrapped message "
            f"({len(raw)} chars, raw saved to {a.out}.raw) at timeout for {rid}.\n")
        if not a.keep_tab:
            c.close_tab()
        return 4
    finally:
        c.close()


def _is_canonical(conversation) -> bool:
    """cgc_spool's canonical-id law, borrowed. Lazy import keeps cdp_consult standalone-runnable;
    unavailable means we cannot vouch for the shape, so it does not pass."""
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        import cgc_spool as _spool
        return _spool.is_canonical_conversation(conversation)
    except Exception:
        return False


def cmd_find_conversation(a) -> int:
    """Read-only: which open ChatGPT tab is carrying this rid?

    The recovery for an uncertain send is addressed BY CONVERSATION, so a round whose submit never
    captured one has nothing to retrieve from even while its answer is generating in a visible tab.
    This is the search that closes that gap: the rid is inside the prompt we sent, so a tab whose
    text carries it IS this round's thread. Scans only — it never sends, clicks, or navigates, so it
    cannot duplicate a consult.

    Best-effort by construction: ChatGPT virtualizes long threads, so a rid scrolled far out of the
    DOM can be missed. A miss is reported as a miss, never as 'the consult was not sent'."""
    base = f"http://127.0.0.1:{a.port}"
    try:
        info = json.load(urllib.request.urlopen(f"{base}/json", timeout=5))
    except Exception as e:
        sys.stderr.write(f"CGC_ERROR browser_unreachable: no debug Chrome on port {a.port} "
                         f"({type(e).__name__}) — nothing to search.\n")
        return 1
    convs, seen = [], set()
    for t in info:
        if t.get("type") != "page":
            continue
        u = urllib.parse.urlparse(t.get("url") or "")
        h = (u.hostname or "").lower()
        if not (h == "chatgpt.com" or h.endswith(".chatgpt.com")):
            continue
        m = re.search(r"(?:^|/)c/([0-9a-f-]+)(?:/|$)", u.path)
        # Canonical ids only — this command's whole output is an id the retrieve path will consume,
        # and every lease/followup gate is a strict fullmatch. Printing a shape they refuse would
        # hand the caller a command that dies terminally instead of an answer.
        if m and m.group(1) not in seen and _is_canonical(m.group(1)):
            seen.add(m.group(1))
            convs.append(m.group(1))
    hits = []
    for conv in convs:
        try:
            c = CDP(a.port, match=conv)
        except SystemExit:
            continue
        try:
            # <main> only. The thread LIST lives in every tab's nav and its titles are derived from
            # the first message, so a whole-body (or title) scan matches our rid in tabs that never
            # held it — which turns every multi-tab search into `ambiguous_rid`, defeating the one
            # case this exists for.
            if _rid_in_main(c, a.rid):
                hits.append(conv)
        except Exception:
            pass
        finally:
            c.close()
    print(json.dumps({"rid": a.rid, "scanned": convs, "conversations": hits}))
    if not hits:
        sys.stderr.write(
            f"CGC_ERROR rid_not_on_any_open_tab: scanned {len(convs)} ChatGPT conversation tab(s); "
            f"none shows {a.rid}. This does NOT prove the prompt was never sent (a closed tab, or a "
            "virtualized thread that scrolled the turn out of the DOM, looks identical) — open "
            "ChatGPT's history and look before re-sending.\n")
        return 1
    if len(hits) > 1:
        sys.stderr.write(f"CGC_ERROR ambiguous_rid: {a.rid} appears in {len(hits)} conversations "
                         f"({', '.join(hits)}) — a human must pick.\n")
        return 1
    sys.stderr.write(f"CGC_FOUND {a.rid} is in conversation {hits[0]}. Retrieve it read-only:\n"
                     f"  python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cgc_spool.py')}"
                     f" enqueue --kind retrieve --conversation {hits[0]} --parent {a.rid}\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cdp_consult.py")
    p.add_argument("--port", type=int, default=CGC_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status")
    s.add_argument("--rid", required=True, help="expected rid (verified against the conversation) or 'auto'")
    s.add_argument("--conversation", default="auto", help="/c/<id> to pin the exact tab (default 'auto' = active thread)")
    s.add_argument("--out", help="the waiter's answer file; if the tab is already closed but this "
                                 "holds a non-empty answer, status reports done/retrieved instead of "
                                 "erroring conversation_not_found")
    s.set_defaults(fn=cmd_status)

    fc = sub.add_parser("find-conversation",
                        help="read-only: find which open ChatGPT tab carries this rid (recovery for "
                             "an uncertain send whose conversation id was never captured)")
    fc.add_argument("--rid", required=True)
    fc.set_defaults(fn=cmd_find_conversation)

    su = sub.add_parser("submit")
    su.add_argument("--rid", required=True)
    su.add_argument("--prompt-file", required=True,
                    help="rendered prompt; '-' reads it from stdin, which is how the daemon passes "
                         "the exact bytes its gate validated (see _read_prompt)")
    su.add_argument("--project-url", default=CGC_PROJECT_URL,
                    help="URL a fresh consult opens (default $CGC_PROJECT_URL, else a new chat). "
                         "Set CGC_PROJECT_URL to your own ChatGPT project to keep consults grouped.")
    su.add_argument("--model", default=CGC_MODEL,
                    help="target model tier (default $CGC_MODEL or 'Pro'). A 'Pro*' target is satisfied "
                         "by any Pro tier the switcher shows (Pro / Pro Extended) but never by "
                         "Medium/Instant/etc. Pass 'skip' to leave as-is.")
    su.add_argument("--model-family", default=CGC_MODEL_FAMILY,
                    help="MODEL to pin in the same picker menu, a separate dimension from the tier "
                         "(default $CGC_MODEL_FAMILY or 'Latest'). GPT-6's composer lists the model "
                         "as radios beside the power slider, so 'Pro on GPT-5.5' is reachable and "
                         "would otherwise pass the tier check silently. 'skip' = don't check. A "
                         "build with no such radios ignores this.")
    su.add_argument("--reuse-tab", action="store_true",
                    help="navigate the existing single tab instead of opening a dedicated one "
                         "(default: own tab per consult, for safe concurrency)")
    su.add_argument("--allow-model-mismatch", action="store_true",
                    help="send even if the target model could not be selected (default: fail-closed, "
                         "do not submit on the wrong model)")
    su.add_argument("--no-gate", action="store_true",
                    help="skip the automated Step-0 gate (don't auto-start/-check the debug Chrome). "
                         "Use only if you manage the debug Chrome yourself.")
    su.set_defaults(fn=cmd_submit)

    fu = sub.add_parser("followup", help="continue a consult thread (keeps its context + model); "
                                         "one-shot with --task, or --prompt-file from prep --followup")
    fu.add_argument("--conversation", default="auto",
                    help="/c/<id> to continue; default 'auto' = the active thread (last submit/followup). "
                         "Re-opens the conversation server-side if its tab was closed.")
    # One-shot input (renders the follow-up prompt for you) …
    fu.add_argument("--task", help="ONE-SHOT: the local results + the next ask. Renders the follow-up "
                                   "prompt internally (no separate `prep` step).")
    fu.add_argument("--title", help="one-shot: short title for the new round")
    fu.add_argument("--context-file", help="one-shot: local results / what diverged (supplement)")
    fu.add_argument("--refs-file", help="one-shot: new/changed code link from `deliver` (if any)")
    # … OR a pre-rendered prompt:
    fu.add_argument("--prompt-file", help="pre-rendered follow-up prompt from `consult.py prep --followup` "
                                          "(alternative to --task)")
    fu.add_argument("--rid", help="the new round's rid (auto-filled in one-shot mode from the rendered prompt)")
    fu.add_argument("--model", default=CGC_MODEL,
                    help="tier to ENFORCE on the thread before sending (default $CGC_MODEL or 'Pro'). A "
                         "follow-up no longer blindly inherits a thread that silently downgraded to "
                         "Instant — it re-selects the target and fails closed if it can't. 'skip' = "
                         "leave as-is (old behavior).")
    fu.add_argument("--model-family", default=CGC_MODEL_FAMILY,
                    help="MODEL to re-assert on the thread alongside the tier (default "
                         "$CGC_MODEL_FAMILY or 'Latest'). 'skip' = don't check; a build without the "
                         "model radios ignores it.")
    fu.add_argument("--allow-model-mismatch", action="store_true",
                    help="send the follow-up even if the target tier can't be selected (default: "
                         "fail-closed — do NOT answer on a degraded model).")
    fu.add_argument("--no-gate", action="store_true",
                    help="skip the automated Step-0 gate (don't auto-start/-check the debug Chrome).")
    # --watch: send AND wait in ONE process, so a follow-up is a single backgrounded command
    # (run_in_background:true) whose exit wakes the agent — no separate `wait` step to forget
    # or mis-arm. This is the recommended way to run a follow-up.
    fu.add_argument("--watch", action="store_true",
                    help="after sending, wait inline for the answer and write it to --out, then exit "
                         "(one-shot send+wait; the exit is the wake). REQUIRES --out.")
    fu.add_argument("--out", help="answer file for --watch mode")
    fu.add_argument("--poll", type=int, default=20, help="(--watch) seconds between DOM checks")
    fu.add_argument("--timeout", type=int, default=STUCK_AFTER_S,
                    help=f"(--watch) seconds to wait for the answer (default {STUCK_AFTER_S} = "
                         f"{STUCK_AFTER_S // 60} min — the point past which the job is stuck rather "
                         "than slow; a deep round can reason ~60 min).")
    fu.add_argument("--settle-seconds", type=int, default=300, help="(--watch) unwrapped-answer settle window")
    fu.add_argument("--min-unwrapped", type=int, default=1500, help="(--watch) min chars to accept an unwrapped answer")
    fu.add_argument("--keep-tab", action="store_true", help="(--watch) keep the tab after retrieving")
    fu.set_defaults(fn=cmd_followup)

    w = sub.add_parser("wait")
    w.add_argument("--rid", required=True, help="expected rid (verified against the conversation) or 'auto'")
    w.add_argument("--conversation", default="auto",
                   help="/c/<id> from submit — pins the exact tab so this waiter cannot pick up "
                        "another request's answer. Default 'auto' = the active thread; re-opens it "
                        "if the tab was closed.")
    w.add_argument("--out", required=True)
    w.add_argument("--poll", type=int, default=20, help="seconds between DOM checks")
    w.add_argument("--timeout", type=int, default=STUCK_AFTER_S,
                   help=f"seconds to wait for the answer (default {STUCK_AFTER_S} = "
                        f"{STUCK_AFTER_S // 60} min — the point past which the job is stuck rather "
                        "than slow; a deep GPT-6 Astra Pro round can reason ~60 min).")
    w.add_argument("--keep-tab", action="store_true",
                   help="do not close the consult's tab after retrieving (default: close it, so "
                        "concurrent consults' tabs don't accumulate)")
    w.add_argument("--settle-seconds", type=int, default=300,
                   help="consider completion only after the answer is non-generating AND byte-stable "
                        "this long (default 300s — long enough not to trip on a Pro Thinking pause)")
    w.add_argument("--min-unwrapped", type=int, default=1500,
                   help="if the model skips the BEGIN/END_RESPONSE wrapper, accept the whole last "
                        "message as the answer only when it is at least this many chars (default 1500 "
                        "— well above ChatGPT's ~100-250 char thinking-summary stubs). Smaller stable "
                        "messages are treated as streaming stubs and the waiter keeps polling.")
    w.set_defaults(fn=cmd_wait)

    a = p.parse_args()  # --port goes before the subcommand: cdp_consult.py --port N submit ...
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
