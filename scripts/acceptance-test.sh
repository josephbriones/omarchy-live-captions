#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_ID="io.github.josephbriones.live-captions"
MODE="demo"
SOURCE="microphone"
TAKE_SCREENSHOT=false
KEEP_OPEN=false

usage() {
  printf '%s\n' \
    'Usage: bash scripts/acceptance-test.sh [--real] [--source microphone|desktop] [--screenshot] [--keep-open]' \
    '' \
    'The default runs deterministic demo mode and never opens an audio device.' \
    '--real explicitly enters a user-driven real-audio test; it still does not auto-start capture.' \
    '--screenshot saves a fullscreen screenshot through Omarchy.' \
    '--keep-open leaves the overlay visible after the checks.'
}

while (( $# > 0 )); do
  case "$1" in
    --real)
      MODE="real"
      ;;
    --source)
      if (( $# < 2 )); then
        printf '%s\n' 'FAIL: --source requires microphone or desktop' >&2
        exit 2
      fi
      SOURCE="$2"
      shift
      ;;
    --screenshot)
      TAKE_SCREENSHOT=true
      ;;
    --keep-open)
      KEEP_OPEN=true
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "$SOURCE" in
  microphone | desktop) ;;
  *)
    printf 'FAIL: unsupported source: %s\n' "$SOURCE" >&2
    exit 2
    ;;
esac

bash "$ROOT/scripts/validate.sh"

for command_name in omarchy omarchy-shell; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'FAIL: %s is required for desktop acceptance\n' "$command_name" >&2
    exit 1
  fi
done

if [[ ${XDG_SESSION_TYPE:-} != "wayland" || -z ${HYPRLAND_INSTANCE_SIGNATURE:-} ]]; then
  printf '%s\n' 'FAIL: run desktop acceptance inside an active Omarchy Hyprland session' >&2
  exit 1
fi

cleanup() {
  if [[ $KEEP_OPEN == "false" ]]; then
    omarchy-shell shell hide "$PLUGIN_ID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

wait_until_closed() {
  local state_json=""
  local attempt
  for (( attempt = 0; attempt < 50; attempt++ )); do
    if ! state_json="$(omarchy-shell "$PLUGIN_ID" state 2>/dev/null)"; then
      sleep 0.1
      continue
    fi
    if python3 - "$state_json" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
raise SystemExit(0 if state.get("open") is False and state.get("running") is False else 1)
PY
    then
      return 0
    fi
    sleep 0.1
  done
  printf 'FAIL: overlay did not settle closed: %s\n' "${state_json:-no IPC state}" >&2
  return 1
}

if [[ $MODE == "demo" ]]; then
  printf '%s\n' 'Summoning deterministic no-audio demo...'
  payload="{\"demo\":true,\"source\":\"$SOURCE\"}"
else
  printf '%s\n' 'REAL AUDIO TEST: an explicit Start action in the overlay or its IPC will capture the selected local source.'
  printf '%s\n' 'No audio or transcript should be retained, but visible captions may reveal private speech.'
  printf '%s\n' 'The script will summon controls only; you must take an explicit Start action.'
  if [[ ! -t 0 ]]; then
    printf '%s\n' 'NOT PERFORMED: real-audio acceptance requires an interactive terminal and human confirmation.' >&2
    exit 3
  fi
  payload="{\"source\":\"$SOURCE\"}"
fi

result="$(omarchy-shell shell summon "$PLUGIN_ID" "$payload")"
if [[ $result != "ok" ]]; then
  printf 'FAIL: shell summon returned %s\n' "$result" >&2
  exit 1
fi

ipc_ready=false
for (( attempt = 0; attempt < 100; attempt++ )); do
  if [[ $(omarchy-shell "$PLUGIN_ID" ping 2>/dev/null || true) == "ok" ]]; then
    ipc_ready=true
    break
  fi
  sleep 0.1
done
if [[ $ipc_ready != "true" ]]; then
  printf '%s\n' 'FAIL: plugin IPC target did not answer ping' >&2
  exit 1
fi

if [[ $MODE == "demo" ]]; then
  # Exercise Close while the deterministic watcher is still producing
  # captions. Polling avoids assuming how quickly a cold plugin Loader starts.
  active=false
  state_json=""
  for (( attempt = 0; attempt < 100; attempt++ )); do
    state_json="$(omarchy-shell "$PLUGIN_ID" state)"
    if python3 - "$state_json" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
raise SystemExit(
    0 if state.get("demo") is True
    and state.get("running") is True
    and int(state.get("segmentCount", 0)) > 0
    else 1
)
PY
    then
      active=true
      break
    fi
    sleep 0.1
  done
  if [[ $active != "true" ]]; then
    printf 'FAIL: demo never produced a caption while running: %s\n' "${state_json:-no IPC state}" >&2
    exit 1
  fi
  python3 - "$state_json" "$SOURCE" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
expected_source = sys.argv[2]
if state.get("demo") is not True:
    raise SystemExit("FAIL: demo state did not report demo=true")
if state.get("running") is not True:
    raise SystemExit("FAIL: demo watcher finished before the active-Close check")
if int(state.get("segmentCount", 0)) <= 0:
    raise SystemExit("FAIL: active demo produced no caption segments")
if state.get("source") != expected_source:
    raise SystemExit(
        f"FAIL: demo source {state.get('source')!r} did not match {expected_source!r}"
    )
print("Active demo verified: running, requested source, and segmentCount>0")
PY

  printf '%s\n' 'Closing the demo while its watcher is running...'
  omarchy-shell shell hide "$PLUGIN_ID" >/dev/null
  wait_until_closed
  printf '%s\n' 'Active Close verified: open=false and running=false.'

  printf '%s\n' 'Summoning a second demo to verify natural idle completion...'
  result="$(omarchy-shell shell summon "$PLUGIN_ID" "$payload")"
  if [[ $result != "ok" ]]; then
    printf 'FAIL: second demo summon returned %s\n' "$result" >&2
    exit 1
  fi
  finished=false
  state_json=""
  for (( attempt = 0; attempt < 100; attempt++ )); do
    state_json="$(omarchy-shell "$PLUGIN_ID" state)"
    if python3 - "$state_json" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
raise SystemExit(
    0 if state.get("demo") is True
    and state.get("state") == "idle"
    and state.get("running") is False
    and int(state.get("segmentCount", 0)) > 0
    else 1
)
PY
    then
      finished=true
      break
    fi
    sleep 0.1
  done
  if [[ $finished != "true" ]]; then
    printf 'FAIL: terminal demo did not settle idle: %s\n' "${state_json:-no IPC state}" >&2
    exit 1
  fi
  python3 - "$state_json" "$SOURCE" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
expected_source = sys.argv[2]
if state.get("open") is not True or state.get("demo") is not True:
    raise SystemExit("FAIL: terminal demo was not open in demo mode")
if state.get("state") != "idle" or state.get("running") is not False:
    raise SystemExit(
        f"FAIL: terminal demo did not finish idle and stopped: {state!r}"
    )
if int(state.get("segmentCount", 0)) <= 0:
    raise SystemExit("FAIL: terminal demo produced no caption segments")
if state.get("source") != expected_source:
    raise SystemExit(
        f"FAIL: terminal demo source {state.get('source')!r} did not match {expected_source!r}"
    )
print("Terminal demo verified: idle, stopped, requested source, and segmentCount>0")
PY
else
  printf 'Select %s, take an explicit Start action, and use a rights-cleared sample.\n' "$SOURCE"
  printf '%s' 'Press Enter after at least one caption appears (you may Stop first)... '
  IFS= read -r _answer
  state_json="$(omarchy-shell "$PLUGIN_ID" state)"
  python3 - "$state_json" "$SOURCE" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
expected_source = sys.argv[2]
if state.get("open") is not True:
    raise SystemExit("FAIL: real-audio overlay was not open at verification")
if state.get("demo") is True:
    raise SystemExit("FAIL: real-audio run remained in demo mode")
if state.get("source") != expected_source:
    raise SystemExit(
        f"FAIL: real-audio source {state.get('source')!r} did not match {expected_source!r}"
    )
allowed_states = {"listening", "recording", "paused", "idle"}
if state.get("state") not in allowed_states:
    raise SystemExit(f"FAIL: real-audio run ended in invalid state: {state.get('state')!r}")
if state.get("state") == "idle" and state.get("running") is not False:
    raise SystemExit("FAIL: idle real-audio state still reported a running watcher")
if int(state.get("segmentCount", 0)) <= 0:
    raise SystemExit("FAIL: real-audio run produced no caption segments")
print("Real-audio state verified: open, expected source/state, and segmentCount>0")
PY
fi

if [[ $TAKE_SCREENSHOT == "true" ]]; then
  printf '%s\n' 'Saving fullscreen screenshot. Review every visible application before sharing it.'
  omarchy capture screenshot fullscreen save
fi

if [[ $KEEP_OPEN == "true" ]]; then
  printf '%s\n' 'PASS: overlay summoned and left open by request.'
else
  omarchy-shell shell hide "$PLUGIN_ID" >/dev/null
  wait_until_closed
  trap - EXIT INT TERM
  printf '%s\n' 'PASS: overlay summoned and closed cleanly.'
fi
