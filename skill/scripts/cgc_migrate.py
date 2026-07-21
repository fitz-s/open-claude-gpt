#!/usr/bin/env python3
"""One-shot spool → store migration.

Reads the file-spool (pending/processing/done job files + status/<rid>.json) READ-ONLY and imports
every round into the SQLite store per a mapping whose one load-bearing rule is:

    a `processing` job with NO recorded conversation → possibly_accepted (NEVER queued)

because we cannot prove from disk whether its browser click reached ChatGPT, and re-queuing it would
risk a duplicate consult. Everything else maps to the obvious state. The migration does not modify or
delete the spool — the live cutover archives it separately, after the old daemon is drained. Running
it twice is safe: a rid already present in the store is skipped (INSERT would collide on the PK).

Mapping:
    pending/                                   -> queued
    processing/ + status.conversation present  -> waiting        (send happened; resume polling)
    processing/ + no conversation              -> possibly_accepted   (uncertain; never auto-resend)
    done/ status=done + non-empty answer file  -> completed_verified  (completed_unverified if a
                                                                        <out>.raw salvage sibling exists)
    done/ status=done + empty/missing answer   -> failed
    done/ status=blocker                       -> blocked
    done/ status=no_answer|error|other         -> failed
"""
from __future__ import annotations

import json
import os


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _rids_in(spool_dir, sub):
    d = os.path.join(spool_dir, sub)
    try:
        return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []


def _answer_state(status):
    """Terminal 'done' → completed_verified/unverified/failed based on the answer file on disk."""
    out = (status or {}).get("out")
    if out and os.path.exists(out) and os.path.getsize(out) > 0:
        # a <out>.raw sibling is the sentinel-less salvage path — the answer was not verified-complete
        if os.path.exists(out + ".raw"):
            return "completed_unverified"
        return "completed_verified"
    return "failed"


def plan_migration(spool_dir):
    """Compute the (rid, kind, target_state, conversation, out) rows WITHOUT touching a store — pure,
    so the mapping is unit-testable on its own and the cutover can dry-run it first."""
    rows = []
    seen = set()

    def job_kind(sub, rid):
        job = _read_json(os.path.join(spool_dir, sub, rid + ".json")) or {}
        return job.get("kind", "submit"), (job.get("conversation") or None), job.get("out")

    for rid in _rids_in(spool_dir, "pending"):
        if rid in seen:
            continue
        seen.add(rid)
        kind, _conv, out = job_kind("pending", rid)
        status = _read_json(os.path.join(spool_dir, "status", rid + ".json")) or {}
        rows.append({"rid": rid, "kind": kind, "state": "queued",
                     "conversation": status.get("conversation"), "out": status.get("out") or out})

    for rid in _rids_in(spool_dir, "processing"):
        if rid in seen:
            continue
        seen.add(rid)
        kind, _conv, out = job_kind("processing", rid)
        status = _read_json(os.path.join(spool_dir, "status", rid + ".json")) or {}
        conv = status.get("conversation")
        # THE load-bearing rule: no conversation on a processing job → uncertain, never re-queued.
        state = "waiting" if conv else "possibly_accepted"
        rows.append({"rid": rid, "kind": kind, "state": state,
                     "conversation": conv, "out": status.get("out") or out})

    for rid in _rids_in(spool_dir, "done"):
        if rid in seen:
            continue
        seen.add(rid)
        kind, _conv, out = job_kind("done", rid)
        status = _read_json(os.path.join(spool_dir, "status", rid + ".json")) or {}
        sstate = status.get("state")
        if sstate == "done":
            state = _answer_state(status)
        elif sstate == "blocker":
            state = "blocked"
        else:
            state = "failed"
        rows.append({"rid": rid, "kind": kind, "state": state,
                     "conversation": status.get("conversation"), "out": status.get("out") or out})

    return rows


def migrate_spool_to_store(spool_dir, store):
    """Apply plan_migration into `store`. Returns a summary {imported, skipped, by_state}. A rid
    already in the store is skipped (idempotent re-run). Reads only; the spool is left intact."""
    summary = {"imported": 0, "skipped": 0, "by_state": {}}
    for row in plan_migration(spool_dir):
        rid = row["rid"]
        if store.get_round(rid) is not None:
            summary["skipped"] += 1
            continue
        conv = row["conversation"]
        result_text = None
        confidence = None
        if row["state"] in ("completed_verified", "completed_unverified") and row["out"]:
            try:
                with open(row["out"], encoding="utf-8") as f:
                    result_text = f.read()
                confidence = "verified" if row["state"] == "completed_verified" else "unverified"
            except OSError:
                pass
        store.import_round(
            rid, row["kind"], row["state"],
            thread_id=conv, conversation_id=conv, out_path=row["out"],
            result_text=result_text, completion_confidence=confidence)
        summary["imported"] += 1
        summary["by_state"][row["state"]] = summary["by_state"].get(row["state"], 0) + 1
    return summary
