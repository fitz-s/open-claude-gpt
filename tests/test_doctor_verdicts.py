# cgc_doctor's three verdicts (installed / can_send / can_read), its required-file list, and the
# store check. A fake Chrome answers /json/version on a free loopback port; the daemon heartbeat is
# written for real into a temp data dir. No browser, no network, nothing under the real home.
import ast
import glob
import http.server
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _doctor():
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location("cgc_doctor_lc", os.path.join(SCRIPTS, "cgc_doctor.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _FakeChrome:
    """Answers /json/version the way the debug Chrome does."""

    def __enter__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"Browser": "FakeChrome/1"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def doc_env(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("CGC_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "cgc-state"))
    monkeypatch.setenv("CGC_DATA_DIR", str(tmp_path / "cgc-data"))
    monkeypatch.setenv("CGC_STORE_DB", str(tmp_path / "cgc-data" / "control.db"))
    monkeypatch.setenv("CGC_CHROME", sys.executable)
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "none"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("PATH", str(tmp_path / "nobin") + os.pathsep + os.environ["PATH"])
    return tmp_path


def _run_doctor(monkeypatch, port):
    monkeypatch.setenv("CGC_PORT", str(port))
    d = _doctor()
    d.PORT = port
    return d, d.run(deep=False)


def _beat(**identity):
    sys.path.insert(0, SCRIPTS)
    import cgc_spool
    import cgc_store
    ident = {"protocol": 1, "schema_version": cgc_store.SCHEMA_VERSION,
             "db_path": os.path.abspath(cgc_store.db_path()), "store_uuid": "t", "daemon_instance_id": "t"}
    ident.update(identity)
    cgc_spool.heartbeat_write(os.getpid(), ident)


def test_doctor_no_daemon_is_not_ready_even_with_chrome_and_a_complete_install(doc_env, monkeypatch):
    with _FakeChrome() as ch:
        d, rep = _run_doctor(monkeypatch, ch.port)
    assert rep.installed and rep.can_read
    assert not rep.can_send and not rep.ready
    assert any("daemon" in w for w in rep.why["can_send"])
    assert rep.exit_code() == 3


def test_doctor_ready_only_when_daemon_alive_and_chrome_port_answers(doc_env, monkeypatch):
    _beat()
    with _FakeChrome() as ch:
        d, rep = _run_doctor(monkeypatch, ch.port)
        assert (rep.installed, rep.can_send, rep.can_read, rep.ready) == (True, True, True, True), rep.why
        assert rep.exit_code() == 0
    d, down = _run_doctor(monkeypatch, _free_port())              # same daemon, Chrome gone
    assert down.installed and not down.can_send and not down.ready
    assert any("debug Chrome" in w for w in down.why["can_send"])


def test_doctor_daemon_on_another_store_or_schema_is_not_serving_this_install(doc_env, monkeypatch):
    with _FakeChrome() as ch:
        _beat(db_path="/somewhere/else/control.db")
        _, other_db = _run_doctor(monkeypatch, ch.port)
        _beat(schema_version=1)
        _, old_schema = _run_doctor(monkeypatch, ch.port)
    for rep, word in ((other_db, "store"), (old_schema, "schema")):
        assert not rep.can_send and not rep.ready
        assert any(word in w for w in rep.why["can_send"]), rep.why


def test_doctor_can_read_is_separate_and_a_newer_store_blocks_both(doc_env, monkeypatch):
    sb_db = doc_env / "cgc-data"
    sb_db.mkdir()
    c = sqlite3.connect(sb_db / "control.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO meta VALUES('schema_version','99')")
    c.commit()
    c.close()
    _beat()
    with _FakeChrome() as ch:
        _, rep = _run_doctor(monkeypatch, ch.port)
    assert rep.installed and not rep.can_read and not rep.can_send and not rep.ready


def test_doctor_does_not_create_or_migrate_the_store(doc_env, monkeypatch):
    _run_doctor(monkeypatch, _free_port())
    assert not (doc_env / "cgc-data" / "control.db").exists()


def test_doctor_missing_required_file_means_not_installed(doc_env, tmp_path):
    d = _doctor()
    tree = tmp_path / "tree"
    subprocess.run(["cp", "-R", os.path.join(ROOT, "skill"), str(tree)], check=True)
    r = d.Report()
    d.check_skill_files(r, str(tree / "scripts"))
    assert r.installed
    os.remove(tree / "scripts" / "chatgpt_contract.json")
    (tree / "scripts" / "fixture_dom.js").write_text("")
    r = d.Report()
    d.check_skill_files(r, str(tree / "scripts"))
    assert not r.installed and not r.can_send
    why = " ".join(r.why["installed"])
    assert "chatgpt_contract.json missing" in why and "fixture_dom.js is empty" in why


def test_doctor_json_reports_verdicts_and_exit_code(doc_env):
    env = dict(os.environ, CGC_PORT=str(_free_port()))
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cgc_doctor.py"), "--json"],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["ready"] is False
    assert out["verdicts"]["installed"]["ok"] is True
    assert out["verdicts"]["can_send"]["ok"] is False and out["verdicts"]["can_send"]["why"]
    assert out["verdicts"]["can_read"]["ok"] is True
    assert r.returncode == 3


def test_required_files_are_exactly_what_the_scripts_directory_holds():
    d = _doctor()
    present = {os.path.basename(p) for p in glob.glob(os.path.join(SCRIPTS, "*"))
               if os.path.isfile(p)}
    assert present == set(d.REQUIRED) | set(d.OPTIONAL), (
        "skill/scripts changed: add the new file to cgc_doctor.REQUIRED (the code loads it) or OPTIONAL "
        f"(nothing on the runtime path does). Unlisted: {present - set(d.REQUIRED) - set(d.OPTIONAL)}; "
        f"listed but absent: {(set(d.REQUIRED) | set(d.OPTIONAL)) - present}")
    assert "retrieval_window.js" not in d.REQUIRED
    for must in ("chatgpt_contract.json", "fixture_dom.js", "cgc_drift.py"):
        assert must in d.REQUIRED


def test_every_module_and_data_file_the_code_loads_is_required():
    d = _doctor()
    mods = {os.path.basename(p)[:-3] for p in glob.glob(os.path.join(SCRIPTS, "*.py"))}
    imported = set()
    for p in glob.glob(os.path.join(SCRIPTS, "*.py")):
        for n in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imported.add(n.module.split(".")[0])
    for m in imported & mods:
        assert f"{m}.py" in d.REQUIRED, f"{m}.py is imported by the scripts but doctor does not require it"
    # data files named in source text
    text = "\n".join(open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(SCRIPTS, "*.py")))
    text += open(os.path.join(SCRIPTS, "cdp_launch.sh"), encoding="utf-8").read()
    for f in ("chatgpt_contract.json", "fixture_dom.js", "cdp_launch.sh"):
        assert f in text and f in d.REQUIRED
    assert "retrieval_window" not in text, "the MCP window script is gone; nothing may load it"


def test_doctor_stale_heartbeat_with_a_live_daemon_process_is_not_reported_as_not_running(doc_env, monkeypatch):
    """A draining or wedged daemon has an old heartbeat and a live process. 'Not running' would send the
    user to start a second daemon; the truth is 'alive but cannot take a consult'."""
    sys.path.insert(0, SCRIPTS)
    import cgc_spool
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "cgc_daemon.py"])
    try:
        cgc_spool._atomic_write(cgc_spool.daemon_path(), {"pid": p.pid, "ts": 1.0})
        with _FakeChrome() as ch:
            _, rep = _run_doctor(monkeypatch, ch.port)
    finally:
        p.kill()
        p.wait()
    assert not rep.can_send and not rep.ready
    why = " ".join(rep.why["can_send"])
    assert f"pid {p.pid}" in why and "heartbeat is stale" in why and "not running" not in why


def test_doctor_a_dead_pid_in_daemon_json_is_plain_not_running(doc_env, monkeypatch):
    sys.path.insert(0, SCRIPTS)
    import cgc_spool
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    cgc_spool._atomic_write(cgc_spool.daemon_path(), {"pid": dead.pid, "ts": 1.0})
    with _FakeChrome() as ch:
        _, rep = _run_doctor(monkeypatch, ch.port)
    assert any("not running" in w for w in rep.why["can_send"])
