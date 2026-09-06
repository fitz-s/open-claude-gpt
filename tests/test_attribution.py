# Tests for PRODUCER ATTRIBUTION (which model the provider says served an answer) and for the
# model-family FREEZE that makes a queued round's policy part of the request instead of part of
# whichever daemon happens to run it.
#
# The distinction under test throughout: `modelBadge` is a PRE-SEND read of the composer's
# switcher — selection evidence, "what was requested". `modelSlug` is data-message-model-slug read
# off the answer's own assistant node — attribution, "what served this". They are never merged,
# neither substitutes for the other, and ABSENT attribution is a third outcome that must not fail
# a consult that otherwise succeeded.
import argparse
import importlib.util
import json
import os
import sqlite3
import types

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(name, db_path=None):
    import sys
    if db_path is not None:
        os.environ["CGC_STORE_DB"] = str(db_path)
        os.environ["CGC_STATE_DIR"] = os.path.dirname(str(db_path))
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("CGC_MODEL_SLUG", raising=False)
    monkeypatch.delenv("CGC_MODEL_FAMILY", raising=False)
    store_mod = _load("cgc_store", tmp_path / "control.db")
    backend = _load("cgc_backend", tmp_path / "control.db")
    s = store_mod.Store(str(tmp_path / "control.db"))
    yield store_mod, backend, s, tmp_path
    s.close()


_OK_GATE = lambda p: (True, "ok")

# The three slugs measured live on 2026-09-06, same build, same page load — the ground truth this
# whole feature rests on, kept here so a future change that breaks the shape breaks a test.
SLUG_GPT6_PRO = "gpt-6-pro"
SLUG_GPT56_PRO = "gpt-5-6-pro"
SLUG_GPT56_THINKING = "gpt-5-6-thinking"

# A canonical conversation id — the followup path fullmatches this shape before it will send.
CONV = "11111111-1111-4111-8111-111111111111"


def _ready(store_mod, s, rid, kind="submit", **spec):
    base = {"project_url": "https://chatgpt.com/", "model": "Pro", "model_family": "Latest"}
    base.update(spec)
    s.create_round(rid, kind, prompt="review https://github.com/acme/x/tree/deadbeef please",
                   spec_json=json.dumps(base))
    s.set_state(rid, store_mod.READY)
    return s.get_round(rid)


def _cdp(slug, badge="6"):
    """A stubbed CDP driver whose wait reports `slug` as the producing model."""
    def run(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": CONV, "model_badge": badge, "stderr": ""}
        with open(kw["out"], "w") as f:
            f.write("the answer")
        return {"code": 0, "out": kw["out"], "model_slug": slug, "stderr": ""}
    return run


# ---- the verdict function ----------------------------------------------------

class TestVerdict:
    def test_absent_slug_is_unknown_not_failure(self, env, monkeypatch):
        store_mod, backend, _s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", SLUG_GPT6_PRO)
        for absent in (None, "", "   "):
            verdict, detail = backend._attribution_verdict(absent)
            assert verdict == store_mod.ATTR_UNKNOWN
            assert "unknown" in detail, "absence must explain itself, not read as a mismatch"

    def test_no_policy_is_unchecked_even_with_a_slug(self, env):
        store_mod, backend, _s, _t = env
        assert backend._attribution_verdict(SLUG_GPT6_PRO) == (store_mod.ATTR_UNCHECKED, None)

    def test_exact_and_glob_patterns_match(self, env, monkeypatch):
        store_mod, backend, _s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-*, gpt-5-6-pro")
        assert backend._attribution_verdict(SLUG_GPT6_PRO)[0] == store_mod.ATTR_MATCHED
        assert backend._attribution_verdict(SLUG_GPT56_PRO)[0] == store_mod.ATTR_MATCHED
        assert backend._attribution_verdict("GPT-6-Pro")[0] == store_mod.ATTR_MATCHED, "case-insensitive"

    def test_the_documented_rate_limit_fallback_is_a_mismatch(self, env, monkeypatch):
        """The concrete case this gate exists for: a Pro request served by the Thinking-tier model
        OpenAI's release notes describe falling back to under rate limits."""
        store_mod, backend, _s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-pro,gpt-5-6-pro")
        verdict, detail = backend._attribution_verdict(SLUG_GPT56_THINKING)
        assert verdict == store_mod.ATTR_MISMATCHED
        assert SLUG_GPT56_THINKING in detail and "CGC_MODEL_SLUG" in detail


# ---- end to end through the worker ------------------------------------------

class TestRoundRecordsAttribution:
    def test_absent_attribution_still_completes_verified(self, env):
        store_mod, backend, s, _t = env
        rid = "REQ-20260906-000000-0000a1"
        r = _ready(store_mod, s, rid)
        assert backend.process_round(s, r, _cdp(None), daemon_instance_id="d1",
                                     validate=_OK_GATE) == store_mod.COMPLETED_VERIFIED
        row = s.get_round(rid)
        assert row["model_slug"] is None
        assert row["attribution"] == store_mod.ATTR_UNKNOWN
        assert row["completion_confidence"] == "verified", \
            "a provider that stamps nothing must not downgrade a sentinel-verified answer"

    def test_matching_attribution_is_recorded_beside_the_badge(self, env, monkeypatch):
        store_mod, backend, s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-*")
        rid = "REQ-20260906-000000-0000a2"
        r = _ready(store_mod, s, rid)
        backend.process_round(s, r, _cdp(SLUG_GPT6_PRO, badge="6"), daemon_instance_id="d1",
                              validate=_OK_GATE)
        row = s.get_round(rid)
        assert row["model_slug"] == SLUG_GPT6_PRO
        assert row["model_badge"] == "6", "selection evidence is kept as its own column"
        assert row["attribution"] == store_mod.ATTR_MATCHED

    def test_mismatch_does_not_change_the_terminal_state(self, env, monkeypatch):
        """No new terminal state, and completed_verified keeps meaning ONLY 'the rid sentinel
        verified'. Identity lives in its own field or it is conflated all over again."""
        store_mod, backend, s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-pro")
        rid = "REQ-20260906-000000-0000a3"
        r = _ready(store_mod, s, rid)
        assert backend.process_round(s, r, _cdp(SLUG_GPT56_THINKING), daemon_instance_id="d1",
                                     validate=_OK_GATE) == store_mod.COMPLETED_VERIFIED
        row = s.get_round(rid)
        assert row["attribution"] == store_mod.ATTR_MISMATCHED
        assert row["model_slug"] == SLUG_GPT56_THINKING
        assert row["completion_confidence"] == "verified"

    def test_verdict_is_frozen_at_completion_not_re_derived_on_read(self, env, monkeypatch):
        """A later policy change must not retroactively reclassify a finished round."""
        store_mod, backend, s, tmp = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-*")
        rid = "REQ-20260906-000000-0000a4"
        backend.process_round(s, _ready(store_mod, s, rid), _cdp(SLUG_GPT6_PRO),
                              daemon_instance_id="d1", validate=_OK_GATE)
        monkeypatch.setenv("CGC_MODEL_SLUG", "nothing-matches-this")
        assert s.get_round(rid)["attribution"] == store_mod.ATTR_MATCHED
        aw = types.SimpleNamespace(rid=rid, out=str(tmp / "a4.txt"), timeout=2, poll=1)
        assert backend.await_round(aw) == 0, "the stored verdict governs, not today's policy"


# ---- THE gate, in await ------------------------------------------------------

class TestAwaitGate:
    def _envelope(self, capsys):
        return json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    def _completed(self, store_mod, s, rid, *, slug, attribution, badge="6"):
        s.create_round(rid, "submit"); s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
        s.mark_accepted(aid, "conv-g", model_badge=badge)
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="the verified answer",
                 model_slug=slug, attribution=attribution)

    def test_matched_is_auto_consumable(self, env, capsys):
        store_mod, backend, s, tmp = env
        rid = "REQ-20260906-000000-0000b1"
        self._completed(store_mod, s, rid, slug=SLUG_GPT6_PRO, attribution=store_mod.ATTR_MATCHED)
        aw = types.SimpleNamespace(rid=rid, out=str(tmp / "b1.txt"), timeout=2, poll=1)
        assert backend.await_round(aw) == 0
        env_json = self._envelope(capsys)
        assert env_json["attribution"] == "matched"
        assert env_json["model_slug"] == SLUG_GPT6_PRO
        assert env_json["model_badge"] == "6"
        assert env_json["next_command"], "a clean round still names the follow-up"

    def test_unknown_does_not_gate(self, env, capsys):
        """Silence from the provider is not evidence of a wrong model. Refusing an otherwise good
        consult over it would be inventing the very verdict this change exists to stop inventing."""
        store_mod, backend, s, tmp = env
        rid = "REQ-20260906-000000-0000b2"
        self._completed(store_mod, s, rid, slug=None, attribution=store_mod.ATTR_UNKNOWN)
        aw = types.SimpleNamespace(rid=rid, out=str(tmp / "b2.txt"), timeout=2, poll=1)
        assert backend.await_round(aw) == 0
        env_json = self._envelope(capsys)
        assert env_json["attribution"] == "unknown" and env_json["model_slug"] is None

    def test_mismatch_is_not_auto_consumable_and_says_why(self, env, capsys, monkeypatch):
        store_mod, backend, s, tmp = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-pro")
        rid = "REQ-20260906-000000-0000b3"
        self._completed(store_mod, s, rid, slug=SLUG_GPT56_THINKING,
                        attribution=store_mod.ATTR_MISMATCHED)
        out = str(tmp / "b3.txt")
        aw = types.SimpleNamespace(rid=rid, out=out, timeout=2, poll=1)
        assert backend.await_round(aw) == 3, "identity-unqualified: a human must judge it"
        env_json = self._envelope(capsys)
        assert env_json["attribution"] == "mismatched"
        assert env_json["next_command"] is None, "must not offer an auto-chained follow-up"
        assert SLUG_GPT56_THINKING in env_json["human_action"]
        assert env_json["confidence"] == "verified", \
            "the sentinel property is untouched — only identity failed"
        assert open(out).read() == "the verified answer", "the answer is still materialized"

    def test_a_pre_change_row_with_no_attribution_reads_back_valid(self, env, capsys):
        """Rows written before schema 5 carry NULL. NULL is 'this round predates attribution' and
        must never be reclassified into a mismatch."""
        store_mod, backend, s, tmp = env
        rid = "REQ-20260906-000000-0000b4"
        s.create_round(rid, "submit"); s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
        s.mark_accepted(aid, "conv-old"); s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="an answer from before all this")
        row = s.get_round(rid)
        assert row["attribution"] is None and row["model_slug"] is None
        aw = types.SimpleNamespace(rid=rid, out=str(tmp / "b4.txt"), timeout=2, poll=1)
        assert backend.await_round(aw) == 0
        assert self._envelope(capsys)["attribution"] is None


# ---- the DOM read ------------------------------------------------------------

class TestSlugRead:
    @pytest.fixture
    def cdp(self, tmp_path):
        return _load("cdp_consult", tmp_path / "control.db")

    def test_the_js_reads_the_named_attribute_off_the_rid_scoped_node(self, cdp):
        js = cdp._model_slug_js("REQ-20260906-000000-00000c", turn_index=3)
        assert "data-message-model-slug" in js
        # scoped to THIS rid's own turn interval, never "the last assistant message" — a stale tab
        # under-reports how many assistant turns exist.
        assert "BEGIN_RESPONSE:REQ-20260906-000000-00000c" in js and "__cgcNode(" in js and ",3)" in js

    def test_unscoped_read_omits_the_turn_index(self, cdp):
        assert ",3)" not in cdp._model_slug_js("REQ-20260906-000000-00000c")

    def test_read_degrades_to_none_never_raises(self, cdp):
        class Boom:
            def eval(self, _js):
                raise RuntimeError("CDP went away mid-read")
        assert cdp._read_attribution(Boom(), "REQ-20260906-000000-00000c") is None

    def test_empty_and_whitespace_read_as_absent(self, cdp):
        class Eval:
            def __init__(self, v): self.v = v
            def eval(self, _js): return self.v
        assert cdp._read_attribution(Eval(""), "r") is None
        assert cdp._read_attribution(Eval("   "), "r") is None
        assert cdp._read_attribution(Eval(SLUG_GPT6_PRO), "r") == SLUG_GPT6_PRO

    def test_receipt_shape(self, cdp, capsys):
        cdp._wait_receipt("REQ-1", "/tmp/x.txt", SLUG_GPT6_PRO)
        rec = json.loads(capsys.readouterr().out.strip())
        assert rec == {"rid": "REQ-1", "out": "/tmp/x.txt", "modelSlug": SLUG_GPT6_PRO}
        cdp._wait_receipt("REQ-1", "/tmp/x.txt", None)
        assert json.loads(capsys.readouterr().out.strip())["modelSlug"] is None


# ---- the daemon's argv + receipt parsing -------------------------------------

class TestDaemonForwarding:
    @pytest.fixture
    def daemon(self, tmp_path):
        return _load("cgc_daemon", tmp_path / "control.db")

    def _capture(self, daemon, monkeypatch, kind, stdout="", **kw):
        seen = {}

        class _R:
            returncode, stderr = 0, ""
            def __init__(self, out): self.stdout = out

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return _R(stdout)

        monkeypatch.setattr(daemon.subprocess, "run", fake_run)
        monkeypatch.setattr(daemon.spool, "live_lease_fds", lambda: ())
        res = daemon._make_run_cdp()(kind, **kw)
        return seen["cmd"], res

    def test_submit_argv_carries_the_family_from_the_spec(self, daemon, monkeypatch):
        cmd, _ = self._capture(daemon, monkeypatch, "submit", rid="REQ-1", prompt="p",
                               project_url="https://chatgpt.com/", model="Pro",
                               model_family="GPT-5.6 Sol")
        assert cmd[cmd.index("--model-family") + 1] == "GPT-5.6 Sol"

    def test_followup_argv_carries_both_dimensions(self, daemon, monkeypatch):
        """Before this change the followup child was handed NEITHER — both came from whatever the
        daemon's ambient environment happened to hold at send time."""
        cmd, _ = self._capture(daemon, monkeypatch, "followup", rid="REQ-1", prompt="p",
                               conversation="c", model="High", model_family="GPT-5.5")
        assert cmd[cmd.index("--model") + 1] == "High"
        assert cmd[cmd.index("--model-family") + 1] == "GPT-5.5"

    def test_wait_parses_the_slug_off_the_receipt(self, daemon, monkeypatch):
        line = json.dumps({"rid": "REQ-1", "out": "/x", "modelSlug": SLUG_GPT6_PRO})
        _, res = self._capture(daemon, monkeypatch, "wait", stdout="noise\n" + line + "\n",
                               rid="REQ-1", conversation="c", out="/x", poll=1, timeout=5)
        assert res["model_slug"] == SLUG_GPT6_PRO

    def test_wait_with_no_receipt_degrades_to_unknown(self, daemon, monkeypatch):
        _, res = self._capture(daemon, monkeypatch, "wait", stdout="", rid="REQ-1",
                               conversation="c", out="/x", poll=1, timeout=5)
        assert res["model_slug"] is None

    def test_last_json_wins(self, daemon):
        """followup --watch prints its send receipt and then its wait receipt into one stream."""
        out = json.dumps({"modelBadge": "6"}) + "\n" + json.dumps({"modelSlug": SLUG_GPT6_PRO})
        assert daemon._last_json(out) == {"modelSlug": SLUG_GPT6_PRO}
        assert daemon._last_json("not json at all") == {}
        assert daemon._last_json('"a bare string"') == {}, "a non-dict is not a receipt"


# ---- the family is part of the REQUEST --------------------------------------

def _enq(rid, prompt_file, **over):
    d = dict(rid=rid, prompt_file=prompt_file, kind="submit",
             project_url="https://chatgpt.com/", conversation="auto", parent=None,
             request_key=None, logical_sha=None, model="Pro", out=None, poll=1, timeout=5,
             quiet=False)
    d.update(over)
    return argparse.Namespace(**d)


class TestFamilyIsFrozenIntoTheRequest:
    def test_enqueue_writes_the_family_into_the_spec(self, env, tmp_path):
        store_mod, backend, s, _t = env
        rid = "REQ-20260906-000000-0000c1"
        a = _enq(rid, None, model_family="GPT-5.6 Sol")
        assert backend.enqueue_round(a, "a maths question, no code", str(tmp_path / "o.txt")) == 0
        spec = json.loads(s.get_round(rid)["spec_json"])
        assert spec["model_family"] == "GPT-5.6 Sol"

    def test_family_survives_enqueue_restart_retry_submit_and_followup(self, env, tmp_path,
                                                                       monkeypatch):
        """The whole point: the round is pinned to what the CALLER configured, and a daemon whose
        own environment says something else cannot retarget it — not on the first send, not after a
        restart-and-redispatch, and not on the follow-up that continues the thread."""
        store_mod, backend, s, _t = env
        rid = "REQ-20260906-000000-0000c2"
        a = _enq(rid, None, model_family="GPT-5.6 Sol")
        backend.enqueue_round(a, "a maths question, no code", str(tmp_path / "o.txt"))
        # a daemon started later, under a DIFFERENT ambient config
        monkeypatch.setenv("CGC_MODEL_FAMILY", "GPT-5.5")
        seen = []

        def cdp(kind, **kw):
            if kind in ("submit", "followup"):
                seen.append((kind, kw.get("model_family")))
                return {"code": 0, "conversation": CONV, "model_badge": "6", "stderr": ""}
            with open(kw["out"], "w") as f:
                f.write("the answer")
            return {"code": 0, "out": kw["out"], "model_slug": SLUG_GPT6_PRO, "stderr": ""}

        s.set_state(rid, store_mod.READY)
        backend.process_round(s, s.get_round(rid), cdp, daemon_instance_id="d1", validate=_OK_GATE)
        # …and a follow-up on the same thread, enqueued with the same explicit family
        frid = "REQ-20260906-000000-0000c3"
        fa = _enq(frid, None, kind="followup", parent=rid, model_family="GPT-5.6 Sol")
        backend.enqueue_round(fa, "and now the next question, no code", str(tmp_path / "f.txt"))
        s.set_state(frid, store_mod.READY)
        backend.process_round(s, s.get_round(frid), cdp, daemon_instance_id="d1", validate=_OK_GATE)
        assert seen == [("submit", "GPT-5.6 Sol"), ("followup", "GPT-5.6 Sol")], \
            "ambient CGC_MODEL_FAMILY must never override the family frozen into the spec"

    def test_same_key_different_family_conflicts(self, env, tmp_path, capsys):
        """Mirrors the existing idempotency law for `model`: a different family is a different
        logical request, so it must CONFLICT rather than return the first round's receipt."""
        store_mod, backend, s, _t = env
        pf = str(tmp_path / "p.txt")
        open(pf, "w").write("a maths question, no code")
        first = _enq("REQ-20260906-000000-0000d1", pf, request_key="k-fam",
                     logical_sha="a" * 64, model_family="Latest")
        assert backend.enqueue_round(first, "a maths question, no code", str(tmp_path / "1.txt")) == 0
        capsys.readouterr()
        second = _enq("REQ-20260906-000000-0000d2", pf, request_key="k-fam",
                      logical_sha="a" * 64, model_family="GPT-5.5")
        assert backend.enqueue_round(second, "a maths question, no code", str(tmp_path / "2.txt")) == 2
        assert "request_key_conflict" in capsys.readouterr().err
        assert s.get_round("REQ-20260906-000000-0000d2") is None, "the conflicting fire creates nothing"

    def test_same_key_same_family_is_still_idempotent(self, env, tmp_path):
        store_mod, backend, s, _t = env
        pf = str(tmp_path / "p2.txt")
        open(pf, "w").write("a maths question, no code")
        for rid in ("REQ-20260906-000000-0000d3", "REQ-20260906-000000-0000d4"):
            a = _enq(rid, pf, request_key="k-same", logical_sha="b" * 64, model_family="Latest")
            assert backend.enqueue_round(a, "a maths question, no code", str(tmp_path / "s.txt")) == 0
        assert s.get_round("REQ-20260906-000000-0000d4") is None, "the second fire reuses the first"

    def test_refire_inherits_the_recorded_family_not_todays_environment(self, env, monkeypatch):
        store_mod, backend, s, _t = env
        assert backend._spec_model_family(
            types.SimpleNamespace(model_family="GPT-5.6 Sol")) == "GPT-5.6 Sol"
        monkeypatch.setenv("CGC_MODEL_FAMILY", "GPT-5.5")
        # an object with no attribute at all (consult.py's fire namespace) falls back to the env
        assert backend._spec_model_family(types.SimpleNamespace()) == "GPT-5.5"


# ---- schema 5 ---------------------------------------------------------------

# The v4 shape as a REAL chain-migrated file has it, transcribed from the live control DB on
# 2026-09-06 (v1 columns first, then each migration's ADD COLUMNs in version order) — not from
# _DDL, whose fresh-DB column ORDER differs from any DB that actually walked the chain. Writing it
# the other way would have made the migration test agree with itself and nothing else.
_V4_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE threads (thread_id TEXT PRIMARY KEY, conversation_id TEXT, model TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rounds (rid TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(thread_id),
  kind TEXT NOT NULL, source_mode TEXT, spec_json TEXT, rendered_prompt TEXT,
  prompt_sha256 TEXT, state TEXT NOT NULL, result_text TEXT, error_code TEXT,
  completion_confidence TEXT, current_attempt_id TEXT, out_path TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  request_key TEXT, parent_rid TEXT, send_disposition TEXT, not_before TEXT,
  gate_retry_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_rounds_state ON rounds(state);
CREATE UNIQUE INDEX idx_rounds_request_key ON rounds(request_key) WHERE request_key IS NOT NULL;
CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, rid TEXT NOT NULL REFERENCES rounds(rid),
  daemon_instance_id TEXT, browser_epoch INTEGER, phase TEXT NOT NULL,
  started_at TEXT NOT NULL, last_progress_at TEXT, remote_evidence_json TEXT);
CREATE INDEX idx_attempts_rid ON attempts(rid);
CREATE TABLE browser_state (singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
  epoch INTEGER NOT NULL, chrome_pid INTEGER, health TEXT, restart_reason TEXT,
  updated_at TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT, attempt_id TEXT,
  kind TEXT NOT NULL, detail TEXT, at TEXT NOT NULL);
"""


def _make_v4(path):
    db = sqlite3.connect(path)
    db.executescript(_V4_DDL)
    db.execute("INSERT INTO meta(key,value) VALUES('schema_version','4')")
    db.execute("INSERT INTO meta(key,value) VALUES('store_uuid','uuid-v4-db')")
    db.execute("INSERT INTO rounds(rid,kind,state,result_text,completion_confidence,"
               "created_at,updated_at) VALUES('REQ-MIG4-A','submit','completed_verified',"
               "'an answer from before attribution existed','verified','t','t')")
    db.commit()
    db.close()


class TestSchema5Migration:
    def test_v4_migrates_additively_and_preserves_pre_change_rows(self, tmp_path):
        m = _load("cgc_store", tmp_path / "unused.db")
        p = str(tmp_path / "v4.db")
        _make_v4(p)
        s = m.Store(p)
        assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
        row = s.get_round("REQ-MIG4-A")
        assert row["result_text"] == "an answer from before attribution existed"
        assert row["completion_confidence"] == "verified", "an existing round is not reclassified"
        assert row["model_badge"] is None and row["model_slug"] is None
        assert row["attribution"] is None, "NULL means 'predates attribution', never 'mismatch'"
        s.close()
        assert os.path.exists(p + ".v4.bak"), "a pre-migration backup must exist"

    def test_migration_5_is_additive_only(self, tmp_path):
        """A daemon mid-flight against this file must survive the migration, exactly as v4 was
        designed to allow — so every statement must be an ADD COLUMN and nothing else."""
        m = _load("cgc_store", tmp_path / "unused.db")
        stmts = m._MIGRATIONS[5]
        assert stmts == [
            "ALTER TABLE rounds ADD COLUMN model_badge TEXT",
            "ALTER TABLE rounds ADD COLUMN model_slug TEXT",
            "ALTER TABLE rounds ADD COLUMN attribution TEXT",
        ]

    def test_migrated_and_fresh_schemas_agree(self, tmp_path):
        """_DDL creates a fresh DB at SCHEMA_VERSION directly, so it must offer the same COLUMNS the
        migration chain produces. Columns, not column order: ALTER TABLE appends, so a DB that
        walked 1->5 orders them by when each migration ran while a fresh one orders them as _DDL is
        written. Verified on a copy of the live v4 control DB (2026-09-06), whose rounds table ends
        ... schema_version, request_key, parent_rid, send_disposition, not_before, gate_retry_count.
        Every access here is by name, so the difference is invisible — asserting order would only
        pin a fixture's own layout."""
        m = _load("cgc_store", tmp_path / "unused.db")
        p = str(tmp_path / "v4b.db")
        _make_v4(p)
        migrated = m.Store(p)
        fresh = m.Store(str(tmp_path / "fresh.db"))

        def cols(st):
            return {r[1] for r in st.db.execute("PRAGMA table_info(rounds)")}
        assert cols(migrated) == cols(fresh)
        assert {"model_badge", "model_slug", "attribution"} <= cols(migrated)
        migrated.close(); fresh.close()

    def test_threads_model_stays_unwritten(self, tmp_path):
        """It was dead before and is deliberately still dead: a thread can span a model change, so
        there is no true value at that grain. The evidence lives on rounds."""
        m = _load("cgc_store", tmp_path / "t.db")
        s = m.Store(str(tmp_path / "t.db"))
        rid = "REQ-20260906-000000-0000e1"
        s.create_round(rid, "submit", prompt="p"); s.set_state(rid, m.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
        s.mark_accepted(aid, "conv-e1", model_badge="6")
        assert s.db.execute("SELECT model FROM threads WHERE thread_id='conv-e1'").fetchone()[0] is None
        s.close()


class TestRetrieveCarriesAttributionBack:
    def test_a_reconciled_source_round_keeps_the_slug_read_off_its_own_turn(self, env,
                                                                            monkeypatch):
        """A retrieve reads the slug off the SOURCE round's turn, so it is the source's fact. If it
        were dropped, that round would read NULL — 'predates attribution' — about a producer we
        actually saw."""
        store_mod, backend, s, _t = env
        monkeypatch.setenv("CGC_MODEL_SLUG", "gpt-6-*")
        source_rid = "REQ-20260906-000000-0000f1"
        retrieve_rid = "REQ-20260906-000000-0000f2"
        # the source round is uncertain: it may have sent, and nobody knows its answer
        s.create_round(source_rid, "submit", prompt="p"); s.set_state(source_rid, store_mod.READY)
        aid = s.begin_send(source_rid, "p", "h" * 64, daemon_instance_id="d0")
        s.mark_possibly_accepted(aid, "worker died mid-click")
        s.create_round(retrieve_rid, "retrieve", thread_id=CONV, parent_rid=source_rid,
                       spec_json=json.dumps({"conversation": CONV, "parent_rid": source_rid}))
        s.db.execute(f"UPDATE threads SET conversation_id='{CONV}' WHERE thread_id='{CONV}'")
        s.set_state(retrieve_rid, store_mod.READY)

        def cdp(kind, **kw):
            assert kw["rid"] == source_rid, "the waiter must be pinned to the SOURCE turn"
            with open(kw["out"], "w") as f:
                f.write("the source round's own sentinel-wrapped answer")
            return {"code": 0, "out": kw["out"], "model_slug": SLUG_GPT6_PRO, "stderr": ""}

        assert backend.process_round(s, s.get_round(retrieve_rid), cdp, daemon_instance_id="d1",
                                     validate=_OK_GATE) == store_mod.COMPLETED_VERIFIED
        for rid in (retrieve_rid, source_rid):
            row = s.get_round(rid)
            assert row["model_slug"] == SLUG_GPT6_PRO, rid
            assert row["attribution"] == store_mod.ATTR_MATCHED, rid
