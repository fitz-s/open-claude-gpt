# Live 2026-10-13, REQ-20261013-171359-975fcc: Target.createTarget's reply timed out, the websocket
# exception escaped CDP.__init__ as a traceback, and a round that never reached a composer was filed
# possibly_accepted (a human had to reconcile it). Tab creation precedes any composer, so it must end
# in the typed new_tab_failed marker, which the backend requeues as proven not-sent.
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skill", "scripts"))
spec = importlib.util.spec_from_file_location("cdp_consult", os.path.join(ROOT, "skill", "scripts", "cdp_consult.py"))
CDP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CDP)


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _TimingOutSocket:
    closed = False

    def send(self, _):
        pass

    def recv(self):
        raise TimeoutError("timed out")

    def close(self):
        self.closed = True


def test_a_tab_creation_timeout_is_typed_not_sent(monkeypatch):
    sock = _TimingOutSocket()
    monkeypatch.setattr(CDP.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"webSocketDebuggerUrl": "ws://x"}'))
    monkeypatch.setattr(CDP.websocket, "create_connection", lambda *a, **k: sock)
    with pytest.raises(SystemExit) as e:
        CDP.CDP(9333, create_url="https://chatgpt.com/")
    assert "CGC_ERROR new_tab_failed" in str(e.value) and "nothing was sent" in str(e.value)
    assert sock.closed


def test_the_backend_requeues_new_tab_failed():
    import cgc_backend
    assert cgc_backend._not_sent_marker({"stderr": "CGC_ERROR new_tab_failed: x"},
                                        cgc_backend._NOT_SENT_RETRY) is not None
