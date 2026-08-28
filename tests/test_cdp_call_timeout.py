# Offline unit tests for CDP.call's timeout handling in cdp_consult.py (round 5 fix).
#
# The bug: the websocket's own recv() timeout is fixed at connect time (CDP.__init__'s `timeout`
# arg, default 10s) and does not track a per-call `timeout=` request. call()'s Python-level deadline
# loop wraps ws.recv(), but each individual recv() still obeys the SOCKET's timeout — so a call
# asking for longer than that (cdp_consult.py drives _clear_composer_js/_paste_chunk_js/
# _composer_text_js at timeout=45) died with a raw WebSocketTimeoutException around the 10s mark,
# long before the requested deadline. No browser, no network: a fake ws stands in for the real
# websocket-client connection and simulates exactly that timeout behaviour.
import importlib.util
import os

import pytest
import websocket

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDP_PATH = os.path.join(REPO, "skill", "scripts", "cdp_consult.py")
_spec = importlib.util.spec_from_file_location("cdp_consult_calltest", CDP_PATH)
_CDP_MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_CDP_MOD)


class _FakeWS:
    """Stands in for websocket.WebSocket. recv() simulates the real socket's behaviour: it raises
    WebSocketTimeoutException unless the CURRENT settimeout() value covers the requested wait — so
    this only returns a result if CDP.call actually stretched the socket timeout before waiting."""

    def __init__(self, initial_timeout, needs_timeout):
        self._timeout = initial_timeout
        self.needs_timeout = needs_timeout
        self.settimeout_calls = []
        self.sent = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self.settimeout_calls.append(value)
        self._timeout = value

    def send(self, msg):
        self.sent.append(msg)

    def recv(self):
        if self._timeout < self.needs_timeout:
            raise websocket.WebSocketTimeoutException("timed out")
        return '{"id": 1, "result": {"ok": true}}'


def _bare_cdp(ws):
    """A CDP instance with none of __init__'s browser-attach machinery run — just the two attributes
    call() touches."""
    c = _CDP_MOD.CDP.__new__(_CDP_MOD.CDP)
    c.ws = ws
    c._id = 0
    return c


def test_call_stretches_the_socket_timeout_to_cover_a_longer_request():
    """The field incident: connect-time timeout=10, a 45s eval call. Without the fix this raises
    WebSocketTimeoutException; with it, the socket timeout is widened before the wait begins."""
    ws = _FakeWS(initial_timeout=10, needs_timeout=45)
    cdp = _bare_cdp(ws)
    result = cdp.call("Runtime.evaluate", {}, timeout=45)
    assert result == {"ok": True}
    assert 45 in ws.settimeout_calls, "the socket timeout must be stretched to the requested deadline"


def test_call_restores_the_prior_socket_timeout_after_a_stretched_call():
    """Restore in `finally`, so a later call (a normal short one) is never left running on a socket
    timeout some earlier long call silently widened."""
    ws = _FakeWS(initial_timeout=10, needs_timeout=45)
    cdp = _bare_cdp(ws)
    cdp.call("Runtime.evaluate", {}, timeout=45)
    assert ws.gettimeout() == 10, "the original socket timeout must be restored after the call"


def test_call_restores_the_socket_timeout_even_when_the_call_raises():
    """The restore must happen on every exit path, not just the happy one — a crash mid-call (here:
    the underlying socket itself times out because the request was never stretched enough) must
    never leave the connection running on a widened timeout for whatever runs next."""
    ws = _FakeWS(initial_timeout=10, needs_timeout=999)  # recv() never satisfies this call
    cdp = _bare_cdp(ws)
    with pytest.raises(websocket.WebSocketTimeoutException):
        cdp.call("Runtime.evaluate", {}, timeout=0.01)
    assert ws.gettimeout() == 10, "restored even though recv() itself raised"


class _FakeWSWrongId:
    """Never raises, never matches — every recv() returns a well-formed message for a DIFFERENT
    request id, so the Python-level deadline loop is what has to give up (the documented
    RuntimeError('CDP {method} timeout')), not the socket."""

    def __init__(self, initial_timeout):
        self._timeout = initial_timeout

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value

    def send(self, msg):
        pass

    def recv(self):
        return '{"id": 999, "result": {}}'


def test_call_still_raises_its_own_deadline_runtimeerror_and_restores_after():
    """The deadline behaviour this fix must NOT change: once the Python-level deadline passes with no
    matching reply, call() raises its own RuntimeError — and the socket timeout is restored after."""
    ws = _FakeWSWrongId(initial_timeout=10)
    cdp = _bare_cdp(ws)
    with pytest.raises(RuntimeError, match="CDP Runtime.evaluate timeout"):
        cdp.call("Runtime.evaluate", {}, timeout=0.05)
    assert ws.gettimeout() == 10


def test_call_does_not_shrink_an_already_longer_socket_timeout():
    """A call whose requested timeout is SHORTER than (or equal to) the socket's current timeout must
    not touch it at all — only widen, never narrow, and only when actually needed."""
    ws = _FakeWS(initial_timeout=60, needs_timeout=0)  # recv() always succeeds regardless of timeout
    cdp = _bare_cdp(ws)
    cdp.call("Runtime.evaluate", {}, timeout=5)
    assert ws.settimeout_calls == [], "no need to touch a socket timeout already long enough"
    assert ws.gettimeout() == 60
