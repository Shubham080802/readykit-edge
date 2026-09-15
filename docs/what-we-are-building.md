# What we are building

Plain English. No jargon. If you read only this page, you will know what the
project does and why it is built the way it is.

---

## The problem

Some boxes have to be complete, or someone gets hurt.

A medic's trauma kit. A crash cart in a hospital corridor. A toolbox for
someone about to work on live electricity. Every one of them is useless in the
exact moment it is needed if something is missing.

Today, these get checked by a person with a clipboard. Usually at the end of a
shift. Usually at 3am. People get tired. People tick boxes they did not really
look at. And nobody finds out anything was wrong until the worst possible
moment.

## What we built

A camera looks at the kit. The computer decides whether the kit is OK. That
decision opens or closes a real lock on the cabinet.

- Kit is fine → the lock opens, you can take it
- Kit is not fine → the lock stays shut

So a bad kit cannot leave the cabinet.

Everything happens on the device itself. There is no internet connection, no
cloud, nothing sent anywhere. It works in a basement with no signal. That
matters because these cabinets live in ambulances, field hospitals and
warehouses.

## The hard part

Here is the thing that makes this project interesting.

Most people would build this with two answers: **yes** or **no**. Kit is good,
or kit is bad.

That is wrong, and it is dangerous.

There is always a third answer: **"I could not tell."** The camera was
blocked. Someone's hand was in the way. The lens was foggy. The computer
crashed. The label was smudged.

If you only have two answers, "I could not tell" has to become one of them.
And in practice it quietly becomes **yes** — because nothing actually went
wrong, so nothing said no.

That means the lock opens on a kit **nobody actually looked at.**

So we built it with three answers:

| Answer | What it means | The lock |
|---|---|---|
| **Pass** | We checked everything and it is all there | Opens |
| **Fail** | We checked, and something is wrong | Stays shut |
| **Don't know** | We could not establish that it is fine | Stays shut |

The rule behind the whole project is one sentence:

> **Not finding a problem is not the same as checking.**

"Don't know" never opens the lock. Ever.

## We can prove the normal way is broken

We did not just claim the two-answer version is dangerous. We built it, kept
it, and we run it side by side with ours on every single check.

The original design decided by looking for the words *"missing"* or *"no"* in
the computer's answer. Here is a real reply from the model:

> "Absent from the tray: Trauma Shears."

It correctly says the shears are gone. But it used the word *"absent"*, not
*"missing"*. So the old design sees neither word, decides everything is fine,
and **opens a trauma kit with no trauma shears in it.**

Run our comparison tool and you get this:

```
19 situations tested
11 of them would have opened the lock on a bad kit
```

We show this live, on screen, while it happens.

We also kept it honest. The old design gets some of them right — including one
purely by luck. We wrote a test to make sure that case stays in, because a
comparison that only ever made us look good would not be worth showing anyone.

## Why it needs AI and not something simpler

Fair question. Simpler software can spot objects in a photo. Why use a big
language-and-vision model?

Because being present is not the same as being usable.

A sealed, undamaged, perfectly-placed packet of **expired** medical gauze
passes every visual check you can imagine. It looks completely fine. It is
still not something you hand to a medic.

So the model reads the **use-by date printed on the packet** and works out
whether that date has passed. Object detection cannot do that. A barcode
scanner cannot do that. Expired medicine in emergency equipment is a real and
documented problem that hurts people.

The result is a demonstration moment that surprises everyone:

> Every item is there. Every item is undamaged. Every tick on the screen is
> green. **And it still fails** — because one item went out of date last
> month.

We do the same thing with quantities. If the kit is supposed to have two
tourniquets and only has one, that is a kit that runs out halfway through. And
if the model cannot count them, it says so, and the lock stays shut.

## It keeps a record you can trust

Every check is written down: what the camera saw, what the model said word for
word, what was decided, and what the lock was told to do.

Each entry is mathematically linked to the one before it. If someone edits an
old entry to turn a failure into a pass, or deletes an inconvenient one, the
links break and the system tells you exactly which entry was touched.

We are careful about what we claim here. This catches editing, deleting and
reordering. It would not stop a determined expert with full access to the file
who rebuilds every link afterwards. The tool prints that limitation every time
it runs, because overstating it would be worse than not having it.

## The safety thinking, in plain terms

A few decisions worth knowing, because they are the difference between a demo
and something you would actually trust:

**The lock defaults to shut.** Power cut, computer crash, cable pulled out —
all of them end with the cabinet locked. Being locked is the resting state;
opening is the exception that has to be earned.

**If the computer goes quiet, the lock closes.** The lock controller waits two
seconds. No word from the computer in that time, and it shuts, rather than
holding whatever it was last told. Because the last thing it was told was
about a kit nobody is watching any more.

**We never assume a command arrived.** The lock controller confirms every
instruction. If no confirmation comes back, the record says the instruction
did not happen — rather than assuming it did.

**The screen never lies about the lock.** It shows what the lock is doing
right now, not what it was told to do a few seconds ago. Somebody reads that
screen before putting their hand in a cabinet.

## What is real and what is not

Being straight about this is part of the point.

**Real and working:**
- The whole decision-making pipeline, with 267 automated tests
- The comparison against the original design, on real model output
- Expiry date checking, quantity checking, the tamper-evident record
- The screen, the simulator, the lock controller's logic

**Not yet done:**
- **It has never run on the actual hardware.** We do not have the Snapdragon
  computer or the Arduino board yet — they are provided at the event. There is
  a checklist ready for the moment we do.
- Counting small identical objects is the model's weakest skill, so kits with
  quantities need more looks per check.
- The record is tamper-evident, not tamper-proof.
- It is one station. There is no fleet dashboard.

## The one-sentence version

> A camera checks that an emergency kit is complete, undamaged and in date,
> decides entirely on the device with no internet, physically locks the
> cabinet if anything is wrong — and treats "I could not tell" as a reason to
> stay locked, which is the thing almost everyone else gets backwards.
