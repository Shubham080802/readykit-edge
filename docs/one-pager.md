# ReadyKit Edge

**Air-gapped visual kit inspection with physical actuation.**
Snapdragon® X Elite (on-device VLM via GenieX) → Arduino® UNO™ Q (latch control)

github.com/taranggoyal70/readykit-edge

---

## The problem

Emergency kits fail quietly. A trauma kit missing a tourniquet, a crash cart
with expired adrenaline, a maintenance box short an insulated tool — nobody
finds out until the moment it matters. Checking is a clipboard task at 3am, and
clipboards lie.

## What it does

A camera watches an equipment kit. A vision-language model running locally on
the Hexagon NPU decides whether the kit is **complete, undamaged, and in date**.
That decision drives a latch: a compliant kit unlocks, anything else stays
shut. No network, no cloud, no remote fallback.

The latch line is fail-secure — de-energised means locked — so a crash, a
pulled cable or a flat battery all land on *shut*. That is a property of the
pin, which is why it holds whether the pin is driving a 12 V solenoid in an
enclosure or an LED on a bare board.

## The idea the project is actually about

> **Absence of evidence is not evidence of compliance.**

A verdict is one of three values, not two:

| | Meaning | Latch |
|---|---|---|
| **PASS** | Every critical item positively found, in date, above the confidence floor | Released |
| **FAIL** | A positive finding of non-compliance | Engaged |
| **INDETERMINATE** | Compliance could not be established | Engaged |

`PASS` is never the fallthrough branch. An occluded lens, a crashed NPU, a
dropped serial link, a smudged expiry date and an unparseable model reply all
land on `INDETERMINATE`, and `INDETERMINATE` never opens anything.

**This is not the obvious implementation.** The reference design for this
hardware decides with `if "missing" in reply or "no" in reply`. Run against
real model output, `"Absent from the tray: Trauma Shears"` contains neither
token — so it writes `PASS_KIT` and unlocks a trauma kit with no trauma shears
in it.

We kept that logic executable and replay it on every frame:

```
readykit compare --manifest manifests/trauma-kit-a.json

  19 scenarios · 11 would have released the latch under the original design
```

It gets two right, including one purely by accident. That's pinned by a test,
because a comparison that only ever flattered us wouldn't be worth showing.

## Why it needs a vision model

Presence is not serviceability. A sealed, undamaged, correctly-placed packet of
**expired** haemostatic gauze passes every visual check and is still not
something to hand a medic. Reading a printed use-by date off crumpled foil and
reasoning about whether it has passed is work object detection cannot do and a
barcode scanner cannot do.

A kit can be complete, undamaged, every tick green — and still fail.

## Engineering

| | |
|---|---|
| **Host link** | CRC-8 framed, sequence-numbered, acknowledged. An unacknowledged command is recorded as not having happened. |
| **Watchdog** | The actuator node treats two seconds of silence as a fault and engages the latch rather than holding its last instruction. A host that dies mid-hold cannot leave a cabinet open. |
| **Firmware** | Non-blocking. No `delay()` anywhere — the reference sketch blocked for five seconds mid-hold, deaf even to the command that would have closed the latch. |
| **Conformance** | The C parser is compiled against a stub `Arduino.h` and cross-checked against the Python encoder by a test, because the safety argument assumes two implementations agree. |
| **Multi-frame** | Several looks per inspection. Frames that disagree produce doubt, never an average. |
| **Audit** | Hash-chained records. Editing, deleting or reordering history is detectable and named. |
| **Simulation** | Every hardware layer has a real and a simulated implementation, so the whole pipeline — including latch behaviour and the watchdog — is testable without a board. |

**256 tests. `ruff` and `mypy --strict` clean. CI on Python 3.11–3.13.**

## Honest limits

- **Not yet run on the hardware.** The bring-up checklist in
  `docs/deployment.md` lists exactly what remains to be verified on device.
  Every item on it is a behaviour a test already pins.
- **Counting is the model's weakest axis.** Quantity is enforced, and a count
  that was never taken fails closed — but counting small identical objects is
  harder for a VLM than identifying them, so multi-quantity kits want more
  frames per inspection.
- **Tamper-evident, not tamper-proof.** The audit chain catches editing,
  deletion and reordering. It does not stop an attacker with write access who
  rebuilds every subsequent hash.
- **Single-station.** No fleet management, no multi-site aggregation.

## Stack

Qualcomm GenieX · Qualcomm AI Hub Models / Hugging Face · Hexagon NPU ·
STM32U585 · Python 3.11+ · FastAPI

Visual language vendored from
[VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md).
