# Run of show

A three-minute demonstration, and the questions that follow it.

Everything below is something the system actually does today. Where a claim
has a limit, the limit is written next to it — being caught overstating one
thing costs you credit on everything else you said.

---

## Before you start

```bash
readykit audit --log records/inspections.jsonl   # chain intact
readykit bench --manifest manifests/trauma-kit-a.json --runs 30
readykit console --manifest manifests/trauma-kit-a.json
```

Console on the big screen. Terminal on your laptop. Have
`readykit demo --dwell 6` ready as the fallback if the hardware misbehaves —
it runs the same five beats in the same order and needs no camera.

If the board is dead, `--link loopback` runs everything except the solenoid.
Say so out loud when you do it. A demo that looks identical whether or not a
real latch moved is exactly what this project argues against.

---

## The three minutes

### 0:00 — What this is (20s)

> A medic grabs a trauma kit in the field. It's missing a tourniquet. Nobody
> checked, because checking is a clipboard task at 3am and clipboards lie.
>
> ReadyKit Edge watches the kit, decides whether it's complete and in date,
> and physically locks the cabinet if it isn't. All of it on-device — this
> thing runs in a basement with no network.

Point at the `air-gapped · no network` pill.

### 0:20 — It works (25s)

Complete kit under the camera. **Latch clunks open.** Green across the
checklist.

> Seven items, all found, all in date. The latch releases for five seconds.

Let them hear the solenoid. That sound is the demo.

### 0:45 — The thing everyone gets wrong (45s)

Remove the trauma shears. Run it.

> Fails, stays locked. Obvious so far.
>
> Now look at what the model actually said —

Point at the quoted line in the console:
`"Absent from the tray: Trauma Shears."`

> The reference design for this hardware decides by checking whether the
> model's answer contains the word "missing" or the word "no". That sentence
> contains neither. So it writes PASS_KIT and opens the cabinet.
>
> We still run that original logic on every frame, beside ours, and record
> what it would have done. That red strip is it, on this frame, right now.

This is the beat that wins the argument. Don't rush it.

### 1:30 — Why this needs a vision model (40s)

Put the complete kit back, but with the expired chest seal.

> Every item is present. Every item is undamaged. Every tick is green.
>
> And it fails.

Point at the red date.

> The chest seal expired six weeks ago. The model read that date off the
> packaging and reasoned about it. That's not something object detection does,
> and it's not something a barcode scanner does — expired adrenaline in a crash
> cart is a real problem that kills people, and it's invisible to everything
> except something that can read.

### 2:10 — Failing closed (30s)

Drape a cloth over the tray. Run it.

> INDETERMINATE. Not pass, not fail — we could not establish anything, so
> nothing opens.
>
> That's the whole design in one word. A system that gates a physical lock has
> to treat "I don't know" as a refusal. The original logic reads a blurry
> frame as a pass, because "I can't tell" doesn't contain the word "missing"
> either.

### 2:40 — The receipts (20s)

Switch to the terminal:

```bash
readykit compare --manifest manifests/trauma-kit-a.json
```

> Nineteen scenarios. The original design would have opened the cabinet on
> eleven of them.

Then:

```bash
readykit audit
```

> And every inspection is hash-chained. Edit a recorded FAIL into a PASS and
> the chain names the record you touched.

(If you have a spare ten seconds, actually edit one and re-run it. It lands.)

### 3:00 — Close

> Complete, serviceable, in date, fully offline, with an audit trail that
> survives a power cut. The hard part wasn't reading the kit. It was deciding
> what to do when the model isn't sure.

---

## Questions you will get

**"Why not just use object detection / YOLO?"**
For presence alone, you could — and it'd be faster. It cannot read an expiry
date off a crumpled foil packet and reason about whether that date has passed.
That's the check that fails a kit which looks perfect, and it's the reason this
is a VLM.

**"Isn't the comparison against the old design a strawman?"**
It's the reference implementation from the hardware blueprint, preserved and
executed, not a paraphrase. Both parsers read the same verbatim model output.
And it gets six of the nineteen scenarios right — including rejecting
"Sorry, I could not process that image", because "could not" happens to
contain "no". Correct, by pure accident. There's a test pinning that case
specifically.

**"What if the model hallucinates a date?"**
The prompt tells it to omit the field rather than guess, and the parser turns
anything unrecognisable into "no date", which is unresolved, which keeps the
latch shut. An invented expiry is the one hallucination that could manufacture
a pass, so every ambiguity resolves the safe way. We can't stop a model
confidently misreading a legible date — that's a model-accuracy problem, and
it's why the confidence floor is tunable per manifest.

**"Is the audit trail actually secure?"**
It's tamper-*evident*, not tamper-proof. It catches editing, deletion,
reordering and corruption. It does not stop someone with write access who
rebuilds every subsequent hash — that needs a signing key in a secure element
or an external anchor. The CLI prints that limitation every time it runs.

**"What happens if the host crashes with the latch open?"**
The actuator node engages it. It treats silence longer than two seconds as a
fault and falls back to locked rather than holding its last instruction. The
original sketch used a blocking `delay(5000)`, during which the MCU reads no
serial at all — it couldn't even receive the command to close.

**"How fast is it?"**
`readykit bench` measures it on whatever engine is loaded. Quote the number it
prints, and say which engine produced it. Do not quote simulated timings as
NPU figures; the tool labels them for exactly that reason.

**"Has this run on the actual hardware?"**
Say where you actually are. The bring-up checklist in `docs/deployment.md` is
the honest answer to what's been verified and what hasn't, and every item on
it is a behaviour a test already pins.

**"What's not finished?"**
It has never run on the hardware — that's the honest headline, and the bring-up
checklist says exactly what remains to be proven on device. Beyond that:
counting is the model's weakest axis, so multi-quantity kits want more frames
per inspection; the audit chain is tamper-evident rather than tamper-proof; and
it's a single station with no fleet view. Volunteering these tends to buy more
credibility than it costs.

---

## If something breaks

| Symptom | Do this |
|---|---|
| Latch doesn't move | `readykit audit` still works; switch to `--link loopback` and say you're doing it |
| Camera won't open | `readykit demo` — scripted, no camera needed |
| Model too slow / won't load | `--engine simulated`, and say so |
| Console blank | It polls once a second; reload. Assets are served `no-store`, so there's no stale cache to fight |
| Total hardware failure | `readykit compare` and `readykit audit` are the argument, and they run anywhere |

Never let a failure become dead air. Narrate the fallback — a team that says
"the board's gone, here's the same thing in simulation" reads as prepared. A
team silently poking at a USB cable does not.
