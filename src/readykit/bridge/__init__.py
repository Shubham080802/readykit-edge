"""Host Link transports.

The link is assumed unreliable. Every Command is framed, checksummed, and
acknowledged, and an unacknowledged Command is reported as not having
happened - never optimistically assumed to have landed.
"""

from .base import HostLink, LinkError, LinkResult
from .loopback import LoopbackLink, VirtualActuatorNode

__all__ = [
    "HostLink",
    "LinkError",
    "LinkResult",
    "LoopbackLink",
    "VirtualActuatorNode",
    "open_link",
]


def open_link(kind: str, **kwargs: object) -> HostLink:
    """Resolve a transport by name. pyserial is imported lazily."""
    if kind == "loopback":
        return LoopbackLink(**kwargs)  # type: ignore[arg-type]
    if kind == "serial":
        from .serial_link import SerialLink

        return SerialLink(**kwargs)  # type: ignore[arg-type]
    raise LinkError(f"unknown link {kind!r}; expected 'serial' or 'loopback'")
