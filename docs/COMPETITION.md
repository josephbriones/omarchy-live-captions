# Competition pitch

## The idea

If Omarchy can hear it, you should be able to read it.

Live Captions puts local microphone or desktop-audio captions across every display. The cards stay out of the way, the controls stay visible, and neither speech nor transcript is saved.

## Why it lasts

Captions should belong to the desktop, not to whichever application remembered to add them. A compositor-level layer works across calls, browsers, media players, classrooms, presentations, and apps nobody has built an accessibility integration for yet.

The local engine can improve without replacing the shell experience. Lower-latency recognizers, translation, accelerator discovery, and per-application audio targeting can all arrive behind the same small boundary.

## What makes it Omarchy

- Click-through caption cards appear on every display.
- One focused-display control card owns Start, Pause, Stop, source, size, and placement.
- The overlay uses Quattro's theme, lifecycle, `Process`, and IPC conventions.
- One persistent local model avoids a reload for every audio window.
- A one-second overlap protects boundary words; deterministic de-duplication keeps them from appearing twice.
- A no-audio demo gives judges and maintainers a truthful way to inspect the whole interface.

## Three-minute judging flow

1. Summon demo mode on two displays.
2. Click and type in an application beneath the captions to show the empty input region.
3. Move the captions, resize the text, and change the number of visible lines.
4. Start a short rights-cleared microphone sample and show the visible capture state.
5. Stop, switch to desktop audio, and caption a local clip.
6. Show that no audio file, transcript, history, or background service remains.

## The deliberate boundary

Version 0.1 produces stable captions after a four-second window plus local inference. It is not sub-second broadcast captioning. It handles one source at a time, does not identify speakers, and does not install software or download a model.

Those are product choices, not footnotes. They make capture obvious, keep echo out of the first release, and leave the user's machine under the user's control.
