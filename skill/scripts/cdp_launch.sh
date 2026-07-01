#!/usr/bin/env bash
# Created: 2026-06-11
# Last reused or audited: 2026-06-15
# Authority basis: open-claude-gpt skill v2 (CDP backend). Adds CGC_GATE automated
#   gate mode so submit/wait can self-heal Step 0 with no LLM step.
#
# Launch / ensure the DEDICATED Chrome debug profile for open-claude-gpt.
# The agent never types credentials — the user logs into ChatGPT Pro once in the
# window that opens; the profile PERSISTS the session across Chrome restarts.
#
# CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+),
# so this uses a separate --user-data-dir reserved for consults. Your normal
# Chrome is untouched and can keep running alongside this.
#
#   bash cdp_launch.sh                  # interactive: verbose launch + status
#   CGC_GATE=1 bash cdp_launch.sh       # automated gate: start if down, probe login,
#                                        #   terse. exit 0 ready / 2 login-needed / 1 chrome-fail
#   CGC_PORT=9444 bash cdp_launch.sh    # custom port
#   CGC_CHROME=/path/to/chrome ...      # explicit Chrome/Chromium/Edge binary
#   CGC_PROJECT_URL=https://chatgpt.com/g/g-p-<id>-<slug>/project   # your project (else new chat)
#
#   "$CHROME" --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 \
#     --remote-allow-origins=http://127.0.0.1:9333 --user-data-dir="$PROFILE" ...
set -euo pipefail

PORT="${CGC_PORT:-9333}"
PROFILE="${CGC_PROFILE:-$HOME/.cgc-chrome}"
GATE="${CGC_GATE:-0}"   # 1 = automated gate (terse, exit-code-driven); 0 = interactive (verbose)
PROJECT_URL="${CGC_PROJECT_URL:-https://chatgpt.com/}"

# Locate a Chromium-family browser cross-platform. $CGC_CHROME wins; else probe the
# known install paths for macOS / Linux, then fall back to whatever is on PATH.
find_chrome() {
  if [ -n "${CGC_CHROME:-}" ]; then printf '%s' "$CGC_CHROME"; return; fi
  local c
  for c in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
    "/usr/bin/google-chrome" "/usr/bin/google-chrome-stable" \
    "/usr/bin/chromium" "/usr/bin/chromium-browser" "/usr/bin/microsoft-edge" \
    "/snap/bin/chromium"; do
    [ -x "$c" ] && { printf '%s' "$c"; return; }
  done
  for c in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return; }
  done
}
CHROME="$(find_chrome)"

if [ -z "$CHROME" ] || [ ! -x "$CHROME" ]; then
  echo "CGC_ERROR chrome_not_found: set CGC_CHROME=/path/to/chrome (looked for Chrome/Chromium/Edge)" >&2
  exit 1
fi

# Report login state via CDP so the gate only alerts when the user must log in.
probe_login() {
  python3 - "$PORT" <<'PY' 2>/dev/null || echo "CGC_LOGIN unknown"
import json,sys,urllib.request,websocket
port=sys.argv[1]
ts=json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json",timeout=5))
pg=[t for t in ts if t.get("type")=="page" and "chatgpt.com" in (t.get("url") or "")] or \
   [t for t in ts if t.get("type")=="page"]
ws=websocket.create_connection(pg[0]["webSocketDebuggerUrl"],timeout=6)
ws.send(json.dumps({"id":1,"method":"Runtime.evaluate","params":{
  "expression":"(!!document.querySelector('input[type=\"password\"]'))||/^\\/(auth|login)(\\/|$)/i.test(location.pathname)",
  "returnByValue":True}}))
while True:
    m=json.loads(ws.recv())
    if m.get("id")==1: break
ws.close()
print("CGC_LOGIN needed" if m["result"]["result"]["value"] else "CGC_LOGIN ok")
PY
}

# Emit the result honoring gate mode, then exit. In gate mode: terse + exit-code-driven
# (0 ready, 2 login-needed) so a caller can branch without parsing prose; ONLY the
# login-needed case is loud. In interactive mode: verbose, always exit 0.
finish() {
  state="$1"
  if [ "$GATE" = "1" ]; then
    case "$state" in
      "CGC_LOGIN ok")     echo "CGC_READY" ; exit 0 ;;
      "CGC_LOGIN needed")
        echo "CGC_LOGIN needed: a debug Chrome window is open — log into ChatGPT Pro there, then retry." >&2
        exit 2 ;;
      *)                  echo "CGC_READY (login unverified)" ; exit 0 ;;  # up but probe inconclusive → let submit's own checks decide
    esac
  fi
  echo "$state"
  case "$state" in
    "CGC_LOGIN needed") echo "First-time setup: log into ChatGPT Pro in that window, then leave it open." ;;
    "CGC_LOGIN ok")     echo "Already logged in (session persisted) — ready for consults." ;;
  esac
  exit 0
}

if curl -s -m 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  [ "$GATE" = "1" ] || echo "CGC_OK debug Chrome already up on port $PORT (profile $PROFILE)"
  finish "$(probe_login)"
fi

# Not up → start it (this is the no-LLM auto-start the gate guarantees).
mkdir -p "$PROFILE"
# allow-origins scoped to loopback (NOT '*') — the CDP client connects from this origin.
# debugging-address pinned to loopback so the port is never reachable off-host.
"$CHROME" \
  --remote-debugging-port="$PORT" \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins="http://127.0.0.1:$PORT" \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  "$PROJECT_URL" >/dev/null 2>&1 &

for _ in $(seq 1 20); do
  sleep 0.5
  if curl -s -m 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    [ "$GATE" = "1" ] || echo "CGC_OK debug Chrome up on port $PORT (profile $PROFILE)."
    sleep 1
    finish "$(probe_login)"
  fi
done
echo "CGC_ERROR debug Chrome did not expose port $PORT" >&2
exit 1
