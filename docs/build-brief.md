# The build prompt

Paste everything below the line into a coding agent running **on the Snapdragon
laptop**. It is self-contained.

**Read this part first, it is not in the prompt:**

The stages are ordered so that **you have a working demo at every checkpoint**.
That is deliberate. The way a good hackathon project loses is not to a better
idea — it is to running out of clock with a half-built thing and nothing to
show. Build them in order. Do not start stage 4 until stage 3 demos cleanly.

| Stage | You can demo | Stop here and you have |
|---|---|---|
| 0 | Simulation, no hardware | A working system and the comparison table |
| 1 | Real NPU reading a real kit | On-device AI, with latency numbers |
| 2 | The latch physically moves | Model-to-device, the track's actual brief |
| 3 | **It shuts while your hand is inside** | The moment people remember |
| 4 | Hands-free voice | The bonus track |
| 5 | Wall display | Polish |

Line 1 clones the existing repo, which already has the verdict logic, audit
chain, firmware and 267 tests. Delete it only if you want to rebuild the safety
argument instead of the interesting part.

---

## PROMPT

Build **ReadyKit Sentinel** — an air-gapped inspection station that watches a
safety-critical equipment kit continuously, decides entirely on-device, drives a
physical lock, and is operated by voice.

Start by cloning `https://github.com/taranggoyal70/readykit-edge` and reading
`README.md`, `CONTEXT.md`, `docs/deployment.md` and `docs/windows-setup.md`.
The verdict logic, hash-chained audit log, serial protocol, operator console
and STM32 firmware already exist and are tested. Reuse them. You are adding
live capture, a continuous watch loop, voice, and a wall display.

Work in the stages below **in order**. Each one must run and be demonstrable
before you begin the next. Do not build ahead.

---

## The rule that governs every stage

> **Absence of evidence is not evidence of compliance.**

A verdict has three values, never two:

| | Meaning | Latch |
|---|---|---|
| `PASS` | Every critical item positively found, in date, above a confidence floor | Released |
| `FAIL` | A positive finding of non-compliance | Engaged |
| `INDETERMINATE` | Compliance could not be established | Engaged |

`PASS` is never the fallthrough branch. An occluded lens, a crashed model, a
dropped serial link, a smudged date, an unparseable reply, a failed
transcription — every one resolves to `INDETERMINATE`, and `INDETERMINATE`
never opens anything. **Write the failing test before the feature, every
time.**

The obvious implementation gets this backwards. Matching `"missing" in reply`
against model prose means `"Absent from the tray: Trauma Shears"` contains no
trigger word and reads as a pass — releasing a latch on a trauma kit with no
trauma shears in it. The repo keeps that original logic executable in
`naive.py` and replays it beside the real verdict on every frame. **Preserve
that.** It is the strongest thing in the demonstration, and it must never be
able to actuate.

---

## The machine

- **Snapdragon X Elite, Windows ARM64.** Not Linux. Paths are `.venv\Scripts\`.
- Python must be the **ARM64** build. Check `platform.machine()` returns
  `ARM64`; an x64 interpreter under emulation cannot load the GenieX runtime,
  and you will not discover that until two stages later.
- **Arduino UNO Q** on a `COMn` port. The STM32U585 core drives the latch, the
  LEDs and the buzzer.
- `readykit doctor` reports dependencies, finds the NPU and names the COM port.
  Run it whenever something is confusing; it is faster than reasoning about
  what broke.

---

## Stage 0 — Prove the machine, in simulation

No camera, no board, no model.

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev,console]"
.venv\Scripts\python -m pytest
.venv\Scripts\readykit inspect --manifest manifests\trauma-kit-a.json --scene complete --no-log
.venv\Scripts\readykit compare --manifest manifests\trauma-kit-a.json
```

**Done when:** tests pass, an inspection prints `PASS`, and `compare` prints the
table showing how many scenarios the original design would have opened.

**Demo at this stage:** the comparison table. It is the intellectual core and
it needs no hardware at all.

---

## Stage 1 — Real inference on the NPU

### Capture — do not reach for OpenCV

`opencv-python` has **no Windows ARM64 wheel**. Do not depend on it. Do not
solve it with x64 Python under emulation — that trades a working camera for a
broken NPU.

Use **ffmpeg with DirectShow**. It has Windows ARM64 builds, and it writes
straight to a file, which is what GenieX wants anyway:

```powershell
ffmpeg -list_devices true -f dshow -i dummy          # find the camera name
ffmpeg -f dshow -i video="<NAME>" -frames:v 1 -q:v 2 -y frame.jpg
```

Wrap that as a `FrameSource` matching the interface in `capture.py`. Overwrite
one frame file rather than accumulating images of every kit the machine has
ever seen. numpy, Pillow, pyserial, fastapi and uvicorn all have ARM64 wheels.

### Inference — this is the real GenieX API, do not invent one

```python
from geniex import AutoModelForCausalLM

model = AutoModelForCausalLM.from_pretrained(
    "ai-hub-models/Qwen3-VL-4B-Instruct",   # repo id, NOT a file path
    device_map="auto",                       # or "<runtime>:<compute_unit>"
)                                            # -> GenieXVLM if multimodal
prompt = model.tokenizer.apply_chat_template(messages, add_generation_prompt=True)
reply  = model.generate(prompt, images=["C:/path/frame.jpg"], stream=False)
model.close()
```

Each of these will bite if ignored:

- **Models are repo ids.** There is no `.qnn` to export or compile.
  `ai-hub-models/...` runs on QAIRT on the Hexagon NPU; a Hugging Face GGUF
  runs via llama.cpp.
- **Images are file paths only.** Not arrays, not PIL objects.
- **A repo id contains a slash**, so any "is this a file path?" guard that
  tests for a path separator rejects every valid model id.
- **Verify you got a vision model.** GenieX returns `GenieXVLM` for multimodal
  and `GenieXLLM` for text-only. Refuse the latter at startup — a text model
  accepts the prompt, ignores the image, and answers confidently about a frame
  it never saw. That is the worst failure available here.
- **`temperature` around 0.1.** A compliance verdict that varies between runs
  on an unchanged kit is not a verdict.
- `pip install geniex` requires the **GenieX Windows installer to have run
  first**. PyPI ships a source archive, not a wheel.

### Two models, two jobs

- `ai-hub-models/Qwen3-VL-4B-Instruct` — looks at the kit. Vision only.
- `google/gemma-4-E4B-it-qat-q4_0-gguf` — carries the spoken conversation.

The second **must never see the kit and must never influence a verdict.** It
explains a decision already made. The thing that decides whether a lock opens
is not the thing talking to the operator. Say this in the README; it is a
design decision, not an implementation detail.

### Prove you are actually on the NPU

Device Manager showing an NPU proves a driver loaded, not that your inference
went there. Benchmark it pinned to the NPU against CPU. If the numbers match,
you are not on the NPU. Record the engine and device on every inspection so the
claim is checkable rather than asserted.

**Done when:** a real photo of a real kit produces a real verdict, and you have
a latency number with the engine string beside it.

**Demo at this stage:** on-device AI reading an actual kit, with the speed to
prove where it ran.

---

## Stage 2 — The latch physically moves

Flash `firmware\mcu_actuator\mcu_actuator.ino` to the **STM32U585 core**. Work
through the bring-up checklist in `docs/deployment.md`.

Do not weaken any of this — each has a test:

- The latch defaults to **engaged**. Power loss, reset and a stale link all end
  locked. Use a **fail-secure** latch: de-energised means locked.
- The firmware never blocks. No `delay()` anywhere.
- Silence longer than two seconds is a fault: engage, do not hold the last
  instruction.
- Every command is checksummed and acknowledged. An unacknowledged command is
  recorded as **not having happened**, never assumed.
- A repeated sequence number is refused, so a retried command cannot actuate
  twice.

**Done when:** a complete kit opens the latch and an incomplete one does not,
with the model and the camera in the loop.

**Demo at this stage:** model-to-device, which is the track's actual brief.
Let people hear the solenoid. That sound is the demonstration.

---

## Stage 3 — The watch loop. This is the centrepiece.

Today the system inspects when asked. Change it to watch continuously, and —
this is the part that matters — **to keep watching after it opens the latch.**

- Inspect on a rolling interval, aggregating several frames per decision.
- **While the latch is released, keep inspecting.** If the kit stops matching
  its manifest mid-hold, because someone lifted an item out, send `REJECT`
  immediately: the latch engages, the buzzer sounds, the screen flips.
  `REJECT` already closes an open latch and there is a test for it.
- The reverse must hold too: put the item back and it recovers on the next
  pass. No manual reset.
- **Debounce.** A hand passing over the tray must not slam the lock. Require
  agreement across frames — `aggregate.py` already does this, reuse it rather
  than writing new logic. Tune the window so a deliberate removal triggers in
  about a second and a passing hand never does.

That behaviour is the demonstration: a cabinet that opens, and then **shuts
itself while your hand is still inside it** because you took the wrong thing
out. It is also a real safety property — a cabinet that stops paying attention
once it opens is a cabinet that gets emptied.

**Done when:** you can open it with a good kit, remove one item, and watch it
close on you. And put the item back and have it reopen.

**Demo at this stage:** the moment people remember. Everything else is support.

---

## Stage 4 — Voice, with Speechmatics

Real-time speech-to-text for commands, spoken verdicts back.

- Use the **real-time streaming** API.
- Speechmatics ships **on-prem CPU/GPU containers**. If you can run one, the
  system stays fully air-gapped. If you use the cloud API, say so plainly in
  the README **and on screen** — do not claim "fully offline" while streaming
  audio to a third party. A judge who spots that themselves costs you far more
  than volunteering it.
- **A failed or low-confidence transcription is `INDETERMINATE`, never a
  command.** Mishearing must not actuate anything. Require a confirmed intent
  before any latch command.
- **Never accept an override by voice.** Refuse it and log the attempt as a
  refusal.
- Worth supporting: start/stop watching, choose a manifest, "what's wrong",
  "why did it fail", "read the last five inspections".

Voice is the correct interface here, not decoration: the operator has both
hands in the tray and is probably wearing gloves.

**Done when:** you can run an entire inspection without touching the keyboard.

---

## Stage 5 — The wall display

Glanceable from across a room. Dark, high contrast.

- **The live camera feed**, large, with per-item state drawn over it.
- The verdict in one word, with the latch beside it — and the latch shown
  **live from actuator telemetry**, never frozen at the moment of the verdict.
  A hold expires while the banner is still on screen, and somebody reads that
  screen before putting a hand in a cabinet.
- Per-item checklist: found / absent / damaged / unreadable, with confidence,
  quantity counted against required, and expiry date where one applies.
- A live transcript of what the operator said, so they can see they were heard.
- Actuator telemetry: latch, indicator, buzzer, link freshness, last
  acknowledged sequence.
- **Colour is never the only signal.** Every state carries a glyph and a text
  label as well, so it reads for a colour-blind judge and on a bad projector.

Serve on loopback only. This device opens a physical lock on command; binding
it to a routable interface turns a local view into a remote actuator.

---

## Things that will waste your time if you do not know them

- `opencv-python`: no Windows ARM64 wheel. Use ffmpeg.
- `pip install geniex`: needs the Windows installer run first.
- A repo id contains a slash. Path-separator guards reject valid model ids.
- Windows refuses to unlink a file another process still holds, so temporary
  frame cleanup must not be able to throw — a latch decision lost to
  housekeeping is a bad trade.
- Write the audit log with `newline=""` so Windows does not rewrite line
  endings and change the bytes an auditor would diff.
- Quantity and expiry both fail closed: a count never taken and a date that
  could not be read are *unresolved*, not *fine*.

---

## Definition of done

- Runs end to end in simulation with no hardware.
- Runs end to end on real camera, real NPU, real Arduino.
- Tests cover the fail-closed paths **specifically**: occluded, crashed model,
  unparseable reply, dropped link, failed transcription, expired item, short
  count, and **an item removed mid-hold**. Each keeps or returns the latch to
  engaged.
- `ruff` and `mypy --strict` clean.
- `readykit doctor` passes, including finding the NPU and naming the COM port.
- The README states plainly anything not yet run on hardware.

Do not overstate anything, anywhere. A limit written next to a claim costs
nothing. Being caught overstating one claim costs credibility on all of them.
