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
Egress gate + daemon runtime files for the store-backed consult path.

The round LIFECYCLE lives in the SQLite store (cgc_store.py) — the file-spool
pending/processing/done/status machinery it replaced is gone. What remains here is everything that
is not lifecycle state:

  - the VALIDATING GATE (validate_prompt + helpers) — the security core the daemon runs on every
    round before anything leaves the machine
  - daemon runtime files under $CGC_SPOOL_DIR (default $CGC_STATE_DIR/spool):
      daemon.json    {pid, ts} liveness heartbeat the daemon refreshes each loop
      daemon.lock    the daemon-singleton flock
      logs/<rid>.log per-round send+wait transcript, streamed live by the daemon
  - the agent-facing CLI (dispatched by bin/cgc; both LOCAL-ONLY, never touch the network):
      enqueue  -> create a queued round in the store. Local write only.
      await    -> poll the round until terminal; three outcomes (0 answer / 3 human / 1 broken).
      status   -> daemon liveness + round counts by state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from typing import NamedTuple
from urllib.parse import quote, unquote, urlsplit

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

import cgc_config as _cfg  # noqa: E402 — config_path/_parse: the gate re-reads the registry file
import cgc_store as _store  # noqa: E402 — the durable data dir that anchors the lock namespace

CGC_STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")
SPOOL_DIR = os.environ.get("CGC_SPOOL_DIR", os.path.join(CGC_STATE_DIR, "spool"))

# A consult with no fresh daemon heartbeat within this many seconds is treated as "daemon down".
HEARTBEAT_STALE_S = 45

# How long `await` tolerates a missing daemon before giving up. launchd (KeepAlive) respawns it in
# seconds, so this only has to outlast a restart, not a repair.
DAEMON_GRACE_S = 60

# THE timeout — the only deadline in this system.
#
# This store's own completion latency over 63 GPT-5.6 rounds was p50 1993s / p90 4802s; no
# GPT-6 Astra round has been measured yet, so "~25 min" is that GPT-5.6 data carried forward as
# an expectation, not a figure measured for Astra itself — the shape of a typical round, not its
# ceiling. That is how long the work TAKES; it is an expectation,
# not a deadline, and nothing may be killed for reaching it. A deadline answers a different
# question: past what point is waiting no longer explained by the work? An hour. Beyond that the
# answer is not late — something is broken — and the right response is to read the job log, not to
# keep waiting.
#
# Everything that used to be a second or third "timeout" was one of these two things wearing the
# wrong name: a 1500s per-consult budget (an expectation) and an 870s agent window (an observation
# interval). Both killed healthy consults for the crime of taking as long as they take.
#
# 90 min, not 60: a deep GPT-6 Astra Pro round — especially a follow-up that triggers fresh re-reasoning
# — was observed to think for ~62 min before emitting its answer. At the old 3600s the waiter timed
# out minutes BEFORE the answer landed, stranded the round as possibly_accepted, and only the
# read-only auto-retrieve recovered it (a wasted hour + a detour). The completion signal itself is
# prompt — `done` fires the instant generation stops — so the only failure was budget < work. 5400s
# covers the observed long tail with margin; auto-retrieve still backstops anything beyond it.
STUCK_AFTER_S = 5400

# How often to look. An interval, not a deadline.
POLL_S = 20

# The three phrases that declare a LEGITIMATE reason a prompt carries no code LINK (compared against
# the lowercased prompt): a follow-up, a self-contained no-code question, or a question whose evidence
# lives in a local WebCodex workspace (`fire --workspace`, which requires a --mention). The first two
# are prose declarations, and rule 1 is a quality guard, not a security boundary. The third is NOT
# honoured on its text: a sentence anyone can type must not decide which prompts need a public source,
# so validate_prompt exempts a prompt only when the round's recorded spec registers the workspace
# (workspace_refusal). The phrase stays here to recognize a prompt that CLAIMS a workspace, for
# enqueue's pre-check and for refusal wording. None of them exempts any other rule.
_WORKSPACE_SENTINEL = "source: local workspace `"

# rid shape must match cdp_consult.py exactly.
_RID_RE = re.compile(r"^REQ-\d{8}-\d{6}-[0-9a-f]{6}$")


def new_rid() -> str:
    """Mint a rid. Lives next to the regex that validates it so the shape has exactly one
    definition — it used to be written here and spelled out again in consult.cmd_prep, two
    copies of a format whose mismatch is unobservable until a round refuses to enqueue."""
    return "REQ-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def default_out(rid: str) -> str:
    """Where a round's answer file goes when nobody chose one."""
    return os.path.join(CGC_STATE_DIR, f"answer_{rid}.txt")

# ---- the URL law ------------------------------------------------------------
#
# The gate judges the URL a browser would actually request. Every `http(s):` token in the prompt is
# parsed ONCE, and that one parse feeds the visibility check, the existence check and the gist rule,
# so no check can look at a different repo than the link reaches. A token is a GitHub URL when its
# host, after what a browser does before connecting (drop userinfo and port, percent-decode, IDNA-fold
# fullwidth and ideographic-dot variants, drop trailing dots, lowercase), is one of _GH_HOSTS. A
# GitHub URL must then be written plainly or it is REFUSED, never normalized and accepted:
#   scheme https with `//`; host written exactly (no userinfo, port, escape, trailing dot or
#   non-ASCII); no backslash; path with no `.`/`..` segment, no percent-escape and no empty segment
#   (one trailing slash is the same page); owner [A-Za-z0-9-], repo [A-Za-z0-9._-].
# A URL that is not a GitHub URL is none of the gate's business (a docs link, an arXiv link).
_GH_HOSTS = {"github.com": "repo", "www.github.com": "repo", "raw.githubusercontent.com": "raw",
             "gist.github.com": "gist", "gist.githubusercontent.com": "gist"}
_URL_TOKEN_RE = re.compile(r"https?:[^\s<>\"'`]+", re.IGNORECASE)
_OWNER_RE = re.compile(r"[A-Za-z0-9-]+")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+")
_DIGITS_RE = re.compile(r"[0-9]+")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")
# github.com/<one of these>/... is a page of the site, never a repo (GitHub reserves the names).
_NON_REPO_OWNERS = ("orgs", "sponsors", "settings", "features", "about", "topics", "trending", "marketplace")
_REF_SEGMENTS = 8     # a ref may itself contain '/': at most this many leading segments form it
# github.com/<owner>/<repo>/<one of these>/<ref>[/<path>]: the ref is a prefix of the rest of the path.
_REF_PATH_KINDS = ("tree", "blob", "blame", "raw", "commits", "find", "edit")


class Link(NamedTuple):
    host: str       # "repo" | "raw" | "gist"
    slug: str       # "owner/repo"; "" for a gist
    target: tuple   # () | ("pull", "12") | ("commit", "<ref>") | ("tree"|"blob", ("seg", ...))


def _trim(tok: str) -> str:
    """Drop what ends a sentence rather than a URL: trailing `,;:!?)]}` and ONE `.`. Nothing else is
    removed or rewritten; a segment that still ends in `.` (`main..`, `repo..`) is refused by the
    parser, because git forbids a ref ending in `.` and a link must be checked as written."""
    dot = False
    while tok:
        if tok[-1] in ",;:!?)]}":
            tok = tok[:-1]
        elif tok[-1] == "." and not dot:
            tok, dot = tok[:-1], True
        else:
            break
    return tok


def _gh_host(authority: str) -> str:
    """The host a browser would connect to for this URL authority, or "" when it cannot be one."""
    hostport = authority.rpartition("@")[2]
    if hostport.startswith("["):
        return ""
    try:
        return unquote(hostport.partition(":")[0]).encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError:
        return ""


def _parse_code_urls(text: str) -> tuple:
    """Every GitHub URL in `text` as a Link, in prompt order, or (None, refusal) on the first one that
    breaks the URL law. Returns (links | None, refusal | None)."""
    links = []
    for m in _URL_TOKEN_RE.finditer(text):
        tok = _trim(m.group(0))
        scheme, _, rest = tok.partition(":")
        try:
            u = urlsplit("https://" + rest.replace("\\", "/").lstrip("/"))
        except ValueError:
            continue    # a browser rejects it too: not a request to GitHub
        host = _gh_host(u.netloc)
        kind = _GH_HOSTS.get(host)
        if kind is None:
            continue

        def no(why, tok=tok, host=host):
            return None, f"refused: {why} in GitHub link {tok[:120]!r} — write it as https://{host}/..."

        if scheme.lower() != "https":
            return no("a non-https scheme")
        if not re.match(r"https://[^/]", tok, re.IGNORECASE):
            return no("a malformed scheme separator")
        if "\\" in tok:
            return no("a backslash")
        if u.netloc.lower() != host:
            return no("userinfo, a port, an escape, a trailing dot or a non-ASCII character in the host")
        if "%" in u.path:
            return no("a percent-escape in the path")
        segs = u.path.split("/")[1:]
        if segs and segs[-1] == "":
            segs.pop()
        if not segs:
            continue    # the site root: no repo, nothing to fetch
        if "" in segs:
            return no("an empty path segment")
        if "." in segs or ".." in segs:
            return no("a `.` or `..` path segment")
        if segs[-1].endswith("."):
            return no("a path segment ending in `.`")
        if kind == "gist":
            links.append(Link("gist", "", ()))
            continue
        owner, repo = segs[0], (segs[1] if len(segs) > 1 else "")
        if kind == "repo" and owner.lower() in _NON_REPO_OWNERS:
            continue    # reserved names no account can hold: a site page, whatever follows
        if not repo or not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repo):
            return no("no verifiable owner/repo")
        rest_segs, target = segs[2:], ()
        if kind == "raw" and rest_segs:
            target = ("raw", tuple(rest_segs))
        elif kind == "repo" and rest_segs:
            what = rest_segs[0]
            if what == "pull" and len(rest_segs) > 1 and _DIGITS_RE.fullmatch(rest_segs[1]):
                target = ("pull", str(int(rest_segs[1])))          # 012 and 12 are one PR
            elif what == "commit":
                if len(rest_segs) != 2:
                    return no("a commit link that is not exactly one SHA or ref")
                target = ("commit", re.sub(r"\.(?:patch|diff)$", "", rest_segs[1]))
            elif what in _REF_PATH_KINDS and len(rest_segs) > 1:
                target = (what, tuple(rest_segs[1:]))
            elif what == "compare" and len(rest_segs) > 1:
                a_, dots, b_ = "/".join(rest_segs[1:]).partition("...")
                if not a_ or not dots or not b_ or "..." in b_:
                    return no("a compare link that is not A...B (three dots)")
                target = ("compare", "/".join(rest_segs[1:]))
        links.append(Link(kind, f"{owner}/{repo}", target))
    return links, None


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

_SUBDIRS = ("logs",)


def _lock_dir():
    """The coordination namespace: daemon singleton, browser lock, per-rid leases, and the heartbeat
    live HERE — under the durable data dir beside control.db, never under SPOOL_DIR. SPOOL_DIR
    (CGC_STATE_DIR/spool) is documented deletable scratch and is overridable per generation; a lock
    file that can be unlinked or moved out from under a holder splits the fencing namespace (the old
    inode stays locked while a new process locks a fresh inode at the same path), defeating the
    singleton and ownership fences. Anchoring locks to the same durable, generation-stable directory
    as the store they fence removes both failure modes. Resolved at call time (like the store's own
    path helpers) so tests and late-loaded config are honoured."""
    db = _store.db_path()
    d = os.path.dirname(db)
    if not d or db == ":memory:":
        d = _store.data_dir()
    return os.path.join(d, "locks")


def ensure_dirs():
    """Create the runtime dirs private to this user. The PROMPTS are public by construction — the
    gate enforces that. The ANSWERS and logs are not: a consult's reply can quote private context,
    and follow-up rounds carry local results outright. Leaving them at the umask's mercy under a
    world-traversable /tmp made confidentiality a property of the host's configuration rather than
    of this tool. The lock dir sits under the durable data dir, not spool (see _lock_dir)."""
    for d in (CGC_STATE_DIR, SPOOL_DIR, _lock_dir()):
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    for d in _SUBDIRS:
        os.makedirs(os.path.join(SPOOL_DIR, d), mode=0o700, exist_ok=True)


def _p(*parts):
    return os.path.join(SPOOL_DIR, *parts)


def _lp(*parts):
    return os.path.join(_lock_dir(), *parts)


def log_path(rid):
    """Everything the daemon's worker saw while running this job. A failed consult used to leave
    only a 240-char tail inside a status file, which is not enough to tell a login lapse from a
    silent send from a model that never answered — so a failure must name where its own evidence is."""
    return _p("logs", rid + ".log")


def daemon_path():
    return _lp("daemon.json")


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

def heartbeat_write(pid=None, identity=None):
    """`identity` (dict) is the daemon's self-description — protocol, schema_version, db_path,
    store_uuid, daemon_instance_id. enqueue compares it against the store IT opened and refuses on
    mismatch, so an old-code or wrong-store daemon (the live split-brain failure) is caught at the
    first write, not 25 minutes later."""
    d = {"pid": pid if pid is not None else os.getpid(), "ts": time.time()}
    if identity:
        d.update(identity)
    _atomic_write(daemon_path(), d)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def daemon_identity():
    """The live daemon's heartbeat dict (pid, ts + its identity fields), or None when no live
    daemon. A stale heartbeat (daemon crashed / was killed) reads as down even though the file
    lingers."""
    d = _read_json(daemon_path())
    if not isinstance(d, dict):
        return None
    if (time.time() - d.get("ts", 0)) > HEARTBEAT_STALE_S:
        return None
    return d if _pid_alive(d.get("pid")) else None


def daemon_alive():
    return daemon_identity() is not None


def daemon_lock_path():
    return _lp("daemon.lock")


def _require_fcntl():
    """Fencing is a hard requirement, not a nicety: the daemon singleton, per-round ownership, and
    browser-maintenance exclusion all rest on real flocks. On a platform without fcntl there is no
    advisory lock to take, so granting a fake lease would silently defeat every fence at once. Fail
    closed — the caller (daemon startup) treats this as fatal."""
    if fcntl is None:
        raise RuntimeError("this platform lacks fcntl.flock — cross-process fencing is unavailable; "
                           "refusing to run without it")


def acquire_daemon_singleton(timeout=40):
    """Take an exclusive, process-lifetime lock proving THIS is the only daemon on this spool.

    The daemon's concurrency limit and child map live in one process's memory, so a second daemon
    (launchd + `cgc watch` + an ad-hoc start can each spawn one) has no knowledge of the first's
    children — the global cap silently doubles and two supervisors race on the same spool and
    browser. Returns the open file handle on success (the CALLER must keep it alive for the daemon's
    lifetime; the lock releases when the process exits and the fd closes), or None if it could not be
    taken within `timeout`, in which case this process must exit rather than run a second supervisor.

    It WAITS (up to timeout) rather than failing instantly: on SIGTERM the outgoing daemon holds the
    lock through a ~30s drain, and a supervised respawn that gave up immediately left a no-daemon
    window until the next respawn. Waiting past the drain lets the respawn take over cleanly, with no
    two-daemon overlap. Pass timeout=0 for an immediate, non-blocking check."""
    _require_fcntl()
    ensure_dirs()
    fh = open(daemon_lock_path(), "a+")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= deadline:
                fh.close()
                return None
            time.sleep(0.5)


# ---- worker / browser leases (cross-generation fencing) ---------------------
# The daemon's in-memory children map only knows workers THIS daemon generation spawned. A worker
# that outlives its parent (shutdown waits 30s then forgets) is invisible to the replacement daemon,
# which would otherwise sweep its tab, restart its Chrome, spawn a duplicate waiter for its round,
# or promote its in-progress `sending` row. These flock leases are the cross-generation truth: held
# for the holder's process lifetime, released by the OS on ANY death (the same mechanism as the
# daemon singleton above them).
#   - per-RID lease (exclusive): a worker owns its round. Anyone else must prove the lease is FREE
#     before touching that round's in-flight state.
#   - browser lease: workers hold SHARED; browser-global actions (tab sweep, Chrome restart) need
#     EXCLUSIVE — impossible while any worker of any generation is alive.

def browser_lock_path():
    return _lp("browser.lock")


def _lease_path(rid):
    return _lp(f"lease.{rid}")


# Live-lease fd registry (Fix S3-A). flock is bound to the OPEN FILE DESCRIPTION, not the process,
# so a mutating CDP subprocess that INHERITS a lease fd keeps that exact lock alive until BOTH the
# backend worker and the CDP child have closed it — the backend dying mid-click no longer releases
# ownership out from under the running mutation. The daemon reads this set to hand the precise fds to
# each mutating subprocess via subprocess pass_fds. Per-process (a module global): each backend
# worker is its own process and holds only its own leases.
_LIVE_LEASE_FDS: set[int] = set()


class _Lease:
    """A held flock lease: the open file handle whose open file description carries the lock, with
    its fd tracked in _LIVE_LEASE_FDS for the lease's lifetime. Every lease-acquire helper returns
    one of these; `.close()` releases the flock (closing the fd) and de-registers it. Exposes only
    the surface the call sites use — truthiness (never None on success), `.fileno()`, `.close()`."""
    __slots__ = ("_fh",)

    def __init__(self, fh):
        self._fh = fh
        _LIVE_LEASE_FDS.add(fh.fileno())

    def fileno(self) -> int:
        return self._fh.fileno()

    def close(self) -> None:
        if self._fh is None:
            return
        # De-register BEFORE close: once the fd is closed fileno() raises, and a closed fd may be
        # reused by a later open(), so a stale number left in the set would falsely mark that fd
        # inheritable.
        _LIVE_LEASE_FDS.discard(self._fh.fileno())
        self._fh.close()
        self._fh = None


def live_lease_fds() -> tuple:
    """The fds of every lease held IN THIS PROCESS right now (per-RID, shared-browser, and — during a
    followup's mutating region — the conversation lease). The daemon passes these to each browser-
    mutating cdp_consult subprocess as pass_fds so the inherited descriptors preserve the already-
    established flock ownership continuously across the backend->CDP boundary (see _Lease)."""
    return tuple(sorted(_LIVE_LEASE_FDS))


def _flock(path, flags):
    """Non-blocking flock on `path`. Returns a _Lease wrapping the open file handle (caller keeps it
    alive for the lease's lifetime; its fd is registered in _LIVE_LEASE_FDS) or None if the lock is
    held elsewhere. Fails CLOSED (raises) when fcntl is unavailable — a fake lease would silently
    defeat the fence it exists to provide."""
    _require_fcntl()
    ensure_dirs()
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), flags | fcntl.LOCK_NB)
        return _Lease(fh)
    except OSError:
        fh.close()
        return None


def acquire_rid_lease(rid):
    """Exclusive ownership of one round, for a worker's whole process lifetime."""
    return _flock(_lease_path(rid), fcntl.LOCK_EX if fcntl else 0)


def rid_lease_free(rid) -> bool:
    """Is NO process (of any daemon generation) working this round? Probes by briefly taking the
    exclusive lease and releasing it."""
    fh = acquire_rid_lease(rid)
    if fh is None:
        return False
    if fcntl is not None:
        fh.close()  # closing the fd releases the flock
    return True


def acquire_browser_lease(shared: bool):
    """SHARED while a worker uses the browser; EXCLUSIVE for browser-global maintenance
    (tab sweep / Chrome restart). Exclusive is unobtainable while any worker lives."""
    return _flock(browser_lock_path(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) if fcntl else 0)


# A canonical ChatGPT conversation id: the bare uuid (8-4-4-4-12 hex) that sits at /c/<id>, and the
# ONLY shape allowed to key a conversation lease. A consult RID (REQ-…), a '/c/<id>' path, or a full
# URL are ALIASES: resolvable to this via the store / CDP registry, but they MUST be canonicalized
# (or rejected) before touching a lock key. Locking on a raw alias is exactly S3-B — an explicit
# --conversation REQ-R0 locks conv.REQ-R0 while --parent R0 resolves to C and locks conv.C, so two
# handles for one thread drive its one composer under two different files.
_CONV_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_canonical_conversation(conversation) -> bool:
    """True iff `conversation` is a bare canonical ChatGPT conversation id (see _CONV_ID_RE). The
    single source of truth for 'this value may key a conversation lease'; every lease-taking and
    lease-locking path gates on it so equivalent-but-differently-spelled handles cannot split the
    lock namespace."""
    return bool(_CONV_ID_RE.fullmatch(conversation or ""))


def _conversation_lease_path(conversation):
    """Same namespace + convention as _lease_path: locks live beside the one control.db, which
    already scopes them to THIS store, so the conversation id alone keys the file (no store prefix).
    Callers guarantee a canonical id (is_canonical_conversation), which is already filename-safe; the
    sanitize is a belt-and-braces no-op for that shape."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conversation or "")
    return _lp(f"conv.{safe}")


def acquire_conversation_lease(conversation):
    """EXCLUSIVE per-conversation mutator lease: at most one worker may drive a single ChatGPT
    conversation's shared composer (attach -> clear -> paste -> verify -> click -> landing-rid) at a
    time. Two follow-ups pinned to the SAME conversation hold DISTINCT rid leases, so the rid lease
    cannot serialize them; W1's click can submit W2's freshly-pasted bytes, and W2's post-paste read
    then sees the cleared composer and exits not-sent-proven though its bytes WERE sent — a retry
    re-sends them. This lease is what makes the pre-click not-sent proof compositional across workers.
    Held (open fh) from before any composer mutation through the CDP follow-up subprocess's return.
    Returns the lease, or None when another worker already holds it (the caller must refuse the round,
    leaving it READY for redispatch). Fresh submits (new thread) and read-only waits/retrieves never
    take it.

    Defense in depth (S3-B): every legal caller now guarantees `conversation` is a canonical id —
    enqueue rejects aliases, the worker revalidates before locking — so a non-canonical key here is a
    caller bug, not input to sanitize. Assert and RAISE rather than lock a mis-keyed file."""
    if not is_canonical_conversation(conversation):
        raise ValueError(
            f"acquire_conversation_lease: {conversation!r} is not a canonical ChatGPT conversation id "
            "(uuid) — the lock key must be canonicalized upstream (enqueue/worker), never here")
    return _flock(_conversation_lease_path(conversation), fcntl.LOCK_EX if fcntl else 0)


# ---- the validating gate (security core) ------------------------------------

# `gh` normally answers in well under a second, but it reads its token from the OS keyring, and a
# background/launchd daemon's keyring access can block far longer than an interactive shell's. One
# attempt at 20s cost a real consult its whole 25-minute budget, so: a wider window, and one retry.
_GH_TIMEOUT_S = 25
_GH_ATTEMPTS = 2


def _private_allowlist() -> set:
    """Repos the USER has declared may be sent even though they are not public.

    A ChatGPT GitHub connector can read repos the account has access to, public or private, so
    "ChatGPT cannot open a private link" stops being true once one is configured. That removes the
    CAPABILITY argument for public-only, but not the SAFETY one: this gate is the mitigation for a
    prompt-injected agent exfiltrating private data, and a plain on/off switch would let an injected
    agent name ANY private repo the connector can reach. An allowlist keeps the decision with the
    human and bounds the blast radius to repos they actually named. `*` restores the boolean
    behaviour for anyone who wants it, deliberately and in one obvious place."""
    raw = os.environ.get("CGC_GATE_PRIVATE_REPOS", "").strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _gist_opt_in() -> bool:
    """The operator's explicit `CGC_GATE_ALLOW_GIST=1` (also true/yes/on). Anything else is off."""
    return os.environ.get("CGC_GATE_ALLOW_GIST", "").strip().lower() in ("1", "true", "yes", "on")


def _repo_is_public(slug: str) -> tuple:
    """Ask gh whether owner/repo is public. Returns (public: bool, detail: str).

    Fail-closed on every path — but the detail DISTINGUISHES the two failures, because they demand
    opposite actions from the caller. "gh says this repo is not public" is terminal: it will never
    be sendable, stop. "gh could not be asked" (missing, timed out, errored) proves NOTHING about
    the repo: the check failed, not the repo, and retrying is correct. Only the latter's detail
    starts with `unverified:`; validate_prompt turns that marker into a retryable refusal instead of
    a security verdict the repo never earned."""
    last = "unverified: gh never answered"
    for _ in range(_GH_ATTEMPTS):
        try:
            r = subprocess.run(["gh", "api", f"repos/{slug}", "--jq", ".visibility"],
                               capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
        except FileNotFoundError:
            return False, "unverified: gh not installed"
        except subprocess.TimeoutExpired:
            last = f"unverified: gh timed out after {_GH_TIMEOUT_S}s"
            continue
        except Exception as e:
            last = f"unverified: gh error: {e}"
            continue
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            # 404 IS an answer: this token cannot see the repo, so it is private or absent. Terminal.
            if "404" in err or "Not Found" in err:
                return False, "visibility=invisible to your gh token (404 — private or nonexistent)"
            last = f"unverified: gh api repos/{slug} failed: {err[:120]}"
            continue
        vis = (r.stdout or "").strip()
        return (vis == "public"), f"visibility={vis or 'unknown'}"
    return False, last


# Dead-reference detection. deliver is offline by design (the agent side must make no network
# call), so a typo'd PR number or a nonexistent ref sails through enqueue and dies 25 minutes later
# when ChatGPT hits a 404 — or worse, answers around it. The daemon already runs gh here, so the
# gate is the right (and only) place to verify the references actually exist: seconds of feedback
# instead of a wasted round.
_EXISTENCE_CAP = 48   # distinct gh probes per prompt; more is a refusal ('partial verification'), never a pass


def _gh_exists(path: str) -> tuple:
    """Does this GitHub API path resolve? Returns (True|False|None, detail) — None = could not ask
    (gh missing/timed out), which is 'unverified', not a verdict."""
    last = "unverified: gh never answered"
    for _ in range(_GH_ATTEMPTS):
        try:
            r = subprocess.run(["gh", "api", path, "--jq", "1"],
                               capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
        except FileNotFoundError:
            return None, "unverified: gh not installed"
        except subprocess.TimeoutExpired:
            last = f"unverified: gh timed out after {_GH_TIMEOUT_S}s"
            continue
        except Exception as e:
            last = f"unverified: gh error: {e}"
            continue
        if r.returncode == 0:
            return True, "exists"
        err = (r.stderr or "").strip()
        if "404" in err or "Not Found" in err:
            return False, "404"
        if "blob is too large" in err:
            return True, "exists"   # the contents API refuses blobs over 1 MB, which proves the path is there
        if "No commit found for SHA" in err:
            # The commits API answers an unresolvable ref with 422, not 404. That is gh ANSWERING
            # "no such ref", so it must not read as an outage: unverified: requeues the round with
            # backoff, and a dead ref never reaches the refusal that names it.
            return False, "422"
        last = f"unverified: gh api {path} failed: {err[:120]}"
    return None, last


class _Stop(Exception):
    """A verdict that ends the existence pass: the refusal / unverified text for validate_prompt."""


def _verify_refs_exist(links) -> tuple:
    """Check every PR / tree / blob / commit target of `links` (the one parse validate_prompt made).
    Returns (verdict | None, n): n distinct targets verified to exist. NOT checked: issue, gist and
    /pulls links, and the sub-pages of a PR.

    Every target is checked, each gh path asked once, in prompt order. A tree/blob ref is a leading
    path prefix: the shortest one gh resolves AND under which the rest of the link exists as a path is
    taken (a SHA or `main` costs two probes, and a PR-pinned link's commit is shared), longer prefixes
    only when that fails, so branch `feat/x` is one ref and is never cut at its first segment, and
    `/blob/main/missing.py` does not verify as `main`. A `commit` link is exactly one ref. More than
    _EXISTENCE_CAP distinct probes is an explicit `refused: partial verification`, never a pass that
    checked only some. A gh outage is `unverified:` (the caller requeues), as for visibility."""
    asked, seen, n = {}, set(), 0

    def ask(path, label):
        if path not in asked:
            if len(asked) >= _EXISTENCE_CAP:
                raise _Stop(f"refused: partial verification — this prompt needs more than {_EXISTENCE_CAP} "
                            "distinct GitHub probes to check its PR / ref links and the gate will not vouch "
                            "for the rest. Nothing was sent. Link fewer (one tree at a commit SHA covers a "
                            "repo) and re-fire.")
            asked[path] = _gh_exists(path)
        ok, detail = asked[path]
        if ok is None:
            raise _Stop(f"unverified: could not check that {label} exists "
                        f"({detail.split(':', 1)[1].strip()}). Nothing is known about it and nothing was "
                        "sent. Re-enqueue to retry.")
        return ok

    def dead(label):
        return _Stop(f"refused: {label} does not exist (a dead link). ChatGPT would browse to nothing or "
                     "answer around it. Fix the reference (a commit SHA is the safest) and re-fire.")

    try:
        for link in links:
            if not link.target:
                continue
            kind, arg = link.target
            if (link.slug.lower(), kind, arg) in seen:
                continue
            seen.add((link.slug.lower(), kind, arg))
            base, where = f"repos/{link.slug}", f"github.com/{link.slug}"
            if kind == "pull":
                label = f"PR {where}/pull/{arg}"
                if not ask(f"{base}/pulls/{arg}", label):
                    raise dead(label)
            elif kind == "commit":
                label = f"commit {arg} of {where}"
                if not ask(f"{base}/commits/{quote(arg, safe='')}", label):
                    raise dead(label)
            elif kind == "compare":
                label = f"compare {arg} of {where}"
                if not ask(f"{base}/compare/{quote(arg, safe='')}", label):
                    raise dead(label)
            else:
                label = f"{kind} {'/'.join(arg)} of {where}"
                for k in range(1, min(len(arg), _REF_SEGMENTS) + 1):
                    ref, path = "/".join(arg[:k]), "/".join(arg[k:])
                    if ask(f"{base}/commits/{quote(ref, safe='')}", label) and (
                            not path or ask(f"{base}/contents/{quote(path, safe='/')}"
                                            f"?ref={quote(ref, safe='')}", label)):
                        break
                else:
                    raise dead(label)
            n += 1
    except _Stop as stop:
        return str(stop), n
    return None, n


# ---- workspace authority ----------------------------------------------------
#
# Rule 1 lets a `fire --workspace` round carry no code link, because its evidence is in a local
# WebCodex workspace the remote model reaches through an app. That exemption is authority, so it is
# granted by the round's structured spec and never by a sentence in the prompt: `fire` records the
# workspace in the round (spec.consult_meta.workspace) beside its mentions, and the gate honours it
# only when the workspace is in CGC_WORKSPACES and a mention is in CGC_APPS. The gate sees a bare
# prompt (the daemon and the driver's send-time backstop both call validate_prompt(prompt)), so it
# finds the round by the rid on the prompt's first line AND the sha256 of these exact bytes: a
# prompt that merely quotes the sentinel, or another round's rid, matches no row and gets nothing.
_HEAD_RID_RE = re.compile(r"CGC_RID:(REQ-\d{8}-\d{6}-[0-9a-f]{6})\n")


def _live_registry(key: str):
    """`key` as the CONFIG FILE holds it now, or None when the file does not set it. A daemon loaded the
    file once, at import, and `load_config` only fills what the environment lacks, so a registry the
    user has since edited would read stale there forever. The gate therefore asks the file again and
    lets it win over the daemon's remembered value (same first-occurrence rule as load_config)."""
    try:
        with open(_cfg.config_path(), encoding="utf-8") as f:
            for k, v in _cfg._parse(f.read()):
                if k == key:
                    return v
    except OSError:
        pass
    return None


def workspace_refusal(workspace, mentions) -> str | None:
    """None when (workspace, mentions) is a registered evidence channel, else why it is not. One
    predicate for enqueue's pre-check and the gate, so the two cannot disagree on what registered
    means. CGC_WORKSPACES and CGC_APPS are read from the config file as it is NOW when it sets them,
    else from the environment (unlike every other CGC_* key, where the environment wins: a daemon's
    environment is a stale copy of the file, and these two are the authority for an exemption)."""
    import consult   # the single definition of how CGC_WORKSPACES / CGC_APPS are parsed
    if not workspace:
        return "no --workspace is recorded for this round (only `consult.py fire` records one)"
    known_ws = consult.known_workspaces(_live_registry("CGC_WORKSPACES"))
    if workspace.lower() not in {w.lower() for w in known_ws}:
        return f"workspace {workspace!r} is not in CGC_WORKSPACES"
    apps = {x.lower() for x in consult.known_apps(_live_registry("CGC_APPS"))}
    if not any(str(m).lower() in apps for m in mentions or ()):
        return "none of this round's --mention apps is in CGC_APPS"
    return None


def _workspace_fix(bad: str) -> str:
    return (f"the 'Source: local workspace' line is only text, and {bad}. A workspace source comes from "
            "`consult.py fire --workspace NAME --mention APP` with NAME in CGC_WORKSPACES and APP in "
            "CGC_APPS (register one with `cgc_config.py set CGC_WORKSPACES \"NAME\"`)")


class _StoreUnreadable(Exception):
    """The store exists but could not be read just now: a transient fact, never a verdict."""


def _round_workspace(text: str) -> tuple:
    """(workspace, mentions) of the stored round this exact prompt belongs to; (None, []) when there is
    no such round (no head rid, no store file, no row for that rid AND those bytes, a spec with no
    workspace). Raises _StoreUnreadable when the store is there but cannot be read now. A read-only
    connection: the gate must never create, migrate or write the store."""
    m = _HEAD_RID_RE.match(text)
    path = _store.db_path()
    if not m or path == ":memory:" or not os.path.exists(path):
        return None, []
    try:
        db = sqlite3.connect("file:" + quote(os.path.abspath(path)) + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT spec_json FROM rounds WHERE rid=? AND prompt_sha256=?",
                             (m.group(1), _store.sha256(text))).fetchone()
        finally:
            db.close()
    except (sqlite3.Error, OSError) as e:
        raise _StoreUnreadable(f"{type(e).__name__}: {e}") from e
    try:
        spec = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return None, []
    return (spec.get("consult_meta") or {}).get("workspace"), spec.get("mentions") or []


def _workspace_verdict(text: str) -> str | None:
    """None when this link-less prompt belongs to a stored round whose recorded workspace is
    registered; else the gate's reason. TERMINAL (`refused:`) when no stored round records a workspace
    for these bytes: the sentence in the text is all there is. TRANSIENT (`unverified:`, which the
    caller requeues with backoff) when the round DOES record one but the registry or the store cannot
    confirm it right now: that is a fact about the registry, fixable without re-firing."""
    try:
        workspace, mentions = _round_workspace(text)
    except _StoreUnreadable as e:
        return (f"unverified: the store could not be read to confirm this round's workspace ({e}). "
                "Nothing was sent. Re-enqueue to retry.")
    bad = workspace_refusal(workspace, mentions)
    if bad is None:
        return None
    if workspace:
        return (f"unverified: this round records workspace {workspace!r}, but {bad}. Nothing was sent; "
                "the round retries, so register it (`cgc_config.py set CGC_WORKSPACES \"NAME\"`) or cancel it.")
    return ("refused: no public code link (github/gist/raw URL) in the prompt — " + _workspace_fix(bad))


def validate_prompt(prompt_text: str) -> tuple:
    """The egress gate. Returns (ok: bool, reason: str). ok means: it is safe to send this prompt to
    the external ChatGPT session. Rules, all fail-closed:
      1. Must contain at least one real public-code link (github/gist/raw), UNLESS it is a follow-up
         (the thread already holds the code), a declared no-code consult (`prep --no-code` renders
         "references no code"), or a `fire --workspace` round whose evidence is in a registered local
         WebCodex workspace. The first two are declared in the prompt text. The third is NOT: the
         "Source: local workspace" line alone exempts nothing; the stored round must register the
         workspace and a mention (see workspace_refusal). This rule is a quality guard against blind
         prose about code; the security guarantees are rules 2-4, untouched by any exemption.
      2. Every github/raw repo a link reaches must be a gh-confirmed PUBLIC repo. Any private /
         unknown / unverifiable repo -> refuse. This is the independent re-check that makes the
         daemon a real gate rather than a blind relay of whatever the agent enqueued. The link is
         judged as a browser would request it (see "the URL law"): userinfo, a port, an escape, a
         `.`/`..` segment, a non-https scheme are refused, never normalized and accepted.
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

    # The URL law: one parse of every GitHub link, refusing any that is not written plainly.
    links, refuse = _parse_code_urls(text)
    if refuse:
        return False, refuse

    if not links and not is_followup and "references no code" not in low:
        if _WORKSPACE_SENTINEL not in low:
            return False, "refused: no public code link (github/gist/raw URL) in the prompt — nothing safe to send"
        verdict = _workspace_verdict(text)
        if verdict:
            return False, verdict

    # 3. gists. The opt-in is honoured only when it is affirmatively set: any other value (a typo,
    #    "disabled", "none") leaves it off, because a switch that opens on junk is not an opt-in.
    if any(link.host == "gist" for link in links) and not _gist_opt_in():
        return False, "refused: prompt contains a gist link and CGC_GATE_ALLOW_GIST is not set to 1 — the gate cannot verify a gist is public"

    # 2. every repo the links reach must be gh-confirmed public (or named by the operator).
    slugs = {link.slug for link in links if link.slug}
    allowed = _private_allowlist()
    for slug in sorted(slugs):
        ok, detail = _repo_is_public(slug)
        if not ok and ("*" in allowed or slug.lower() in allowed):
            # Declared sendable by the user. Still fail closed on a repo gh could not resolve AT
            # ALL — an allowlist entry says "this repo of mine may go", not "skip the check".
            if detail.startswith("unverified:"):
                return False, (f"unverified: {slug} is allowlisted, but gh could not confirm it "
                               f"exists ({detail.split(':', 1)[1].strip()}). Re-enqueue to retry.")
            continue
        if not ok:
            if detail.startswith("unverified:"):
                return False, (f"unverified: could not check whether repo {slug} is public "
                               f"({detail.split(':', 1)[1].strip()}). This is NOT a finding that the "
                               f"repo is private — the check itself failed, so nothing was sent. "
                               f"Re-enqueue to retry.")
            return False, f"refused: repo {slug} is not confirmed PUBLIC ({detail}) — will not send its link to an external service"

    # 5. dead references: a typo'd PR number or nonexistent ref would otherwise fail 25 minutes
    #    later at ChatGPT (or be silently answered around). The gate has gh anyway — check now.
    dead, n_refs = _verify_refs_exist(links)
    if dead:
        return False, dead

    return True, (f"ok: {len(slugs)} public repo(s), {n_refs} PR/ref link(s) verified to exist, "
                  "no recognized secret shape (free text is not vouched for)")


# ---- CLI: enqueue -----------------------------------------------------------

def cmd_enqueue(a) -> int:
    """Create a queued round in the store — a pure LOCAL write. A CHEAP pre-check (secret scan +
    link presence, no network) gives the agent instant feedback on what the daemon's authoritative
    gate would reject anyway; the real gate runs in the daemon worker before anything is sent."""
    import cgc_backend
    # The rid is a property of the RENDERED PROMPT, not a caller input: prep writes
    # BEGIN_RESPONSE:<rid> into the text, and the waiter accepts an answer only if that exact
    # sentinel comes back. So read it from the prompt when it wasn't passed, and REFUSE a passed rid
    # that disagrees with the text — a mismatch guarantees an answer that can never verify, and
    # hand-typing the format is a step that only ever produces `bad_rid` (observed: an agent burned
    # a round on it, then reached for `date` to synthesise one).
    if a.kind != "retrieve" and a.prompt_file and os.path.exists(a.prompt_file):
        m = re.search(r"^BEGIN_RESPONSE:(REQ-\d{8}-\d{6}-[0-9a-f]{6})\s*$",
                      open(a.prompt_file, encoding="utf-8").read(), re.M)
        if m:
            if not a.rid:
                a.rid = m.group(1)
            elif a.rid != m.group(1):
                sys.stderr.write(
                    f"CGC_ERROR rid_prompt_mismatch: --rid {a.rid!r} is not the rid the prompt "
                    f"carries ({m.group(1)!r}). The waiter verifies the prompt's OWN sentinel, so "
                    "this round could never be confirmed. Drop --rid (it is read from the prompt) "
                    "or pass the prompt this rid belongs to.\n")
                return 2
    # A retrieve has no prompt, so there is no sentinel to read a rid back from — but its own rid is
    # pure bookkeeping (what identifies the recovery is --parent, the round being recovered), so
    # there is nothing for a caller to know and nothing to hand-write. Mint it. Hand-writing was the
    # only remaining path to `bad_rid`, and it burned a live recovery in the field.
    if a.kind == "retrieve" and not a.rid:
        a.rid = new_rid()
    if not _RID_RE.match(a.rid or ""):
        sys.stderr.write(f"CGC_ERROR bad_rid: {a.rid!r} (want REQ-YYYYMMDD-HHMMSS-hhhhhh). Do not "
                         "hand-write one: `consult.py prep` mints it and puts it in the prompt, and "
                         "enqueue reads it back from there — omit --rid entirely.\n")
        return 2
    out = a.out or default_out(a.rid)
    if (a.kind == "retrieve" and (not a.conversation or a.conversation == "auto")
            and not a.parent):
        # Without --parent there is nothing to resolve the conversation FROM — with it,
        # cgc_backend.enqueue_round resolves it from the parent round's own recorded thread (the
        # store already knows it), so this only refuses the case neither can answer.
        sys.stderr.write("CGC_ERROR need_conversation: --kind retrieve requires an explicit "
                         "--conversation <id>, or --parent <rid> to resolve it from the round "
                         "being recovered.\n")
        return 2
    prompt = ""
    if a.kind != "retrieve":
        if not a.prompt_file or not os.path.exists(a.prompt_file):
            sys.stderr.write("CGC_ERROR need_prompt_file: --prompt-file is required.\n")
            return 2
        prompt = open(a.prompt_file, encoding="utf-8").read()
        low = prompt.lower()
        is_followup = a.kind == "followup" or "continuing this consult" in low
        # The turn this round will send must parse as THIS rid under the reader's own rule (the same
        # helper the driver re-applies before the click): refuse now, so `fire` says so at once and
        # not the daemon later. Imported lazily: cdp_consult is a heavy module the spool otherwise skips.
        import cdp_consult as _cdp
        why = _cdp._refuse_ambiguous_rid(a.rid, prompt, getattr(a, "mentions", None), "enqueue")
        if why:
            sys.stderr.write(why + "\n")
            return 2
        for rx, label in _SECRET_RES:
            if rx.search(prompt):
                sys.stderr.write(f"CGC_ERROR gate_secret: prompt looks like it contains a {label} — "
                                 "NOT enqueuing. Remove the secret from the prompt/context file, "
                                 "re-render it, then enqueue again.\n")
                return 2
        if _WORKSPACE_SENTINEL in low and not getattr(a, "mentions", None):
            # The text claims its evidence is in a local workspace, which the remote model can only
            # reach through an app. The gates see prompt text alone, so the requirement is enforced
            # here, where the mentions are known (fire passes them; a bare `enqueue` has none).
            sys.stderr.write("CGC_ERROR workspace_needs_mention: the prompt names a local workspace "
                             "as its source, but the round has no --mention to reach it. Use "
                             "`consult.py fire --workspace NAME --mention APP`.\n")
            return 2
        links, refuse = _parse_code_urls(prompt)
        if refuse:
            sys.stderr.write(f"CGC_ERROR gate_refused: {refuse}. NOT enqueuing.\n")
            return 2
        if (not links and not is_followup and "references no code" not in low):
            if _WORKSPACE_SENTINEL not in low:
                sys.stderr.write("CGC_ERROR no_code_source: prompt has no public code link — deliver "
                                 "a link first, render a no-code question with `prep --no-code`, or name "
                                 "a local workspace with `fire --workspace NAME --mention APP`.\n")
                return 2
            # The same authority test the daemon's gate applies, from the same inputs: the workspace
            # `fire` recorded for this round, not the sentence in the text.
            bad = workspace_refusal((getattr(a, "consult_meta", None) or {}).get("workspace"),
                                    getattr(a, "mentions", None))
            if bad:
                sys.stderr.write(f"CGC_ERROR no_code_source: {_workspace_fix(bad)}. Nothing was queued.\n")
                return 2
    return cgc_backend.enqueue_round(a, prompt, os.path.abspath(out))


# ---- CLI: await -------------------------------------------------------------

def cmd_await(a) -> int:
    """Wait for the answer. Pure LOCAL polling of the store — no CDP, no network, which is what
    keeps it invisible to the auto-mode classifier.

    It exits on exactly three things, because exactly three things are worth acting on:

        0  the answer is on disk    -> the path is printed; go read it
        3  a human must act in the ChatGPT window (login / captcha / rate limit / safeguard refusal)
        1  broken                   -> the job log path is printed; go look

    The exit code is a routing bit; the RETURN is an address. Every outcome names the artifact it
    produced — `answer_path` on 0, `next_command` + `human_action` on 3, `log_path` on 1 — because
    a caller told only "0" still has to find the answer, and the round already knows where it is.
    That is also why --out is an override and not an argument: the address is chosen once, when the
    round is created, and recorded on the round. Requiring the caller to carry it back in was the
    last surviving instance of the copy-a-path-between-JSON-blobs step `fire` exists to delete.

    "Still running" is NOT among them. A consult routinely outlasts any particular observation, and
    making that an exit code turned every healthy 25-minute round into a failure the caller had to
    notice and manually retry. Waiting longer is the waiter's job, so the waiter just keeps waiting.
    The only reason it ever stops without an answer is STUCK_AFTER_S — and reaching that does not
    mean the answer is late, it means something is wrong and waiting more cannot fix it."""
    import cgc_backend
    return cgc_backend.await_round(a)


def _point_at_log(rid, out=None):
    """Name the evidence on any failure. Costs one line; without it the caller can only guess or
    ask a human to go dig."""
    lp = log_path(rid)
    if os.path.exists(lp):
        sys.stderr.write(f"CGC_LOG send+wait transcript: {lp}\n")
    if out and os.path.exists(out + ".raw"):
        sys.stderr.write(f"CGC_RAW partial text salvaged: {out}.raw\n")


# ---- CLI: status (human) ----------------------------------------------------

def cmd_status(a) -> int:
    ensure_dirs()
    import cgc_backend
    return cgc_backend.store_status(daemon_alive(), a.rid)


# ---- CLI: reconcile (operator-driven durable reconcile of a stuck round) ----

def cmd_reconcile(a) -> int:
    """The operator step `find-conversation`'s own miss message points at and TROUBLESHOOTING.md's
    uncertain-round recovery flow ends with: you looked in the ChatGPT window (or its history) and
    confirmed a round's send never happened, so record that proof durably. `--not-sent` is required
    explicitly rather than defaulted — a bare `reconcile --rid X` does nothing, so a future reconcile
    action can be added without changing what today's invocation means."""
    if not a.not_sent:
        sys.stderr.write("CGC_ERROR no_reconcile_action: pass --not-sent to record a not-sent proof "
                         "(the only reconcile action this command supports today) — a bare "
                         "`reconcile --rid` does nothing on purpose.\n")
        return 2
    evidence = (a.evidence or "").strip()
    if not evidence:
        sys.stderr.write("CGC_ERROR evidence_required: --evidence must be a non-empty description of "
                         "what you verified and how (e.g. 'checked ChatGPT history for 2026-08-18, "
                         "no matching prompt/thread exists') — the proof is the point of this "
                         "command.\n")
        return 2
    import cgc_backend
    return cgc_backend.reconcile_not_sent(a.rid, evidence)


def cmd_refire(a) -> int:
    """Re-fire a proven-not-sent round under a fresh rid, replaying its exact prompt and spec. See
    cgc_backend.refire_round for the full contract (REFUSES unless the prior is durably proven
    not-sent — never a round that may have sent)."""
    import cgc_backend
    return cgc_backend.refire_round(a.rid)


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_spool.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="create a queued consult round in the store (LOCAL write only)")
    e.add_argument("--rid", help="never needed — omit it. For submit/followup prep mints the rid "
                                 "INTO the prompt (BEGIN_RESPONSE:<rid>) and enqueue reads it back "
                                 "from there; one that disagrees with the prompt is refused, since "
                                 "the waiter verifies the prompt's own sentinel and such a round "
                                 "could never confirm. For --kind retrieve (no prompt) enqueue mints "
                                 "one and prints it — a retrieve is identified by --parent.")
    e.add_argument("--prompt-file", help="rendered prompt (required except for --kind retrieve)")
    e.add_argument("--kind", choices=("submit", "followup", "retrieve"), default="submit",
                   help="submit=new consult, followup=continue a thread, retrieve=read an existing "
                        "conversation's answer without sending anything")
    e.add_argument("--project-url", default=os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/"))
    e.add_argument("--conversation", default="auto", help="(followup) /c/<id> or 'auto'")
    e.add_argument("--parent", help="(followup) the rid of the consult being continued — resolves to "
                                    "ITS conversation (causal, unambiguous under concurrency). "
                                    "Preferred over --conversation auto.")
    e.add_argument("--model", default=os.environ.get("CGC_MODEL", "Pro"))
    e.add_argument("--model-family", default=os.environ.get("CGC_MODEL_FAMILY", "GPT-6"),
                   help="model family to pin, FROZEN into the round's spec here rather than read "
                        "from the daemon's environment at send time — so a queued round keeps the "
                        "family it was fired with. Part of the request-key fingerprint: the same "
                        "key under a different family CONFLICTS, it does not return the old receipt.")
    e.add_argument("--request-key",
                   help="caller-chosen logical-request id — same key + same content returns the "
                        "original receipt (idempotent retry); same key + different content refuses")
    e.add_argument("--logical-sha",
                   help="sha256 of the prompt rendered with the <RID> placeholder (from prep) — the "
                        "rid-independent content identity used in the request-key fingerprint. "
                        "Without it, the fingerprint falls back to normalizing this round's rid "
                        "out of the rendered bytes.")
    e.add_argument("--out", help="answer file (default $CGC_STATE_DIR/answer_<rid>.txt)")
    e.add_argument("--poll", type=int, default=POLL_S)
    e.add_argument("--timeout", type=int, default=STUCK_AFTER_S,
                   help=f"seconds before this consult is considered STUCK, not slow (default "
                        f"{STUCK_AFTER_S} = {STUCK_AFTER_S // 60} min). A round reasons ~25 min; "
                        f"this is the point past which waiting stops being an explanation.")
    e.set_defaults(fn=cmd_enqueue)

    w = sub.add_parser("await", help="poll the local answer/status for a queued job (LOCAL read only)")
    w.add_argument("--rid", required=True)
    w.add_argument("--out", help="OPTIONAL override. The answer's address was chosen when the round "
                                 "was created and is recorded on the round, so await reads it back "
                                 "and prints it — pass this only to materialize the answer somewhere "
                                 "else as well.")
    w.add_argument("--poll", type=int, default=POLL_S)
    w.add_argument("--timeout", type=int, default=STUCK_AFTER_S,
                   help=f"seconds before declaring the job STUCK (default {STUCK_AFTER_S} = "
                        f"{STUCK_AFTER_S // 60} min). Not a budget for the consult — reaching it "
                        f"means something is broken, so read the log rather than waiting again.")
    w.set_defaults(fn=cmd_await)

    s = sub.add_parser("status", help="print daemon liveness + round counts by state")
    s.add_argument("--rid", help="also print this job's status record")
    s.set_defaults(fn=cmd_status)

    c = sub.add_parser("cancel", help="cancel a round BEFORE it sends (queued/ready only; idempotent)")
    c.add_argument("--rid", required=True)
    c.set_defaults(fn=lambda a: __import__("cgc_backend").cancel_round(a.rid))

    st = sub.add_parser("stats", help="reliability metrics: outcome rates + completion latency "
                                      "percentiles + sentinel-drift alarm")
    st.add_argument("--week", action="store_true",
                    help="instead: the rolling-7-day view — potentially spent messages vs "
                         "CGC_WEEKLY_BUDGET, answers awaiting a grade, backlog, expired rounds, "
                         "grades by trigger and by mention, useful outcomes per message, and whether "
                         "the weekly policy review was recorded")
    st.set_defaults(fn=lambda a: __import__("cgc_backend").week_report() if a.week
                    else __import__("cgc_backend").stats_report())

    fb = sub.add_parser("feedback", help="grade a consult's answer (LOCAL only), or record the "
                                         "weekly policy review with --policy")
    fb.add_argument("--rid", help="the round whose answer you are grading (a retrieve round "
                                  "resolves to the round it recovered)")
    fb.add_argument("--grade", type=int, choices=(-1, 0, 1, 2),
                    help="2 changed a decision or caught a defect, verified; 1 validated improvement "
                         "or new evidence; 0 duplicate/late/refuted/unused/transport; -1 acting on it "
                         "caused rework. 1 and 2 require --evidence.")
    fb.add_argument("--reason", choices=("useful", "decision-changed", "defect-caught", "duplicate",
                                         "late", "refuted", "unused", "transport", "missing-source",
                                         "wrong"))
    fb.add_argument("--action", help="what you did with the answer")
    fb.add_argument("--evidence", help="a commit SHA, test, file, or concrete acceptance judgment")
    fb.add_argument("--policy", help="record the weekly review: what rule/template/test changed and "
                                     "why (stamped with the current policy_rev; takes no other flag)")
    fb.set_defaults(fn=lambda a: __import__("cgc_backend").record_feedback(a))

    rc = sub.add_parser("reconcile", help="operator-driven durable reconcile of a stuck round "
                                          "(today: recording a verified not-sent proof)")
    rc.add_argument("--rid", required=True)
    rc.add_argument("--not-sent", action="store_true", dest="not_sent",
                    help="record that this round's send was PROVEN to have never reached ChatGPT — "
                         "must be passed explicitly, there is no default reconcile action")
    rc.add_argument("--evidence",
                    help="REQUIRED, non-empty: what you verified and how (e.g. checked ChatGPT's "
                         "own history, no matching thread exists). A closed tab or a virtualized "
                         "thread looks identical to a never-sent prompt — look before stamping this.")
    rc.set_defaults(fn=cmd_reconcile)

    rf = sub.add_parser("refire", help="re-fire a proven-not-sent round under a fresh rid, replaying "
                                       "its exact stored prompt and spec (refuses a round that may "
                                       "have sent)")
    rf.add_argument("--rid", required=True, help="the terminal, proven-not-sent round to replay")
    rf.set_defaults(fn=cmd_refire)

    dg = sub.add_parser("diagnose", help="one bounded JSON line on a stuck/failed round: state, "
                                         "code, phases, last driver verdict, UI verdicts, and the "
                                         "next command — read this, never the log")
    dg.add_argument("--rid", required=True)
    dg.set_defaults(fn=lambda a: __import__("cgc_backend").diagnose(a.rid))

    import cgc_backend
    if (sys.argv[1:2] or [""])[0] == "stats":   # an operator report, not an agent verb
        a = p.parse_args()
        return a.fn(a)
    return cgc_backend.agent_cli(lambda: (lambda a: a.fn(a))(p.parse_args()), _argv_rid)


def _argv_rid():
    """The --rid the caller named, for an envelope synthesized before (or without) parsing."""
    argv = sys.argv[1:]
    for i, x in enumerate(argv):
        if x == "--rid" and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith("--rid="):
            return x[6:]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
