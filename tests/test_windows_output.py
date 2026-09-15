"""Terminal output on the machine this actually ships to.

The target is a Windows ARM64 laptop, and the failure here is silent and
embarrassing rather than dangerous: ANSI escapes printed as literal text. It
happens three ways, none of which show up on a developer's Mac.

  - output redirected to a file or piped, where escapes become garbage in the
    artefact rather than colour on a screen
  - a Windows console host without virtual terminal processing enabled
  - NO_COLOR set, which is a convention worth honouring

The rule underneath all of it: **colour is never the only carrier of
meaning.** Every verdict is printed in words as well, so losing colour costs
legibility and never changes what the output says.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

import readykit.cli as cli


class FakeStdout:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class TestWhenColourIsSuppressed:
    def test_no_color_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(sys, "stdout", FakeStdout(tty=True))
        assert cli._colour_supported() is False

    def test_a_pipe_gets_no_escapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redirecting to a file must produce a readable file, not one full of
        escape sequences."""
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", FakeStdout(tty=False))
        assert cli._colour_supported() is False

    def test_a_real_terminal_gets_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", FakeStdout(tty=True))
        monkeypatch.setattr(os, "name", "posix")
        assert cli._colour_supported() is True


class TestOnWindows:
    def test_colour_depends_on_virtual_terminal_processing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", FakeStdout(tty=True))
        monkeypatch.setattr(os, "name", "nt")

        monkeypatch.setattr(cli, "_windows_vt_enabled", lambda: True)
        assert cli._colour_supported() is True

        monkeypatch.setattr(cli, "_windows_vt_enabled", lambda: False)
        assert cli._colour_supported() is False

    def test_a_console_that_refuses_is_handled_not_crashed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy console host, or output not attached to one at all. It has
        to degrade to plain text, never raise - this runs before any command
        does."""

        class Boom:
            def __getattr__(self, name: str) -> Any:
                raise OSError("no console")

        monkeypatch.setattr(cli, "ctypes", Boom(), raising=False)
        assert cli._windows_vt_enabled() is False

    def test_it_is_never_called_off_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ctypes.windll does not exist on macOS or Linux, so reaching this on
        those platforms would be an AttributeError at startup."""
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(sys, "stdout", FakeStdout(tty=True))
        monkeypatch.setattr(os, "name", "posix")

        called: list[int] = []

        def record() -> bool:
            called.append(1)
            return True

        monkeypatch.setattr(cli, "_windows_vt_enabled", record)
        cli._colour_supported()
        assert called == []


class TestMeaningSurvivesWithoutColour:
    def test_every_verdict_carries_a_word_not_only_a_colour(self) -> None:
        """Colour-blind readers, monochrome terminals, piped logs and
        photographs all lose the colour. None of them may lose the verdict."""
        for _, label, latch in cli._VERDICT_STYLE.values():
            assert label.strip()
            assert latch.strip()

    def test_the_labels_are_distinguishable_as_text(self) -> None:
        labels = {label for _, label, _ in cli._VERDICT_STYLE.values()}
        assert labels == {"PASS", "FAIL", "INDETERMINATE"}

    def test_the_latch_state_is_spelled_out_for_each(self) -> None:
        """The thing a person acts on is never implied by colour alone."""
        for _, label, latch in cli._VERDICT_STYLE.values():
            expected = "released" if label == "PASS" else "engaged"
            assert expected in latch
