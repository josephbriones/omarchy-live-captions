#!/bin/bash
set -euo pipefail

PLUGIN_ID="io.github.josephbriones.live-captions"

for command_name in omarchy omarchy-shell; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'FAIL: %s is required\n' "$command_name" >&2
    exit 1
  fi
done

if [[ ${XDG_SESSION_TYPE:-} != "wayland" || -z ${HYPRLAND_INSTANCE_SIGNATURE:-} ]]; then
  printf '%s\n' 'FAIL: run this inside an active Omarchy Hyprland session' >&2
  exit 1
fi

cleanup() {
  omarchy-shell shell hide "$PLUGIN_ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

result="$(omarchy-shell shell summon "$PLUGIN_ID" '{"demo":true}')"
if [[ $result != "ok" ]]; then
  printf 'FAIL: shell summon returned %s\n' "$result" >&2
  exit 1
fi

if [[ $(omarchy-shell "$PLUGIN_ID" ping) != "ok" ]]; then
  printf '%s\n' 'FAIL: plugin IPC target did not answer ping' >&2
  exit 1
fi

sleep 7
printf '%s\n' 'Saving demo screenshot. Review the entire image before copying it to preview.png.'
omarchy capture screenshot fullscreen save
printf '%s\n' 'The overlay used fixed demo text and no audio. Other visible applications may still contain private information.'
