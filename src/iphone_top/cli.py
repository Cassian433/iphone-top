"""Command line entry point: ``iphone-top`` (TUI), ``iphone-top --json`` (NDJSON), ``--replay FILE``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from iphone_top import __version__

EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iphone-top",
        description="htop for a USB-connected iPhone: live CPU, memory, FPS, GPU and per-process network.",
        epilog=(
            "iOS 16 and older need Developer Mode on and the Developer Disk Image mounted "
            "(pymobiledevice3 mounter auto-mount). iOS 17.4+ opens a no-root tunnel by itself; "
            "iOS 17.0-17.3 on Linux/Windows needs `sudo pymobiledevice3 remote tunneld` and --tunnel ''."
        ),
    )
    parser.add_argument(
        "--udid",
        default=os.environ.get("PYMOBILEDEVICE3_UDID"),
        help="device to watch when several are plugged in (default: $PYMOBILEDEVICE3_UDID, or the only one)",
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--rsd",
        nargs=2,
        metavar=("HOST", "PORT"),
        help="iOS 17+: RemoteServiceDiscovery address printed by `pymobiledevice3 remote start-tunnel`",
    )
    transport.add_argument(
        "--tunnel",
        nargs="?",
        const="",
        metavar="UDID",
        help="iOS 17+: use a device from a running `pymobiledevice3 remote tunneld` ('' = the only one)",
    )
    parser.add_argument(
        "-i", "--interval", type=int, default=1000, metavar="MS", help="sampling interval in ms (default 1000)"
    )
    parser.add_argument("--no-net", action="store_true", help="skip per-process network sampling")
    parser.add_argument("--json", action="store_true", help="print NDJSON samples instead of the TUI")
    parser.add_argument("-n", "--count", type=int, metavar="N", help="with --json: stop after N samples")
    parser.add_argument("--replay", type=Path, metavar="FILE", help="play back a --json recording in the TUI")
    parser.add_argument("--speed", type=float, default=1.0, help="with --replay: playback speed (default 1.0)")
    parser.add_argument("--version", action="version", version=f"iphone-top {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 100:
        print("iphone-top: --interval must be at least 100 ms", file=sys.stderr)
        return 2
    _quiet_logging()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


async def _run(args: argparse.Namespace) -> int:
    from iphone_top.connect import explain

    try:
        if args.replay is not None:
            from iphone_top.sampler import replay_stream

            return await _present(args, replay_stream(args.replay, speed=args.speed), transport="replay")

        from iphone_top.connect import connect
        from iphone_top.sampler import LiveSampler

        rsd = (args.rsd[0], int(args.rsd[1])) if args.rsd else None
        async with connect(udid=args.udid, rsd=rsd, tunnel=args.tunnel) as connection:
            sampler = LiveSampler(connection, interval_ms=args.interval, network=not args.no_net)
            return await _present(args, sampler.stream(), transport=connection.transport)
    except Exception as e:
        message = explain(e)
        if message is None:
            raise
        print(f"iphone-top: {message}", file=sys.stderr)
        return EXIT_ERROR


async def _present(args: argparse.Namespace, events: AsyncIterator[object], transport: str) -> int:
    if args.json:
        return await _print_json(events, args.count)
    from iphone_top.app import IPhoneTop

    app = IPhoneTop(events, transport=transport)
    await app.run_async()
    if app.error is not None:
        raise app.error
    return app.return_code or 0


async def _print_json(events: AsyncIterator[object], count: int | None) -> int:
    from iphone_top.model import DeviceHeader, Sample, header_to_dict, sample_to_dict

    printed = 0
    async with contextlib.aclosing(events) as stream:  # pyright: ignore[reportArgumentType]
        async for event in stream:
            if isinstance(event, DeviceHeader):
                line = header_to_dict(event)
            elif isinstance(event, Sample):
                line = sample_to_dict(event)
                line["processes"].sort(key=lambda p: -(p["cpu"] or 0))
                printed += 1
            else:
                continue
            try:
                sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:  # `iphone-top --json | head`
                with contextlib.suppress(OSError):
                    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                return 0
            if count is not None and printed >= count:
                break
    return 0


def _quiet_logging() -> None:
    # pymobiledevice3 logs failures (with tracebacks) through logging; on a TUI that paints over the
    # screen, and the errors that matter are explained in plain words anyway.
    logging.getLogger().addHandler(logging.NullHandler())
    level = logging.DEBUG if os.environ.get("IPHONE_TOP_DEBUG") else logging.CRITICAL
    logging.getLogger("pymobiledevice3").setLevel(level)
    if os.environ.get("IPHONE_TOP_DEBUG"):
        logging.basicConfig(filename=os.environ["IPHONE_TOP_DEBUG"], level=logging.DEBUG)


if __name__ == "__main__":
    sys.exit(main())
