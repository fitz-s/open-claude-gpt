// Created: 2026-06-10
// Last reused or audited: 2026-06-10
// Authority basis: open-claude-gpt skill v1 — DOM retrieval windowing
//
// Purpose: read a large ChatGPT answer through get_page_text under the 50000-char
// tool-output cap. get_page_text has no offset param, so this script renders the
// answer ONE chunk-window at a time into the page; the agent then calls
// get_page_text to read that window. The script NEVER returns answer content,
// URLs, or page text to the tool layer — every function returns metadata only
// (counts / indices / status). Content reaches the agent solely via get_page_text.
//
// Rendered by consult.py prep: __CGC_CONFIG__ is replaced with
// {"requestId": "...", "targetChars": 32000, "hardChars": 40000}.
//
// Usage (each is its own javascript_tool call; all returns are JSON strings):
//   inject this file                -> defines window.__cgcWin + runs install
//   window.__cgcWin.show(1)         -> render chunk 1, then call get_page_text
//   window.__cgcWin.show(2)         -> render chunk 2, then call get_page_text
//   window.__cgcWin.status()        -> {chunkCount,totalChars,status}
//   window.__cgcWin.restore()       -> reload tab to restore normal view
(function () {
  "use strict";
  var CFG = __CGC_CONFIG__;
  var RID = String(CFG.requestId || "");
  var TARGET = Number(CFG.targetChars || 32000);
  var HARD = Number(CFG.hardChars || 40000);
  var BEGIN = "BEGIN_RESPONSE:" + RID;
  var END = "END_RESPONSE:" + RID;

  function extractAnswer() {
    var nodes = document.querySelectorAll('[data-message-author-role="assistant"]');
    for (var k = nodes.length - 1; k >= 0; k--) {
      var t = (nodes[k].innerText || "").replace(/\r\n/g, "\n");
      var e = t.lastIndexOf(END);   // last occurrence = the real closing sentinel, even if echoed earlier
      if (e < 0) continue;
      var b = t.lastIndexOf(BEGIN, e);
      if (b < 0) return null;       // require a matching BEGIN before END; no silent fallback
      return t.slice(b + BEGIN.length, e).trim();
    }
    return null;
  }

  function splitChunks(text, TARGET, HARD) {
    var out = [];
    var n = text.length;
    if (n <= HARD) return [text];
    var i = 0;
    while (i < n) {
      if (n - i <= HARD) { out.push(text.slice(i)); break; }
      var lo = i + Math.floor(TARGET * 0.6);
      var hi = i + HARD;
      var cut = -1;
      var seps = ["\n\n", "\n", " "];
      for (var s = 0; s < seps.length; s++) {
        var p = text.lastIndexOf(seps[s], i + TARGET);
        if (p >= lo) { cut = p + seps[s].length; break; }
      }
      if (cut < 0 || cut > hi) cut = i + TARGET;
      // Keep URL tokens whole. If the whitespace-delimited token straddling the cut
      // has a scheme: push it to the next chunk (ws>i); or if it starts this chunk
      // (ws==i) keep it whole when it fits HARD; only split when it exceeds HARD.
      var ws = cut; while (ws > i && !/\s/.test(text[ws - 1])) ws--;
      var we = cut; while (we < n && !/\s/.test(text[we])) we++;
      if (/https?:\/\//.test(text.slice(ws, we))) {
        if (ws > i) cut = ws;
        else if (we - i <= HARD) cut = we;
      }
      if (cut <= i) cut = Math.min(i + Math.max(1, TARGET), n);   // progress guard
      out.push(text.slice(i, cut));
      i = cut;
    }
    return out;
  }

  var win = (window.__cgcWin = window.__cgcWin || {});

  win.show = function (i) {
    var st = win[RID];
    if (!st || !st.chunks) return JSON.stringify({ ok: false, err: "not-installed" });
    var idx = Number(i);
    if (!(idx >= 1 && idx <= st.chunkCount)) {
      return JSON.stringify({ ok: false, err: "bad-index", chunkCount: st.chunkCount });
    }
    var c = st.chunks[idx - 1];
    var header = "BEGIN_CHUNK:" + RID + ":" + idx + "/" + st.chunkCount;
    var footer = "END_CHUNK:" + RID + ":" + idx + "/" + st.chunkCount;
    var pre = document.createElement("pre");
    pre.textContent = header + "\n" + c + "\n" + footer;
    document.body.innerHTML = "";
    document.body.appendChild(pre);
    return JSON.stringify({ ok: true, mode: "show_chunk", chunkIndex: idx, chunkCount: st.chunkCount, chars: c.length });
  };

  win.status = function () {
    var st = win[RID];
    if (!st) return JSON.stringify({ ok: false, err: "not-installed" });
    return JSON.stringify({ ok: true, chunkCount: st.chunkCount, totalChars: st.totalChars, status: st.status });
  };

  win.restore = function () {
    location.reload();
    return JSON.stringify({ ok: true, status: "reloading" });
  };

  // Re-chunk the SAME answer at a smaller size (recovery for a truncated chunk).
  // Reuses the stored body + request id — do NOT re-run prep, which would mint a
  // new request id and break sentinel matching against the already-submitted answer.
  win.setTarget = function (t, h) {
    var st = win[RID];
    if (!st || !st.body) return JSON.stringify({ ok: false, err: "not-installed" });
    var nt = Number(t);
    var nh = Number(h || Math.round(nt * 1.25));
    if (!isFinite(nt) || nt < 2000 || !isFinite(nh) || nh < nt) {
      return JSON.stringify({ ok: false, err: "bad-target" });
    }
    st.chunks = splitChunks(st.body, nt, nh);
    st.chunkCount = st.chunks.length;
    st.target = nt;
    st.hard = nh;
    return JSON.stringify({ ok: true, chunkCount: st.chunkCount, totalChars: st.totalChars, target: nt, hard: nh });
  };

  // install (runs now)
  var body = extractAnswer();
  if (body === null) {
    win[RID] = { status: "no-answer" };
    return JSON.stringify({ ok: false, status: "no-answer" });
  }
  var chunks = splitChunks(body, TARGET, HARD);
  win[RID] = { requestId: RID, body: body, chunks: chunks, chunkCount: chunks.length, totalChars: body.length, target: TARGET, hard: HARD, status: "installed" };
  return JSON.stringify({ ok: true, chunkCount: chunks.length, totalChars: body.length });
})()
