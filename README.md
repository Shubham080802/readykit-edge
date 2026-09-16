# ReadyKit Edge

Air-gapped visual inspection for equipment kits, with physical actuation.

A camera watches an equipment kit. A vision-language model running locally on a
Qualcomm Hexagon NPU decides whether the kit is complete and serviceable. That
decision drives a fail-secure latch on an Arduino UNO Q. Nothing leaves the
device — no cloud, no network call, no remote fallback.

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

## Run the original design beside it

That claim is checkable rather than rhetorical. [`src/readykit/naive.py`](src/readykit/naive.py)
is the original substring matcher, preserved and executable, and every
inspection replays it against the **same verbatim model reply** before
recording what it would have done:

```bash
.venv/bin/readykit compare --manifest manifests/trauma-kit-a.json
```

```
scene                   blueprint           readykit        divergence
--------------------------------------------------------------------------
complete                PASS_KIT            pass            agreed
complete-negated        ERR_MISSING_TOOL    pass            rejects a good kit
empty                   ERR_MISSING_TOOL    fail            agreed
expired                 PASS_KIT            fail            UNLOCKS A BAD KIT
short                   PASS_KIT            fail            UNLOCKS A BAD KIT
occluded                PASS_KIT            indeterminate   UNLOCKS A BAD KIT
missing-shears          PASS_KIT            fail            UNLOCKS A BAD KIT
...

11 of 19 scenes would have released the latch under the original design.
```

The sharpest one is `missing-shears`. The model correctly reports the shears
are gone — it just phrases it as *"Absent from the tray"*. No `"missing"`, no
`"no"`, so the original design writes `PASS_KIT` and opens a trauma kit with no
trauma shears in it.

Two things keep this honest rather than a strawman:

- **The blueprint gets some right.** It handles `complete` and `empty`
  correctly, and it rejects *"Sorry, I could not process that image"* — because
  `"could not"` happens to contain `"no"`. Correct, and entirely by accident.
  [`tests/test_naive.py`](tests/test_naive.py) pins that case deliberately.
- **Both parsers read the same string.** The simulator emits realistic model
  output — prose narration plus a JSON block, which is what instruction-tuned
  VLMs actually produce — and that text goes through the production parser.
  The console shows the verbatim line the substring match ran against.

The replay is recorded and displayed, never enacted. `naive.py` cannot reach
the Host Link, and a test asserts the latch stays engaged on an `unsafe`
divergence.

---

## First, on unfamiliar hardware

```bash
readykit doctor
```

Reports whether GenieX is installed, **which serial port the UNO Q is on**,
whether the camera opens, whether the manifests parse, and whether the audit
log is writable — each with the exact command that fixes it. Run it before you
need any of those to work.

> **Setting up a Snapdragon X Elite laptop from scratch?**
> [`docs/windows-setup.md`](docs/windows-setup.md) is the ordered sequence,
> including the one dependency that has no Windows ARM64 wheel.
>
> **On a Snapdragon X Elite AI PC** the host is Windows ARM64, so the commands
> below are written `.venv/bin/readykit` but you type `.venv\Scripts\readykit`.
> `readykit doctor` prints the form that works on the machine it is running on.

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

## Presence is not serviceability

A kit can be complete, undamaged, every tick green — and still fail:

```bash
.venv/bin/readykit inspect --manifest manifests/trauma-kit-a.json --scene expired
```

```
  FAIL  latch engaged
  Kit non-compliant: expired Vented Chest Seal
    + Windlass Tourniquet    found      0.97
    x Vented Chest Seal      found      0.96  EXPIRED 2026-07-30
    + Trauma Shears          found      0.94
    ...
```

A sealed, undamaged, correctly-placed packet of expired haemostatic gauze
satisfies every visual check and is still not something to hand a medic.
Reading a printed date off crumpled foil and reasoning about it is work object
detection cannot do — it is why this is a vision-language model.

Expiry obeys the same rule as everything else rather than getting a special
case. A date that could not be read is **unresolved**, not assumed fine, so a
smudged use-by fails closed exactly like an occluded item. The prompt tells the
model to omit the field rather than guess, because an invented expiry is the
one hallucination that would manufacture a pass.

## Two is not one

A manifest can require more than one of something, and one of a required pair
is a kit that runs out halfway through:

```bash
.venv/bin/readykit inspect --manifest manifests/trauma-kit-a.json --scene short
```

```
  FAIL  latch engaged
  Kit non-compliant: short on Windlass Tourniquet
    < Windlass Tourniquet    found      0.97  1/2
    + Vented Chest Seal      found      0.96  2/2  exp 2027-10-20
    + Trauma Shears          found      0.94
```

Counting obeys the same rule as everything else. A count that was never taken
is **unresolved**, not assumed sufficient — `--scene count-unreadable` shows
`?/2` and resolves to INDETERMINATE, because "I can see tourniquets" does not
establish that there are two of them. The parser refuses to coerce a bad count
to the required number, which is the one place it could manufacture a pass, and
frames that disagree on a count establish no count rather than taking the
minimum or the maximum.

## The audit trail

Every record is hash-chained to the one before it:

```bash
.venv/bin/readykit audit
```

```
  chain intact over 4 records
  head 968db8eeaeddd07590cdf13c73c98912
```

Edit a recorded `FAIL` into a `PASS` and it names the record you touched:

```
  CHAIN BROKEN
  record 1 (9b71768bdd1e) does not match its own hash - its contents were
  edited after it was written
```

Losing power mid-write is reported as `TRUNCATED`, not as tampering, and
appending afterwards continues the chain from the last complete record. An
air-gapped field device will lose power eventually, and crying wolf about it
would train operators to ignore the alarm.

This is tamper-**evident**, not tamper-proof — it does not stop someone with
write access who rebuilds every subsequent hash. The CLI says so every time it
runs.

## Running it on the hardware

```bash
.venv/bin/pip install -e ".[host]"
.venv/bin/readykit inspect \
  --manifest manifests/trauma-kit-a.json \
  --engine geniex --model qualcomm/Qwen3-VL-4B-Instruct \
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

That command serves scripted scenes. Point it at a real camera and a real
model and it inspects what the camera sees:

```bash
.venv/bin/readykit console --manifest manifests/trauma-kit-a.json \
  --camera 0 --engine geniex --model qualcomm/Qwen3-VL-4B-Instruct
```

```bash
# any machine, when GenieX will not install
.venv/bin/readykit console --manifest manifests/desk-rehearsal.json \
  --camera 0 --engine ollama --model qwen2.5vl
```

A live console **drops the scene picker** and names its input instead. An
operator reading a dropdown of scene names believes the input is scripted, so
showing one in front of a live camera would report a verdict about their
actual kit under the name of a rehearsal — the same lie as a simulated latch
beside a real one. For the same reason `--camera` without a real model is
refused up front rather than one button press later: the simulated engine
answers from a scene name and never looks at the frame.

### Seeing what it sees

A live console shows the camera, and reads left to right:

**Live** → **Image captured** → *arrow* → **Recognised**

- **Live** is what the camera sees now, for aiming. It is never recorded.
- **Image captured** is the exact frame the verdict is decided on. It is only
  shown when its digest matches the Inspection Record, so a picture from one
  look can never sit beside the findings from another.
- The **arrow** runs while the model works and stops when it answers.
- **Recognised** names what the model found, in large type, beside a mark that
  says what the verdict says — ✓ PASS, ✗ FAIL, ? INDETERMINATE, never a tick
  on a kit that did not pass — with each item's reading underneath.

The name is the model's own reading, including one the kit check set aside:
hold a pen up to the camera and the card says **Pen**, the row says "found ·
not counted", and the verdict stays INDETERMINATE because no kit is laid out.
What is shown and what is decided are kept apart on purpose.

### Inspecting with the browser's camera

On a Mac, a server only gets the camera if it was started from an app that
already has camera permission — in practice, a Terminal opened by hand every
time. With `--browser-camera` the page opens the camera instead, using the
permission the browser already holds, so the console can be started from
anywhere:

```bash
.venv/bin/readykit console --manifest manifests/desk-rehearsal.json \
  --browser-camera --engine ollama --model qwen2.5vl:7b
```

Open it in Chrome and:

- **Inspect** turns the camera on, counts down six seconds on the live view so
  you can put the items down and take your hand away, then captures.
- **Auto** watches the live view and inspects once it has held still for six
  seconds. After a look it waits for the view to change, so a scene left alone
  is not inspected over and over.
- **Reset** switches the camera off and clears both frames. Pressed during the
  countdown, it cancels it.

**This is a rehearsal mode.** A page that uploads frames can be handed any
picture, so never use it for a latch guarding a real kit — the Inspection Host
should read its own fixed camera with `--camera`. Without the flag the upload
endpoint does not exist.

### How long a look takes

Measured with Ollama `qwen2.5vl:7b` on an Apple M-series Mac. Reading the
frame, not writing the answer, is most of the cost:

| Change | Per look |
|---|---|
| Full-HD frame sent as-is | ~39s |
| Frame capped at 960px — smaller buys nothing, Ollama resizes every image to the same ~1,040 tokens | ~19s |
| Instructions sent first, as a system message, so Ollama reuses them between looks | ~15s |

So a result lands about 21 seconds after pressing Inspect: six seconds of
countdown, then the model. On the Snapdragon's Hexagon NPU the project measured
13.6s of decode for a seven-item manifest.

### Keeping it running on a Mac

To have the console start at login, point a LaunchAgent in
`~/Library/LaunchAgents` at `.venv/bin/readykit console … --browser-camera`
with `RunAtLoad` and `KeepAlive`. Two things bite:

- **Keep the checkout out of `~/Documents`, `~/Desktop` and `~/Downloads`.**
  macOS does not let a login item read those folders; the agent fails with
  `Operation not permitted` and exit code 126.
- **Ollama must be up first.** The console refuses to start without its model,
  so `KeepAlive` retries until Ollama is listening. Open `Ollama.app` at login.

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

## Other commands

```bash
readykit demo     --manifest manifests/trauma-kit-a.json   # scripted five-beat sequence
readykit compare  --manifest manifests/trauma-kit-a.json   # every scene vs the original design
readykit bench    --manifest manifests/trauma-kit-a.json   # measured inference latency
readykit audit                                             # verify the record chain
readykit scenes                                            # list simulator scenes
```

`--frames N` aggregates several looks per inspection. Frames that disagree
produce doubt rather than an average: two frames saying found and two saying
absent is not "probably fine", it is a kit nobody has established anything
about.

New to the project, or explaining it to someone who is?
[`docs/what-we-are-building.md`](docs/what-we-are-building.md) is the whole
thing in plain English, no jargon.

For the pitch, [`docs/demo.md`](docs/demo.md) is a timed run-of-show with the
questions judges actually ask, and [`docs/one-pager.md`](docs/one-pager.md) is
the handout.

## Development

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

## Status

Runs end to end in simulation; every fail-closed path is covered by tests.

**Run on the hardware, not yet fully signed off.** The firmware has been
flashed, the Host Link round-trips real `PING` and `RELEASE` commands with real
acknowledgements, and a real GenieX/Qwen3-VL-4B inference on the Hexagon NPU
has driven a real latch release end to end. The
[bring-up checklist](docs/deployment.md#bring-up-checklist) is only partly
worked through: resting state and the `PASS` and `INDETERMINATE` command paths
are confirmed; the watchdog and integrity items are still open. Treat those as
unproven until they are.

The operator console has also been run against a real webcam through Ollama on
a Mac, with the host camera and with `--browser-camera`.

That checklist is the point of building the simulator first. Every item on it
is a behaviour already pinned by a test, so bench time goes on confirming the
hardware agrees rather than discovering what the hardware does. The item that
matters most is the cloth-over-the-tray one: if a covered kit ever passes, stop
and raise the confidence floor.

Known gaps are listed at the end of that document.

## License

MIT
