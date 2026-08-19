## Summary

Describe the user-visible change and why it belongs in Live Captions.

## Validation

- [ ] `bash scripts/validate.sh`
- [ ] Demo acceptance on Omarchy, if QML or shell behavior changed
- [ ] Explicit real-audio acceptance, if capture or process cleanup changed
- [ ] Privacy and security documentation reviewed, if data flow changed

## Privacy check

- [ ] No real caption text, audio, username, machine path, or secret is included.
- [ ] Stopping the QML-owned watcher cannot terminate unrelated capture or inference processes.
