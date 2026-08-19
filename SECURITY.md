# Security policy

## Supported versions

Security fixes are made on the latest released version. Until the first stable release, only the latest `0.x` release is supported.

## Report a vulnerability

Use GitHub private vulnerability reporting:

<https://github.com/josephbriones/omarchy-live-captions/security/advisories/new>

Do not put captured speech, usernames, machine paths, or exploit details in a public issue. Include the plugin version, Omarchy version, `whisper.cpp` version, impact, and a minimal sanitized reproduction.

## Security boundaries

Omarchy plugins run unsandboxed inside the user's long-lived Quickshell process. Marketplace validation is compatibility checking, not a security review.

Live Captions keeps a small operational surface:

- It executes its bundled standard-library Python helper, `pw-record`, and `whisper-server`.
- It has no `sudo`, package installation, download hook, remote API, analytics, or public listener.
- Local inference is hard-bound to `127.0.0.1` on an OS-assigned ephemeral port, behind a per-run 128-bit request-path token and an empty private public directory.
- The private XDG runtime directory is mode `0700`; its single-session lock is mode `0600`.
- Model and configuration paths are validated before use and passed as argument values, never evaluated as shell source.
- Structured events use standard output; diagnostics use standard error.
- Quickshell owns the watcher directly and sends pause/resume over its private stdin pipe. Stop closes that owned process; the helper then signals only live `Popen` children in the process groups it created. No PID file or process-name match is trusted.
- Demo mode does not access audio or inference.

Dependencies remain part of the trust boundary:

- Omarchy and Quattro host the QML.
- PipeWire and `pw-record` expose audio devices.
- `whisper.cpp`, `whisper-server`, and the selected local model process audio.

## Hardening expectations

- Keep Omarchy, PipeWire, and `whisper.cpp` updated.
- Review plugin updates before accepting them.
- Use microphone capture only when needed; use desktop capture only with content you are allowed to process.
- Store models in user-owned, non-world-writable paths.
- Do not run the helper as root.

Automated tests cover process ownership, bounded data, sanitization, and failure cleanup. They are not a security audit.
