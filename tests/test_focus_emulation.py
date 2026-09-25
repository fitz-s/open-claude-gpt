# 2026-09-25: every submit refused model_not_selectable ("switcher shows 'Extra High' and it could not
# be changed"). Consult tabs sit in a debug Chrome nobody looks at, so their page is hidden+unfocused,
# and ChatGPT's power slider ignores arrow keys then. Live repro on the project page: slider stuck at
# 3/4 under CDP arrow presses; with Emulation.setFocusEmulationEnabled it walks to Pro. Pin that every
# attach turns it on, and that a Chrome which lacks the method still attaches.
import importlib.util
import json
import os
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_consult", os.path.join(ROOT, "skill", "scripts", "cdp_consult.py"))
CDP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CDP)


class _WS:
    def __init__(self, reject=()):
        self.sent, self.reject, self._q = [], set(reject), []

    def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m["method"])
        self._q.append({"id": m["id"], "error": {"message": "not found"}} if m["method"] in self.reject
                       else {"id": m["id"], "result": {}})

    def recv(self):
        return json.dumps(self._q.pop(0))

    def gettimeout(self):
        return 10

    def settimeout(self, v):
        pass

    def close(self):
        pass


def _attach(monkeypatch, ws):
    tabs = [{"type": "page", "id": "T1", "url": "https://chatgpt.com/c/abc",
             "webSocketDebuggerUrl": "ws://x"}]

    class _R:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(CDP.urllib.request, "urlopen",
                        lambda url, timeout=None: _R(tabs if url.endswith("/json") else {}))
    monkeypatch.setattr(CDP.websocket, "create_connection", lambda *a, **kw: ws)
    return CDP.CDP(9333, match="abc")


def test_every_attach_turns_on_focus_emulation(monkeypatch):
    ws = _WS()
    _attach(monkeypatch, ws)
    assert ws.sent[:3] == ["Runtime.enable", "Page.enable", "Emulation.setFocusEmulationEnabled"]


def test_a_chrome_without_focus_emulation_still_attaches(monkeypatch):
    ws = _WS(reject={"Emulation.setFocusEmulationEnabled"})
    c = _attach(monkeypatch, ws)
    assert c.ws is ws
