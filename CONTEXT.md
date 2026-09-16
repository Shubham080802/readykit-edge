# ReadyKit Edge

An air-gapped visual inspection system. A camera watches an equipment kit, a
vision-language model running on local hardware decides whether the kit is
complete and serviceable, and that decision drives a physical latch.

Nothing leaves the device. There is no cloud, no network call, and no remote
fallback - the decision and the actuation are both local.

## Language

### What is being inspected

**Manifest**:
The specification of what a compliant kit contains - the list of items that must
be present and serviceable, plus how strictly each is judged. A Manifest is
authored ahead of time and is the same for every kit of that type.
_Avoid_: Checklist, template, kit definition, schema

**Kit**:
The physical container in front of the camera during one inspection. A Kit is
judged against a Manifest; it never defines its own expectations.
_Avoid_: Box, tray, case, kit definition

**Required Item**:
One entry in a Manifest - a thing that must be found in the Kit. Carries the
label the model is asked about and whether its absence is disqualifying.
_Avoid_: Item, tool, component, part

**Sighting**:
What the model reports about one Required Item in one frame: found, absent,
damaged, or unreadable, with a confidence. A Sighting is an observation, never a
decision.
_Avoid_: Detection, result, match, observation

### Deciding

**Inspection**:
One complete pass - capture a frame, run inference against a Manifest, produce a
Verdict, and act on it. The unit of work and the unit of audit.
_Avoid_: Scan, check, run, cycle

**Verdict**:
The outcome of an Inspection. Exactly one of Pass, Fail, or Indeterminate. Only
Pass may release the Latch.
_Avoid_: Result, status, decision, outcome

**Indeterminate**:
The Verdict when the system could not establish compliance for a *critical*
Required Item - the frame was occluded, inference errored, confidence fell
below the Manifest's floor, or the model's reply could not be parsed. Distinct
from Fail, which is a positive finding that the Kit is non-compliant. Both keep
the Latch engaged; only Indeterminate means "ask again", and it is never a
reason to unlock.

Unestablished evidence about an *advisory* item is not Indeterminate. The
Manifest has already said the Kit is serviceable without that item, so doubt
about it cannot outrank certainty about it: it is recorded as an advisory and
the Kit's fate turns on the critical items.
_Avoid_: Unknown, error, null result, inconclusive

**Expiry Check**:
A Required Item whose printed use-by date must be read off the packaging and
judged, not just its presence confirmed. Presence is not serviceability - a
sealed, undamaged, correctly-placed packet of expired gauze satisfies every
visual check and is still not serviceable.
_Avoid_: Date check, freshness, shelf life

**Expired**:
A Required Item whose printed use-by date has passed. A positive finding of
non-compliance, exactly like an absent item, and distinct from Damaged: the
item is intact, it is simply out of date. Use-by dates are inclusive, so an
item expiring today is still serviceable today.
_Avoid_: Stale, out of date, lapsed, invalid

**Expiring Soon**:
In date today, but falling due within the Manifest's warning window. An
advisory - the Kit passes, and whoever restocks it is told.
_Avoid_: Nearly expired, warning, amber

**Count**:
How many of a Required Item the model reports seeing. None is not one - it is
"no quantity was established", which for an item the Manifest requires more
than one of is unresolved, exactly like an unread use-by date.
_Avoid_: Quantity (that is the requirement), number, tally

**Short**:
A Required Item that is present, undamaged and in date, but fewer than the
Manifest requires. A positive finding of non-compliance, reported apart from
Missing because the remedy differs - top up rather than replace.
_Avoid_: Incomplete, understocked, partial, low

**Confidence Floor**:
The per-Manifest threshold a Sighting must clear to count as evidence. A
Sighting below the floor contributes Indeterminate, not Fail.
_Avoid_: Threshold, cutoff, minimum score

**Inspection Record**:
The durable, append-only account of one Inspection - the Manifest used, every
Sighting, the Verdict, the reason, and what the hardware was commanded to do.
This is what an auditor reads after the fact.
_Avoid_: Log entry, history, audit log, event

### Acting

**Latch**:
The physical lock on the enclosure. Engaged is the safe resting state; Released
is the exception, granted only by a Pass and only for the Manifest's stated
hold. Loss of power, loss of the Host Link, or a firmware restart all leave it
Engaged.
_Avoid_: Lock, solenoid, bolt, door

**Command**:
A single instruction sent from the Inspection Host to the Actuator Node -
release the Latch, signal a failure, and so on. Commands are framed and
acknowledged; a Command that is not acknowledged did not happen.
_Avoid_: Signal code, trigger code, message, packet

### The two nodes

**Inspection Host**:
The machine that sees and decides - the camera, the model, and the Verdict live
here. Snapdragon X Elite in the field; any machine in simulation.
_Avoid_: Node A, Snapdragon, server, master

**Actuator Node**:
The machine that acts - it drives the Latch, indicators, and buzzer, and it
trusts nothing it has not been told in an acknowledged Command. Arduino UNO Q in
the field; a virtual device in simulation.
_Avoid_: Node B, Arduino, MCU, slave, client

**Host Link**:
The serial connection between the Inspection Host and the Actuator Node. It is
assumed unreliable: the Actuator Node treats silence as a fault and falls back
to the safe resting state rather than holding its last instruction.
_Avoid_: Serial bridge, connection, channel, pipe
