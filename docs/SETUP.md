# Local engine setup

Live Captions never installs software or downloads a model. These are explicit, user-run setup steps for Omarchy.

## 1. Install the local engine

Install the official Arch packages through Omarchy's package helper:

```bash
omarchy-pkg-add whisper-cpp
omarchy-pkg-add util-linux
```

Live Captions v0.2 was tested against the current Arch `whisper-cpp` 1.9.1 package and requires `setpriv` from `util-linux`; `setpriv` and every probed server capability are mandatory. Version 1.9.1 is a baseline, not a semantic minimum: older or newer `whisper-server` builds may work when they expose the exact fixed-argument options checked by `doctor`. Check the installed package version with `pacman`, confirm `setpriv`, and verify those options. Do not use `whisper-server --version`: compatible servers can treat it as an unknown argument while still exiting successfully.

```bash
pacman -Q whisper-cpp
setpriv --version
WHISPER_HELP="$(whisper-server --help 2>&1)"
for flag in --model --host --port --request-path --public; do
  printf '%s\n' "$WHISPER_HELP" | rg -F -- "$flag"
done
pw-record --version
```

## 2. Select a local GGML model

### Reuse a compatible model

If Omarchy's Dictation option installed VoxType, a compatible model may already be present:

```bash
find ~/.local/share/voxtype/models -maxdepth 1 -type f -name 'ggml-*.bin' -print
```

Use an absolute path from that output in the configure command below.

### Acquire and verify the official tiny English model

This smaller model is suitable for a first functional test. Download to a temporary name, verify the pinned digest, and only then move it into place:

```bash
install -d -m 700 ~/.local/share/omarchy-live-captions/models
curl --fail --location --proto '=https' --tlsv1.2 \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin \
  --output ~/.local/share/omarchy-live-captions/models/ggml-tiny.en.bin.part
printf '%s  %s\n' \
  921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f \
  ~/.local/share/omarchy-live-captions/models/ggml-tiny.en.bin.part \
  | sha256sum --check --strict
mv ~/.local/share/omarchy-live-captions/models/ggml-tiny.en.bin.part \
  ~/.local/share/omarchy-live-captions/models/ggml-tiny.en.bin
```

The URL is the model repository used by the upstream `whisper.cpp` project. If verification fails, do not use or rename the partial file.

`ggml-tiny.en.bin` is English-only, so configure it with `--language en`. For `auto` or any other language, choose a multilingual GGML model whose filename does not contain `.en.bin`. Discovery applies the same rule: it skips incompatible English-only models instead of silently using one for another language.

## 3. Preview and apply plugin preferences

Replace the model path if you selected a different model:

```bash
HELPER=~/.config/omarchy/plugins/io.github.josephbriones.live-captions/bin/live-captions
MODEL=~/.local/share/omarchy-live-captions/models/ggml-tiny.en.bin

"$HELPER" configure --model "$MODEL" --source microphone --language en
"$HELPER" configure --model "$MODEL" --source microphone --language en --apply
```

The first command is preview-only. `--apply` explicitly writes `$XDG_CONFIG_HOME/omarchy/live-captions/config.json` with mode `0600`. It does not edit PipeWire, Omarchy, Hyprland, or VoxType.

Supported language values are Whisper language codes such as `en` or `es`, BCP-47-style tags such as `pt-BR`, and `auto`. Version 0.2 defaults to English. Tags are validated against Whisper's supported languages and reduced to their primary code (`pt-BR` becomes `pt`). English-only `.en.bin` models require `en`; `auto` and non-English choices require a multilingual model.

For a temporary model override, set `LIVE_CAPTIONS_MODEL` to an absolute path in the environment that launches `omarchy-shell`. `LIVE_CAPTIONS_LANGUAGE` similarly overrides the configured language.

## 4. Diagnose without capturing audio

```bash
"$HELPER" doctor
```

Doctor checks the platform, mandatory `setpriv`, fixed local executables, supported `whisper-server` options, preferences, and a readable language-compatible model. It does not start `pw-record`, open an audio source, or launch the inference server. Source-routing failures can still surface after an explicit Start action because PipeWire device availability changes at runtime. After Start, the UI waits for the first PCM bytes before reporting Listening and fails the session if no bytes arrive within five seconds.
