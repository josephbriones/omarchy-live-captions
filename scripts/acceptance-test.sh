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

if [[ $MODE == "demo" ]]; then
  printf '%s\n' 'Summoning deterministic no-audio demo...'
  payload='{"demo":true}'
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

if [[ $(omarchy-shell "$PLUGIN_ID" ping) != "ok" ]]; then
  printf '%s\n' 'FAIL: plugin IPC target did not answer ping' >&2
  exit 1
fi

if [[ $MODE == "demo" ]]; then
  sleep 7
  state_json="$(omarchy-shell "$PLUGIN_ID" state)"
  python3 - "$state_json" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
if state.get("demo") is not True:
    raise SystemExit("FAIL: demo state did not report demo=true")
if int(state.get("segmentCount", 0)) <= 0:
    raise SystemExit("FAIL: demo produced no caption segments")
print("Demo state verified: demo=true and segmentCount>0")
PY
else
  printf 'Select %s, take an explicit Start action, and use a rights-cleared sample.\n' "$SOURCE"
  printf '%s' 'Press Enter after at least one caption appears (you may Stop first)... '
  IFS= read -r _answer
  state_json="$(omarchy-shell "$PLUGIN_ID" state)"
  python3 - "$state_json" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
if state.get("demo") is True:
    raise SystemExit("FAIL: real-audio run remained in demo mode")
if int(state.get("segmentCount", 0)) <= 0:
    raise SystemExit("FAIL: real-audio run produced no caption segments")
print("Real-audio state verified: segmentCount>0")
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
  trap - EXIT INT TERM
  printf '%s\n' 'PASS: overlay summoned and closed cleanly.'
fi
