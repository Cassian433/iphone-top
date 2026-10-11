"""Record test fixtures and a replayable recording from a connected device.

Everything here is read-only DVT sampling plus lockdown/diagnostics queries. The output keeps only
Apple system processes (selected by executable path and bundle id), drops any other process, and
replaces the device name with "iPhone", so a recording can be committed without describing what is
installed on the phone it came from.

    uv run python scripts/record_fixtures.py --udid UDID --out tests/fixtures --samples 8
    uv run python scripts/record_fixtures.py --udid UDID --recording-only --samples 40 --out /tmp/demo
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.graphics import Graphics
from pymobiledevice3.services.dvt.instruments.network_statistics import NetworkStatistics
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap

from iphone_top.cli import _quiet_logging
from iphone_top.connect import Connection, connect
from iphone_top.model import PROCESS_ATTRIBUTES, SYSTEM_ATTRIBUTES, DeviceHeader, header_to_dict, sample_to_dict
from iphone_top.sampler import LiveSampler

SYSTEM_PREFIXES = ("/System/", "/usr/", "/sbin/", "/bin/", "/Developer/")
# The instruments server leaves itself out of runningProcesses, so it can't be checked by path.
UNLISTED_APPLE = frozenset({"DTServiceHub"})
IO_POWER_SOURCE_KEYS = (
    "CurrentCapacity",
    "MaxCapacity",
    "IsCharging",
    "ExternalConnected",
    "FullyCharged",
    "Temperature",
    "Voltage",
)


def is_apple_system_process(process: dict[str, Any]) -> bool:
    if process.get("pid") == 0:
        return True
    path = str(process.get("realAppName") or "")
    bundle = process.get("bundleIdentifier")
    apple_bundle = isinstance(bundle, str) and bundle.startswith("com.apple.")
    if path.startswith("/Applications/"):
        return apple_bundle
    return path.startswith(SYSTEM_PREFIXES) and (bundle is None or apple_bundle)


async def allowed_pids(dvt: DvtProvider) -> tuple[set[int], list[dict[str, Any]]]:
    async with DeviceInfo(dvt) as info:
        processes = await info.proclist()
    keep = [p for p in processes if is_apple_system_process(p)]
    return {int(p["pid"]) for p in keep}, keep


async def record_raw(connection: Connection, out: Path, rows_wanted: int) -> set[str]:
    names: set[str] = set()
    async with DvtProvider(connection.dvt) as dvt:
        allowed, processes = await allowed_pids(dvt)
        names.update(str(p.get("name")) for p in processes)
        async with DeviceInfo(dvt) as info:
            supported_process = sorted(await info.sysmon_process_attributes())
            supported_system = sorted(await info.sysmon_system_attributes())
            mach_time = list(await info.mach_time_info())[:3]
        device_info = {
            "process_attributes_supported": supported_process,
            "system_attributes_supported": supported_system,
            "mach_time_info": mach_time,
            "proclist": [
                {
                    "pid": p["pid"],
                    "name": p.get("name"),
                    "isApplication": bool(p.get("isApplication")),
                    "startDate": p["startDate"].timestamp() if hasattr(p.get("startDate"), "timestamp") else None,
                }
                for p in processes
            ],
        }
        _write(out / "device_info.json", device_info)

        process_attributes = [a for a in PROCESS_ATTRIBUTES if a in supported_process]
        system_attributes = [a for a in SYSTEM_ATTRIBUTES if a in supported_system]
        rows: list[dict[str, Any]] = []
        async with Sysmontap(dvt, process_attributes, system_attributes, interval_ms=1000) as sysmon:
            async for row in sysmon:
                if not isinstance(row, dict):
                    continue
                row = dict(row)
                if "Processes" in row:
                    name_index = process_attributes.index("name")
                    for pid, values in row["Processes"].items():
                        if values[name_index] in UNLISTED_APPLE:
                            allowed.add(int(pid))
                    row["Processes"] = {str(pid): v for pid, v in row["Processes"].items() if int(pid) in allowed}
                rows.append(row)
                if len(rows) >= rows_wanted:
                    break
        _write(
            out / "sysmontap_rows.json",
            {"process_attributes": process_attributes, "system_attributes": system_attributes, "rows": rows},
            compact=True,
        )

        events: list[Any] = []
        async with Graphics(dvt) as graphics:
            async for event in graphics:
                events.append(event)
                if len(events) >= 4:
                    break
        _write(out / "graphics_events.json", events)

        samples: list[dict[int, dict[str, Any]]] = []
        async with NetworkStatistics(dvt, sorted(allowed)) as network:
            async for sample in network:
                samples.append({int(pid): record for pid, record in sample.items() if int(pid) in allowed})
                if len(samples) >= 3:
                    break
        # Keep every pid that moved bytes plus a handful of idle ones; 370 idle records add nothing.
        busy = {
            pid for sample in samples for pid, r in sample.items() if r.get("net.bytes.delta") or _moved(samples, pid)
        }
        idle = sorted(set(samples[0]).difference(busy))[:8]
        keep = busy.union(idle)
        _write(
            out / "network_samples.json",
            [
                {
                    str(pid): {k: (v.timestamp() if k == "time" else v) for k, v in record.items()}
                    for pid, record in sample.items()
                    if pid in keep
                }
                for sample in samples
            ],
        )

    lockdown_battery = None
    with contextlib.suppress(Exception):
        lockdown_battery = await connection.lockdown.get_value(domain="com.apple.mobile.battery")
    io_power_source = None
    with contextlib.suppress(Exception):
        async with DiagnosticsService(connection.lockdown) as diagnostics:
            raw = await diagnostics.get_battery() or {}
            io_power_source = {k: raw[k] for k in IO_POWER_SOURCE_KEYS if k in raw}
    _write(out / "battery.json", {"lockdown": lockdown_battery, "io_power_source": io_power_source})
    return names


async def record_samples(connection: Connection, path: Path, count: int) -> set[str]:
    names: set[str] = set()
    async with DvtProvider(connection.dvt) as dvt:
        allowed, _ = await allowed_pids(dvt)
        written = 0
        stream = LiveSampler(connection).stream()
        with path.open("w", encoding="utf-8") as out:
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if isinstance(event, DeviceHeader):
                        event.name = "iPhone"
                        out.write(json.dumps(header_to_dict(event)) + "\n")
                        continue
                    if written % 5 == 4:  # new processes appear all the time
                        allowed, _ = await allowed_pids(dvt)
                    event.processes = [p for p in event.processes if p.pid in allowed or p.name in UNLISTED_APPLE]
                    names.update(p.name for p in event.processes)
                    out.write(json.dumps(sample_to_dict(event), separators=(",", ":")) + "\n")
                    written += 1
                    if written >= count:
                        break
    return names


def _moved(samples: list[dict[int, dict[str, Any]]], pid: int) -> bool:
    totals = {sample[pid].get("net.bytes") for sample in samples if pid in sample}
    return len(totals) > 1


def _write(path: Path, data: Any, compact: bool = False) -> None:
    text = json.dumps(data, separators=(",", ":"), default=str) if compact else json.dumps(data, indent=1, default=str)
    path.write_text(text + "\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--udid")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--recording-only", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    _quiet_logging()
    async with connect(udid=args.udid) as connection:
        names: set[str] = set()
        if not args.recording_only:
            names |= await record_raw(connection, args.out, rows_wanted=8)
        names |= await record_samples(connection, args.out / "recording.ndjson", args.samples)
    print(f"{len(names)} process names kept:")
    print(", ".join(sorted(names)))


if __name__ == "__main__":
    asyncio.run(main())
