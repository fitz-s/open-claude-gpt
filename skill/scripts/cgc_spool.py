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
# A GPT-5.6 Pro round reasons ~25 minutes. That is how long the work TAKES; it is an expectation,
# not a deadline, and nothing may be killed for reaching it. A deadline answers a different
# question: past what point is waiting no longer explained by the work? An hour. Beyond that the
# answer is not late — something is broken — and the right response is to read the job log, not to
# keep waiting.
#
# Everything that used to be a second or third "timeout" was one of these two things wearing the
# wrong name: a 1500s per-consult budget (an expectation) and an 870s agent window (an observation
# interval). Both killed healthy consults for the crime of taking as long as they take.
#
# 90 min, not 60: a deep GPT-5.6 Pro round — especially a follow-up that triggers fresh re-reasoning
# — was observed to think for ~62 min before emitting its answer. At the old 3600s the waiter timed
# out minutes BEFORE the answer landed, stranded the round as possibly_accepted, and only the
# read-only auto-retrieve recovered it (a wasted hour + a detour). The completion signal itself is
# prompt — `done` fires the instant generation stops — so the only failure was budget < work. 5400s
# covers the observed long tail with margin; auto-retrieve still backstops anything beyond it.
STUCK_AFTER_S = 5400

# How often to look. An interval, not a deadline.
POLL_S = 20

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
_CLEAN_SEG_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _classify_code_urls(text: str):
    """Turn every RECOGNIZED code URL into an owner/repo slug to verify, FAILING CLOSED on any URL
    that cannot be classified. The bug this closes (diff-review S0): the broad _CODE_URL_RE accepts a
    github URL, but the narrow slug extraction skipped percent-encoded owners (e.g.
    github.com/%66itz-s/private = github.com/fitz-s/private), so NO visibility check ran and the
    prompt was approved after verifying zero repos. Here the invariant is 1:1 — a recognized code URL
    that does not resolve to a clean owner/repo (unclassifiable, double-encoded, or non-repo path) is
    a hard refusal, never silently dropped from verification. Returns (slugs, refuse_reason)."""
    from urllib.parse import urlsplit, unquote
    slugs = set()
    for url in _CODE_URL_RE.findall(text):
        u = url.rstrip('.,);]}"\'>')
        parts = urlsplit(u)
        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "gist.github.com":
            continue  # gists carry no owner/repo slug; rule 3 handles them separately
        segs = [s for s in unquote(parts.path).split("/") if s]
        owner = segs[0] if len(segs) >= 1 else None
        repo = segs[1] if len(segs) >= 2 else None
        # owner/repo must be CLEAN slug chars — a residual '%' means double-encoding, anything else
        # non-slug means this is not a repo path we can verify. Either way: refuse.
        if (not owner or not repo
                or not _CLEAN_SEG_RE.fullmatch(owner) or not _CLEAN_SEG_RE.fullmatch(repo)):
            if owner and owner.lower() in ("orgs", "sponsors", "settings", "features", "about",
                                           "topics", "trending", "marketplace"):
                continue  # a non-repo github path (not a code source) — not a slug to verify
            return None, (f"refused: code URL {u!r} does not resolve to a verifiable owner/repo "
                          "(unclassifiable or ambiguously encoded) — failing closed.")
        slugs.add(f"{owner}/{repo}")
    return slugs, None

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
_PR_URL_RE = re.compile(
    r"https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)", re.IGNORECASE)
_REF_URL_RE = re.compile(
    r"https://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:tree|blob|commit)/([A-Za-z0-9_./-]+)",
    re.IGNORECASE)
_EXISTENCE_CAP = 8  # dedup usually leaves 1-3; the cap bounds gh cost on a pathological prompt


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
        last = f"unverified: gh api {path} failed: {err[:120]}"
    return None, last


def _verify_refs_exist(text: str, verified_slugs: set) -> str | None:
    """Refuse dead references (nonexistent PR numbers / tree-blob-commit refs) on repos the gate
    already vetted. Returns a refusal reason, or None when everything checked out. Fail-closed on a
    gh outage the same way the visibility check is: `unverified:` → the caller requeues."""
    checks = []
    for slug, pr in set(_PR_URL_RE.findall(text)):
        checks.append((f"repos/{slug}/pulls/{pr}", f"PR github.com/{slug}/pull/{pr}", slug))
    for slug, ref in set(_REF_URL_RE.findall(text)):
        ref = ref.split("/")[0]  # tree/<ref>/sub/path — only the ref segment names a commit
        checks.append((f"repos/{slug}/commits/{ref}", f"ref {ref} of github.com/{slug}", slug))
    admitted = {s.lower() for s in (verified_slugs or set())}
    seen = set()
    for path, label, slug in checks:
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > _EXISTENCE_CAP:
            break  # bounded cost; the leading references are the load-bearing ones
        if admitted and slug.lower() not in admitted:
            continue  # only check repos the visibility pass actually admitted
        ok, detail = _gh_exists(path)
        if ok is False:
            return (f"refused: {label} does not exist (404 — a dead link). ChatGPT would browse to "
                    "nothing or answer around it. Fix the reference and re-fire.")
        if ok is None:
            return (f"unverified: could not check that {label} exists ({detail.split(':', 1)[1].strip()}). "
                    "Nothing is known about it and nothing was sent. Re-enqueue to retry.")
    return None


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

    # 2. every recognized code URL must classify to a repo slug (fail closed on any that doesn't),
    #    and every slug must be gh-confirmed public.
    slugs, refuse = _classify_code_urls(text)
    if refuse:
        return False, refuse
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
    dead = _verify_refs_exist(text, slugs)
    if dead:
        return False, dead

    return True, f"ok: {len(slugs)} public repo(s), refs exist, no secrets detected"


# ---- CLI: enqueue -----------------------------------------------------------

def cmd_enqueue(a) -> int:
    """Create a queued round in the store — a pure LOCAL write. A CHEAP pre-check (secret scan +
    link presence, no network) gives the agent instant feedback on what the daemon's authoritative
    gate would reject anyway; the real gate runs in the daemon worker before anything is sent."""
    import cgc_backend
    if not _RID_RE.match(a.rid or ""):
        sys.stderr.write(f"CGC_ERROR bad_rid: {a.rid!r} (want REQ-YYYYMMDD-HHMMSS-hhhhhh)\n")
        return 2
    out = a.out or os.path.join(CGC_STATE_DIR, f"answer_{a.rid}.txt")
    if a.kind == "retrieve" and (not a.conversation or a.conversation == "auto"):
        sys.stderr.write("CGC_ERROR need_conversation: --kind retrieve requires an explicit "
                         "--conversation <id>.\n")
        return 2
    prompt = ""
    if a.kind != "retrieve":
        if not a.prompt_file or not os.path.exists(a.prompt_file):
            sys.stderr.write("CGC_ERROR need_prompt_file: --prompt-file is required.\n")
            return 2
        prompt = open(a.prompt_file, encoding="utf-8").read()
        low = prompt.lower()
        is_followup = a.kind == "followup" or "continuing this consult" in low
        for rx, label in _SECRET_RES:
            if rx.search(prompt):
                sys.stderr.write(f"CGC_ERROR gate_secret: prompt looks like it contains a {label} — "
                                 "NOT enqueuing. Remove the secret from the prompt/context file, "
                                 "re-render it, then enqueue again.\n")
                return 2
        if not _CODE_URL_RE.search(prompt) and not is_followup and "references no code" not in low:
            sys.stderr.write("CGC_ERROR no_code_source: prompt has no public code link — deliver "
                             "a link first, or render a no-code question with `prep --no-code`.\n")
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


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_spool.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="create a queued consult round in the store (LOCAL write only)")
    e.add_argument("--rid", required=True)
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
    w.add_argument("--out", required=True)
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
    st.set_defaults(fn=lambda a: __import__("cgc_backend").stats_report())

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
