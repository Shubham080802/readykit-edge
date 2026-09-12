# Deployment

Taking ReadyKit Edge from a laptop simulation to a Snapdragon X Elite host
driving a real solenoid on an Arduino UNO Q.

Everything in this document has been designed and unit-tested but **not yet
run on the hardware** — see [Bring-up checklist](#bring-up-checklist), which is
written to be worked through the first time the boards are on the bench.

---

## 1. Export a model

The engine expects a vision-language model quantised for the Hexagon NPU.

1. Sign in to [Qualcomm AI Hub](https://aihub.qualcomm.com/) and pick a VLM
   that can answer structured questions about an image. Qwen2-VL is the usual
   starting point; a detection model like YOLOv8/v10 works too if you replace
   `GenieXEngine` with a detector that emits the same `Sighting` list.
2. Compile it for **Snapdragon X Elite**, targeting the Hexagon NPU.
3. Download the `.qnn` artifact into `models/`.

`models/*.qnn` is gitignored — these are large binaries under Qualcomm's own
licence and do not belong in this repository.

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
  --engine geniex --model models/readykit_vlm.qnn \
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

- **Quantity is not verified.** `RequiredItem.quantity` is carried through the
  manifest, the prompt, and the console, but `resolve_verdict` only checks
  presence. A manifest asking for two tourniquets passes on one. Counting
  needs either a detection model that returns instances or a prompt that asks
  for a count, and it needs its own tests before anyone relies on it.
- **No physical tamper detection.** The system knows what the camera sees. A
  kit swapped after a pass, during the hold, is not detected — the hold is
  deliberately short for this reason.
- **Single frame per inspection.** Aggregating several frames before deciding
  would cut the INDETERMINATE rate on a moving or reflective kit. The
  interface supports it; the loop does not do it yet.
