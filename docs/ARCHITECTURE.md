# Architecture

Live Captions is one Omarchy `overlay` plugin with `keepLoaded` enabled. The lightweight QML owner stays resident so Close can wait for its child process to finish cleanup; it has no background service and never opens audio until Start.

```text
microphone OR desktop monitor
          |
      pw-record
          |
 rolling 4 s windows, 1 s overlap
          |
 local whisper-server (one persistent model process)
          |
 normalized JSONL events
          |
 click-through Quickshell caption surfaces
```

## Components

### `LiveCaptions.qml`

The Quattro entry point implements `open(payloadJson)` and `close()`. The host injects its shell and manifest properties after construction, so those properties are not required during component creation.

One transparent layer-shell window is created for every display. Caption cards use an empty input region, leaving applications beneath clickable. A focused-display control surface provides keyboard- and assistive-technology-accessible Start, Stop, source, size, and position controls without turning the whole overlay into a click target. It uses Omarchy's brief exclusive focus prime, settles to on-demand focus, and releases keyboard ownership as soon as the overlay closes.

The root QML object owns one watcher process. Pause and resume travel over that process's stdin. Closing the overlay clears caption text, stops the same QML-owned process, and keeps the invisible QML owner alive long enough for bounded child cleanup.

### `CaptionModel.js`

The JavaScript module contains deterministic payload parsing, event normalization, visible-row selection, labels, and time/latency formatting. It has no QML object ownership or I/O, so the same functions run under Node in CI.

### `bin/live-captions` and `lib/live_captions.py`

The standard-library Python helper is the sole boundary around capture and inference. Standard output is reserved for newline-delimited JSON; diagnostics go to standard error.

The helper:

- diagnoses commands, the selected source, and model readiness without opening audio;
- previews or explicitly writes plugin-only JSON preferences;
- starts one `pw-record` process for the chosen source;
- starts one persistent local `whisper-server` for the chosen model, hard-bound to a random loopback port and tokenized request path;
- rotates overlapping audio windows through inference;
- de-duplicates normalized word overlap, with exact-character overlap for unspaced CJK text, and resets that history after a fully silent window;
- rejects stale audio before emitting an inference result when the selected model cannot keep up with the live stream;
- stops only processes belonging to its own session;
- emits deterministic no-audio demo events.

## Capture and inference state

```text
open -> checking -> idle
                    |
                  Start
                    |
        starting (server health)
                    |
          capture + first PCM
                    |
                 listening -> recording

listening or recording -> paused -> listening (Resume)

starting/listening/recording/paused -> stopping -> idle

Any startup, capture, inference, or stale-backlog failure -> error
```

Opening the overlay reaches `idle`/`checking`; only an explicit Start action (UI or direct IPC) begins the real-audio path. The model server becomes healthy before `pw-record` opens the device, and the UI does not report Listening until the first PCM bytes arrive. Demo follows its own synthetic path and does not launch capture or inference.

## Why one source per session

Microphone and desktop monitor streams can carry the same voice at different delays. Mixing them makes duplicate captions and echo likely. Version 0.2 therefore makes the active source an explicit exclusive choice. A later dual-source design would need independent timelines and stronger de-duplication rather than a hidden audio mix.

## Rolling window model

Each completed four-second window is sent to the already-loaded local model. Adjacent windows overlap by one second to reduce clipped words at boundaries. Text overlap is normalized and de-duplicated before a new segment is emitted.

Low-RMS silent windows skip inference to reduce idle hallucinations and CPU use. During Pause, the owned `pw-record` process keeps running and its PipeWire stream remains linked; the helper drains and discards PCM, advances a transition epoch, and emits no captions. The one window already being prepared or requested may finish locally after Pause, but its older-epoch result is suppressed and no later window starts until Resume. Resume starts with a fresh window rather than replaying pre-pause speech; Stop or Close ends capture entirely.

Practical latency is window completion plus inference, queueing, and local handoff time. It depends on the model, language, hardware, and audio. The overlay's number is only four seconds plus local inference duration; it is labeled as an estimate and is not an observed end-to-caption measurement. Only a separate real-hardware acceptance run can measure end-to-caption latency, and no such v0.2 result is recorded in this repository. If queued audio ages beyond one three-second stride, the helper fails with guidance to choose a smaller model rather than displaying stale text. It does not label the result “realtime” or promise sub-second output.

## Data lifetime

The helper does not create a meeting, transcript database, history, or export. It holds a bounded transcript tail and rolling PCM/WAV windows in memory, discarding each window after local inference. Its mode-`0700` XDG runtime directory contains only a mode-`0600` single-session lock—not caption text or audio. Preferences at `$XDG_CONFIG_HOME/omarchy/live-captions/config.json` store only model/source/language settings.

## Process ownership

Every capture run has helper-owned process handles. Quickshell stops the watcher it started; the watcher then signals those exact children and escalates only within their owned process groups after a bounded wait. `setpriv --pdeathsig KILL` is the hard-death fallback if the watcher itself disappears. It cannot target unrelated `pw-record` or `whisper-server` instances.

No predictable shared `/tmp` PID file is trusted, and model/config values are never interpolated into a shell command.

The real-audio path requires `setpriv` from `util-linux`, `pw-record`, and a `whisper-server` exposing all fixed local API flags probed by `doctor`. The current/tested Arch baseline for v0.2 is `whisper-cpp` 1.9.1, not a semantic minimum; older or newer builds may work when the capability probe passes. Model selection is language-aware: `.en.bin` models are valid only for English; automatic detection and other languages require a multilingual model.

## Failure behavior

- Missing command/model/source: setup state; no capture starts.
- Capture exit: owned inference stops and the UI receives a sanitized error.
- Capture produces no PCM within five seconds: both children stop and the UI identifies the selected route as unavailable.
- Inference startup/health timeout: the owned server is stopped and audio capture never starts.
- Inference server exit or stale audio backlog: capture stops instead of leaving a false Listening state.
- Malformed inference response: last valid captions remain bounded; an error is emitted.
- Overlay close: Quickshell stops the watcher it owns, whose finalizer terminates capture and inference.
- Abrupt shell death: parent-death controls terminate owned children where supported; no persisted audio or transcript exists to recover.

## Local trust boundary

The random request path prevents accidental cross-talk with another loopback service; it is not a sandbox or an authentication boundary against other local processes that can inspect the user's process table. Omarchy plugins, PipeWire clients, and other processes in the same desktop session already run inside the local-user trust boundary.

## Explicit non-goals for 0.2

- Sub-second partial-word captioning.
- Simultaneous microphone and desktop capture.
- Cloud transcription or translation.
- Voice biometric identification.
- Automatic package/model installation.
- Editing Omarchy, Hyprland, or PipeWire configuration.
