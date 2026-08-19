# Privacy

Live Captions is designed for local processing, visible capture state, and no transcript retention.

## Data flow

Nothing captures audio merely because the overlay opened. The manifest keeps the lightweight QML owner resident after Close so it can finish cleanup, but that owner does not open audio. An explicit Start action first loads a local `whisper-server`; only after its health check passes does the helper launch `pw-record` for the selected source—microphone or desktop monitor. The UI reports Listening only after the first PCM bytes arrive. Rolling in-memory audio windows are sent to that local inference process. Normalized caption events travel over the helper's standard output to Quickshell.

The plugin has no analytics, account, telemetry, advertising, cloud API, or external network request. The inference server is hard-bound to `127.0.0.1`, and each run uses a random 128-bit request path plus an empty private web directory. Loopback communication does not leave the machine. The random path prevents accidental cross-talk; it is not authentication against another process running as the same user, which can inspect process arguments. This plugin, Quickshell, PipeWire clients, and other same-user desktop processes share the local-user trust boundary.

## Retention

- Audio is not recorded as a meeting or saved as a user document.
- Transcript text is not written to a history, database, or export file.
- The UI keeps only a bounded recent-caption list in memory.
- Short rolling PCM/WAV windows exist only in helper memory and are discarded after inference.
- Plugin preferences contain model path, source, and language settings, never captions.

Pause leaves the owned recorder running and its PipeWire stream linked. The helper drains and discards new PCM and emits no caption. The one window already being prepared or requested may finish locally after Pause; its result is suppressed, and no later window starts until Resume. Resume starts with fresh audio. **Stop** or **Close** terminates the owned capture and inference processes and is the privacy boundary to use when capture must end.

A mode-`0600` single-session lock may live below the private user runtime directory, never the persistent data directory. It contains no caption text or audio. Runtime cleanup is best effort after an abrupt power loss or process kill.

## Model discovery

`doctor` checks explicitly configured paths and known user-owned local model locations. It does not upload the path or model. The `LIVE_CAPTIONS_MODEL` environment variable is an explicit override. Live Captions does not download models.

## Demo mode

The `{"demo":true}` summon payload emits fixed sample text. It does not open an audio device, launch inference, or create audio data.

## Clipboard and screenshots

Captions are not copied automatically. If a future or explicit copy action is used, clipboard history and synchronization follow the user's clipboard configuration.

Screenshots can contain visible captions and content from other applications. The preview script uses demo text, but review the entire image before sharing it.

## Local access and trust

Omarchy plugins are unsandboxed code running as your user inside `omarchy-shell`. This plugin can start its bundled helper and user-installed audio/inference processes. Review the source before installing, as Omarchy recommends for every third-party plugin.

See [SECURITY.md](SECURITY.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical boundaries.
