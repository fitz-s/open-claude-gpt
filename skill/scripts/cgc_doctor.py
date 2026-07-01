#!/usr/bin/env python3
# Created: 2026-07-01
# Last reused or audited: 2026-07-01
# Authority basis: open-claude-gpt OSS packaging — health check / preflight.
"""
cgc doctor — verify a open-claude-gpt install end-to-end.

Runs a sequence of independent checks (deps, browser, debug port, login,
scratch dir, skill files, config) and prints a pass/warn/fail line for each
with a concrete fix. Exit 0 when nothing HARD-failed, 1 otherwise, so it can
gate CI or a first-run setup.

    python3 cgc_doctor.py              # human-readable report
    python3 cgc_doctor.py --json       # machine-readable
    python3 cgc_doctor.py --deep       # also probe login state via CDP (needs Chrome up)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = YELLOW = RED = DIM = RESET = ""

PORT = int(os.environ.get("CGC_PORT", "9333"))
PROFILE = os.environ.get("CGC_PROFILE", os.path.expanduser("~/.cgc-chrome"))
STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")
MODEL = os.environ.get("CGC_MODEL", "Pro")
PROJECT_URL = os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/ (new chat)")
HERE = os.path.dirname(os.path.abspath(__file__))

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


class Report:
    def __init__(self):
        self.rows = []       # (level, name, detail, fix)
        self.hard_fail = False

    def ok(self, name, detail=""):
        self.rows.append(("pass", name, detail, ""))

    def warn(self, name, detail="", fix=""):
        self.rows.append(("warn", name, detail, fix))

    def fail(self, name, detail="", fix=""):
        self.rows.append(("fail", name, detail, fix))
        self.hard_fail = True

    def render(self):
        icon = {"pass": f"{GREEN}✓{RESET}", "warn": f"{YELLOW}!{RESET}", "fail": f"{RED}✗{RESET}"}
        for level, name, detail, fix in self.rows:
            line = f"  {icon[level]} {name}"
            if detail:
                line += f" {DIM}— {detail}{RESET}"
            print(line)
            if fix and level != "pass":
                print(f"      {DIM}fix:{RESET} {fix}")
        n_fail = sum(1 for r in self.rows if r[0] == "fail")
        n_warn = sum(1 for r in self.rows if r[0] == "warn")
        print()
        if self.hard_fail:
            print(f"{RED}✗ {n_fail} check(s) failed, {n_warn} warning(s).{RESET} Fix the failures above, then re-run `cgc doctor`.")
        elif n_warn:
            print(f"{YELLOW}! ready, with {n_warn} warning(s).{RESET} Consults will run; warnings are optional polish.")
        else:
            print(f"{GREEN}✓ all checks passed — open-claude-gpt is ready.{RESET}")


def run(deep: bool) -> Report:
    r = Report()

    # 1. Python
    v = sys.version_info
    if v >= (3, 8):
        r.ok("python3", f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.fail("python3", f"{v.major}.{v.minor}", "need Python >= 3.8")

    # 2. websocket-client (the only hard pip dep — the CDP client)
    try:
        import websocket  # noqa: F401
        ver = getattr(websocket, "__version__", "?")
        if hasattr(websocket, "create_connection"):
            r.ok("websocket-client", f"v{ver}")
        else:
            r.fail("websocket-client", "wrong 'websocket' package installed",
                   "pip uninstall websocket && pip install websocket-client")
    except ImportError:
        r.fail("websocket-client", "not installed", "pip install websocket-client")

    # 3. gh CLI (used by `deliver` to resolve PRs / repo visibility)
    gh = shutil.which("gh")
    if gh:
        try:
            auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
            if auth.returncode == 0:
                r.ok("gh CLI", "installed + authenticated")
            else:
                r.warn("gh CLI", "installed but not authenticated",
                       "run `gh auth login` (needed for private-repo visibility checks + PR resolution)")
        except Exception:
            r.warn("gh CLI", "installed, auth status unknown", "run `gh auth login`")
    else:
        r.warn("gh CLI", "not found",
               "install from https://cli.github.com — `deliver` uses it to resolve PRs and repo visibility")

    # 4. Chrome / Chromium / Edge
    chrome = _find_chrome()
    if chrome:
        r.ok("browser", chrome)
    else:
        r.fail("browser", "no Chrome/Chromium/Edge found",
               "install Chrome, or set CGC_CHROME=/path/to/chrome")

    # 5. Debug port reachable (is the dedicated Chrome up?)
    up = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=3) as f:
            data = json.load(f)
        up = True
        r.ok(f"debug Chrome (port {PORT})", data.get("Browser", "up"))
    except Exception:
        r.warn(f"debug Chrome (port {PORT})", "not running",
               f"start it: `bash {os.path.join(HERE, 'cdp_launch.sh')}` (then log into ChatGPT once)")

    # 6. Login state (deep only — needs Chrome up)
    if deep and up:
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5))
            pages = [t for t in tabs if t.get("type") == "page"]
            cgpt = [t for t in pages if "chatgpt.com" in (t.get("url") or "")] or pages
            if not cgpt:
                r.warn("ChatGPT login", "no page tab to probe",
                       "open chatgpt.com in the debug Chrome window")
            else:
                import websocket
                ws = websocket.create_connection(cgpt[0]["webSocketDebuggerUrl"], timeout=6)
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
                    r.warn("ChatGPT login", "logged OUT",
                           "log into ChatGPT (Pro/Plus) in the debug Chrome window, then re-run")
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
        r.fail("scratch dir", f"{STATE_DIR} not writable ({e})", "set CGC_STATE_DIR to a writable path")

    # 8. Skill files present
    need = ["consult.py", "cdp_consult.py", "cdp_launch.sh", "retrieval_window.js"]
    missing = [f for f in need if not os.path.exists(os.path.join(HERE, f))]
    skill_md = os.path.exists(os.path.join(HERE, "..", "SKILL.md"))
    if not missing and skill_md:
        r.ok("skill files", "all present")
    else:
        detail = (f"missing scripts: {', '.join(missing)}" if missing else "") + \
                 ("; SKILL.md not found" if not skill_md else "")
        r.fail("skill files", detail, "re-run install.sh from the repo")

    # 9. Config summary (never a failure — just what's in effect)
    r.ok("config", f"port={PORT} model={MODEL!r} profile={PROFILE}")
    r.ok("project", PROJECT_URL)

    return r


def main() -> int:
    ap = argparse.ArgumentParser(prog="cgc doctor")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--deep", action="store_true", help="also probe ChatGPT login via CDP (needs Chrome up)")
    a = ap.parse_args()
    rep = run(a.deep)
    if a.json:
        print(json.dumps({
            "ok": not rep.hard_fail,
            "checks": [{"level": lv, "name": n, "detail": d, "fix": fx} for lv, n, d, fx in rep.rows],
        }, indent=2))
    else:
        print(f"\n{DIM}open-claude-gpt doctor{RESET}\n")
        rep.render()
    return 1 if rep.hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
