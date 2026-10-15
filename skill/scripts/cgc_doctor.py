#!/usr/bin/env python3
# Created: 2026-07-01
# Last reused or audited: 2026-07-01
# Authority basis: open-claude-gpt OSS packaging — health check / preflight.
"""
cgc doctor — verify a open-claude-gpt install end-to-end.

Runs a sequence of independent checks (deps, browser, debug port, login, daemon, store,
scratch dir, skill files, config) and prints a pass/warn/fail line for each with a concrete fix.
It ends in three separate verdicts, because "the files are there" and "a consult can be sent" are
different claims:

    installed   the skill tree is complete and its dependencies are present
    can_send    installed, AND the egress daemon is alive and serves THIS install's store, AND the
                debug Chrome port answers (with --deep: ChatGPT is logged in)
    can_read    the durable store opens at a schema this code understands (recorded answers are
                readable with `await`/`stats` without the daemon or Chrome)

Overall `ready` is can_send: no daemon, no ready.

Exit status: 0 ready; 1 not installed (or a --secure advisory failed); 3 installed but cannot send.

    python3 cgc_doctor.py              # human-readable report
    python3 cgc_doctor.py --json       # machine-readable: ok/ready, verdicts, checks
    python3 cgc_doctor.py --deep       # also probe login state via CDP (needs Chrome up)
    python3 cgc_doctor.py --secure     # hard-fail on loopback-bind / gh-auth advisories
    python3 cgc_doctor.py --check-store  # only the store check (install.sh runs it before replacing files)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = YELLOW = RED = DIM = RESET = ""

# Load persisted CGC_* settings from the user's config file (env still wins) — see
# cgc_config.py and `cgc set-project`, so the doctor reports the SAME effective config
# the skill will actually use.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cgc_config import load_config as _cgc_load_config
    _cgc_load_config()
except Exception:
    pass

# The debug port is on loopback and must never go through an HTTP proxy: with http_proxy set, urlopen
# routes /json to the proxy, gets its HTML back, and a running Chrome reads as dead (the daemon and the
# driver already install a proxy-free opener for the same reason). A local opener, not a global one:
# this module is imported by bin/cgc and by tests.
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open



def ws_connect(url: str, timeout: float):
    """A CDP websocket to the loopback debug Chrome, opened on a socket we connect ourselves.
    websocket-client reads http_proxy/HTTPS_PROXY from the environment and would tunnel 127.0.0.1
    through it (a local LLM router answers CONNECT with its own error page); a caller-supplied socket
    is the one option that bypasses proxy resolution entirely, in every 1.x version."""
    import websocket
    u = urllib.parse.urlparse(url)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
    try:
        return websocket.create_connection(url, timeout=timeout, socket=sock)
    except BaseException:
        sock.close()
        raise


PORT = int(os.environ.get("CGC_PORT", "9333"))
PROFILE = os.environ.get("CGC_PROFILE", os.path.expanduser("~/.cgc-chrome"))
STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")
MODEL = os.environ.get("CGC_MODEL", "Pro")
# The MODEL, as distinct from the reasoning tier. GPT-6's composer (2026-09-03) exposes both in
# one picker, so the tier alone no longer says which model answered — the doctor reports both or
# it reports a half-truth. See the model-selection law in cdp_consult.py.
MODEL_FAMILY = os.environ.get("CGC_MODEL_FAMILY", "GPT-6")
PROJECT_URL = os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/ (new chat)")
HERE = os.path.dirname(os.path.abspath(__file__))

# What an install must hold: every module the CLI and the daemon import or spawn, and the data files
# they load at import. tests/test_lifecycle.py pins this to the scripts directory and to the code's
# own references, so a file the code starts (or stops) loading fails a test instead of drifting here.
REQUIRED = (
    "cdp_consult.py", "cdp_launch.sh", "cgc_activation.py", "cgc_backend.py", "cgc_config.py",
    "cgc_daemon.py", "cgc_doctor.py", "cgc_drift.py", "cgc_probe.py", "cgc_spool.py", "cgc_store.py",
    "consult.py", "chatgpt_contract.json", "fixture_dom.js",
)
# Shipped, but nothing on the runtime path loads it (an offline one-shot importer).
OPTIONAL = ("cgc_migrate.py",)

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
]


def _find_chrome():
    if os.environ.get("CGC_CHROME"):
        return os.environ["CGC_CHROME"]
    for c in CHROME_CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge"):
        p = shutil.which(name)
        if p:
            return p
    return None


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_loopback_addr(addr: str) -> bool:
    """Classify a listen-address string as loopback (True) or routable (False).

    Accepts forms seen across `lsof`/`ss`/`netstat` output, e.g.:
      "127.0.0.1:9333", "127.0.0.1", "[::1]:9333", "::1", "localhost:9333",
      "0.0.0.0:9333", "*:9333", "0.0.0.0", "::", "192.168.1.5:9333".
    """
    a = addr.strip()
    if not a:
        return False
    # Strip IPv6 brackets, e.g. "[::1]:9333" -> "::1:9333" handled below.
    if a.startswith("["):
        host, _, rest = a[1:].partition("]")
        return host in LOOPBACK_HOSTS
    # Bare "*" (netstat/lsof wildcard) is routable (binds all interfaces).
    if a in ("*", "0.0.0.0", "::"):
        return False
    # host:port form — split on the LAST colon so bare IPv6 (multiple colons)
    # without brackets is still handled reasonably.
    if ":" in a:
        host, _, port = a.rpartition(":")
        if port.isdigit() or port == "*":
            if host in LOOPBACK_HOSTS or host in ("0.0.0.0", "*"):
                return host in LOOPBACK_HOSTS
            # No port separator matched a digit/star — fall through to full-string check.
    return a in LOOPBACK_HOSTS


class Report:
    def __init__(self):
        self.rows = []       # (level, name, detail, fix)
        self.why = {"installed": [], "can_send": [], "can_read": []}   # verdict -> what makes it false
        self.advisory_fail = False   # a --secure advisory failed: belongs to no verdict

    def ok(self, name, detail=""):
        self.rows.append(("pass", name, detail, ""))

    def warn(self, name, detail="", fix=""):
        self.rows.append(("warn", name, detail, fix))

    def fail(self, name, detail="", fix="", verdict=None):
        """A failed check falsifies the verdict(s) it names (a str or a tuple); none = an advisory."""
        self.rows.append(("fail", name, detail, fix))
        if verdict is None:
            self.advisory_fail = True
        for v in ((verdict,) if isinstance(verdict, str) else verdict or ()):
            self.why[v].append(f"{name}: {detail}" if detail else name)

    @property
    def installed(self):
        return not self.why["installed"]

    @property
    def can_send(self):
        return self.installed and not self.why["can_send"]

    @property
    def can_read(self):
        return not self.why["can_read"]

    @property
    def ready(self):
        return self.can_send and not self.advisory_fail

    def exit_code(self):
        if self.ready:
            return 0
        return 3 if (self.installed and not self.advisory_fail) else 1

    def reasons(self, verdict):
        if verdict == "can_send" and not self.installed:
            return ["not installed"]
        return self.why[verdict]

    def render(self):
        icon = {"pass": f"{GREEN}✓{RESET}", "warn": f"{YELLOW}!{RESET}", "fail": f"{RED}✗{RESET}"}
        for level, name, detail, fix in self.rows:
            line = f"  {icon[level]} {name}"
            if detail:
                line += f" {DIM}— {detail}{RESET}"
            print(line)
            if fix and level != "pass":
                print(f"      {DIM}fix:{RESET} {fix}")
        n_warn = sum(1 for r in self.rows if r[0] == "warn")
        print()
        for v, ok in (("installed", self.installed), ("can_send", self.can_send), ("can_read", self.can_read)):
            why = "; ".join(self.reasons(v))
            print(f"  {v:<9} " + (f"{GREEN}yes{RESET}" if ok else f"{RED}NO{RESET}{DIM} — {why}{RESET}"))
        print()
        if self.ready:
            tail = f", with {n_warn} warning(s)" if n_warn else ""
            print(f"{GREEN}✓ ready{tail}.{RESET} A consult can be sent.")
        elif not self.installed:
            print(f"{RED}✗ NOT READY: the install is incomplete.{RESET} Fix the failures above, then re-run `cgc doctor`.")
        elif self.advisory_fail and self.can_send:
            print(f"{RED}✗ NOT READY under --secure:{RESET} an advisory above failed.")
        else:
            print(f"{RED}✗ NOT READY: consults cannot be sent yet.{RESET} Installed, but see can_send above.")


def _report(r: "Report", secure: bool, name, detail="", fix=""):
    """Warn by default; under --secure, escalate to a hard fail."""
    if secure:
        r.fail(name, detail, fix)   # an advisory: falsifies no verdict, but not ready under --secure
    else:
        r.warn(name, detail, fix)


def _listen_addrs_for_port(port: int):
    """Best-effort list of listen addresses bound to `port`, using whichever
    of lsof/ss/netstat is available on the host. Returns (addrs, tool_used);
    tool_used is None if no suitable tool was found.
    """
    # lsof -nP -iTCP:<port> -sTCP:LISTEN
    if shutil.which("lsof"):
        try:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            addrs = []
            for line in out.stdout.splitlines()[1:]:  # skip header
                parts = line.split()
                if not parts:
                    continue
                name_field = parts[-2] if len(parts) >= 2 and parts[-1] == "(LISTEN)" else parts[-1]
                # NAME field looks like "127.0.0.1:9333" or "*:9333"
                addrs.append(name_field.rsplit("->", 1)[0])
            if addrs:
                return addrs, "lsof"
        except Exception:
            pass

    # ss -ltnp
    if shutil.which("ss"):
        try:
            out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=5)
            addrs = []
            for line in out.stdout.splitlines():
                if f":{port}" not in line:
                    continue
                cols = line.split()
                for col in cols:
                    if col.rstrip("0123456789").endswith(":") or ":" in col:
                        if col.endswith(f":{port}"):
                            addrs.append(col)
                            break
            if addrs:
                return addrs, "ss"
        except Exception:
            pass

    # netstat -an
    if shutil.which("netstat"):
        try:
            out = subprocess.run(["netstat", "-an"], capture_output=True, text=True, timeout=5)
            addrs = []
            for line in out.stdout.splitlines():
                if f".{port} " not in line and f":{port} " not in line and \
                   not line.rstrip().endswith(f".{port}") and not line.rstrip().endswith(f":{port}"):
                    continue
                if "LISTEN" not in line:
                    continue
                cols = line.split()
                if len(cols) >= 4:
                    addrs.append(cols[3])
            if addrs:
                return addrs, "netstat"
        except Exception:
            pass

    return [], None


def check_skill_files(r: "Report", here: str = HERE) -> None:
    """The whole tree, not three of its files. A half-copied tree used to pass because the list named
    only the entry points; the daemon then died on its first lazy import."""
    problems = []
    for f in REQUIRED:
        fp = os.path.join(here, f)
        try:
            if os.path.getsize(fp) == 0:
                problems.append(f"{f} is empty")
            elif f.endswith(".json"):
                with open(fp, encoding="utf-8") as fh:
                    json.load(fh)
        except OSError:
            problems.append(f"{f} missing")
        except ValueError as e:
            problems.append(f"{f} is not valid JSON ({e})")
    if not os.path.exists(os.path.join(here, "..", "SKILL.md")):
        problems.append("SKILL.md missing")
    if not problems:
        r.ok("skill files", f"all {len(REQUIRED) + 1} present")
    else:
        r.fail("skill files", "; ".join(problems), "re-run install.sh from the repo", verdict="installed")


def _valid_pid(pid) -> bool:
    """A real process id: a positive int. kill(0, sig) addresses the caller's whole process group and
    kill(-n, sig) the group n (and -1 every process the caller may signal), so a 0 or negative pid from a
    hand-edited or corrupt daemon.json must never reach os.kill. bool is an int subclass: True would be pid 1."""
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0


def _proc_stat_state(pid):
    """The process state letter from /proc/<pid>/stat (Linux), or None when /proc has no answer. The state
    is the field after the command name, which is parenthesised and may itself hold spaces and parens, so
    it is read after the LAST ')'."""
    if not _valid_pid(pid):
        return None
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    tail = raw.rpartition(")")[2].split()
    return tail[0] if tail else None


def _ps_field(pid, field):
    """`ps -ww -o <field>= -p <pid>` (macOS, BSD and procps-ng take it), or None when ps is absent, fails, or
    prints nothing. -ww matters: procps truncates `command` to $COLUMNS (CI runners set it) when ps is not
    writing to a terminal, and the daemon's script name is at the END of the line, so without it a long
    interpreter path cuts the very word being looked for."""
    if not _valid_pid(pid):
        return None
    for argv in (["ps", "-ww", "-o", f"{field}=", "-p", str(int(pid))],
                 ["ps", "-o", f"{field}=", "-p", str(int(pid))]):     # a ps that rejects -ww
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except Exception:
            return None
        out = r.stdout.strip()
        if r.returncode == 0 and out:
            return out
    return None


def _pid_running(pid) -> bool:
    """A process that exists and is not a zombie: a zombie answers kill -0 until its parent reaps it, and
    holds no lock, so it must not count as a daemon that is still running. Fail-safe: when kill -0 says it
    exists and nothing can say it is a zombie, it counts as running (a wrong "still there" blocks a
    replace; a wrong "gone" would allow one under a live daemon). A pid that is not a positive int is no
    process at all (see _valid_pid): it never reaches os.kill."""
    if not _valid_pid(pid):
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        pass                                    # exists, owned by another user
    except (OSError, ValueError, TypeError):
        return False
    state = _proc_stat_state(pid)               # Linux: no ps needed
    if state is None:
        state = _ps_field(pid, "stat")          # macOS/BSD, or a Linux without /proc
    return not (state or "").startswith("Z")


def _command_of(pid) -> str:
    """The command line of `pid`, or "" when it cannot be read. /proc first (exact, not width-limited, no
    ps needed), then ps."""
    if not _valid_pid(pid):
        return ""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        raw = b""
    if raw:
        return raw.replace(b"\0", b" ").decode("utf-8", "replace")
    return _ps_field(pid, "command") or ""


def _lock_holders(lock_dir: str) -> list:
    """Pids with any file under the lock directory open, per lsof (empty when lsof is absent). That
    directory is the fencing namespace: the daemon holds daemon.lock for its whole life, and a worker
    that outlived its daemon still holds its lease.<rid>. Every one of them is executing this tree."""
    if not shutil.which("lsof") or not os.path.isdir(lock_dir):
        return []
    try:
        out = subprocess.run(["lsof", "-t", "+D", lock_dir], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [int(w) for w in out.split() if w.isdigit() and int(w) != os.getpid()]


def _lock_held(path: str) -> bool:
    """True when another open file description holds an flock on `path` right now."""
    try:
        import fcntl
        fd = os.open(path, os.O_RDWR)
    except (ImportError, OSError):
        return False                            # no lock file (or no flock): nobody holds it
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def daemon_processes() -> dict:
    """Is an egress daemon PROCESS still there, whatever its heartbeat says? {"pids": [...],
    "lock_held": bool}. This decides whether code may be replaced or deleted under it, so it asks
    about the process and its lock, never about freshness: a daemon that is draining, wedged or
    mid-restart has a stale heartbeat and still executes the files. A process counts if it holds
    anything in the lock directory (daemon.lock, held for the process lifetime; a surviving worker's
    lease), or is the pid recorded in daemon.json (read regardless of age) and is alive and runs
    cgc_daemon.py."""
    import cgc_spool
    pids = set()
    try:
        with open(cgc_spool.daemon_path(), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = None
    pid = rec.get("pid") if isinstance(rec, dict) else None
    if _valid_pid(pid) and pid != os.getpid() and _pid_running(pid):
        cmd = _command_of(pid).strip()
        if not cmd or "cgc_daemon.py" in cmd:   # an unreadable command line is not proof of a reused pid
            pids.add(pid)
    pids.update(p for p in _lock_holders(os.path.dirname(cgc_spool.daemon_lock_path())) if _pid_running(p))
    return {"pids": sorted(pids), "lock_held": _lock_held(cgc_spool.daemon_lock_path())}


def daemon_mismatch(ident: dict):
    """Why a live daemon is not serving this install, or "" when it is. Only the heartbeat is read:
    opening the store here would migrate it."""
    import cgc_store
    path = cgc_store.db_path()
    if path != ":memory:" and ident.get("db_path") != os.path.abspath(path):
        return f"serves store {ident.get('db_path')!r}, not {os.path.abspath(path)!r} (old code or a relocated DB)"
    if ident.get("schema_version") != cgc_store.SCHEMA_VERSION:
        return (f"runs store schema {ident.get('schema_version')!r}, this install is "
                f"v{cgc_store.SCHEMA_VERSION} (old code)")
    return ""


def _check_store(r: "Report") -> None:
    """can_read: the store file opens read-only and its schema is one this code understands. A newer
    schema (written by a newer install) is refused by the store itself, so it also blocks sending."""
    both = ("can_read", "can_send")
    try:
        import cgc_store
        path = cgc_store.db_path()
        schema = cgc_store.SCHEMA_VERSION
    except Exception as e:
        r.fail("store", f"cgc_store not importable ({e})", "re-run install.sh from the repo", verdict=both)
        return
    if path == ":memory:":
        r.ok("store", ":memory:")
        return
    if not os.path.exists(path):
        r.ok("store", f"{path} (none yet; created by the first fire)")
        return
    import sqlite3
    import urllib.parse
    try:
        db = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro", uri=True, timeout=1)
        try:
            has = db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if has else None
        finally:
            db.close()
        have = int(row[0]) if row else None
    except Exception as e:
        r.fail("store", f"{path} cannot be read ({e})",
               "restore a copy: control.db.v<n>.bak beside it is the pre-migration backup", verdict=both)
        return
    if have is None:
        r.ok("store", f"{path} (empty, initialised on first use)")
    elif have > schema:
        r.fail("store", f"{path} is schema v{have}; this install understands up to v{schema}",
               "re-run install.sh from a current checkout; never run older code against it", verdict=both)
    elif have < schema:
        r.ok("store", f"{path} (schema v{have}; migrates to v{schema} on first use, with a backup)")
    else:
        r.ok("store", f"{path} (schema v{have})")


def run(deep: bool, secure: bool = False) -> Report:
    r = Report()

    # 1. Python
    v = sys.version_info
    if v >= (3, 8):
        r.ok("python3", f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.fail("python3", f"{v.major}.{v.minor}", "need Python >= 3.8", verdict="installed")

    # 2. websocket-client (the only hard pip dep — the CDP client)
    try:
        import websocket  # noqa: F401
        ver = getattr(websocket, "__version__", "?")
        if hasattr(websocket, "create_connection"):
            r.ok("websocket-client", f"v{ver}")
        else:
            r.fail("websocket-client", "wrong 'websocket' package installed",
                   "pip uninstall websocket && pip install websocket-client", verdict="installed")
    except ImportError:
        r.fail("websocket-client", "not installed", "pip install websocket-client", verdict="installed")

    # 3. gh CLI (used by `deliver` to resolve PRs / repo visibility)
    gh = shutil.which("gh")
    gh_authed = False
    if gh:
        try:
            auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
            if auth.returncode == 0:
                gh_authed = True
                r.ok("gh CLI", "installed + authenticated")
            else:
                _report(r, secure, "gh CLI", "installed but not authenticated",
                        "run `gh auth login` (needed for private-repo visibility checks + PR resolution)")
        except Exception:
            _report(r, secure, "gh CLI", "installed, auth status unknown", "run `gh auth login`")
    else:
        _report(r, secure, "gh CLI", "not found",
                "install from https://cli.github.com — `deliver` uses it to resolve PRs and repo visibility")

    # 3b. release-safe advisory: without gh auth, `deliver`'s public-visibility
    # stamping degrades to best-effort. Lenient by default; hard fail under --secure.
    if not gh_authed:
        _report(r, secure, "release-safe (gh auth)",
                "public-visibility stamping in `deliver` is best-effort without gh auth",
                "run `gh auth login` before relying on deliver's visibility checks")
    else:
        r.ok("release-safe (gh auth)", "gh authenticated — visibility stamping fully supported")

    # 4. Chrome / Chromium / Edge
    chrome = _find_chrome()
    if chrome:
        r.ok("browser", chrome)
    else:
        r.fail("browser", "no Chrome/Chromium/Edge found",
               "install Chrome, or set CGC_CHROME=/path/to/chrome", verdict="installed")

    # 5. Debug port reachable (is the dedicated Chrome up?)
    up = False
    try:
        with _open(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as f:
            data = json.load(f)
        up = True
        r.ok(f"debug Chrome (port {PORT})", data.get("Browser", "up"))
    except Exception:
        r.fail(f"debug Chrome (port {PORT})", "not reachable",
               f"start it: `bash {os.path.join(HERE, 'cdp_launch.sh')}` (then log into ChatGPT once)",
               verdict="can_send")

    # 5b. Debug port bound to loopback only (security: must not be reachable off-host).
    if up:
        addrs, tool = _listen_addrs_for_port(PORT)
        if tool is None:
            _report(r, secure, f"loopback bind (port {PORT})",
                    "cannot verify DevTools bind address (no lsof/ss/netstat)"
                    + (" — refusing under --secure" if secure else ""),
                    "install lsof, ss (iproute2), or netstat to verify the bind address")
        elif not addrs:
            _report(r, secure, f"loopback bind (port {PORT})",
                    f"no listener found for port {PORT}"
                    + (" — refusing under --secure" if secure else f" (via {tool})"),
                    "verify manually, e.g. `lsof -nP -iTCP:%d -sTCP:LISTEN`" % PORT)
        else:
            routable = [a for a in addrs if not is_loopback_addr(a)]
            if routable:
                _report(r, secure, f"loopback bind (port {PORT})",
                        f"listening on non-loopback address(es) via {tool}: {', '.join(routable)}",
                        "restart with `--remote-debugging-address=127.0.0.1` (see cdp_launch.sh); "
                        "never expose the CDP port beyond localhost")
            else:
                r.ok(f"loopback bind (port {PORT})", f"loopback-only via {tool}: {', '.join(addrs)}")
    else:
        r.warn(f"loopback bind (port {PORT})", "skipped (Chrome not up)")

    # 6. Login state (deep only — needs Chrome up)
    if deep and up:
        try:
            tabs = json.load(_open(f"http://127.0.0.1:{PORT}/json", timeout=5))
            pages = [t for t in tabs if t.get("type") == "page"]
            cgpt = [t for t in pages if "chatgpt.com" in (t.get("url") or "")] or pages
            if not cgpt:
                r.warn("ChatGPT login", "no page tab to probe",
                       "open chatgpt.com in the debug Chrome window")
            else:
                ws = ws_connect(cgpt[0]["webSocketDebuggerUrl"], 6)
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "(!!document.querySelector('input[type=\"password\"]'))"
                                  "||/^\\/(auth|login)(\\/|$)/i.test(location.pathname)",
                    "returnByValue": True}}))
                m = {}
                while m.get("id") != 1:
                    m = json.loads(ws.recv())
                ws.close()
                needs = m["result"]["result"]["value"]
                if needs:
                    r.fail("ChatGPT login", "logged OUT",
                           "log into ChatGPT (Pro/Plus) in the debug Chrome window, then re-run",
                           verdict="can_send")
                else:
                    r.ok("ChatGPT login", "logged in")
        except Exception as e:
            r.warn("ChatGPT login", f"probe failed ({e})", "check the debug Chrome window manually")
    elif deep and not up:
        r.warn("ChatGPT login", "skipped (Chrome not up)")

    # 7. Scratch dir writable
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        probe = os.path.join(STATE_DIR, ".doctor_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        r.ok("scratch dir", f"{STATE_DIR} (writable)")
    except Exception as e:
        r.fail("scratch dir", f"{STATE_DIR} not writable ({e})", "set CGC_STATE_DIR to a writable path",
               verdict="can_send")

    # 8. Skill files present.
    check_skill_files(r)

    # 8b. The durable store, read-only: this check can neither create nor migrate it.
    _check_store(r)

    # 9. Config summary (never a failure — just what's in effect)
    r.ok("config", f"port={PORT} model={MODEL!r} family={MODEL_FAMILY!r} profile={PROFILE}")
    r.ok("project", PROJECT_URL)

    # 9b. Proactive activation hook (SessionStart). Delegates entirely to cgc_activation.py so
    # this check can never drift from what `cgc activation-hook --status` reports. `absent` is a
    # legitimate, supported configuration (on-demand activation still works) — never a warning;
    # `broken` (hook present, ACTIVATION.md missing) is the failure mode this whole feature exists
    # to surface, since a silent `|| true` used to make it invisible. `unreadable` (settings.json
    # itself can't be parsed) is also a warning, not an `ok` — status not finding an entry in a
    # file it couldn't read is not the same claim as genuinely finding none.
    try:
        import cgc_activation
        act = cgc_activation.status(cgc_activation.default_settings_path(), cgc_activation.DEFAULT_ACTIVATION)
        if act["state"] == "configured":
            detail = "SessionStart hook present, ACTIVATION.md readable"
            if act.get("stale"):
                detail += " (stale form — `cgc activation-hook --install` would refresh it)"
            r.ok("proactive activation", detail)
        elif act["state"] == "broken":
            r.warn("proactive activation", act.get("detail", "hook present but ACTIVATION.md missing"),
                   "re-run install.sh from the repo, or: cgc activation-hook --install")
        elif act["state"] == "unreadable":
            r.warn("proactive activation", act.get("detail", "settings.json could not be parsed"),
                   f"fix the JSON in {act.get('settings_path', '?')} (Claude Code ignores an "
                   "unparseable settings file), then: cgc activation-hook --status")
        else:
            r.ok("proactive activation", "not installed — on-demand activation still works; "
                 "`cgc activation-hook --install` enables the proactive hook")
    except Exception as e:
        r.warn("proactive activation", f"cgc_activation not importable ({e})", "re-run install.sh from the repo")

    # 10. Egress daemon. Every send goes through it (the direct path is retired), so a consult
    # enqueued without one sits in the queue forever: no live daemon is not-ready, not a warning. It
    # must also serve THIS install's store and schema: the old-code / relocated-DB daemon is the
    # recorded split-brain failure, and `enqueue` refuses against it.
    try:
        import cgc_spool
        ident = cgc_spool.daemon_identity()
        procs = daemon_processes() if ident is None else None
        if ident is None and (procs["pids"] or procs["lock_held"]):
            who = ", ".join(map(str, procs["pids"])) or "unidentified"
            r.fail("daemon", f"a daemon process is alive (pid {who}) but its heartbeat is stale or missing; "
                   "it is hung, draining or still starting, and cannot take a consult",
                   "give it a minute; if this persists restart it: `launchctl kickstart -k gui/$(id -u)/"
                   "com.open-claude-gpt.daemon`", verdict="can_send")
        elif ident is None:
            r.fail("daemon", "egress daemon not running; consults would sit in the queue and never run",
                   "install it once (survives login and crashes): `cgc install-daemon`; "
                   "or run `cgc watch` / `cgc up`", verdict="can_send")
        else:
            bad = daemon_mismatch(ident)
            if bad:
                r.fail("daemon", f"pid {ident.get('pid')} is alive but {bad}",
                       "restart it on the current code: `launchctl kickstart -k gui/$(id -u)/"
                       "com.open-claude-gpt.daemon` (or stop the foreground `cgc watch` and start it again)",
                       verdict="can_send")
            else:
                r.ok("daemon", f"egress daemon running, pid {ident.get('pid')}, serves {ident.get('db_path')}")
        try:
            cgc_spool.ensure_dirs()
        except Exception as e:
            r.fail("daemon spool dir", f"{cgc_spool.SPOOL_DIR} not writable ({e})",
                   "set CGC_STATE_DIR (or CGC_SPOOL_DIR / CGC_DATA_DIR) to a writable path", verdict="can_send")
    except Exception as e:
        r.fail("daemon", f"cgc_spool not importable ({e})", "re-run install.sh from the repo",
               verdict=("installed", "can_send"))

    return r


def main() -> int:
    ap = argparse.ArgumentParser(prog="cgc doctor")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--deep", action="store_true", help="also probe ChatGPT login via CDP (needs Chrome up)")
    ap.add_argument("--secure", action="store_true",
                     help="hard-fail on loopback-bind and gh-auth advisories instead of warning")
    ap.add_argument("--check-store", action="store_true",
                    help="run only the store check; exit 1 if this code cannot read the store")
    a = ap.parse_args()
    if a.check_store:
        rep = Report()
        _check_store(rep)
        for level, name, detail, fix in rep.rows:
            print(f"{name}: {level} - {detail}" + (f"\n  fix: {fix}" if fix and level != "pass" else ""))
        return 0 if rep.can_read else 1
    rep = run(a.deep, a.secure)
    if a.json:
        print(json.dumps({
            "ok": rep.ready,
            "ready": rep.ready,
            "verdicts": {v: {"ok": ok, "why": rep.reasons(v)} for v, ok in
                         (("installed", rep.installed), ("can_send", rep.can_send), ("can_read", rep.can_read))},
            "checks": [{"level": lv, "name": n, "detail": d, "fix": fx} for lv, n, d, fx in rep.rows],
        }, indent=2))
    else:
        print(f"\n{DIM}open-claude-gpt doctor{RESET}\n")
        rep.render()
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
