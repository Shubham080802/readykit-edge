"""The console is not allowed onto a routable interface.

This is the smallest test in the repository and one of the more important
ones. The console can trigger an inspection, and an inspection can release the
latch, so the bind address is not a convenience setting - it is the difference
between a local view and a remote unlock.

Deliberately imports nothing from the console extras: the refusal has to work
on a machine where the console was never installed, and has to happen before
anything is loaded or bound.
"""

from __future__ import annotations

import pytest

from readykit.cli import LOOPBACK_HOSTS, _loopback_refusal


class TestLoopbackIsAllowed:
    @pytest.mark.parametrize("host", LOOPBACK_HOSTS)
    def test_every_loopback_spelling_passes(self, host: str) -> None:
        assert _loopback_refusal(host, 8420) is None

    def test_the_default_is_one_of_them(self) -> None:
        """A default that is not itself allowed would refuse on a bare run."""
        assert "127.0.0.1" in LOOPBACK_HOSTS


class TestEverythingElseIsRefused:
    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",          # the classic one-keystroke mistake
            "::",
            "192.168.1.40",
            "10.0.0.5",
            "example.local",
            "0.0.0.0 ",         # not normalised into the allow-list
            "127.0.0.1.evil.com",
            "127.0.0.2",        # loopback /8, but not a spelling we accept
        ],
    )
    def test_routable_hosts_are_refused(self, host: str) -> None:
        refusal = _loopback_refusal(host, 8420)
        assert refusal is not None, host

    def test_the_refusal_says_why(self) -> None:
        """Someone who hits this at 2am needs to know it is deliberate and
        what the actual risk is, not just that a flag was rejected."""
        refusal = _loopback_refusal("0.0.0.0", 8420)
        assert refusal is not None
        assert "0.0.0.0" in refusal
        assert "latch" in refusal
        assert "8420" in refusal
        assert "127.0.0.1" in refusal

    def test_it_is_not_merely_a_warning(self) -> None:
        """The whole point of the change. If this ever returns None again for
        a routable host, the console is serving an actuator to the network."""
        assert _loopback_refusal("0.0.0.0", 8420) is not None
