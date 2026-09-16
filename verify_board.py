"""Bring-up verification against the real Actuator Node over the Host Link.

Covers every item in the deployment.md bring-up checklist that is observable
over the wire. Latch pin state and LED colour are not - the firmware answers
with ACKs and nothing else - so those stay eyeball checks and are printed as
prompts rather than asserted.
"""
import sys
import time

sys.path.insert(0, "src")

import serial

from readykit.protocol import (
    Command,
    CommandFrame,
    crc8,
    decode_ack,
    encode_command,
)

PORT = "COM4"
BAUD = 115200

passed = 0
failed = 0


def check(label: str, got, want) -> None:
    global passed, failed
    ok = got == want
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}\n          expected {want!r}\n          got      {got!r}")


def exchange(port: serial.Serial, wire: bytes) -> str:
    port.reset_input_buffer()
    port.write(wire)
    port.flush()
    raw = port.readline()
    if not raw:
        return "<no reply>"
    try:
        ack = decode_ack(raw)
    except Exception as exc:
        return f"<unparseable: {exc}: {raw!r}>"
    return f"{ack.seq} {ack.status.value}"


def framed(seq: int, command: Command, payload: str = "") -> bytes:
    return encode_command(CommandFrame(seq=seq, command=command, payload=payload))


port = serial.Serial(PORT, BAUD, timeout=1.0)
time.sleep(2.0)  # the MCU resets when the port opens
port.reset_input_buffer()
port.reset_output_buffer()

print(f"\nActuator Node on {PORT} @ {BAUD}\n")

print("Commands")
check("PING acknowledged", exchange(port, framed(1, Command.PING)), "1 OK")
check("REJECT acknowledged",
      exchange(port, framed(2, Command.REJECT, "test fail")), "2 OK")
check("HOLD acknowledged", exchange(port, framed(3, Command.HOLD, "cannot see")), "3 OK")
check("RESET acknowledged", exchange(port, framed(4, Command.RESET)), "4 OK")
check("RELEASE acknowledged", exchange(port, framed(5, Command.RELEASE, "5000")), "5 OK")

print("\nIntegrity")
# The original design's unframed command, sent by hand.
check("unframed PASS_KIT refused", exchange(port, b"PASS_KIT\n"), "0 BAD_FRAME")

# A valid frame with one payload byte altered after the checksum was computed.
good = framed(10, Command.RELEASE, "5000")
corrupted = good.replace(b"5000", b"6000")
check("altered payload refused", exchange(port, corrupted), "0 BAD_CRC")

# Correct CRC, unknown verb.
body = "RK1 11 LAUNCH "
wire = f"{body} {crc8(body.encode('ascii')):02X}\n".encode("ascii")
check("unknown command refused", exchange(port, wire), "0 BAD_FRAME")

# Correct CRC, wrong protocol version.
body = "RK2 12 PING "
wire = f"{body} {crc8(body.encode('ascii')):02X}\n".encode("ascii")
check("wrong version refused", exchange(port, wire), "0 BAD_FRAME")

check("line noise refused", exchange(port, b"\x01\x02rubbish\n"), "0 BAD_FRAME")

# Replay: the same sequence number twice in a row.
exchange(port, framed(20, Command.PING))
check("replayed sequence refused", exchange(port, framed(20, Command.PING)), "20 REFUSED")

# A hold longer than the firmware's ceiling.
check(
    "over-long hold refused",
    exchange(port, framed(21, Command.RELEASE, "99999")),
    "21 REFUSED",
)
check(
    "zero hold refused",
    exchange(port, framed(22, Command.RELEASE, "0")),
    "22 REFUSED",
)

print("\nWatchdog")
print("  (LINK_TIMEOUT_MS is 2000 in the firmware)")
exchange(port, framed(30, Command.RELEASE, "10000"))
print("  RELEASE sent with a 10s hold, now going silent for 3s...")
time.sleep(3.0)
check("board still answers after the link went stale",
      exchange(port, framed(31, Command.PING)), "31 OK")
print("  -> the latch should have re-engaged on its own before this PING,")
print("     because 3s of silence exceeds the 2s link timeout.")

print("\nLeaving the enclosure locked")
check("final RESET acknowledged", exchange(port, framed(99, Command.RESET)), "99 OK")
port.close()

print(f"\n  {passed} passed, {failed} failed\n")
print("Not checkable over the wire - watch the board itself:")
print("  - latch LED red at rest, green only while a hold is live")
print("  - verdict LED green on PASS, red fast blink on FAIL,")
print("    amber slow pulse on INDETERMINATE, blue wig-wag when the host is gone")
sys.exit(1 if failed else 0)
