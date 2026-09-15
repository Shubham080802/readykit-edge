"""The real Host Link, over USB serial to the Arduino UNO Q.

A note on which core you are actually talking to: the UNO Q's USB-C port is
owned by the QRB2210 running Debian, not by the STM32U585 that drives the
pins. Depending on how you have the board set up, `--port` is either the
Linux-side bridge that forwards to the MCU, or - if you are driving the MCU
directly over its own UART - that UART. The protocol is identical either way;
only the device path changes. See docs/deployment.md.

pyserial is imported at construction rather than module load so the simulated
path runs without the host extras installed.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from ..protocol import (
    Command,
    CommandFrame,
    ProtocolError,
    decode_ack,
    encode_command,
)
from .base import HostLink, LinkError, LinkResult


class SerialLink(HostLink):
    def __init__(
        self,
        port: str,
        baud_rate: int = 115200,
        ack_timeout: float = 1.0,
        settle_seconds: float = 2.0,
        retries: int = 2,
    ) -> None:
        try:
            import serial
        except ImportError as exc:
            raise LinkError(
                "pyserial is not installed. Install the host extras: "
                'pip install -e ".[host]"'
            ) from exc

        try:
            self._port: Any = serial.Serial(port, baud_rate, timeout=ack_timeout)
        except Exception as exc:
            raise LinkError(f"could not open {port} at {baud_rate} baud: {exc}") from exc

        # Opening the port resets the MCU on most Arduino boards. Anything
        # written during the bootloader window is lost, so wait it out.
        time.sleep(settle_seconds)
        self._port.reset_input_buffer()
        self._port.reset_output_buffer()

        self._seq = 0
        self._retries = max(0, retries)

    def send(self, command: Command, payload: str = "") -> LinkResult:
        self._seq = (self._seq + 1) % 65536
        seq = self._seq

        try:
            wire = encode_command(
                CommandFrame(seq=seq, command=command, payload=payload)
            )
        except ProtocolError as exc:
            return LinkResult(command=command, seq=seq, ack=None, error=str(exc))

        # max(0, retries) guarantees at least one pass, so this is always set.
        result = LinkResult(
            command=command, seq=seq, ack=None, error="no attempt made"
        )
        for attempt in range(self._retries + 1):
            # A retry reuses the same sequence number on purpose: the Actuator
            # Node refuses a repeat, so a RELEASE that actually landed but
            # whose ACK was lost cannot be applied twice.
            result = self._attempt(command, seq, wire)
            if result.acknowledged:
                return result
            if attempt < self._retries:
                time.sleep(0.05)

        # The last attempt's result, rather than a fresh one carrying only its
        # error text. A board that answered REFUSED is a different situation
        # from a board that said nothing at all, and that difference is
        # structural - callers should not have to match on error strings to
        # recover it.
        return result

    def _attempt(self, command: Command, seq: int, wire: bytes) -> LinkResult:
        try:
            self._port.reset_input_buffer()
            self._port.write(wire)
            self._port.flush()
            raw = self._port.readline()
        except Exception as exc:
            return LinkResult(
                command=command, seq=seq, ack=None, error=f"serial I/O failed: {exc}"
            )

        if not raw:
            return LinkResult(
                command=command,
                seq=seq,
                ack=None,
                error="actuator node did not acknowledge within the timeout",
            )

        try:
            ack = decode_ack(raw)
        except ProtocolError as exc:
            return LinkResult(
                command=command, seq=seq, ack=None, error=f"unreadable ack: {exc}"
            )

        if ack.seq != seq:
            return LinkResult(
                command=command,
                seq=seq,
                ack=None,
                error=f"ack was for seq {ack.seq}, expected {seq}",
            )
        if ack.status.value != "OK":
            return LinkResult(
                command=command,
                seq=seq,
                ack=ack,
                error=f"actuator node returned {ack.status.value}",
            )
        return LinkResult(command=command, seq=seq, ack=ack)

    def close(self) -> None:
        port = getattr(self, "_port", None)
        if port is not None and port.is_open:
            # Leave the enclosure in its safe state on the way out rather than
            # however the last inspection left it.
            with contextlib.suppress(Exception):
                self.send(Command.RESET)
            port.close()
