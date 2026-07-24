# Deterministic test environment. The store cutover is switched by CGC_STORE_BACKEND, which is read
# from the ambient environment / the user's ~/.config/cgc/config. Once the cutover flag is persisted
# there, spool-path tests would otherwise silently take the store path and fail. Pin the flag OFF by
# default so every test runs against a known backend; the store-backend tests exercise cgc_backend
# and the store directly (not via the CGC_STORE_BACKEND gate), so this does not affect them.
import pytest


@pytest.fixture(autouse=True)
def _pin_backend_off(monkeypatch):
    monkeypatch.setenv("CGC_STORE_BACKEND", "0")


@pytest.fixture(autouse=True)
def _isolate_durable_state(monkeypatch, tmp_path):
    """No test may ever read or write the user's real durable store (~/.local/state/cgc) —
    pin both the data dir and the DB path into the test's own tmp dir."""
    monkeypatch.setenv("CGC_DATA_DIR", str(tmp_path / "cgc-data"))
    monkeypatch.setenv("CGC_STORE_DB", str(tmp_path / "cgc-data" / "control.db"))
