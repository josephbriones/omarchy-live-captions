# Testing

## Portable validation

Run from the repository root:

```bash
bash scripts/validate.sh
```

The script checks:

- root manifest JSON, required files, and entry-point existence;
- absence of symlinks, as required by Omarchy validation;
- Python syntax and unit tests with fake capture/inference commands;
- JavaScript model tests under Node;
- shell syntax for repository scripts;
- `shellcheck` when available;
- `qmllint` when it and an Omarchy source path are available;
- `omarchy plugin validate` when run on Omarchy.

Unavailable platform-only tools are reported as skips, never as passes.

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
6. Closing removes every surface and watcher process.

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
3. Captions arrive after the documented rolling window plus local inference.
4. Boundary phrases appear once despite the one-second overlap.
5. Stop terminates the helper's capture and inference children.
6. No transcript/history appears below the user data directory.
7. Private runtime state contains no WAV or caption text.
8. An unrelated `pw-record` or `whisper-server` remains untouched.

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

Record model name, CPU/GPU, median inference duration, observed end-to-caption latency, and duplicate rate for a fixed rights-cleared sample. Release notes must report measured results from the tested configuration and must not generalize them as a universal latency promise.

## Release evidence

Attach `scripts/validate.sh` output, Omarchy version, `whisper.cpp` version, model name, and a reviewed demo screenshot. Never attach a real caption log or audio window.
