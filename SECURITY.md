# Security policy

## Supported versions

Security fixes are made on the latest released version. Until the first stable release, only the latest `0.x` release is supported.

## Report a vulnerability

Use GitHub private vulnerability reporting:

<https://github.com/josephbriones/omarchy-live-captions/security/advisories/new>

Do not put captured speech, usernames, machine paths, or exploit details in a public issue. Include the plugin version, Omarchy version, `whisper-cpp` package version, sanitized `doctor` capability result, impact, and a minimal sanitized reproduction.

## Security boundaries

Omarchy plugins run unsandboxed inside the user's long-lived Quickshell process. Marketplace validation is compatibility checking, not a security review.

Live Captions keeps a small operational surface:

- It executes its bundled standard-library Python helper, `setpriv`, `pw-record`, and `whisper-server`.
- It has no `sudo`, package installation, download hook, remote API, analytics, or public listener.
- Local inference is hard-bound to `127.0.0.1` on an OS-assigned ephemeral port, behind a per-run random 128-bit request path and an empty private public directory. The random path prevents accidental cross-talk; it is not authentication against same-user processes that can inspect the process table.
- Private runtime and configuration leaf directories are mode `0700`; the session lock and configuration file are mode `0600`.
- Configuration directory components are opened relative to verified directory descriptors without following symlinks. Reads accept only a bounded, single-link, user-owned regular file; writes replace it atomically through a same-directory private temporary file and durable directory sync.
- Model and configuration values are validated before use and passed as argument values, never evaluated as shell source.
- Structured events use standard output; diagnostics use standard error.
- Quickshell keeps the lightweight QML owner resident, but audio starts only after an explicit Start action. It owns the watcher directly and sends pause/resume over its private stdin pipe. While paused, `pw-record` keeps running and linked; the helper drains and discards new PCM and suppresses caption output. The one window already being prepared or requested may finish locally, but no later window starts until Resume. Stop or Close ends capture; the helper then signals only live `Popen` children in the process groups it created. No PID file or process-name match is trusted.
- The shell launches the real watcher with `setpriv --pdeathsig TERM`, matching Omarchy's native long-lived-helper contract and allowing bounded cleanup after an abrupt shell exit. Each recorder and inference process also uses mandatory `setpriv --pdeathsig KILL` as the hard fallback if its watcher disappears first.
- Demo mode does not access audio or inference.

Dependencies remain part of the trust boundary:

- Omarchy and Quattro host the QML.
- PipeWire and `pw-record` expose audio devices.
- `util-linux` supplies mandatory `setpriv` process hardening.
- The tested Arch baseline is `whisper-cpp` 1.9.1. Compatibility is determined by `doctor` probing the exact required `whisper-server` flags, not by a semantic minimum; the selected local model processes audio.
- English-only `.en.bin` models are accepted only for English; `auto` and other languages require a multilingual model.

These are local same-user boundaries, not isolation from other software in the desktop session. Omarchy plugins and other same-user processes may already have the ability to inspect processes or connect to PipeWire.

## Hardening expectations

- Keep Omarchy, PipeWire, `util-linux`, and `whisper-cpp` updated.
- Review plugin updates before accepting them.
- Use microphone capture only when needed; use desktop capture only with content you are allowed to process.
- Store models in user-owned, non-world-writable paths.
- Do not run the helper as root.

Automated tests cover process ownership, bounded data, configuration symlink/FIFO/size/ownership rejection, sanitization, stale-backlog failure, and cleanup, including a deterministic fake-command subprocess pipeline. They do not exercise real Omarchy hardware and are not a security audit.
