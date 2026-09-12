"""The Host Link interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..protocol import Ack, Command


class LinkError(RuntimeError):
    """The Host Link could not be used.

    Upstream this means the Verdict could not be enacted. The Inspection
    Record says so explicitly rather than recording an intended command as a
    performed one.
    """


@dataclass(frozen=True, slots=True)
class LinkResult:
    """What actually happened when a Command was sent."""

    command: Command
    seq: int
    ack: Ack | None
    error: str = ""

    @property
    def acknowledged(self) -> bool:
        return self.ack is not None and self.error == ""

    def describe(self) -> str:
        """The string written into the Inspection Record's `commanded` field."""
        if self.acknowledged:
            assert self.ack is not None
            return f"{self.command.value} seq={self.seq} ack={self.ack.status.value}"
        return f"{self.command.value} seq={self.seq} NOT-ACKNOWLEDGED: {self.error}"


class HostLink(ABC):
    """A connection to the Actuator Node."""

    @abstractmethod
    def send(self, command: Command, payload: str = "") -> LinkResult:
        """Send one Command and wait for its acknowledgement.

        Never raises for a missing ACK - returns a LinkResult carrying the
        failure, so the caller records what really happened.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, not every link holds a resource
        """Release the port."""

    def __enter__(self) -> HostLink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
