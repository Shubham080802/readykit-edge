"""Proving the NPU is in use, and refusing to pretend when it cannot be.

The failure this guards against is the quiet one. GenieX `device_map="auto"`
falls back to the CPU without complaint; inference still works, answers still
look right, and every latency number taken afterwards is meaningless while
remaining perfectly plausible. On a bench in front of people whose question is
literally "is this on the NPU", that is the worst available outcome.

So the device is read back from the model rather than taken from the argument,
and `--require-npu` turns "cannot establish" into a refusal instead of a
shrug - the same three-state rule this project applies to kits.

Imports only the module, never the Qualcomm SDK, so these run off-device.
"""

from __future__ import annotations

from typing import Any

import pytest

from readykit.inference.base import InferenceError
from readykit.inference.geniex import (
    NPU_MARKERS,
    GenieXEngine,
    looks_like_npu,
    resolve_device,
)


class FakeModel:
    """Stands in for a loaded GenieX model reporting a device, or not."""

    def __init__(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            setattr(self, key, value)


class TestReadingBackTheDevice:
    @pytest.mark.parametrize(
        "attribute", ["device", "device_map", "compute_unit", "backend"]
    )
    def test_it_finds_the_device_wherever_the_build_puts_it(
        self, attribute: str
    ) -> None:
        model = FakeModel(**{attribute: "qnn-htp"})
        assert resolve_device(model) == "qnn-htp"

    def test_a_silent_build_reports_nothing(self) -> None:
        """Not an error, and emphatically not a guess. A third answer."""
        assert resolve_device(FakeModel()) is None

    def test_a_blank_string_counts_as_nothing(self) -> None:
        assert resolve_device(FakeModel(device="   ")) is None

    def test_a_non_string_counts_as_nothing(self) -> None:
        """Some builds expose a device object. Stringifying it would put a
        repr into the audit trail and call it evidence."""
        assert resolve_device(FakeModel(device=object())) is None

    def test_it_is_stripped(self) -> None:
        assert resolve_device(FakeModel(device="  htp  ")) == "htp"


class TestRecognisingAnNpu:
    @pytest.mark.parametrize(
        "device",
        ["htp", "QNN-HTP", "hexagon", "Hexagon-v73", "npu", "NPU0", "dsp"],
    )
    def test_npu_backends_are_recognised(self, device: str) -> None:
        assert looks_like_npu(device) is True

    @pytest.mark.parametrize("device", ["cpu", "CPU", "gpu", "adreno", "auto"])
    def test_everything_else_is_not(self, device: str) -> None:
        assert looks_like_npu(device) is False

    def test_auto_is_not_evidence(self) -> None:
        """The exact trap: 'auto' is what you asked for, never what you got."""
        assert looks_like_npu("auto") is False


class TestInsistingOnTheNpu:
    """`_insist_on_npu` alone, without loading a model."""

    def build(self, resolved: str | None, requested: str = "auto") -> Any:
        engine = GenieXEngine.__new__(GenieXEngine)
        engine.resolved_device = resolved
        engine.device_map = requested
        return engine

    def test_an_npu_passes(self) -> None:
        self.build("qnn-htp")._insist_on_npu()

    def test_a_cpu_is_refused(self) -> None:
        with pytest.raises(InferenceError, match="not an NPU backend"):
            self.build("cpu")._insist_on_npu()

    def test_an_unknown_device_is_refused(self) -> None:
        """Absence of evidence is not evidence of compliance - the same rule
        that governs an unreadable kit."""
        with pytest.raises(InferenceError, match="cannot be established"):
            self.build(None)._insist_on_npu()

    def test_the_refusal_names_the_way_out(self) -> None:
        with pytest.raises(InferenceError) as caught:
            self.build("cpu")._insist_on_npu()
        message = str(caught.value)
        assert "--device" in message
        assert "doctor" in message

    def test_the_unverifiable_refusal_points_at_bench(self) -> None:
        """When nothing can be read back, latency is the only real evidence
        left, so the error has to say so."""
        with pytest.raises(InferenceError) as caught:
            self.build(None)._insist_on_npu()
        assert "bench" in str(caught.value)


class TestTheAuditTrailDoesNotOverclaim:
    def test_markers_are_lowercase(self) -> None:
        """looks_like_npu lowercases its input, so a capitalised marker here
        would silently never match."""
        assert all(marker == marker.lower() for marker in NPU_MARKERS)
