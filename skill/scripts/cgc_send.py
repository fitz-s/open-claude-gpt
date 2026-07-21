#!/usr/bin/env python3
"""The `send_round` boundary — the ONE place a consult may cross from local to remote.

This is the vertical slice the first-principles teardown named as the single highest-leverage move:
render/validate happens before it (the gate), the browser click happens inside it, and it is the only
code allowed to commit the irreversible transition. Its whole job is to place the durable `sending`
commit at exactly the right instant so that recovery can be correct:

  - Everything BEFORE the click (open tab, select model, insert prompt, detect a login/captcha
    blocker) runs while the round is still `ready`. A failure here is a PRE-SEND failure: nothing
    left, so it is safely retryable or `blocked`.
  - The durable `ready -> sending` commit happens in the `on_before_click` callback, which the CDP
    adapter must call EXACTLY ONCE, immediately before pressing Enter, and only if it is actually
    going to press it.
  - Anything after that — a clean accept, an ambiguous `unknown_send`, a crash — is treated as a
    POSSIBLE send. Only a clean `accepted` clears it; everything else is `possibly_accepted`, which
    recovery will never auto-resend. A browser click cannot be made atomic with a local commit, so
    at-most-once AUTOMATIC retry under uncertainty is the strongest correct guarantee.

`cdp_send` contract (injected, so this module is testable without a browser and stays a pure bridge):
    cdp_send(prompt: str, on_before_click: Callable[[], None]) -> dict
      returns {"outcome": "accepted"|"unknown_send"|"presend_fail"|"blocker",
               "conversation": <id or None>, "evidence": <json str or None>, "reason": <str or None>}
    It MUST invoke on_before_click() once right before the click, and only when it will click.
"""
from __future__ import annotations

import cgc_store as store_mod


def send_round(store, rid: str, cdp_send, *, rendered_prompt: str, prompt_sha256: str,
               daemon_instance_id: str, browser_epoch: int | None = None) -> dict:
    """Drive one `ready` round across the send boundary. Returns the cdp_send result annotated with
    the resolved round state. Never leaves the round in a limbo the state machine doesn't name."""
    committed = {"attempt_id": None}

    def on_before_click():
        # Called by the adapter immediately before the irreversible click. Commit `sending` durably
        # HERE (not earlier), so a pre-click failure never masquerades as a possible send.
        if committed["attempt_id"] is not None:
            raise RuntimeError("on_before_click called twice — the click must be a single event")
        committed["attempt_id"] = store.begin_send(
            rid, rendered_prompt, prompt_sha256,
            daemon_instance_id=daemon_instance_id, browser_epoch=browser_epoch)

    try:
        result = cdp_send(rendered_prompt, on_before_click)
    except BaseException as e:  # noqa: BLE001 — any failure must resolve the round, then re-raise
        aid = committed["attempt_id"]
        if aid is not None:
            # crashed AFTER the click was committed → the send may have reached ChatGPT.
            store.mark_possibly_accepted(aid, f"exception after send commit: {e!r}")
            return {"outcome": "crashed", "state": store_mod.POSSIBLY_ACCEPTED, "error": repr(e)}
        # crashed BEFORE any click → nothing left; the round is still `ready`, retryable by the caller.
        return {"outcome": "presend_crash", "state": store_mod.READY, "error": repr(e)}

    aid = committed["attempt_id"]
    outcome = result.get("outcome")

    if aid is None:
        # The adapter never clicked → this is a pre-send condition; the round is still `ready`.
        if outcome == "blocker":
            store.set_state(rid, store_mod.BLOCKED, error_code=result.get("reason") or "blocker")
            return {**result, "state": store_mod.BLOCKED}
        # presend_fail (composer not ready, model not selectable, …): leave `ready` for the caller to
        # retry or fail; send_round does not invent a verdict for a send that never happened.
        return {**result, "state": store_mod.READY}

    # The click WAS committed. Only a clean accept clears it; everything else is uncertain.
    if outcome == "accepted":
        store.mark_accepted(aid, result.get("conversation"), result.get("evidence"))
        return {**result, "state": store_mod.ACCEPTED}
    store.mark_possibly_accepted(aid, result.get("reason") or f"post-click outcome={outcome!r}")
    return {**result, "state": store_mod.POSSIBLY_ACCEPTED}
