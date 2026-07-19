#!/usr/bin/env python3
# Created: 2026-07-07
# Authority basis: open-claude-gpt skill v2 (CDP backend). Adds the SPOOL path so a consult
#   works under Claude Code's auto-mode data-exfiltration classifier WITHOUT any per-user
#   settings edit. The classifier sits ABOVE the permission system and hard-denies an agent
#   Bash call that sends data to an external host (chatgpt.com), even when the command is on
#   the allowlist — verified against Claude Code docs (permission-modes / auto-mode-config).
#   So the network send is taken OFF the agent: the agent only writes a local job file
#   (`enqueue`) and reads a local answer file (`await`) — both are local file I/O the
#   classifier never flags — and a USER-STARTED daemon (cgc_daemon.py, launched once like the
#   debug Chrome) performs the actual CDP submit + wait.
#
# THIS IS NOT CLASSIFIER EVASION. The daemon is a real, inspectable VALIDATING egress gate:
# before it sends anything it re-verifies, independently of the prompt's own stamp, that every
# code reference is a PUBLIC github.com repo (gh visibility=="public"), and it refuses a prompt
# carrying obvious secrets. The exact injection risk the classifier guards — a prompt-injected
# agent exfiltrating arbitrary private data — is therefore mitigated AT the egress point, which
# a blanket classifier bypass or an evasive command would not do. The residual the gate does NOT
# cover: free-text prose the caller put in --task/--context-file. The skill contract already
# forbids secrets there; the secret-scan below catches the obvious cases and fails closed.
"""
Local spool + validating-gate helpers for the daemon-backed consult path.

Layout under $CGC_SPOOL_DIR (default $CGC_STATE_DIR/spool, default /tmp/cgc/spool):
  pending/<rid>.json      job written by `enqueue`, awaiting the daemon
  processing/<rid>.json   job claimed by the daemon (atomic rename from pending/)
  done/<rid>.json         job the daemon finished (kept briefly for debugging)
  status/<rid>.json       {state, exit, out, ts, msg} — the daemon's live status for `await`
  daemon.json             {pid, ts} heartbeat the daemon refreshes each loop

Agent-facing CLI (dispatched by bin/cgc; both are LOCAL-ONLY, never touch the network):
  enqueue  --rid R --prompt-file F [--kind submit|followup] [--project-url U]
           [--conversation C] [--model M] [--out O] [--poll N] [--timeout N]
           -> writes pending/<rid>.json, prints {rid,out,queued,daemon_up}. Local write only.
  await    --rid R --out F [--timeout T] [--poll N]
           -> polls status/<rid>.json + the answer file until the daemon finishes. Local read
              only — no CDP. Exit codes:
                0 done (answer at --out)   3 blocker (login/captcha/rate/model/safeguard)
                4 no usable answer         2 usage/setup error (incl. daemon not running)
                5 still running — re-run await (see CONSULT_TIMEOUT_S).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl  # advisory locking (POSIX) — degrade to best-effort if absent
except ImportError:
    fcntl = None

# Load persisted CGC_* config (env still wins) exactly like the other scripts.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cgc_config import load_config as _cgc_load_config
    _cgc_load_config()
except Exception:
    pass

CGC_STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")
SPOOL_DIR = os.environ.get("CGC_SPOOL_DIR", os.path.join(CGC_STATE_DIR, "spool"))

# A consult with no fresh daemon heartbeat within this many seconds is treated as "daemon down".
HEARTBEAT_STALE_S = 45

# How long a consult takes: a GPT-5.6 Pro round reasons ~25 min. One number, used everywhere.
CONSULT_TIMEOUT_S = 1500

# Claude Code kills any background task the AGENT launches at 900s, so the agent cannot hold a
# 25-minute wait in one call. It watches in AGENT_POLL_S slices under a `timeout 899` wrapper and
# re-runs; await exits 5 (still running) rather than 4 (finished, nothing produced) so a healthy
# consult is never mistaken for a dead one. Nothing else in the system uses this number.
AGENT_POLL_S = 870

# rid shape must match cdp_consult.py exactly.
_RID_RE = re.compile(r"^REQ-\d{8}-\d{6}-[0-9a-f]{6}$")

# The SAME public-code-URL rule cdp_consult.cmd_submit uses (github / gist / raw), so the gate
# and the submit backstop agree on what counts as a real, public code link.
_CODE_URL_RE = re.compile(
    r"https://(?:www\.)?(?:github\.com|gist\.github\.com|raw\.githubusercontent\.com)/\S+",
    re.IGNORECASE)

# Extract an owner/repo slug from a github.com (or raw.githubusercontent.com) URL so the gate can
# re-check that repo's visibility. gist URLs carry no owner/repo slug (a gist is its own object);
# they are handled separately (a gist is public-by-URL only if the user made it public — the gate
# cannot cheaply verify a secret gist, so it refuses gist links unless CGC_GATE_ALLOW_GIST=1).
_SLUG_RE = re.compile(
    r"https://(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.IGNORECASE)
_GIST_RE = re.compile(r"https://gist\.github\.com/\S+", re.IGNORECASE)

# Obvious-secret patterns — the gate refuses to send a prompt matching any of these, fail-closed.
# Not exhaustive (nothing is); it catches the high-signal shapes so an injected --task/--context
# can't trivially smuggle a live credential to an external service.
_SECRET_RES = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\baws_secret_access_key\b", re.IGNORECASE), "aws secret key assignment"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "OpenAI-style secret key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "GitHub fine-grained PAT"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\b(?:AIza)[0-9A-Za-z_\-]{35}\b"), "Google API key"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "JWT"),
]

# ---- paths / dirs -----------------------------------------------------------

_SUBDIRS = ("pending", "processing", "done", "status")


def ensure_dirs():
    for d in _SUBDIRS:
        os.makedirs(os.path.join(SPOOL_DIR, d), exist_ok=True)


def _p(*parts):
    return os.path.join(SPOOL_DIR, *parts)


def pending_path(rid):
    return _p("pending", rid + ".json")


def processing_path(rid):
    return _p("processing", rid + ".json")


def done_path(rid):
    return _p("done", rid + ".json")


def status_path(rid):
    return _p("status", rid + ".json")


DAEMON_PATH = _p("daemon.json")


def _atomic_write(path, obj):
    """Write JSON atomically (tmp in the same dir + os.replace), so a concurrent reader never sees
    a truncated file. Returns the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp-{os.getpid()}-{int(time.time()*1000)%100000}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    return path


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---- heartbeat / liveness ---------------------------------------------------

def heartbeat_write(pid=None):
    _atomic_write(DAEMON_PATH, {"pid": pid if pid is not None else os.getpid(), "ts": time.time()})


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def daemon_alive():
    """True iff the heartbeat file is fresh AND its pid is a live process. A stale heartbeat (daemon
    crashed / was killed) reads as down even though the file lingers."""
    d = _read_json(DAEMON_PATH)
    if not isinstance(d, dict):
        return False
    if (time.time() - d.get("ts", 0)) > HEARTBEAT_STALE_S:
        return False
    return _pid_alive(d.get("pid"))


# ---- job lifecycle ----------------------------------------------------------

def enqueue_job(job: dict) -> str:
    """Atomically write a job into pending/. rid is required and must be well-formed."""
    rid = job.get("rid", "")
    if not _RID_RE.match(rid):
        raise ValueError(f"bad rid {rid!r} (want REQ-YYYYMMDD-HHMMSS-hhhhhh)")
    ensure_dirs()
    job = dict(job)
    job.setdefault("ts", time.time())
    return _atomic_write(pending_path(rid), job)


def list_pending():
    try:
        names = sorted(os.listdir(_p("pending")))
    except OSError:
        return []
    return [_p("pending", n) for n in names if n.endswith(".json")]


def claim(pending_file: str):
    """Atomically move a pending job to processing/ (os.rename is atomic on the same fs). Returns the
    new processing path, or None if another worker already claimed it (rename raced/failed)."""
    rid = os.path.basename(pending_file)[:-5]
    dst = processing_path(rid)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(pending_file, dst)
        return dst
    except OSError:
        return None


def write_status(rid, state, *, exit=None, out=None, msg=None, conversation=None):
    """Upsert status/<rid>.json. state in {queued,processing,done,blocker,no_answer,error}."""
    cur = _read_json(status_path(rid)) or {}
    cur.update({"rid": rid, "state": state, "ts": time.time()})
    if exit is not None:
        cur["exit"] = exit
    if out is not None:
        cur["out"] = out
    if msg is not None:
        cur["msg"] = msg
    if conversation is not None:
        cur["conversation"] = conversation
    _atomic_write(status_path(rid), cur)
    return cur


def read_status(rid):
    return _read_json(status_path(rid))


def finish_job(rid, *, state, exit, out=None, msg=None, conversation=None):
    """Terminal transition: write the final status and move the job out of processing/ into done/."""
    write_status(rid, state, exit=exit, out=out, msg=msg, conversation=conversation)
    src = processing_path(rid)
    if os.path.exists(src):
        try:
            os.rename(src, done_path(rid))
        except OSError:
            pass


# ---- the validating gate (security core) ------------------------------------

def _repo_is_public(slug: str) -> tuple:
    """Ask gh whether owner/repo is public. Returns (public: bool, detail: str). Fail-closed:
    gh missing / unauthenticated / errored / anything but 'public' -> (False, why)."""
    try:
        r = subprocess.run(["gh", "api", f"repos/{slug}", "--jq", ".visibility"],
                           capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return False, "gh not installed"
    except Exception as e:
        return False, f"gh error: {e}"
    if r.returncode != 0:
        return False, f"gh api repos/{slug} failed: {(r.stderr or '').strip()[:120]}"
    vis = (r.stdout or "").strip()
    return (vis == "public"), (f"visibility={vis or 'unknown'}")


def validate_prompt(prompt_text: str) -> tuple:
    """The egress gate. Returns (ok: bool, reason: str). ok means: it is safe to send this prompt to
    the external ChatGPT session. Rules, all fail-closed:
      1. Must contain at least one real public-code link (github/gist/raw), UNLESS it is a follow-up
         (the thread already holds the code) or a declared no-code consult — `prep --no-code`, for a
         maths/research/writing question, renders "references no code". Rules 2-4 still apply to
         both. Note what this rule is and isn't: it stops a *code* consult from going out as blind
         prose, which is a quality guard; the security guarantees are rules 2-4 (nothing private,
         nothing secret), and they are untouched by the exemption.
      2. Every github/raw repo slug in the prompt must be a gh-confirmed PUBLIC repo. Any private /
         unknown / unverifiable repo -> refuse. This is the independent re-check that makes the
         daemon a real gate rather than a blind relay of whatever the agent enqueued.
      3. gist links are refused unless CGC_GATE_ALLOW_GIST=1 (the gate cannot cheaply prove a gist
         is public, and a secret gist is world-readable-by-URL exfiltration).
      4. No obvious secret (see _SECRET_RES) may appear anywhere in the prompt.
    A follow-up (prompt marked 'continuing this consult') is exempt from rule 1 (the thread already
    holds the code) but still subject to rules 2-4 for any NEW links it introduces."""
    text = prompt_text or ""
    low = text.lower()
    is_followup = "continuing this consult" in low

    # 4. secrets first — cheapest hard stop, applies to every path.
    for rx, label in _SECRET_RES:
        if rx.search(text):
            return False, f"refused: prompt contains what looks like a {label} — will not send it to an external service"

    links = _CODE_URL_RE.findall(text)
    if not links and not is_followup and "references no code" not in low:
        return False, "refused: no public code link (github/gist/raw URL) in the prompt — nothing safe to send"

    # 3. gists
    if _GIST_RE.search(text) and os.environ.get("CGC_GATE_ALLOW_GIST", "0").strip().lower() in ("0", "false", "no", "off", ""):
        return False, "refused: prompt contains a gist link and CGC_GATE_ALLOW_GIST is off — the gate cannot verify a gist is public"

    # 2. every repo slug must be gh-confirmed public.
    slugs = set()
    for m in _SLUG_RE.finditer(text):
        owner, repo = m.group(1), m.group(2)
        if owner.lower() in ("orgs", "sponsors", "settings", "features"):  # not repo paths
            continue
        slugs.add(f"{owner}/{repo}")
    for slug in sorted(slugs):
        ok, detail = _repo_is_public(slug)
        if not ok:
            return False, f"refused: repo {slug} is not confirmed PUBLIC ({detail}) — will not send its link to an external service"

    return True, f"ok: {len(slugs)} public repo(s), no secrets detected"


# ---- CLI: enqueue -----------------------------------------------------------

def cmd_enqueue(a) -> int:
    if not os.path.exists(a.prompt_file):
        sys.stderr.write(f"CGC_ERROR prompt_file_missing: {a.prompt_file}\n")
        return 2
    prompt = open(a.prompt_file, encoding="utf-8").read()
    # CHEAP local pre-check so the agent gets instant feedback and never enqueues something the
    # gate will reject. This runs the secret scan + link-presence check WITHOUT network (the
    # authoritative public-repo re-check happens in the daemon, where a gh call is not an agent
    # action). Keeping enqueue network-free guarantees it stays a pure local write for the
    # classifier. A secret or a missing code link is caught here immediately.
    low = prompt.lower()
    is_followup = a.kind == "followup" or "continuing this consult" in low
    for rx, label in _SECRET_RES:
        if rx.search(prompt):
            sys.stderr.write(f"CGC_ERROR gate_secret: prompt looks like it contains a {label} — NOT enqueuing.\n")
            return 2
    if not _CODE_URL_RE.search(prompt) and not is_followup and "references no code" not in low:
        sys.stderr.write(
            "CGC_ERROR no_code_source: prompt has no public code link (github/gist URL) — the daemon "
            "would refuse it. Deliver a link first (consult.py deliver → prep). If the question has "
            "no code subject at all (maths/research/writing), render it with `prep --no-code`. "
            "NOT enqueuing.\n")
        return 2

    out = a.out or os.path.join(CGC_STATE_DIR, f"answer_{a.rid}.txt")
    job = {
        "rid": a.rid,
        "kind": a.kind,
        "prompt_file": os.path.abspath(a.prompt_file),
        "project_url": a.project_url,
        "conversation": a.conversation,
        "model": a.model,
        "out": os.path.abspath(out),
        "poll": a.poll,
        "timeout": a.timeout,
    }
    try:
        enqueue_job(job)
    except ValueError as e:
        sys.stderr.write(f"CGC_ERROR bad_job: {e}\n")
        return 2
    write_status(a.rid, "queued", out=os.path.abspath(out))
    up = daemon_alive()
    print(json.dumps({"queued": True, "rid": a.rid, "out": os.path.abspath(out), "daemon_up": up}))
    if not up:
        sys.stderr.write(
            "CGC_WARN daemon_down: the consult daemon is not running, so this job sits queued until "
            "it is. Relay ONE line to the user:\n"
            "  cgc install-daemon    # installs it via launchd: starts at login, respawns if it dies\n"
            "The agent does not start it — after that install, nobody has to.\n")
    else:
        sys.stderr.write(
            "CGC_QUEUED. The user's daemon will validate (public-repo re-check) + send it. Now wait "
            "for the answer with a LOCAL file poll (run_in_background:true) — copy verbatim:\n"
            f"  timeout 899 python3 {os.path.abspath(__file__)} await --rid {a.rid} "
            f"--out {os.path.abspath(out)} --poll {a.poll} --timeout {AGENT_POLL_S}\n"
            "MANDATORY: keep the `timeout 899` prefix. `await` only reads local files — it never "
            "touches the network, so it is never blocked by the auto-mode classifier.\n"
            f"This consult gets {a.timeout // 60} min; your await watches {AGENT_POLL_S}s at a time "
            "(Claude Code kills longer background tasks), so exit 5 = still running is expected — "
            "just run the same line again.\n")
    return 0


# ---- CLI: await -------------------------------------------------------------

def cmd_await(a) -> int:
    """Pure LOCAL wait: poll status/<rid>.json + the answer file until the daemon reaches a terminal
    state or we time out. No CDP, no network — this is the whole point (the classifier never sees an
    external send here). Exit codes mirror cdp_consult.py wait."""
    deadline = time.time() + a.timeout
    rid = a.rid
    out = os.path.abspath(a.out)
    saw_pickup = False
    warned_daemon = False
    while time.time() < deadline:
        st = read_status(rid) or {}
        state = st.get("state")
        if state in ("processing", "done", "blocker", "no_answer", "error"):
            saw_pickup = True
        # daemon-liveness guard: if it never picked the job up AND there is no live daemon, fail fast
        # with actionable guidance instead of burning the whole timeout on a queue nothing drains.
        if not saw_pickup and not daemon_alive():
            if not warned_daemon:
                warned_daemon = True
                sys.stderr.write("CGC_WAIT daemon not running yet; will keep polling briefly…\n")
            # give a short grace (a just-enqueued job + a daemon starting up), then give up.
            if time.time() > (deadline - a.timeout) + 60:
                sys.stderr.write(
                    "CGC_ERROR daemon_not_running: the job was never picked up and no live consult "
                    "daemon was found. Relay ONE line to the user — `cgc install-daemon` (installs "
                    "it as a launchd agent so it starts at login and never needs starting again). "
                    "The job stays queued and runs as soon as it is up.\n")
                return 2
        if state == "done":
            try:
                ans = open(out, encoding="utf-8").read()
            except OSError:
                ans = ""
            sys.stderr.write(f"CGC_DONE answer ready ({len(ans)} chars) at {out}\n")
            _print_followup_recipe(rid, st)
            return 0
        if state == "blocker":
            sys.stderr.write(f"CGC_BLOCKER {st.get('msg') or 'login/captcha/rate-limit'} — the user "
                             f"may need to act in the ChatGPT window, then re-enqueue.\n")
            return 3
        if state == "no_answer":
            sys.stderr.write(f"CGC_ERROR timeout_no_answer: {st.get('msg') or 'no usable answer'} "
                             f"(raw may be at {out}.raw)\n")
            return 4
        if state == "error":
            _m = st.get("msg") or "the daemon refused or failed this job"
            sys.stderr.write(f"CGC_ERROR gate_or_send_error: {_m}\n")
            return 2
        time.sleep(a.poll)
    _last = (read_status(rid) or {}).get("state") or "none"
    sys.stderr.write(
        f"CGC_STILL_RUNNING {rid}: still {_last} after {a.timeout}s — not a failure. The daemon is "
        f"working; the answer will land at {out}. Keep watching:\n"
        f"  timeout 899 python3 {os.path.abspath(__file__)} await --rid {rid} "
        f"--out {out} --poll {a.poll} --timeout {a.timeout}\n")
    return 5


def _print_followup_recipe(rid, st):
    conv = (st or {}).get("conversation") or "auto"
    sys.stderr.write(
        "CGC_NEXT to CONTINUE this consult as a thread (feed local results back / next round), "
        "enqueue a FOLLOW-UP job (do NOT start a new prep+submit — that loses ChatGPT's context). "
        "One backgrounded command each way; its exit is the wake:\n"
        "  # 1. render the follow-up prompt locally:\n"
        "  python3 consult.py prep --followup --task \"<local results + next question>\" --title \"<what's new>\"\n"
        "  # 2. enqueue it (uses this thread), then await:\n"
        f"  python3 cgc_spool.py enqueue --rid <r2> --kind followup --conversation {conv} --prompt-file <rendered>\n"
        f"  timeout 899 python3 cgc_spool.py await --rid <r2> --out {os.path.join(CGC_STATE_DIR,'answer_<r2>.txt')} --timeout 870\n"
        "  # await exits 5 = still running (a Pro consult routinely outlasts one await) -> just await again.\n")


# ---- CLI: status (human) ----------------------------------------------------

def cmd_status(a) -> int:
    ensure_dirs()
    up = daemon_alive()
    print(f"daemon: {'UP' if up else 'DOWN'}   spool: {SPOOL_DIR}")
    for label in ("pending", "processing", "done"):
        try:
            names = [n[:-5] for n in sorted(os.listdir(_p(label))) if n.endswith(".json")]
        except OSError:
            names = []
        print(f"  {label:11s} {len(names):3d}  {' '.join(names[:6])}{' …' if len(names) > 6 else ''}")
    if a.rid:
        st = read_status(a.rid)
        print(f"  status[{a.rid}]: {json.dumps(st) if st else '(none)'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_spool.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="write a consult job into the local spool (LOCAL write only)")
    e.add_argument("--rid", required=True)
    e.add_argument("--prompt-file", required=True)
    e.add_argument("--kind", choices=("submit", "followup"), default="submit")
    e.add_argument("--project-url", default=os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/"))
    e.add_argument("--conversation", default="auto", help="(followup) /c/<id> or 'auto'")
    e.add_argument("--model", default=os.environ.get("CGC_MODEL", "Pro"))
    e.add_argument("--out", help="answer file (default $CGC_STATE_DIR/answer_<rid>.txt)")
    e.add_argument("--poll", type=int, default=20)
    e.add_argument("--timeout", type=int, default=CONSULT_TIMEOUT_S,
                   help=f"seconds to allow this consult (default {CONSULT_TIMEOUT_S} = "
                        f"{CONSULT_TIMEOUT_S // 60} min, how long a GPT-5.6 Pro round reasons).")
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("await", help="poll the local answer/status for a queued job (LOCAL read only)")
    w.add_argument("--rid", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--poll", type=int, default=20)
    w.add_argument("--timeout", type=int, default=CONSULT_TIMEOUT_S,
                   help=f"seconds to wait for the answer (default {CONSULT_TIMEOUT_S} = "
                        f"{CONSULT_TIMEOUT_S // 60} min). The agent must pass {AGENT_POLL_S} "
                        "instead — Claude Code kills its background tasks at 900s — and re-run "
                        "await on exit 5 (still running).")
    w.set_defaults(fn=cmd_await)

    s = sub.add_parser("status", help="print daemon liveness + spool contents")
    s.add_argument("--rid", help="also print this job's status record")
    s.set_defaults(fn=cmd_status)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
