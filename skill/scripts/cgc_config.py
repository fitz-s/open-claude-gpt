#!/usr/bin/env python3
# Created: 2026-07-02
# Last audited: 2026-07-02
# Authority basis: chatgpt-consult skill — persistent per-user config.
"""Shared config store for the chatgpt-consult skill.

Lets ANY user persist their `CGC_*` settings — most importantly `CGC_PROJECT_URL`
(which ChatGPT project a fresh consult opens) — in a tool-owned config file, so they
don't have to hand-edit their shell rc or Claude's settings.json and the public repo
never hard-codes anyone's project.

Precedence: a real environment variable ALWAYS wins; the config file is only the
persistent default (loaded via os.environ.setdefault). So `CGC_PROJECT_URL=… cmd`
or an env export still overrides the stored value for that run.

Config file location (first that applies):
  $CGC_CONFIG                                  — explicit override
  $XDG_CONFIG_HOME/cgc/config                  — if XDG_CONFIG_HOME is set
  ~/.config/cgc/config                         — default
Format: simple `KEY=value` lines (`#` comments allowed). Only `CGC_*` keys are loaded.

Writes: `set` takes an exclusive lock on <config>.lock, re-reads the file under it, and replaces it
through a unique temp file + os.replace, so two concurrent `set`s both land and a reader never sees a
partial file. A key must be a CGC_ name; a name the code does not read is stored with a warning (a
typo is the usual cause); a value may not contain any character the reader treats as a line break
(`str.splitlines()`: \n \r \v \f \x1c-\x1e \x85 U+2028 U+2029) or NUL, because that would write a
second, unreviewed line; a few keys the scripts parse at import (CGC_PORT, CGC_WEEKLY_BUDGET, the
on/off flags) must parse, because a bad one there fails every command. Only a missing file counts as
an empty config: any other read failure aborts the write and leaves the file as it was.

CLI (used by `bin/cgc`):
  cgc_config.py set KEY VALUE   persist KEY=VALUE (atomic; replaces an existing key)
  cgc_config.py get KEY         print the effective value (env or file)
  cgc_config.py path            print the config file path
  cgc_config.py show            print all stored CGC_* keys (warns on stderr about unknown ones)
"""
from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import tempfile

try:
    import fcntl
except ImportError:  # no advisory lock on this platform: still atomic, only the lost-update guard is gone
    fcntl = None


def _port(v: str) -> None:
    if not (v.isdigit() and 1 <= int(v) <= 65535):
        raise ValueError("must be a TCP port, 1-65535")


def _count(v: str) -> None:
    if not v.isdigit():
        raise ValueError("must be a non-negative integer")


def _flag(v: str) -> None:
    if v.strip().lower() not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
        raise ValueError("must be one of 1/0, true/false, yes/no, on/off")


# Every CGC_* key the scripts read from the environment (and so from this file). None = free text.
# A key the code starts reading belongs here; tests/test_lifecycle.py greps the scripts for the ones
# missing.
KNOWN = {
    "CGC_APPS": None, "CGC_AUTO_APPROVE": None, "CGC_AUTO_MODEL": _flag, "CGC_CHROME": None,
    "CGC_CONTRACT": None, "CGC_DATA_DIR": None, "CGC_GATE_ALLOW_GIST": _flag,
    "CGC_GATE_PRIVATE_REPOS": None, "CGC_MODEL": None, "CGC_MODEL_FAMILY": None,
    "CGC_MODEL_SLUG": None, "CGC_PORT": _port, "CGC_PROFILE": None, "CGC_PROJECT_URL": None,
    "CGC_SPOOL_DIR": None, "CGC_STATE_DIR": None, "CGC_STORE_DB": None, "CGC_WEEKLY_BUDGET": _count,
    "CGC_WORKSPACES": None,
}
_KEY_RE = re.compile(r"CGC_[A-Z0-9_]+\Z")


def config_path() -> str:
    explicit = os.environ.get("CGC_CONFIG")
    if explicit:
        return explicit
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "cgc", "config")


def _parse(text: str):
    """Yield (key, value) for each valid `CGC_*=...` line."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k.startswith("CGC_"):
            yield k, v


def load_config() -> None:
    """Populate os.environ with stored CGC_* keys — setdefault, so a real env var wins.
    Silent + safe: a missing/unreadable config file is simply ignored."""
    try:
        with open(config_path(), encoding="utf-8") as f:
            text = f.read()
    except (OSError, ValueError):     # missing, unreadable, or not UTF-8: no stored defaults
        return
    for k, v in _parse(text):
        try:
            check_pair(k, v)
        except ValueError as e:   # a bad typed value would crash every script at import: skip, say so
            sys.stderr.write(f"cgc_config: ignoring {k} in {config_path()}: {e}\n")
            continue
        os.environ.setdefault(k, v)


def check_pair(key: str, value: str) -> bool:
    """Raise ValueError for a pair that must not be stored; return True when `key` is a known one."""
    if not _KEY_RE.match(key):
        raise ValueError(f"refusing to store {key!r}: a config key is CGC_ followed by A-Z, 0-9 or _")
    if "\0" in value or len(("x" + value + "x").splitlines()) != 1:
        raise ValueError(f"refusing to store {key}: the value contains a line break (newline, CR, VT, FF, "
                         "FS/GS/RS, NEL, LS or PS) or NUL; the config reader would see a second line")
    check = KNOWN.get(key)
    if check is not None:
        try:
            check(value)
        except ValueError as e:
            raise ValueError(f"refusing to store {key}={value!r}: {e}")
    return key in KNOWN


@contextlib.contextmanager
def _locked(path: str):
    """Exclusive flock on a sidecar that is never unlinked (a lock file that can be removed splits the
    namespace: one writer holds the old inode, the next locks a new one)."""
    if fcntl is None:
        yield
        return
    with open(path + ".lock", "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield


def set_key(key: str, value: str) -> str:
    """Persist KEY=value to the config file and return its path. The read-modify-write runs under the
    lock, and the new content reaches the file by os.replace of a unique temp file, so concurrent
    updates of different keys all land. Raises ValueError for a pair check_pair refuses; an unknown
    CGC_ key is stored, with a warning on stderr."""
    if not check_pair(key, value):
        sys.stderr.write(f"cgc_config: warning: {key} is not a setting the scripts read from this file "
                         f"— a typo? (known: {', '.join(sorted(KNOWN))})\n")
    p = config_path()
    d = os.path.dirname(p) or "."
    os.makedirs(d, exist_ok=True)
    with _locked(p):
        out, found = [], False
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = ""          # the one read failure that means "no config yet"; any other OSError aborts
        # Split exactly as _parse does (str.splitlines), so a line the reader sees as two is two here.
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
                out.append(f"{key}={value}\n")
                found = True
            else:
                out.append(line + "\n")
        if not found:
            out.append(f"{key}={value}\n")
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(out)
                f.flush()
                os.fsync(f.fileno())
            with contextlib.suppress(OSError):   # mkstemp is 0600; an existing file keeps its own mode
                os.chmod(tmp, stat.S_IMODE(os.stat(p).st_mode))
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise
    return p


def _main(argv) -> int:
    if not argv:
        sys.stderr.write("usage: cgc_config.py set KEY VALUE | get KEY | path | show\n")
        return 2
    cmd = argv[0]
    if cmd == "path":
        print(config_path())
        return 0
    if cmd == "set" and len(argv) == 3:
        try:
            p = set_key(argv[1], argv[2])
        except ValueError as e:
            sys.stderr.write(f"CGC_ERROR {e}\n")
            return 2
        except OSError as e:     # unreadable/unwritable config or lock: nothing was changed
            sys.stderr.write(f"cgc_config: config not updated ({e}); the file was left as it was\n")
            return 1
        print(f"set {argv[1]} in {p}")
        return 0
    if cmd == "get" and len(argv) == 2:
        load_config()
        print(os.environ.get(argv[1], ""))
        return 0
    if cmd == "show":
        try:
            with open(config_path(), encoding="utf-8") as f:
                for k, v in _parse(f.read()):
                    print(f"{k}={v}")
                    if k not in KNOWN:
                        sys.stderr.write(f"cgc_config: warning: {k} is not a setting the scripts read from this file\n")
        except OSError:
            pass
        return 0
    sys.stderr.write("usage: cgc_config.py set KEY VALUE | get KEY | path | show\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
