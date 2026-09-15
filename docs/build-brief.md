# Build brief

A prompt to hand to a coding agent. Everything below the line is written to be
pasted as-is.

It carries the facts that cost real time to discover — the actual GenieX API,
the Windows ARM64 dependency that does not exist, the model id formats. An
agent given this brief starts where we finished rather than where we started.

---

## THE PROMPT

You are building **ReadyKit Voice** — an air-gapped inspection station that
checks safety-critical equipment kits and is operated by speaking to it.

### What it does

A camera watches an equipment kit (a medic's trauma kit, a crash cart, an
electrical toolbox). A vision-language model running locally on a Snapdragon
NPU decides whether the kit is complete, undamaged and in date. That decision
drives a physical solenoid latch on an Arduino: a compliant kit unlocks, a
non-compliant one stays locked.

The operator never touches a keyboard. They have both hands in the tray and
they are probably wearing gloves. They talk to it:

- *"Check trauma kit A."*
- It speaks back: *"Fail. The chest seal expired last month."*
- *"Why?"* → it explains, from the record, not from a fresh guess.
- *"Override and log it."* → refused, and the refusal is recorded.

### The rule that governs everything

> **Absence of evidence is not evidence of compliance.**

A verdict has three values, never two:

| | Meaning | Latch |
|---|---|---|
| `PASS` | Every critical item positively found, in date, above a confidence floor | Released |
| `FAIL` | A positive finding of non-compliance | Engaged |
| `INDETERMINATE` | Compliance could not be established | Engaged |

`PASS` must never be the fallthrough branch. An occluded lens, a crashed
model, a dropped serial link, a smudged date, an unparseable reply and a failed
transcription all resolve to `INDETERMINATE`, and `INDETERMINATE` never opens
anything. Write the test for that before the feature.

The obvious implementation gets this backwards. Matching `"missing" in reply`
against free model text means `"Absent from the tray: Trauma Shears"` contains
no trigger word and reads as a pass — releasing a latch on a trauma kit with no
trauma shears in it. Do not build that.

### Hardware and platform

- **Host**: Snapdragon X Elite, **Windows ARM64**. Not Linux. Paths are
  `.venv\Scripts\`, not `.venv/bin/`.
- **Actuator**: Arduino UNO Q — STM32U585 MCU core drives the latch, LEDs and
  buzzer. It appears as `COMn`.
- Python must be the **ARM64** build. An x64 interpreter under emulation cannot
  load the native GenieX runtime.

### Inference — use these exact APIs

GenieX is the runtime. Do **not** invent an API; this is the real one:

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

Facts that follow from that, and each one will bite if ignored:

- **Models are repo ids**, not `.qnn` files. There is nothing to export or
  compile. `ai-hub-models/...` runs on QAIRT on the Hexagon NPU; a Hugging Face
  GGUF runs via llama.cpp.
- **Images are file paths only.** Not arrays, not PIL objects. Write the frame
  to a temporary file, pass the path, delete it afterwards — and make the
  delete unable to throw, because Windows refuses to unlink a file another
  process still holds.
- **Check you got a vision model.** GenieX returns `GenieXVLM` for multimodal
  and `GenieXLLM` for text-only. Refuse the latter at startup: a text model
  accepts the prompt, ignores the image, and answers confidently about a frame
  it never saw. That is the worst failure available here.
- **Set `temperature` low (~0.1).** A compliance verdict that varies between
  runs on an unchanged kit is not a verdict.

Use **two models, for two jobs**:

- `ai-hub-models/Qwen3-VL-4B-Instruct` — looks at the kit. Vision only.
- `google/gemma-4-E4B-it-qat-q4_0-gguf` — handles the spoken conversation.
  Small and fast, which is what a dialogue turn needs. It must never see the
  kit and must never influence the verdict; it explains a decision that has
  already been made.

That separation is deliberate. The thing that decides whether a lock opens is
not the thing chatting with the operator.

### Voice — Speechmatics

Real-time speech-to-text for operator commands, and spoken verdicts back.

- Use the **real-time** API for streaming transcription.
- Speechmatics also ships **on-prem CPU/GPU containers**. If you can run one,
  the whole system stays air-gapped. If you use the cloud API instead, say so
  plainly in the README and in the UI — do not claim "fully offline" while
  streaming audio to a third party.
- **A failed transcription is `INDETERMINATE`, never a command.** Mishearing
  "check the kit" as something else must not actuate anything. Require a
  confirmed intent before any latch command.
- Never accept an override by voice. Log the attempt.

### Make it visual

The operator console is a wall display, glanceable from across a room:

- The verdict, huge, in one word, with the latch state beside it — and the
  latch state shown **live**, not frozen at the moment of the verdict. A hold
  expires while the banner is still on screen, and someone reads that screen
  before putting a hand in a cabinet.
- A per-item checklist: found / absent / damaged / unreadable, with confidence,
  quantity counted against required, and expiry date where one applies.
- A live waveform or transcript of what the operator is saying, so they can see
  they were heard.
- Actuator telemetry: latch, indicator, buzzer, link freshness.
- Colour is never the only signal. Every state also carries a glyph and a text
  label.

### Also build

- **A simulator for every hardware layer** — capture, inference and the serial
  link each behind an interface with a real and a fake implementation, so the
  whole pipeline including latch behaviour is testable with no hardware.
- **A tamper-evident audit log**: JSON Lines, each record hash-chained to the
  previous. Editing, deleting or reordering breaks the chain and names the
  record. Report a torn final line as truncation, not tampering — an
  air-gapped device loses power eventually.
- **Firmware that never blocks.** No `delay()`. The latch defaults to engaged;
  power loss, reset and a stale link all end locked. Treat silence longer than
  two seconds as a fault and engage, rather than holding the last instruction.
  Checksum and acknowledge every command; an unacknowledged command is
  recorded as not having happened.
- **A `doctor` command** that checks dependencies, finds the NPU, enumerates
  serial ports and flags which one is the board.

### Traps, from having hit them

- `opencv-python` has **no Windows ARM64 wheel**. Do not depend on it. numpy,
  Pillow, pyserial, fastapi and uvicorn all have ARM64 wheels or are pure
  Python. Use Pillow for image encoding.
- `pip install geniex` needs the **GenieX runtime installed first** via the
  Windows installer. PyPI ships it as a source archive, not a wheel.
- A repo id contains a slash. Any "is this a file path?" check that tests for a
  path separator will reject every valid model id.
- Quantity and expiry both fail closed: a count that was never taken and a date
  that could not be read are both *unresolved*, not *fine*.

### Definition of done

- The full pipeline runs on the simulator with no hardware attached.
- Tests cover the fail-closed paths specifically: occluded, crashed model,
  unparseable reply, dropped link, failed transcription, expired item, short
  count. Each must keep the latch engaged.
- `ruff` and `mypy --strict` clean.
- A README that states plainly what has not been run on hardware yet.

Do not overstate anything. A limit written next to a claim costs nothing; being
caught overstating one claim costs credibility on all of them.

---

## Before you paste this

Two decisions are yours, not the agent's:

**Rebuilding versus extending.** ReadyKit Edge already implements everything
above except the voice layer, with 267 tests and a working GenieX integration.
Starting fresh means rebuilding the safety argument, the blueprint comparison
and the audit chain from nothing. If what you actually want is voice, the
smaller and stronger move is to add it to what exists.

**Cloud voice versus the air gap.** The project's central claim is that nothing
leaves the device. Streaming audio to Speechmatics' cloud API contradicts that
unless you are explicit about it. The honest framing, if you go cloud: *the
inspection decision is fully local; the voice interface is a convenience layer
and is not.* The on-prem container removes the tension entirely if you can get
one running.
