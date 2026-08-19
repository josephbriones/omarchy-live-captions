# Changelog

All notable changes are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-08-19

### Added

- Resident `keepLoaded` QML ownership so Close can wait for bounded child cleanup without enabling background capture.
- Mandatory `setpriv --pdeathsig KILL` launch hardening for owned capture and inference children.
- Language validation and language-aware model discovery; English-only `.en.bin` models are rejected for `auto` and non-English sessions.
- First-PCM startup readiness, inference-process supervision, and stale-audio backlog failure instead of delayed captions.
- Deterministic subprocess integration coverage using fake `setpriv`, `pw-record`, and `whisper-server` executables.

### Changed

- Pause now creates a strict caption boundary while `pw-record` remains running and linked: new PCM is drained and discarded, the current prepared/requested window may finish locally with its result suppressed, and no later window starts until Resume; Stop and Close terminate capture.
- Rolling-window de-duplication now falls back to exact-character overlap for unspaced CJK text.
- Unicode joiners and combining marks are preserved so multilingual captions and overlap comparisons do not collapse distinct words.
- Demo mode preserves its selected source and finishes in idle with its generated captions available for acceptance checks.
- Protocol parsing, process-start failure handling, source selection, shutdown cleanup, and user-facing diagnostics are stricter.
- The UI describes window-plus-inference latency as an estimate rather than an observed end-to-caption measurement.

### Security

- Clarified that the random loopback request path prevents accidental cross-talk but is not authentication against same-user processes.

## [0.1.0] - 2026-08-19

### Added

- Multi-display, click-through caption overlay for Omarchy Quattro.
- User-selected microphone or desktop-audio capture through PipeWire.
- Local rolling-window transcription through a persistent `whisper-server` process.
- Visible capture, source, position, and text-size controls.
- Bounded in-memory caption history with no audio or transcript retention.
- No-audio demo mode, diagnostics, portable tests, and Omarchy acceptance scripts.
- Privacy, security, architecture, testing, release, and marketplace documentation.

[Unreleased]: https://github.com/josephbriones/omarchy-live-captions/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/josephbriones/omarchy-live-captions/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/josephbriones/omarchy-live-captions/releases/tag/v0.1.0
