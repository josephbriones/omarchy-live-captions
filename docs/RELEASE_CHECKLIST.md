# Release checklist

## Code and compatibility

- [ ] `manifest.json` version matches `CHANGELOG.md` and the intended tag.
- [ ] The plugin ID is unique and outside the reserved `omarchy.*` namespace.
- [ ] `bash scripts/validate.sh` passes with no unexplained skips on Omarchy.
- [ ] `omarchy plugin validate .` passes on current stable Omarchy.
- [ ] Supported `whisper-server` versions and at least one documented model pass real-audio acceptance.
- [ ] Demo, microphone, and desktop paths pass.
- [ ] Multi-display placement and click-through behavior pass.
- [ ] Rolling-window duplicate tests and the measured latency gate pass.

## Privacy and failure cases

- [ ] Opening without Start does not access audio or launch inference.
- [ ] Demo mode creates no runtime audio.
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
