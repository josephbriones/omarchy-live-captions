# Marketplace submission draft

Do not open this issue until the repository is public, the preview is reviewed, all release checks pass, and the owner explicitly confirms every checklist statement.

**Title**

```text
[Plugin]: Live Captions
```

**Body**

```markdown
### Repository URL

https://github.com/josephbriones/omarchy-live-captions

### Category

Desktop

### Tags

ai, media, quickshell

### Suggest a missing tag

accessibility

### Maintainer notes

Local `pw-record`, `whisper-server`, and a compatible `whisper.cpp` model are required. An explicit Start action captions either microphone or desktop audio through rolling four-second windows with one-second overlap; the UI labels window plus inference as an estimate and makes no sub-second claim. The plugin sends no external network request and writes no speech, audio, or transcript. It never installs packages, downloads a model, requests administrator access, or rewrites another application's configuration; model, source, and language preferences change only through an explicit preview-and-apply command.

### Submission checklist

- [ ] The repository is public and contains installation and removal instructions.
- [ ] I have documented the plugin license and any external dependencies.
- [ ] I confirm that I own or have permission to submit this plugin and its preview assets.
- [ ] The plugin does not overwrite user configuration without explicit consent.
- [ ] I understand that approval is for listing and is not a security review.
```

After the owner personally confirms these statements, change all five boxes to `[x]`, show the final title and body to the owner, obtain explicit approval, and create the issue in `HANCORE-linux/omarchy-plugin-marketplace`.

The category and tags use the marketplace's controlled vocabulary. `accessibility` is intentionally proposed as one missing reusable tag.
