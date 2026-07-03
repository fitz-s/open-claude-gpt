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

CLI (used by `bin/cgc`):
  cgc_config.py set KEY VALUE   persist KEY=VALUE (atomic; replaces an existing key)
  cgc_config.py get KEY         print the effective value (env or file)
  cgc_config.py path            print the config file path
  cgc_config.py show            print all stored CGC_* keys
"""
from __future__ import annotations

import os
import sys


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
    except OSError:
        return
    for k, v in _parse(text):
        os.environ.setdefault(k, v)


def set_key(key: str, value: str) -> str:
    """Persist KEY=value to the config file (atomic write; replaces an existing key).
    Returns the config file path. Only CGC_* keys are accepted."""
    if not key.startswith("CGC_"):
        raise ValueError(f"refusing to store non-CGC key: {key!r}")
    p = config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    out, found = [], False
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
                    out.append(f"{key}={value}\n")
                    found = True
                else:
                    out.append(line if line.endswith("\n") else line + "\n")
    except OSError:
        pass
    if not found:
        out.append(f"{key}={value}\n")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, p)
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
        except OSError:
            pass
        return 0
    sys.stderr.write("usage: cgc_config.py set KEY VALUE | get KEY | path | show\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
