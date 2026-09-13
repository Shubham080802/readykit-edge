"""Command line entry point.

    readykit inspect --manifest manifests/trauma-kit-a.json --scene complete
    readykit watch   --manifest manifests/trauma-kit-a.json --port /dev/ttyACM0
    readykit scenes
    readykit records --log records/inspections.jsonl
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .bridge import open_link
from .bridge.base import HostLink, LinkError
from .capture import CameraSource, CaptureError, FrameSource, ScriptedSource
from .domain import Manifest, Verdict
from .engine import InspectionEngine, InspectionOutcome
from .inference import load_engine
from .inference.base import InferenceEngine, InferenceError
from .inference.simulated import SimulatedEngine
from .naive import Divergence
from .recorder import InspectionLog

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

_VERDICT_STYLE = {
    Verdict.PASS: ("\033[38;5;41m", "PASS", "latch released"),
    Verdict.FAIL: ("\033[38;5;203m", "FAIL", "latch engaged"),
    Verdict.INDETERMINATE: ("\033[38;5;221m", "INDETERMINATE", "latch engaged"),
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{RESET}", file=sys.stderr)
        return 130
    except (LinkError, InferenceError, CaptureError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="readykit",
        description="Air-gapped visual kit inspection with physical actuation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="run a single inspection")
    _add_pipeline_args(inspect)
    inspect.set_defaults(handler=_cmd_inspect)

    watch = sub.add_parser("watch", help="inspect continuously")
    _add_pipeline_args(watch)
    watch.add_argument(
        "--interval", type=float, default=3.0, help="seconds between inspections"
    )
    watch.add_argument(
        "--limit", type=int, default=0, help="stop after N inspections (0 = forever)"
    )
    watch.set_defaults(handler=_cmd_watch)

    scenes = sub.add_parser("scenes", help="list simulator scenes")
    scenes.set_defaults(handler=_cmd_scenes)

    console = sub.add_parser("console", help="serve the operator console")
    console.add_argument("--manifest", type=Path, required=True)
    console.add_argument("--host", default="127.0.0.1", help="loopback by default")
    console.add_argument("--port", type=int, default=8420)
    console.add_argument(
        "--log", type=Path, default=Path("records/inspections.jsonl")
    )
    console.set_defaults(handler=_cmd_console)

    compare_cmd = sub.add_parser(
        "compare", help="replay every scene against the original blueprint's logic"
    )
    compare_cmd.add_argument("--manifest", type=Path, required=True)
    compare_cmd.set_defaults(handler=_cmd_compare)

    records = sub.add_parser("records", help="show recent inspection records")
    records.add_argument("--log", type=Path, default=Path("records/inspections.jsonl"))
    records.add_argument("--limit", type=int, default=20)
    records.set_defaults(handler=_cmd_records)

    return parser


def _add_pipeline_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--engine",
        default="simulated",
        choices=("simulated", "geniex"),
        help="simulated runs anywhere; geniex needs the Qualcomm SDK",
    )
    parser.add_argument("--model", type=Path, help="path to the .qnn model (geniex)")
    parser.add_argument("--scene", default="complete", help="simulator scene")
    parser.add_argument("--camera", type=int, help="camera index (real capture)")
    parser.add_argument(
        "--link",
        default="loopback",
        choices=("loopback", "serial"),
        help="loopback drives a virtual actuator node in-process",
    )
    parser.add_argument("--port", help="serial device, e.g. /dev/ttyACM0 or COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--log", type=Path, default=Path("records/inspections.jsonl")
    )
    parser.add_argument("--no-log", action="store_true")


def _cmd_inspect(args: argparse.Namespace) -> int:
    with _build_engine(args) as engine:
        outcome = engine.run_once()
        _render(outcome, engine)
        _record(args, outcome)
        return 0 if outcome.verdict is Verdict.PASS else 2


def _cmd_watch(args: argparse.Namespace) -> int:
    completed = 0
    with _build_engine(args) as engine:
        while args.limit == 0 or completed < args.limit:
            outcome = engine.run_once()
            _render(outcome, engine)
            _record(args, outcome)
            completed += 1

            if args.limit and completed >= args.limit:
                break
            _sleep_with_heartbeats(engine, args.interval)
    return 0


def _sleep_with_heartbeats(engine: InspectionEngine, seconds: float) -> None:
    """Idle between inspections without letting the watchdog trip.

    The Actuator Node engages the latch when the link goes quiet, so a plain
    sleep here would cut every hold short and strand the node in its STALE
    indicator between inspections.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))
        engine.heartbeat()


def _cmd_console(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        raise ValueError(
            "the console needs the console extras: pip install -e \".[console]\""
        ) from None

    from .console import create_app

    manifest = _load_manifest(args.manifest)
    app = create_app(
        manifest=manifest, log_path=args.log, link=open_link("loopback")
    )

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # This device holds a latch open on command. Binding it to a routable
        # interface turns a local view into a remote actuator.
        print(
            f"  warning: binding to {args.host} exposes the inspect endpoint "
            "beyond this machine",
            file=sys.stderr,
        )

    print(f"\n  {BOLD}ReadyKit Edge console{RESET}  {DIM}{manifest.name}{RESET}")
    print(f"  {DIM}http://{args.host}:{args.port}{RESET}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_scenes(args: argparse.Namespace) -> int:
    print(f"{BOLD}Simulator scenes{RESET}\n")
    for name, description in sorted(SimulatedEngine.scenes().items()):
        print(f"  {name:<16} {DIM}{description}{RESET}")
    print(
        f"\n  {'missing-<key>':<16} {DIM}knock out one item, e.g. missing-shears{RESET}"
    )
    print(f"  {'damaged-<key>':<16} {DIM}mark one item damaged{RESET}")
    return 0


def _cmd_records(args: argparse.Namespace) -> int:
    log = InspectionLog(args.log)
    rows = log.read(limit=args.limit)
    if not rows:
        print(f"{DIM}no records in {args.log}{RESET}")
        return 0

    tally = log.tally()
    print(
        f"{BOLD}{sum(tally.values())} inspections{RESET}  "
        f"{DIM}pass {tally['pass']} · fail {tally['fail']} · "
        f"indeterminate {tally['indeterminate']}{RESET}\n"
    )
    for row in rows:
        verdict = Verdict(row["verdict"])
        colour, label, _ = _VERDICT_STYLE[verdict]
        stamp = str(row.get("started_at", ""))[:19].replace("T", " ")
        print(f"  {DIM}{stamp}{RESET}  {colour}{label:<14}{RESET} {row['reason']}")
    return 0


def _build_engine(args: argparse.Namespace) -> InspectionEngine:
    manifest = _load_manifest(args.manifest)
    return InspectionEngine(
        manifest=manifest,
        source=_build_source(args),
        engine=_build_inference(args),
        link=_build_link(args),
    )


def _load_manifest(path: Path) -> Manifest:
    try:
        return Manifest.from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read manifest {path}: {exc}") from exc


def _build_source(args: argparse.Namespace) -> FrameSource:
    if args.camera is not None:
        return CameraSource(index=args.camera)
    if args.engine == "geniex":
        raise ValueError(
            "the geniex engine needs real frames - pass --camera 0 "
            "(or use --engine simulated with --scene)"
        )
    return ScriptedSource(args.scene)


def _build_inference(args: argparse.Namespace) -> InferenceEngine:
    if args.engine == "geniex":
        if args.model is None:
            raise ValueError("--engine geniex requires --model path/to/model.qnn")
        return load_engine("geniex", model_path=args.model)
    return load_engine("simulated")


def _build_link(args: argparse.Namespace) -> HostLink:
    if args.link == "serial":
        if not args.port:
            raise ValueError("--link serial requires --port, e.g. --port /dev/ttyACM0")
        return open_link("serial", port=args.port, baud_rate=args.baud)
    return open_link("loopback")


def _record(args: argparse.Namespace, outcome: InspectionOutcome) -> None:
    if not args.no_log:
        InspectionLog(args.log).append(outcome.record)


def _render(outcome: InspectionOutcome, engine: InspectionEngine) -> None:
    resolution = outcome.record.resolution
    colour, label, latch = _VERDICT_STYLE[resolution.verdict]

    print(f"\n  {colour}{BOLD}{label}{RESET}  {DIM}{latch}{RESET}")
    print(f"  {resolution.reason}")

    expired = set(resolution.expired)
    soon = set(resolution.expiring_soon)

    for sighting in outcome.record.sightings:
        item = engine.manifest.item(sighting.key)
        name = item.label if item else sighting.key
        mark = {"found": "+", "absent": "-", "damaged": "!", "unreadable": "?"}[
            sighting.presence.value
        ]
        # An expired item is present and undamaged, so its presence glyph says
        # nothing is wrong. Override it, or the row reads as compliant.
        if sighting.key in expired:
            mark = "x"

        if item is not None and item.expiry_checked:
            if sighting.expiry is None:
                dated = f"  {DIM}exp unreadable{RESET}"
            elif sighting.key in expired:
                dated = f"  \033[38;5;203mEXPIRED {sighting.expiry}{RESET}"
            elif sighting.key in soon:
                dated = f"  \033[38;5;221mexpires {sighting.expiry}{RESET}"
            else:
                dated = f"  {DIM}exp {sighting.expiry}{RESET}"
        else:
            dated = ""

        note = f"  {DIM}{sighting.note}{RESET}" if sighting.note else ""
        print(
            f"    {mark} {name:<22} {DIM}{sighting.presence.value:<11}"
            f"{sighting.confidence:.2f}{RESET}{dated}{note}"
        )

    _render_comparison(outcome)

    if not outcome.enacted and outcome.link_result is not None:
        print(f"  {DIM}! {outcome.record.commanded}{RESET}")
    print(
        f"  {DIM}{outcome.record.engine} · {outcome.record.latency_ms:.0f}ms · "
        f"frame {outcome.record.frame_digest}{RESET}"
    )


def _render_comparison(outcome: InspectionOutcome) -> None:
    """Show what the original blueprint would have done with the same reply."""
    comparison = outcome.comparison
    if comparison is None:
        return

    if comparison.divergence is Divergence.UNSAFE:
        colour = "\033[38;5;203m"
        headline = "the original blueprint would have RELEASED the latch here"
    elif comparison.divergence is Divergence.SPURIOUS:
        colour = "\033[38;5;221m"
        headline = "the original blueprint would have rejected this kit"
    else:
        colour = DIM
        headline = "the original blueprint would have reached the same decision"

    print(f"  {colour}vs blueprint{RESET} {DIM}{headline}{RESET}")
    print(
        f"    {DIM}its parser saw{RESET} "
        f"{colour}{comparison.signal}{RESET}"
        f"{DIM} from: {_excerpt(outcome.record.raw_reply)}{RESET}"
    )


def _excerpt(raw: str, limit: int = 88) -> str:
    """First line of the model's reply - the part the substring matcher hits."""
    first = raw.strip().splitlines()[0] if raw.strip() else "(empty reply)"
    return first if len(first) <= limit else first[: limit - 1] + "\u2026"


def _cmd_compare(args: argparse.Namespace) -> int:
    """Replay every scene and tabulate where the two logics disagree."""
    manifest = _load_manifest(args.manifest)
    scenes = [*sorted(SimulatedEngine.scenes()), *(
        f"missing-{item.key}" for item in manifest.items
    )]

    rows: list[tuple[str, str, str, Divergence | None]] = []
    for scene in scenes:
        engine = InspectionEngine(
            manifest=manifest,
            source=ScriptedSource(scene),
            engine=load_engine("simulated"),
            link=None,
        )
        outcome = engine.run_once()
        rows.append(
            (
                scene,
                outcome.record.blueprint_signal or "(no output)",
                outcome.verdict.value,
                outcome.comparison.divergence if outcome.comparison else None,
            )
        )

    print(f"\n  {BOLD}{manifest.name}{RESET}  {DIM}{len(rows)} scenes{RESET}\n")
    print(
        f"  {DIM}{'scene':<24}{'blueprint':<20}{'readykit':<16}"
        f"{'divergence'}{RESET}"
    )
    print(f"  {DIM}{'-' * 74}{RESET}")

    unsafe = 0
    for scene, signal, verdict, divergence in rows:
        if divergence is Divergence.UNSAFE:
            unsafe += 1
            colour, label = "\033[38;5;203m", "UNLOCKS A BAD KIT"
        elif divergence is Divergence.SPURIOUS:
            colour, label = "\033[38;5;221m", "rejects a good kit"
        elif divergence is Divergence.AGREED:
            colour, label = DIM, "agreed"
        else:
            colour, label = DIM, "not comparable"
        print(
            f"  {scene:<24}{DIM}{signal:<20}{RESET}{verdict:<16}"
            f"{colour}{label}{RESET}"
        )

    print(
        f"\n  {BOLD}{unsafe} of {len(rows)}{RESET} scenes would have released "
        f"the latch under the original design.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
