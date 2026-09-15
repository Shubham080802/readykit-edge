# Deployment

Taking ReadyKit Edge from a laptop simulation to a Snapdragon X Elite host
driving an Arduino UNO Q — on its own, or wired to a real solenoid.

Everything in this document has been designed and unit-tested but **not yet
run on the hardware**. The [bring-up checklist](#bring-up-checklist) is written
to be worked through in one sitting the first time the boards are on the bench
— every item on it is a behaviour the simulator already pins, so it confirms
the hardware agrees rather than discovering the behaviour from scratch.

---

## 0. Check the machine

```bash
readykit doctor            # add --cameras to probe camera indices too
```

Everything below assumes a POSIX shell. The Snapdragon X Elite AI PCs are
Windows ARM64, so `.venv/bin/readykit` is `.venv\Scripts\readykit` there;
`readykit doctor` prints whichever applies.

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

Check the firmware on the host before flashing:

```bash
.venv/bin/python -m pytest tests/test_firmware_sketch.py \
                           tests/test_firmware_indicators.py \
                           tests/test_firmware_protocol.py
```

Three separate things, all cheap:

- **`test_firmware_sketch`** compiles the real `.ino` against a stub
  `Arduino.h`, five times, with different LED macros defined each time so
  every branch of the pin guards gets read. A sketch that will not build is a
  bad thing to discover at a bench with one board.
- **`test_firmware_indicators`** drives the real `rkRenderPanel()` and asserts
  what the LEDs show — that red means locked, that amber is not red, that the
  buzzer never sounds out of step with the light.
- **`test_firmware_protocol`** feeds the C parser frames from
  `readykit.protocol`. If you edited either side of the protocol, this is what
  catches the divergence.

None of them prove anything about the STM32U585 toolchain or the board's own
pin macros. Only flashing settles those. They catch the typo, not the target.

---

## 3. The board on its own

**You do not need to wire anything.** A bare UNO Q, one USB-C cable and no
other parts is a complete, honest demonstration — and the sketch is written so
the same binary runs on a bare board and inside an enclosure.

The UNO Q carries two RGB LEDs wired straight to the STM32U585, which happens
to be exactly the two signals this system has to show:

| LED | Answers | Shows |
|---|---|---|
| **Latch** | Is it locked *right now*? | Red — engaged. Green — released. Never blinks, never dark. |
| **Verdict** | What did the model conclude? | Green — pass. Red fast blink — fail. **Amber slow pulse — could not tell.** Blue wig-wag — host is gone. |

Keeping those apart is the point of having two. The latch light reports the
lock and nothing else, so it can never disagree with the physical state a
person is about to act on. The verdict light reports the model, *including*
the case where the model concluded nothing.

Amber sitting beside red is the whole argument in one glance: the kit could
not be read, so the latch did not move. Point at that.

The colours are worth more than the discrete red/green pair they replace. On
the old two-LED build, "your kit is wrong" and "I cannot see your kit" were
both red and differed only in blink rate — and rate is the cue people read
last. They are now different colours, which is the cue people read first.

> The sketch guards the LED macro names and falls back to `LED_BUILTIN` if a
> core spells them differently, so it compiles either way. In that fallback the
> single lamp is lit **only when the latch is open** — so a dead, unpowered or
> unflashed board reads as locked, which is the truth, because the relay line
> falls to its de-energised state at the same moment.

### When you do add an enclosure

The header pins are the deployed unit's lines. Nothing is attached on a bare
board, and the firmware drives them anyway.

| Component | Pin | Notes |
|---|---|---|
| Green status lamp | D2 | 220 Ω to GND. Pass. |
| Red status lamp | D3 | 220 Ω to GND. Fail (fast blink), hold (slow pulse). |
| Piezo buzzer | D4 | Active buzzer. Fail only. |
| Relay module IN | D5 | Isolated 5 V relay switching the 12 V solenoid. |
| Ground | GND | Common rail across MCU, lamps, relay, and both supplies. |

Until then, the audible alarm comes from the host — the console plays it
through the laptop's speakers, which carries across a noisy room considerably
better than a piezo does anyway.

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

Note that the safety property is a property of **the pin**, not of the part
hanging off it. D5 goes LOW on reset, on power loss, on a stale link and on
hold expiry whether a relay coil or nothing at all is attached. That is why
the bare-board demonstration is not a compromise: you are watching the real
control logic, with a $6 part left off the end of it.

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

The console is **loopback-only, and refuses anything else**. This device
releases a physical latch on command and the console can trigger an
inspection, so serving it on a routable interface would not expose a
dashboard, it would expose a remote unlock to anyone who can reach the port.

`--host` accepts `127.0.0.1`, `localhost` or `::1`. Anything else exits with a
message explaining why, before it loads or binds anything. This used to be a
warning; a warning is the wrong shape for a mistake you make once, in a hurry,
on a machine whose scrollback you are not reading. If you genuinely need it
elsewhere, edit `LOOPBACK_HOSTS` in `cli.py` - which is about the right amount
of friction for turning a lock into a network service.

There is no deployment story here and there is not meant to be one. No
container, no host, no tunnel: the entire system runs on the laptop in front of
you and talks to a board over a cable.

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

- [ ] Power on with no host attached. Latch LED red, verdict LED dark.
- [ ] Reset the MCU while the latch is released. It locks immediately, and the
      latch LED goes red before anything else happens.
- [ ] Pull the USB-C cable while released. The board goes dark — which reads
      as locked, because the latch is the one thing never shown by an unlit
      LED.

**Commands**

- [ ] `PASS` releases the latch: latch LED green, verdict LED green, for the
      manifest's hold.
- [ ] The latch re-engages when the hold expires, with the host still running.
      Latch LED returns to red on its own.
- [ ] `FAIL` keeps the latch LED **red** and blinks the verdict LED red fast.
- [ ] `INDETERMINATE` keeps the latch LED **red** and pulses the verdict LED
      **amber**, slowly. Stand back and check you can still tell it from
      `FAIL` across the room — different colour and different rate, so it
      survives both a glance and a photograph.
- [ ] Throughout all of the above, confirm the latch LED never once disagrees
      with what the console says the latch is doing.

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
