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
            2 usage error
  status  --rid R [--port P]   one-shot JSON {generating,done,blocker,len}
"""
from __future__ import annotations

import argparse
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

# How long a consult takes: a GPT-5.6 Pro round reasons ~25 min. Same number as
# cgc_spool.STUCK_AFTER_S. This is NOT a budget for the consult — a GPT-5.6 Pro round reasons
# ~25 min and must never be killed for it. It is the point past which waiting is no longer
# explained by the work, so the job is stuck and the log is what to read next.
STUCK_AFTER_S = 3600


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
            bw.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": create_url}}))
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
        deadline = time.time() + (timeout or 30)
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result", {})
        raise RuntimeError(f"CDP {method} timeout")

    def eval(self, expr):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
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
# and childNodes are populated regardless of tab visibility. (_user_rid_js already used
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
    "function __cgcNode(BG){"
    "var all=document.querySelectorAll(" + _JS_A + ");"
    # 1) exact: any assistant node containing our (unique) BEGIN sentinel — global search.
    "for(var i=all.length-1;i>=0;i--){if((all[i].textContent||'').indexOf(BG)>=0)return all[i];}"
    # 2) fallback (no sentinel yet): the LARGEST assistant node of the CURRENT turn (after the
    #    last user message). NOT a[last] — that is often a 1-char trailing streaming placeholder,
    #    so the waiter saw len=1 for 25 min while the model was actually producing content in
    #    sibling nodes. NOT a global max either — that would read a PRIOR round's big answer.
    "var nx=document.querySelectorAll(" + _JS_ANY + ");"
    "var lu=-1;for(var j=0;j<nx.length;j++){if(__cgcRole(nx[j])==='user')lu=j;}"
    "var best=null,bl=-1;"
    "for(var k=lu+1;k<nx.length;k++){if(__cgcRole(nx[k])==='assistant'){"
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


def _detect_js(rid: str) -> str:
    """Returns JSON {generating,done,blocker,len,begin,end,ac} for the answer node.
    done comes from the canonical bare-line _sentinel_js parser (SHARED CONTRACT #1) — a line
    merely CONTAINING BEGIN/END_RESPONSE:<rid> (quoted in prose or a code block) never counts,
    only a bare standalone sentinel line does. This is NOT stop-button driven: the model writes
    END_RESPONSE only as its final line, so its presence == complete + extractable. Depending on
    the stop-button was fragile — if that UI selector ever persists, done would never fire and
    wait would only return on timeout. begin/end/ac are diagnostics for the heartbeat."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    end = json.dumps(f"END_RESPONSE:{rid}")
    sentinel = _sentinel_js(rid, "__cgcText(node)")
    return (
        "(function(){" + _NODE_FN + _TEXT_FN +
        "var a=document.querySelectorAll(" + _JS_A + ");"
        "var BG=" + begin + ",EN=" + end + ";"
        "var node=__cgcNode(BG);"
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


def _user_rid_js() -> str:
    """Read the request id from the LAST user message's `BEGIN_RESPONSE:<rid>` echo.
    Lets `wait`/`status` watch the rid that was actually submitted in THIS conversation,
    making a submit/wait rid mismatch structurally impossible."""
    # Exact rid shape (REQ-YYYYMMDD-HHMMSS-hhhhhh) so a missing whitespace boundary in
    # concatenated text can't make the capture swallow trailing characters.
    return ("(function(){var u=document.querySelectorAll(" + _JS_U + ");"
            "var n=u[u.length-1];var t=n?n.textContent:'';"
            "var m=t.match(/BEGIN_RESPONSE:(REQ-\\d{8}-\\d{6}-[0-9a-f]{6})/);return m?m[1]:'';})()")


def _resolve_rid(c, rid):
    """Resolve + VERIFY the rid against the attached conversation.
    - rid=='auto': read it from the page's last user message.
    - explicit rid: confirm the page actually holds THAT request — if the page's rid
      differs, the wrong tab/conversation is attached → fail loudly (prevents one
      request from receiving another's answer)."""
    page_rid = c.eval(_user_rid_js()) or ""
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


def _last_assistant_js(rid: str) -> str:
    """Reconstructed text of the answer node (no sentinel slicing) — for the stall raw dump."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    return ("(function(){" + _NODE_FN + _TEXT_FN +
            "return __cgcText(__cgcNode(" + begin + "));})()")


def _extract_js(rid: str) -> str:
    """Returns the answer text BETWEEN the bare-line sentinels of the answer node (SHARED
    CONTRACT #1, via the canonical _sentinel_js parser), or '' if no valid wrapper is present."""
    begin = json.dumps(f"BEGIN_RESPONSE:{rid}")
    sentinel = _sentinel_js(rid, "__cgcText(__cgcNode(" + begin + "))")
    return (
        "(function(){" + _NODE_FN + _TEXT_FN +
        "var res=" + sentinel + ";"
        "return res.done?res.body:'';})()"
    )


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
    except SystemExit as e:
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


# ---- model selection (two-menu aware: model menu vs reasoning-effort menu) ----
# The composer has more than one switcher button. The reasoning-effort menu
# (Instant/Medium/High/Extra High) does NOT contain 'Pro' — 'Pro' lives in the model
# menu. So we must try EACH candidate switcher, open its menu, and pick the one whose
# menu actually contains the target. Detection is by short button label, not a fixed
# whitelist, so it survives ChatGPT renaming the tiers.
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


def _cand_count_js():
    return "(function(){return %s.length;})()" % _CAND_JS


# ChatGPT's model pill is a Radix popover: it opens ONLY on a real pointer-event
# sequence, NOT on a bare .click(). A plain click left the menu closed, so no
# menuitems ever appeared and selection silently fell back to the project default
# (looked fine only because the default was already Pro). Dispatch the full gesture.
_GESTURE = ("['pointerdown','mousedown','pointerup','mouseup','click'].forEach(function(t){"
            "EL.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));});")


def _open_cand_js(i):
    return ("(function(){var c=%s;var EL=c[%d];if(!EL)return null;%s"
            "return (EL.innerText||'').trim().split('\\n')[0];})()" % (_CAND_JS, i, _GESTURE))


def _click_item_js(target):
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


def _cand_labels_js():
    """Every switcher-ish button's first-line label, in DOM order. c[0] is the composer's own
    model pill; later entries are the other switchers (ChatGPT splits model and reasoning effort)."""
    return ("(function(){return %s.map(function(b){"
            "return (b.innerText||'').trim().split('\\n')[0];});})()" % _CAND_JS)


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


def _select_model(c, target):
    """Switch the composer to `target` by trying each switcher menu. Returns (confirmed, shown).
    Fully automated — no human step."""
    ok, shown = _model_verdict(c.eval(_cand_labels_js()), target)
    if ok:
        return True, shown
    for _ in range(2):
        n = c.eval(_cand_count_js()) or 0
        for i in range(min(int(n), 8)):
            opened = c.eval(_open_cand_js(i))
            if opened is None:
                continue
            time.sleep(1.0)  # Radix menu renders async after the gesture
            if c.eval(_click_item_js(target)):
                time.sleep(0.7)
                ok, shown = _model_verdict(c.eval(_cand_labels_js()), target)
                if ok:
                    return True, shown
            # wrong menu (target not in it) → close and try the next switcher
            c.key("Escape", "Escape", 27)
            time.sleep(0.25)
        time.sleep(0.4)
    return _model_verdict(c.eval(_cand_labels_js()), target)


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
        # REUSE-TAB false-positive guard: a reused tab may already hold OLD user messages
        # from a prior conversation, so "at least one user message after send" can be
        # satisfied by stale history even when THIS submit's composer insert/click failed.
        # Capture the count before insertion so the post-send check can require a NEW
        # message (after > before) on the reuse-tab path. A fresh tab always starts at 0,
        # so this is a no-op there — new-tab behavior is unchanged.
        before_user_count = 0
        if a.reuse_tab:
            before_user_count = c.eval(
                "document.querySelectorAll(" + _JS_U + ").length") or 0
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
        if a.model and a.model.lower() != "skip":
            target = a.model
            model_confirmed, model_now = _select_model(c, target)
            if not model_confirmed and not a.allow_model_mismatch:
                if not a.reuse_tab:
                    c.close_tab()  # don't orphan the dedicated tab we opened for this submit
                c.close()
                print(json.dumps({"ok": False, "submitted": False,
                                  "modelConfirmed": False, "model": model_now, "wanted": target}))
                sys.stderr.write(
                    f"CGC_ERROR model_not_selectable: wanted '{target}', switcher shows "
                    f"'{model_now}' and it could not be changed — NOT submitting. Set a "
                    f"'{target}' tier in the ChatGPT window, or pass --allow-model-mismatch.\n")
                return 3
            if not model_confirmed:
                sys.stderr.write(f"CGC_WARN proceeding on '{model_now}' not '{target}' "
                                 f"(--allow-model-mismatch)\n")
            time.sleep(0.3)
        # Focus composer + insert text via execCommand (typed newlines would submit early).
        c.call("Runtime.evaluate", {"expression":
            "(function(){var d=document.querySelector('div[role=\"textbox\"][contenteditable=\"true\"]')"
            "||document.querySelector('#prompt-textarea');d.focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('insertText',false," + json.dumps(prompt) + ");"
            "return d.innerText.length;})()", "returnByValue": True})
        time.sleep(0.3)
        # Submit. Prefer the send button; fall back to Enter.
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
        if verdict == "unknown_send":
            # Deliberately NOT close_tab(): this outcome exists because a human has to look
            # at this window to see whether the prompt actually went. Closing it destroys
            # the only evidence. It is the one failure allowed to leave a tab behind.
            c.close()
            print(json.dumps({"ok": False, "submitted": None, "rid": a.rid,
                              "reason": "unknown_send", "contract": contract}))
            sys.stderr.write(
                "CGC_ERROR unknown_send: clicked send, but no known turn schema shows a user turn "
                f"carrying BEGIN_RESPONSE:{a.rid} within {_RID_LANDED_S}s. This does NOT prove the "
                "prompt was not sent, so it will NOT be resent automatically — a duplicate would "
                "cost another full round. A human should look at the ChatGPT window.\n"
                f"observed: {json.dumps(contract)}\n")
            return 3
        if verdict == "selector_drift":
            c.close_tab()   # the send is confirmed and the conversation persists server-side
            print(json.dumps({"ok": False, "submitted": True, "rid": a.rid,
                              "reason": "selector_drift", "adapter": adapter, "contract": contract}))
            sys.stderr.write(
                f"CGC_ERROR selector_drift: the prompt WAS sent (its rid is in a {adapter} user "
                f"turn), but no assistant turn or generating indicator is recognizable within "
                f"{_GENERATION_SIGNAL_S}s. ChatGPT's DOM has moved and this tool can no longer read "
                "replies — a tool defect, not a missing answer. Do not resend; fix the adapter.\n"
                f"observed: {json.dumps(contract)}\n")
            return 1
        n, ok = 1, True
        # The URL transitions /project -> /c/<id> a beat after the message sends; poll for it.
        # SUBMIT-RACE: a submit that reports ok=true with no captured conversation id is worse
        # than a clean failure — a later `wait`/`followup --conversation auto` would resolve to
        # whatever OTHER tab/thread is active and silently answer the wrong request. So poll a
        # little longer here (up to ~25s total) and, if still no conv id, fail closed: do NOT
        # write active-thread state (that would point `auto` at a request with no known tab) and
        # do NOT return success.
        conv = ""
        for _ in range(40):
            conv = c.conversation_id()
            if conv:
                break
            time.sleep(0.5)
        if not ok:
            print(json.dumps({"ok": False, "userMsgs": n, "model": model_now,
                              "modelConfirmed": model_confirmed, "conversation_id": conv}))
            return 2
        if not conv:
            print(json.dumps({"ok": False, "userMsgs": n, "model": model_now,
                              "modelConfirmed": model_confirmed, "conversation_id": ""}))
            sys.stderr.write(
                "CGC_ERROR no_conversation_id: the message sent but the tab never transitioned to "
                "/c/<id> within the grace window — NOT recording active-thread state (a follow-up/"
                "wait using --conversation auto would otherwise silently target the wrong thread). "
                "Recovery: retry submit, or attach manually with `status --conversation <id>` once "
                "the tab's URL shows a /c/<id>.\n")
            return 2
        _write_state(conversation=conv, rid=a.rid)  # so `followup`/`wait` can auto-resolve
        print(json.dumps({"ok": ok, "userMsgs": n, "model": model_now, "adapter": adapter,
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
            _fu_ok, model_now = _select_model(c, target)
            if not _fu_ok and not a.allow_model_mismatch:
                print(json.dumps({"ok": False, "followup": True, "conversation_id": conv,
                                  "rid": rid, "modelConfirmed": False, "model": model_now,
                                  "wanted": target}))
                sys.stderr.write(
                    f"CGC_ERROR model_not_selectable: thread offers '{model_now}', not '{target}' "
                    f"— NOT sending this follow-up (it would answer on a DEGRADED model, the "
                    f"Instant-stall failure). Restore the '{target}' tier in the ChatGPT window, "
                    f"or pass --allow-model-mismatch to override.\n")
                return 2
        # Count user messages BEFORE sending so we can confirm a NEW one landed (the
        # thread already has >=1 user message, so an absolute >0 check would false-pass).
        u_before = c.eval("document.querySelectorAll(" + _JS_U + ").length") or 0
        # Insert via execCommand (typed newlines submit early). Model already gated above.
        c.call("Runtime.evaluate", {"expression":
            "(function(){var d=document.querySelector('div[role=\"textbox\"][contenteditable=\"true\"]')"
            "||document.querySelector('#prompt-textarea');d.focus();"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('insertText',false," + json.dumps(prompt) + ");"
            "return d.innerText.length;})()", "returnByValue": True})
        time.sleep(0.3)
        clicked = c.eval(
            "(function(){var b=document.querySelector('button[data-testid=\"send-button\"],"
            "button[aria-label*=\"Send\" i],button[aria-label*=\"\\u53d1\\u9001\"]');"
            "if(b&&!b.disabled){b.click();return true;}return false;})()")
        if not clicked:
            c.key("Enter", "Enter", 13)
        n = u_before
        for _ in range(10):
            time.sleep(0.5)
            n = c.eval("document.querySelectorAll(" + _JS_U + ").length") or 0
            if n > u_before:
                break
        ok = bool(n > u_before)
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
        if rid:
            echoed = c.eval(_user_rid_js()) or ""
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
        print(json.dumps({"ok": True, "userMsgs": n, "conversation_id": conv, "rid": rid,
                          "followup": True, "wait_out": default_out, "watching": bool(a.watch)}))
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
        # poll loop — never hard-exit at startup over a transient.
        rid = None
        rdl = time.time() + min(120, a.timeout)
        while time.time() < rdl:
            try:
                rid = _resolve_rid(c, a.rid)
                break
            except SystemExit as e:
                sys.stderr.write(f"CGC_WAIT resolving rid… ({e})\n")
                time.sleep(5)
        if rid is None:
            sys.stderr.write("CGC_ERROR rid_unresolved: could not resolve/verify the rid in the "
                             "attached conversation within the grace window — wrong tab or no "
                             "submitted prompt. Pass --conversation <id> from submit.\n")
            return 2
        sys.stderr.write(f"CGC_WAIT watching {rid} (poll {a.poll}s, timeout {a.timeout}s, "
                         f"settle {a.settle_seconds}s)\n")
        last_len = -1
        settle_start = None  # wall-clock when the answer FIRST became non-generating + byte-stable
        ticks = 0
        while time.time() < deadline:
            try:
                c.eval(_FORCE_RENDER_JS)  # materialize the virtualized answer node before reading
            except Exception:
                pass
            try:
                st = json.loads(c.eval(_detect_js(rid)) or "{}")
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
                ans = c.eval(_extract_js(rid)) or ""
                _write_private(a.out, ans)  # answer file stays PURE — the follow-up recipe goes to stderr only
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
                    ans = c.eval(_extract_js(rid)) or ""
                    if ans:
                        _write_private(a.out, ans)
                        sys.stderr.write(f"CGC_DONE wrote {len(ans)} chars to {a.out} (recovered at settle)\n")
                        if not a.keep_tab:
                            c.close_tab()
                        return 0
                    raw = c.eval(_last_assistant_js(rid)) or ""
                    if len(raw) >= a.min_unwrapped:
                        _write_private(a.out, raw)
                        _write_private(a.out + ".raw", raw)
                        sys.stderr.write(
                            f"CGC_UNWRAPPED wrote {len(raw)} chars to {a.out} (also saved to {a.out}.raw): "
                            f"the model did NOT emit BEGIN/END_RESPONSE:{rid}, so the whole last message "
                            f"was taken — verify it is complete (not cut off) before trusting it.\n")
                        if not a.keep_tab:
                            c.close_tab()
                        return 0
                    # tiny + stable + no sentinel → stub/gap, not a dead answer. Keep polling.
                    _write_private(a.out + ".raw", raw)
                    sys.stderr.write(
                        f"CGC_WAIT stub-stable: last assistant only {len(raw)} chars, no sentinel — "
                        f"likely a thinking/streaming gap; still waiting (raw saved to {a.out}.raw).\n")
                    settle_start = None
            else:
                settle_start = None
                last_len = cur_len
            time.sleep(a.poll)
        # Timeout: the answer is usually PRESENT but was virtualized out of the inactive tab's
        # DOM (the failure that returned "no answer" on a completed consult). Force-render hard
        # — bring the tab to front AND scroll — so React commits the answer node, then re-read.
        try:
            c.call("Page.bringToFront", {})
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
        rescue = c.eval(_extract_js(rid)) or ""
        if rescue:
            _write_private(a.out, rescue)
            sys.stderr.write(f"CGC_DONE wrote {len(rescue)} chars to {a.out} (rescued at timeout)\n")
            if not a.keep_tab:
                c.close_tab()
            return 0
        raw = c.eval(_last_assistant_js(rid)) or ""
        if len(raw) >= a.min_unwrapped:
            _write_private(a.out, raw)
            _write_private(a.out + ".raw", raw)
            sys.stderr.write(
                f"CGC_UNWRAPPED wrote {len(raw)} chars to {a.out} (also saved to {a.out}.raw): the "
                f"model did NOT emit BEGIN/END_RESPONSE:{rid} — best-effort salvage at timeout; "
                f"verify it is complete (not cut off) before trusting it.\n")
            if not a.keep_tab:
                c.close_tab()
            return 0
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
                    help="(--watch) seconds to wait for the answer (default 1500 = 25 min, how long "
                         "the point past which the job is stuck rather than slow)."
                         "its background tasks at 900s.")
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
                   help="seconds to wait for the answer (default 1500 = 25 min, how long a GPT-5.6 "
                        "the point past which the job is stuck rather than slow)."
                        "background tasks at 900s.")
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
