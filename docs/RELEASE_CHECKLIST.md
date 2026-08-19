# Release checklist

Every box is an evidence gate, not a statement that the current checkout passed. Keep a box unchecked until its named environment and artifact have been reviewed; in particular, a skipped `qmllint` or Omarchy-only check is not a pass. The deterministic subprocess suite does not substitute for real-hardware acceptance.

## Code and compatibility

- [ ] `manifest.json` version matches `CHANGELOG.md` and the intended tag.
- [ ] The plugin ID is unique and outside the reserved `omarchy.*` namespace.
- [ ] `bash scripts/validate.sh` passes with no unexplained skips on Omarchy.
- [ ] `qmllint` completes with the current Omarchy shell imports; it was not merely skipped.
- [ ] `omarchy plugin validate .` passes on current stable Omarchy.
- [ ] Mandatory `setpriv` is present, and `doctor` passes every required `whisper-server` flag on the release machine (the tested Arch baseline is 1.9.1, not a semantic minimum).
- [ ] A language-compatible documented model passes real-audio acceptance; an English-only model is rejected for `auto` and non-English.
- [ ] The deterministic fake-command subprocess lifecycle test passes.
- [ ] Demo, microphone, and desktop paths pass.
- [ ] Multi-display placement and click-through behavior pass.
- [ ] Demo Close is exercised while its watcher is running and settles at `open=false`/`running=false`; a second demo finishes idle with the requested source and a nonzero segment count.
- [ ] Rolling-window word/CJK duplicate tests and the separately observed real-hardware latency gate pass.

## Privacy and failure cases

- [ ] Opening without Start does not access audio or launch inference.
- [ ] A resident `keepLoaded` QML owner has no recorder, model server, or audio stream before Start or after Close.
- [ ] Demo mode creates no runtime audio.
- [ ] Listening is not shown until the first PCM bytes arrive; no-audio startup fails and cleans up.
- [ ] Pause drains/discards new PCM and suppresses the current prepared/requested window; no later window starts until Resume while PipeWire remains linked; Stop and Close end capture.
- [ ] Stale audio backlog stops the session with actionable guidance instead of emitting delayed captions.
- [ ] Normal stop leaves no WAV or transcript history.
- [ ] Capture failure terminates the owned inference process.
- [ ] Inference failure terminates the owned capture process.
- [ ] Closing and killing the overlay do not leave an owned capture silently running.
- [ ] An unrelated `pw-record` or `whisper-server` is never terminated.
- [ ] README, Privacy, and Security descriptions match observed behavior.

## Distribution

- [ ] Repository is public and contains exactly one root plugin.
- [ ] Root README contains installation, dependency, and removal instructions.
- [ ] Root license is correct.
- [ ] Root preview uses demo data, contains no private information, and is below marketplace limits.
- [ ] GitHub Actions pass at the release commit.
- [ ] Tag and GitHub release are created from the validated commit.
- [ ] A clean Omarchy machine can install from the public repository URL.

## Marketplace

- [ ] Owner confirms rights to the code and preview.
- [ ] Owner confirms every marketplace checklist statement.
- [ ] The draft in `docs/MARKETPLACE_SUBMISSION.md` is updated and shown to the owner.
- [ ] Submission issue is created only after explicit owner approval.
- [ ] Automated compatibility and security-baseline comments are reviewed.
- [ ] Requested fixes use the same repository and submission issue.

Marketplace validation and maintainer approval are not a security review.
