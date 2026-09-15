# Deployment

Taking ReadyKit Edge from a laptop simulation to a Snapdragon X Elite host
driving a real solenoid on an Arduino UNO Q.

Everything in this document has been designed and unit-tested but **not yet
run on the hardware**. The [bring-up checklist](#bring-up-checklist) is written
to be worked through in one sitting the first time the boards are on the bench
— every item on it is a behaviour the simulator already pins, so it confirms
the hardware agrees rather than discovering the behaviour from scratch.

---

## 1. Get a model onto the device

GenieX loads models **by repo id**, not from a file you export yourself. There
is no `.qnn` to produce, no ONNX step and no converter to fight — which is
worth knowing, because the obvious mental model (compile a graph, ship the
binary) is not how this runtime works.

```bash
pip install geniex

# Pre-compiled for the Hexagon NPU via Qualcomm AI Engine Direct
geniex pull ai-hub-models/Qwen2.5-VL-7B-Instruct
```

That id is what `--model` takes:

```bash
readykit inspect --engine geniex \
  --model ai-hub-models/Qwen2.5-VL-7B-Instruct \
  --device auto --camera 0 --manifest manifests/trauma-kit-a.json
```

### It has to be a vision-language model

ReadyKit looks at the kit; it does not read about it. GenieX returns a
`GenieXVLM` for a multimodal model and a `GenieXLLM` for a text-only one, and
`GenieXEngine` refuses the latter at construction. A text-only model would
accept the prompt, silently ignore the frame, and answer confidently about an
image it never saw — the worst failure available, so it is made impossible
rather than documented.

### Two runtimes, and which one you are on

| Source | Runtime | Hardware |
|---|---|---|
| `ai-hub-models/...` | Qualcomm AI Engine Direct | Hexagon NPU only |
| Hugging Face GGUF, e.g. `unsloth/...-GGUF` | llama.cpp | CPU / GPU / NPU |

`--device` maps to GenieX's `device_map`: `auto` takes the first available
runtime, and `<runtime>:<compute_unit>` pins one. Whatever you pass is written
into the `engine` field of every Inspection Record as
`geniex:<model>@<device>`, so "it ran on the NPU" is a claim an auditor can
check rather than take on trust. Use `readykit bench` to get the latency
number, and quote the engine string beside it.

### Determinism

`GenieXEngine` defaults to `temperature=0.1`. This model decides whether a
latch opens, and a compliance verdict that varies between runs on an unchanged
kit is not a verdict. There is nothing to be gained from sampling entropy here.

### Nothing above changes any code

`--model` is a swappable part. A detection model works too, provided you wrap
it in something that emits the same `Sighting` list. The safety rule sits
downstream in `resolve_verdict`, where no model can reach it.

### What the model is asked

`readykit.reply.build_prompt` generates the prompt. It names every Required
Item by key and label and demands JSON back. The important part is that it
offers `unreadable` as a sanctioned answer:

> Do not guess. If you are unsure, answer 'unreadable'. Reporting 'unreadable'
> is always correct when you cannot see the item clearly.

Without that, a model instructed to fill every field will guess, and roughly
half of those guesses are `found`. If you swap the model, keep this property —
the safety argument depends on the model having a way to say "I don't know"
that does not cost it anything.

Tune `confidence_floor` per manifest against real footage. Start at `0.6` and
raise it until occluded and glare-affected frames reliably land on
`INDETERMINATE` rather than `PASS`.

---

## 2. Flash the firmware

```bash
arduino-cli core install arduino:stm32
arduino-cli compile --fqbn arduino:stm32:unoq firmware/mcu_actuator
arduino-cli upload  --fqbn arduino:stm32:unoq -p /dev/ttyACM0 firmware/mcu_actuator
```

Or open `firmware/mcu_actuator/mcu_actuator.ino` in the Arduino IDE or Arduino
App Lab, select the UNO Q board, and upload to the **STM32U585 MCU core** (not
the Linux side).

Confirm the C parser still agrees with the Python host before flashing:

```bash
.venv/bin/python -m pytest tests/test_firmware_protocol.py
```

That compiles `protocol.h` against a stub `Arduino.h` and feeds it frames from
`readykit.protocol`. If you edited either side of the protocol, this is the
test that catches the divergence.

---

## 3. Wire it

| Component | Pin | Notes |
|---|---|---|
| Green status LED | D2 | 220 Ω to GND. Pass. |
| Red status LED | D3 | 220 Ω to GND. Fail (fast blink), hold (slow pulse). |
| Piezo buzzer | D4 | Active buzzer. Fail only. |
| Relay module IN | D5 | Isolated 5 V relay switching the 12 V solenoid. |
| Ground | GND | Common rail across MCU, LEDs, relay, and both supplies. |

### Use a fail-secure latch

The firmware drives D5 **LOW** as the first statement of `setup()`, before it
touches anything else, and LOW is where the pin sits during reset and after
power loss. That only produces a safe enclosure if the latch is **fail-secure**
— de-energised means locked.

A fail-safe latch (de-energised means *unlocked*, as required by code on egress
doors) inverts the entire safety argument: every power cut becomes an unlock.
If the enclosure must use a fail-safe latch for life-safety reasons, that is a
legitimate requirement, but it means this system can no longer be the thing
keeping it shut, and you need a different mechanism.

Power the solenoid from its own 12 V supply, not from the board. Fit a flyback
diode across the coil.

### Which serial port

The UNO Q's USB-C port belongs to the **QRB2210 running Debian**, not to the
STM32U585 that drives the pins. So `--port` is one of:

- the Linux-side bridge that forwards to the MCU, if you are running the
  board's stock bridging service, or
- the MCU's own UART, if you are driving it directly.

The protocol is identical either way; only the device path changes.

---

## 4. Run it

```bash
.venv/bin/pip install -e ".[host]"

.venv/bin/readykit watch \
  --manifest manifests/trauma-kit-a.json \
  --engine geniex --model ai-hub-models/Qwen2.5-VL-7B-Instruct \
  --device auto \
  --camera 0 \
  --link serial --port /dev/ttyACM0 \
  --interval 3
```

With the console:

```bash
.venv/bin/readykit console --manifest manifests/trauma-kit-a.json
# http://127.0.0.1:8420
```

The console binds to loopback deliberately. This device releases a physical
latch on command; binding it to a routable interface turns a local view into a
remote actuator. `--host` will let you override that and warns when you do.

---

## Bring-up checklist

Work through this with the boards on the bench. Each step is a behaviour the
simulator already pins — this confirms the hardware agrees.

**The model, first** — everything else is useless if this fails, and it is the
step with the most unknowns

- [ ] `pip install geniex` succeeds on the Snapdragon host.
- [ ] `geniex pull ai-hub-models/Qwen2.5-VL-7B-Instruct` completes. Note how
      long it took and how much disk it used.
- [ ] `geniex infer ai-hub-models/Qwen2.5-VL-7B-Instruct` runs and answers a
      question about an image. If this does not work, nothing downstream will.
- [ ] `readykit inspect --engine geniex --camera 0 --manifest ...` returns a
      verdict of any kind. A verdict of INDETERMINATE here is a success: it
      means the pipeline ran end to end and the model simply was not confident.
- [ ] `readykit bench --engine geniex --camera 0 --runs 20` prints a latency.
      Record the number **and** the engine string beside it.
- [ ] The `engine` field in `readykit records` reads
      `geniex:<model>@<device>` and names the device you expected.

**Resting state**

- [ ] Power on with no host attached. Latch is locked, both LEDs dark.
- [ ] Reset the MCU while the latch is released. It locks immediately.
- [ ] Pull the 12 V supply while released. It locks.

**Commands**

- [ ] `PASS` releases the latch and lights green for the manifest's hold.
- [ ] The latch re-engages when the hold expires, with the host still running.
- [ ] `FAIL` keeps it locked, blinks red fast, sounds the buzzer.
- [ ] `INDETERMINATE` keeps it locked, pulses red slowly, **stays silent** —
      it must be distinguishable from `FAIL` across the room.

**The watchdog**

- [ ] Trigger a pass, then kill the host process mid-hold
      (<kbd>Ctrl</kbd>+<kbd>C</kbd>). The latch engages within
      `LINK_TIMEOUT_MS` and the LEDs go to the stale wig-wag.
- [ ] Same, but unplug the USB cable instead. Same result.

**Integrity**

- [ ] Send `PASS_KIT\n` down the wire by hand (`screen`, `minicom`). It is
      refused with `ACK 0 BAD_FRAME` and nothing moves.
- [ ] Send a frame with one payload byte altered. `ACK 0 BAD_CRC`, nothing
      moves.
- [ ] Send the same valid RELEASE frame twice. The second is `ACK <n> REFUSED`.

**The whole loop**

- [ ] A complete kit under the camera passes and unlocks.
- [ ] Remove one critical item. It fails and stays locked.
- [ ] Cover the tray with a cloth. **INDETERMINATE, and it stays locked.**
      This is the case the whole design exists for — if a covered tray ever
      passes, stop and raise the confidence floor.
- [ ] Breathe on the lens / shine a light into it. Still not a pass.
- [ ] `readykit records` shows every one of the above with an honest
      `commanded` field.

---

## Tuning

| Setting | Where | Notes |
|---|---|---|
| `confidence_floor` | manifest JSON | Raise until occlusion reliably reads INDETERMINATE. |
| `hold_seconds` | manifest JSON | Must be shorter than `LINK_TIMEOUT_MS` unless the host heartbeats through it — `readykit watch` does. |
| `LINK_TIMEOUT_MS` | `mcu_actuator.ino` | Keep in step with `VirtualActuatorNode.link_timeout_ms`, which is what the tests exercise. |
| `--interval` | CLI | Inference latency on the NPU sets the floor. |

## Known gaps

- **Counting accuracy is the model's weakest axis.** Quantity is enforced -
  an item the manifest requires two of resolves to INDETERMINATE unless the
  model returns a count, and to FAIL if the count falls short. But counting
  small identical objects is harder for a VLM than identifying them, so tune
  `--frames` upward for kits with multi-quantity items: frames that disagree
  on a count establish no count, which fails closed rather than guessing.
- **No physical tamper detection.** The system knows what the camera sees. A
  kit swapped after a pass, during the hold, is not detected — the hold is
  deliberately short for this reason.
- **Single frame per inspection.** Aggregating several frames before deciding
  would cut the INDETERMINATE rate on a moving or reflective kit. The
  interface supports it; the loop does not do it yet.
