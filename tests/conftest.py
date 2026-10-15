# Deterministic test environment.
import pytest


@pytest.fixture(autouse=True)
def _isolate_durable_state(monkeypatch, tmp_path):
    """No test may ever read or write the user's real durable store (~/.local/state/cgc) —
    pin both the data dir and the DB path into the test's own tmp dir."""
    monkeypatch.setenv("CGC_DATA_DIR", str(tmp_path / "cgc-data"))
    monkeypatch.setenv("CGC_STORE_DB", str(tmp_path / "cgc-data" / "control.db"))
    # Nor the user's config file: the egress gate re-reads CGC_WORKSPACES / CGC_APPS from it at check
    # time, so a developer's real registry would decide a test's verdict. An absent file reads as unset.
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "cgc-config-absent"))


@pytest.fixture
def sb(tmp_path):
    """The lifecycle sandbox (tests/test_lifecycle.py): a throwaway HOME, a stateful launchctl stub on PATH.
    Imported lazily so the many tests that never use it do not pay for it."""
    import test_lifecycle
    s = test_lifecycle.Sandbox(tmp_path)
    yield s
    s.cleanup()
