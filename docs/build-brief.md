# The build prompt

Paste everything below the line into a coding agent running **on the Snapdragon
laptop**. It is self-contained — it assumes the agent knows nothing about this
project.

It carries the facts that were expensive to discover: the real GenieX API, the
dependency that has no Windows ARM64 wheel, the capture path that works on
these machines. An agent given this starts where we finished.

**Line 1 clones the existing repo as the foundation.** That repo already has the
verdict logic, the audit chain, the firmware and 267 tests. If you genuinely
want to start from nothing, delete that line — but you will be rebuilding the
safety argument rather than the interesting part.

---

## PROMPT

Build **ReadyKit Sentinel**: an air-gapped inspection station that watches a
safety-critical equipment kit continuously, decides entirely on-device, drives a
physical lock, and is operated by voice.

Start from `https://github.com/taranggoyal70/readykit-edge` — clone it and read
`README.md`, `CONTEXT.md` and `docs/deployment.md` before writing anything. The
verdict logic, hash-chained audit log, serial protocol and STM32 firmware are
already there and tested. You are adding four things: live capture, a
continuous watch loop, a voice interface, and a wall display.

### The machine you are on

- **Snapdragon X Elite, Windows ARM64.** Not Linux. Paths are `.venv\Scripts\`.
- Python must be the **ARM64** build — check `platform.machine()` returns
  `ARM64`. An x64 interpreter under emulation cannot load the GenieX runtime,
  and you will not find that out until much later.
- **Arduino UNO Q** on a `COMn` port. The STM32U585 core drives the latch.

### The rule that governs everything

> **Absence of evidence is not evidence of compliance.**

Three verdicts, never two:

| | Meaning | Latch |
|---|---|---|
| `PASS` | Every critical item positively found, in date, above a confidence floor | Released |
| `FAIL` | A positive finding of non-compliance | Engaged |
| `INDETERMINATE` | Compliance could not be established | Engaged |

`PASS` must never be the fallthrough branch. Occluded lens, crashed model,
dropped link, smudged date, unparseable reply, failed transcription — all
`INDETERMINATE`, all keep the lock shut. Write that test before the feature.

The obvious implementation gets this backwards: matching `"missing" in reply`
against model prose means `"Absent from the tray: Trauma Shears"` contains no
trigger word and reads as a pass, releasing a latch on a trauma kit with no
trauma shears in it. The repo keeps that original logic executable in
`naive.py` and replays it beside the real one — preserve that, it is the
strongest thing in the demo.

### 1. Live capture — do not reach for OpenCV

`opencv-python` has **no Windows ARM64 wheel**. Do not depend on it, and do not
solve it with x64 Python under emulation — that trades a working camera for a
broken NPU.

Use **ffmpeg with DirectShow**, which has Windows ARM64 builds and writes
straight to a file. GenieX takes image *file paths* anyway, so this is a better
fit than an in-memory array ever was.

```powershell
ffmpeg -list_devices true -f dshow -i dummy          # find the camera's name
ffmpeg -f dshow -i video="<NAME>" -frames:v 1 -q:v 2 -y frame.jpg
```

Wrap that in a `FrameSource` matching the existing interface in `capture.py`.
Keep one frame file and overwrite it; do not accumulate images of every kit the
machine has ever seen. numpy, Pillow, pyserial, fastapi and uvicorn all have
ARM64 wheels and are fine.

### 2. Inference — this is the real GenieX API

Do not invent it. This is correct:

```python
from geniex import AutoModelForCausalLM

model = AutoModelForCausalLM.from_pretrained(
    "ai-hub-models/Qwen3-VL-4B-Instruct",   # repo id, NOT a file path
    device_map="auto",
)                                            # -> GenieXVLM if multimodal
prompt = model.tokenizer.apply_chat_template(messages, add_generation_prompt=True)
reply  = model.generate(prompt, images=["C:/path/frame.jpg"], stream=False)
model.close()
```

Facts that follow, each of which bites if ignored:

- **Models are repo ids.** There is no `.qnn` to export or compile.
  `ai-hub-models/...` runs on QAIRT on the Hexagon NPU; a Hugging Face GGUF
  runs via llama.cpp.
- **Images are file paths only.** Not arrays, not PIL objects.
- **A repo id contains a slash**, so any "is this a file path?" check that tests
  for a path separator rejects every valid model id.
- **Verify you got a vision model.** GenieX returns `GenieXVLM` for multimodal,
  `GenieXLLM` for text-only. Refuse the latter at startup — a text model takes
  the prompt, ignores the image, and answers confidently about a frame it never
  saw.
- **`temperature` low, around 0.1.** A verdict that varies between runs on an
  unchanged kit is not a verdict.
- `pip install geniex` needs the **GenieX Windows installer run first**; PyPI
  ships a source archive, not a wheel.

Use **two models for two jobs**:

- `ai-hub-models/Qwen3-VL-4B-Instruct` — looks at the kit. Vision only.
- `google/gemma-4-E4B-it-qat-q4_0-gguf` — carries the spoken conversation.
  Small and fast, which is what a dialogue turn needs.

The second model **must never see the kit and must never influence a verdict**.
It explains a decision that has already been made. The thing that decides
whether a lock opens is not the thing talking to the operator.

### 3. The watch loop — this is the centrepiece

Today the repo inspects when told to. Change it to watch continuously, and to
**keep watching after it opens the latch**.

- Inspect on a rolling interval, aggregating several frames per decision.
- **While the latch is released, keep inspecting.** If the kit stops matching
  its manifest mid-hold — someone lifts an item out — send `REJECT`
  immediately: latch engages, buzzer sounds, screen flips. `REJECT` already
  closes an open latch and there is a test for it.
- The reverse must also hold: put the item back and it recovers cleanly on the
  next pass. No manual reset.
- Debounce against single-frame noise, or a hand passing over the tray will
  slam the lock. Require agreement across frames, which the existing
  `aggregate.py` already does — reuse it rather than writing new logic.

That behaviour is the demo: a cabinet that opens, and then **shuts itself while
your hand is still inside it** because you took the wrong thing out. It is also
a genuine safety property — a cabinet that stops paying attention after it
opens is a cabinet that gets emptied.

### 4. Voice — Speechmatics

Real-time speech-to-text for commands; spoken verdicts back.

- Use the **real-time** streaming API.
- Speechmatics ships **on-prem CPU/GPU containers**. If you can run one, the
  whole system stays air-gapped. If you use the cloud API instead, say so
  plainly in the README *and on screen* — do not claim "fully offline" while
  streaming audio to a third party.
- **A failed or low-confidence transcription is `INDETERMINATE`, never a
  command.** Mishearing must not actuate anything. Require a confirmed intent
  before any latch command.
- **Never accept an override by voice.** Log the attempt as a refusal.
- Commands worth supporting: start/stop watching, which manifest, "what's
  wrong", "why did it fail", "read the audit log".

### 5. The wall display

Glanceable from across a room, dark, high contrast:

- **The live camera feed**, large, with per-item state drawn over it.
- The verdict in one word, with the latch state beside it — and the latch shown
  **live, from actuator telemetry**, never frozen at the moment of the verdict.
  A hold expires while the banner is still up, and somebody reads that screen
  before putting a hand in a cabinet.
- Per-item checklist: found / absent / damaged / unreadable, confidence,
  quantity counted against required, expiry date where one applies.
- A live transcript of what the operator said, so they can see they were heard.
- Actuator telemetry: latch, indicator, buzzer, link freshness, last
  acknowledged sequence.
- **Colour is never the only signal.** Every state carries a glyph and a text
  label too.

Serve it on loopback only. This device opens a physical lock on command;
binding it to a routable interface turns a local view into a remote actuator.

### 6. Keep what is already there

Do not regress these — each exists for a reason and there are tests:

- The latch defaults to **engaged**. Power loss, reset and a stale link all end
  locked.
- The firmware never blocks. No `delay()` anywhere.
- Silence longer than two seconds is a fault: engage, do not hold the last
  instruction.
- Every command is checksummed and acknowledged. An unacknowledged command is
  recorded as **not having happened**.
- Records are hash-chained. A torn final line is truncation, not tampering.
- `naive.py` runs beside the real verdict on every frame and is recorded. It
  can never actuate.

### Definition of done

- Runs end to end on the simulator with no hardware attached.
- Runs end to end on the real camera, real NPU and real Arduino.
- Tests cover the fail-closed paths specifically — occluded, crashed model,
  unparseable reply, dropped link, failed transcription, expired item, short
  count, **and an item removed mid-hold**. Each keeps or returns the latch to
  engaged.
- `ruff` and `mypy --strict` clean.
- `readykit doctor` passes, including finding the NPU and naming the COM port.
- README states plainly anything not yet run on hardware.

Do not overstate anything. A limit written next to a claim costs nothing; being
caught overstating one costs credibility on all of them.
