# Testing

## Portable validation

Run from the repository root:

```bash
bash scripts/validate.sh
```

The script checks:

- root manifest JSON, required files, and entry-point existence;
- absence of symlinks, as required by Omarchy validation;
- Python syntax and unit tests with mocks plus a deterministic real-subprocess pipeline using fake `setpriv`, `pw-record`, and `whisper-server` executables;
- JavaScript model tests under Node;
- shell syntax for repository scripts;
- `shellcheck` when available;
- `qmllint` when it and an Omarchy source path are available;
- `omarchy plugin validate` when run on Omarchy.

Unavailable platform-only tools are reported as skips, never as passes.

## Current v0.2 evidence

The deterministic automated suite exercises the local HTTP protocol, real subprocess creation and cleanup, first-PCM readiness, pause boundaries, stale-backlog failure, bounded output, and event parsing. Its fake commands do not use PipeWire, a real Whisper model, or physical audio hardware.

No completed v0.2 Omarchy-hardware run is recorded in this repository. Microphone capture, desktop-monitor capture, multiple physical displays, observed end-to-caption latency, `qmllint` with Omarchy imports, and `omarchy plugin validate` remain separate environment-dependent gates. A validator skip is not evidence that a gate passed.

## Safe desktop acceptance

From an Omarchy Wayland/Hyprland session with this plugin installed and enabled:

```bash
bash scripts/acceptance-test.sh
```

The default path summons deterministic demo captions. It must not open an audio device, launch `whisper-server`, or write audio/transcript data. Confirm:

1. Caption cards appear on each connected display.
2. The latest lines remain readable over dark and light content.
3. Clicking outside the control card reaches the application beneath.
4. Controls appear on the focused display.
5. Text-size and top/bottom controls respond.
6. The script polls until the first demo has a nonzero `segmentCount`, the requested source, and a still-running watcher.
7. The script closes that active demo and polls IPC until it reports `open=false` and `running=false`; the invisible `keepLoaded` QML owner may remain resident without capture.
8. A second demo completes naturally and reports `state=idle`, `running=false`, the requested source, and a nonzero `segmentCount`.

Use `--screenshot` to save a fullscreen demo screenshot through Omarchy.

## Explicit real-audio acceptance

Real capture is opt-in:

```bash
bash scripts/acceptance-test.sh --real --source microphone
bash scripts/acceptance-test.sh --real --source desktop
```

The script prints a privacy warning before invoking real capture. Speak into the microphone or play a short, rights-cleared local clip. Allow at least one four-second window plus inference time.

Confirm for each source:

1. The capture state and selected source are visible.
2. Opening without Start did not access audio.
3. Listening appears only after the selected route supplies its first PCM bytes.
4. Captions arrive after the documented rolling window plus local inference.
5. Boundary phrases appear once despite the one-second overlap, including exact-character overlap for unspaced CJK text.
6. Pause keeps the PipeWire stream linked, drains/discards new PCM, suppresses the current prepared/requested window, and starts no later window until Resume; Resume starts with fresh audio.
7. Stop and Close terminate the helper's capture and inference children.
8. A deliberately too-slow model stops with the stale-backlog guidance instead of presenting old captions.
9. No transcript/history appears below the user data directory.
10. Private runtime state contains no WAV or caption text.
11. An unrelated `pw-record` or `whisper-server` remains untouched.

Do not use private calls, copyrighted media, or confidential speech as test material.

## Platform matrix

| Area | macOS/non-Omarchy | Omarchy VM | Omarchy hardware |
| --- | --- | --- | --- |
| Manifest, Python, JS, shell syntax | Required | Required | Required |
| Fake capture/inference lifecycle | Required | Required | Required |
| Quattro load and demo | Not available | Required | Required |
| Microphone capture | Not available | Useful | Required |
| PipeWire desktop monitor | Not available | Environment-dependent | Required |
| Multiple physical displays | Not available | Simulated if possible | Required before stable release |
| Cleanup after close/crash | Unit coverage only | Required | Required |

## Performance gate

The UI's latency number is an estimate: the fixed four-second window plus local inference duration. It does not observe the wall-clock delay from speech to displayed caption and excludes some queueing and handoff time.

For the manual performance gate, separately record model name, language, CPU/GPU, median inference duration, observed end-to-caption latency, and duplicate rate for a fixed rights-cleared sample. Release notes may report only measurements from that tested configuration and must not generalize them as a universal latency promise. No such real-hardware result is recorded for v0.2 yet.

## Release evidence

Attach `scripts/validate.sh` output, Omarchy version, `whisper-cpp` package version, sanitized `doctor` capability result, model name, language, and a reviewed demo screenshot. Never attach a real caption log or audio window.
