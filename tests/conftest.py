# Deterministic test environment.
import pytest


@pytest.fixture(autouse=True)
def _isolate_durable_state(monkeypatch, tmp_path):
    """No test may ever read or write the user's real durable store (~/.local/state/cgc) —
    pin both the data dir and the DB path into the test's own tmp dir."""
    monkeypatch.setenv("CGC_DATA_DIR", str(tmp_path / "cgc-data"))
    monkeypatch.setenv("CGC_STORE_DB", str(tmp_path / "cgc-data" / "control.db"))
