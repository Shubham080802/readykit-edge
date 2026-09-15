"""Inference engines.

An engine takes a Frame and a Manifest and returns Sightings. It never returns
a Verdict - deciding is `resolve_verdict`'s job alone, so that the safety rule
lives in exactly one place and every engine is held to it.
"""

from .base import InferenceEngine, InferenceError, Observation
from .simulated import SimulatedEngine

__all__ = [
    "InferenceEngine",
    "InferenceError",
    "Observation",
    "SimulatedEngine",
    "load_engine",
]


def load_engine(name: str, **kwargs: object) -> InferenceEngine:
    """Resolve an engine by name.

    GenieX is imported lazily: the Qualcomm SDK only exists on the Snapdragon
    host, and the simulated path must work without it.
    """
    if name == "simulated":
        return SimulatedEngine(**kwargs)  # type: ignore[arg-type]
    if name == "geniex":
        from .geniex import GenieXEngine

        return GenieXEngine(**kwargs)  # type: ignore[arg-type]
    if name == "ollama":
        from .ollama import OllamaEngine

        return OllamaEngine(**kwargs)  # type: ignore[arg-type]
    raise InferenceError(
        f"unknown engine {name!r}; expected 'geniex', 'ollama' or 'simulated'"
    )
