# Architecture

Live Captions is one on-demand Omarchy `overlay` plugin. It intentionally has no always-loaded service.

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

One transparent layer-shell window is created for every display. Caption cards use an empty input region, leaving applications beneath clickable. A focused-display control surface provides the interactive Start, Stop, source, size, and position controls without turning the whole overlay into a click target.

The root QML object owns one watcher process. Pause and resume travel over that process's stdin; closing the overlay stops the same QML-owned process before the on-demand plugin unloads.

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
- de-duplicates overlap and emits bounded normalized caption events;
- stops only processes belonging to its own session;
- emits deterministic no-audio demo events.

## Capture and inference state

```text
idle -> checking -> starting -> recording -> stopping -> idle
           |                         |
           +-------- error <---------+
```

Opening the overlay reaches `idle`/`checking`; only an explicit Start action (UI or direct IPC) begins the real-audio path. The model server becomes healthy before `pw-record` opens the device. Demo follows its own synthetic path and does not launch capture or inference.

## Why one source per session

Microphone and desktop monitor streams can carry the same voice at different delays. Mixing them makes duplicate captions and echo likely. Version 0.1 therefore makes the active source an explicit exclusive choice. A later dual-source design would need independent timelines and stronger de-duplication rather than a hidden audio mix.

## Rolling window model

Each completed four-second window is sent to the already-loaded local model. Adjacent windows overlap by one second to reduce clipped words at boundaries. Text overlap is normalized and de-duplicated before a new segment is emitted.

Low-RMS silent windows skip inference to reduce idle hallucinations and CPU use. Pause stops the owned recorder, discards queued/window audio, advances a transition epoch, and suppresses an inference result from an older epoch. Resume starts with a fresh window rather than replaying pre-pause speech.

Practical latency is window completion plus inference, queueing, and local handoff time. It depends on the model, language, hardware, and audio. The overlay labels its window-plus-inference value as an estimate; real-hardware acceptance measures end-to-caption latency separately. It does not label the result “realtime” or promise sub-second output.

## Data lifetime

The helper does not create a meeting, transcript database, history, or export. It holds a bounded transcript tail and rolling PCM/WAV windows in memory, discarding each window after local inference. Its mode-`0700` XDG runtime directory contains only a mode-`0600` single-session lock—not caption text or audio. Preferences at `$XDG_CONFIG_HOME/omarchy/live-captions/config.json` store only model/source/language settings.

## Process ownership

Every capture run has helper-owned process handles. Quickshell stops the watcher it started; the watcher then signals those exact children and escalates only within their owned process groups after a bounded wait. It cannot target unrelated `pw-record` or `whisper-server` instances.

No predictable shared `/tmp` PID file is trusted, and model/config values are never interpolated into a shell command.

## Failure behavior

- Missing command/model/source: setup state; no capture starts.
- Capture exit: owned inference stops and the UI receives a sanitized error.
- Inference startup/health timeout: the owned server is stopped and audio capture never starts.
- Malformed inference response: last valid captions remain bounded; an error is emitted.
- Overlay close: Quickshell stops the watcher it owns, whose finalizer terminates capture and inference.
- Abrupt shell death: parent-death controls terminate owned children where supported; no persisted audio or transcript exists to recover.

## Explicit non-goals for 0.1

- Sub-second partial-word captioning.
- Simultaneous microphone and desktop capture.
- Cloud transcription or translation.
- Voice biometric identification.
- Automatic package/model installation.
- Editing Omarchy, Hyprland, or PipeWire configuration.
