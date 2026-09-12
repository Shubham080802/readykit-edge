# ReadyKit Edge

Air-gapped visual inspection for equipment kits, with physical actuation.

A camera watches an equipment kit. A vision-language model running locally on a
Qualcomm Hexagon NPU decides whether the kit is complete and serviceable. That
decision drives a solenoid latch on an Arduino UNO Q. Nothing leaves the device
— no cloud, no network call, no remote fallback.

Built for **Qualcomm Snapdragon® X Elite** (inference) + **Arduino® UNO™ Q**
(actuation).

---

## The one rule

> **Absence of evidence is not evidence of compliance.**

A Verdict is one of three values, not two:

| Verdict | Meaning | Latch |
|---|---|---|
| `PASS` | Every critical item positively found above the confidence floor | **Released** for the manifest's hold |
| `FAIL` | A positive finding that the kit is non-compliant | Engaged |
| `INDETERMINATE` | Compliance could not be established — occluded, low confidence, unparseable, or the model simply didn't say | Engaged |

`PASS` is never the fallthrough branch. An empty reply, a crashed engine, a
dropped serial link, and a fogged lens all land on `INDETERMINATE`, and
`INDETERMINATE` never opens anything.

This matters because the obvious implementation gets it backwards. Matching
`"missing" in reply or "no" in reply` against free model text means
`"I cannot determine, the tray is occluded"` contains neither token and is read
as a pass — releasing the latch on a kit nobody has actually seen. Meanwhile
`"No items are missing"` contains both and is read as a failure. See
[`tests/test_verdict.py`](tests/test_verdict.py), which pins both cases.

---

## Running it without the hardware

The full pipeline runs on any machine. Capture, inference, and the host link
each sit behind an interface with both a real and a simulated implementation,
so you can drive the whole system — including latch state and the audit trail —
before the boards arrive.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# One inspection against a scripted scene
.venv/bin/readykit inspect --manifest manifests/trauma-kit-a.json --scene complete

# The same kit with the shears removed
.venv/bin/readykit inspect --manifest manifests/trauma-kit-a.json --scene missing-shears

# A fogged lens — the case that must NOT unlock
.venv/bin/readykit inspect --manifest manifests/trauma-kit-a.json --scene occluded
```

## Running it on the hardware

```bash
.venv/bin/pip install -e ".[host]"
.venv/bin/readykit inspect \
  --manifest manifests/trauma-kit-a.json \
  --engine geniex --model models/readykit_vlm.qnn \
  --camera 0 \
  --link serial --port /dev/ttyACM0
```

See [`docs/deployment.md`](docs/deployment.md) for model export, firmware
flashing, and wiring.

---

## The operator console

```bash
.venv/bin/pip install -e ".[console]"
.venv/bin/readykit console --manifest manifests/trauma-kit-a.json
# http://127.0.0.1:8420
```

Live verdict, per-item checklist, actuator telemetry (latch, indicator,
buzzer, link freshness, last acknowledged sequence), and the audit trail.

It binds to loopback deliberately: this device releases a physical latch on
command, and binding it to a routable interface would turn a local view into a
remote actuator.

The latch pill in the banner always shows the latch **now**, never the latch at
the moment of the verdict — a hold expires while the banner is still on screen,
and nobody should read "released" over telemetry saying "engaged" before
reaching into an enclosure.

Visual language is [`DESIGN.md`](DESIGN.md), vendored from
[VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md).

## Layout

```
src/readykit/
  domain.py        Manifest, Sighting, Verdict, resolve_verdict — pure, no I/O
  protocol.py      Host Link framing: commands, checksums, acknowledgement
  capture.py       Frame sources — camera and scripted
  inference/       Engines — GenieX on Hexagon NPU, and a simulator
  bridge/          Host Link transports — pyserial and loopback
  engine.py        The inspection loop
  recorder.py      Append-only Inspection Records
  console/         Operator console — FastAPI + static page
  cli.py
firmware/mcu_actuator/   STM32U585 sketch — non-blocking, watchdogged
firmware/test/           Cross-checks the C parser against the Python encoder
manifests/               Kit specifications
tests/
```

Domain vocabulary is defined in [`CONTEXT.md`](CONTEXT.md). The terms there are
load-bearing; each lists what it must not be confused with.

Visual language for the operator console is [`DESIGN.md`](DESIGN.md).

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

## Status

Runs end to end in simulation; every fail-closed path is covered by tests.
**Not yet run on the hardware** — no Snapdragon X Elite or UNO Q on hand. The
GenieX and pyserial backends are written against their documented interfaces
and the C firmware parser is cross-checked against the Python encoder, but the
on-device bring-up checklist in [`docs/deployment.md`](docs/deployment.md) has
not been worked through. Treat it as unproven until it has.

Known gaps, including `quantity` being carried but not enforced, are listed at
the end of that document.

## License

MIT
