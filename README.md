# ReadyKit Edge

> An air-gapped visual-inspection prototype that uses on-device vision-language inference to control a fail-secure physical latch.

**[Open the live project page →](https://readykit-edge-tau.vercel.app)**

> **Fork notice:** This repository is a fork of [taranggoyal70/readykit-edge](https://github.com/taranggoyal70/readykit-edge). Preserve that attribution and describe only your own contributions when presenting this work.

## Inspiration

Safety-critical kit checks should not treat uncertainty as compliance. ReadyKit Edge explores how a local vision-language model can inspect equipment while a fail-secure control system keeps the latch engaged whenever a complete, high-confidence pass cannot be established.

## What it does

- Uses a camera and an on-device vision-language model to assess whether critical kit items are present and serviceable.
- Returns three explicit verdicts—`PASS`, `FAIL`, or `INDETERMINATE`—with only a verified pass able to release the latch.
- Connects local inference on Snapdragon X Elite hardware to an Arduino UNO Q actuator without cloud inference or remote fallback.

## How we built it

The prototype combines Python orchestration, local Ollama/Genie X vision-language inference, manifest-driven checks, structured verdict parsing, a local console, serial hardware control, and an Arduino-based latch/indicator system.

## Challenges we ran into

- Free-form model text is unsafe as a control signal; a missing or ambiguous response must not become an unlock decision.
- Hardware, serial links, image capture, and model inference can all fail independently, so the system needs a fail-closed state at every boundary.
- Demonstrating the design fairly required replaying the original substring-matching approach against the same model responses rather than comparing unrelated inputs.

## Accomplishments we're proud of

- Made `INDETERMINATE` a first-class safe state so occlusion, low confidence, malformed output, and failures keep the latch engaged.
- Added testable verdict parsing and a replay comparison that exposes unsafe divergences from the original naive matching approach.

## What we learned

When AI participates in a physical control loop, the important question is not whether a response sounds plausible—it is whether uncertainty, failures, and unsafe interpretations are handled predictably.

## What's next

Potential next steps include expanded manifest coverage, calibration workflows for real kit imagery, and additional hardware-in-the-loop validation.

## Built with

`Python` · `Ollama` · `Genie X` · `Vision-Language Models` · `Snapdragon X Elite` · `Arduino UNO Q` · `Serial I/O`

## Run locally

Follow the repository's hardware and model setup instructions before running an inspection. The local console is designed for the attached camera and actuator environment; see `docs/deployment.md` for bring-up details.
