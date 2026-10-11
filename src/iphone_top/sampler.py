"""Sample streams: live from a device over DVT, or replayed from an ``--json`` recording.

Both yield one :class:`DeviceHeader` first, then a :class:`Sample` per interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from pymobiledevice3.exceptions import ConnectionTerminatedError
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.graphics import Graphics
from pymobiledevice3.services.dvt.instruments.network_statistics import NetworkStatistics
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap

from iphone_top.connect import Connection, UserFacingError, diagnose_dvt_failure, is_dvt_start_failure
from iphone_top.model import (
    PROCESS_ATTRIBUTES,
    SYSTEM_ATTRIBUTES,
    Battery,
    CounterRate,
    DeviceHeader,
    NetworkRates,
    Sample,
    battery_from_lockdown,
    header_from_dict,
    model_name,
    parse_cpu,
    parse_graphics,
    parse_memory,
    parse_processes,
    row_values,
    sample_from_dict,
)

Event = DeviceHeader | Sample

logger = logging.getLogger(__name__)

BATTERY_POLL_SECONDS = 30
NAMES_REFRESH_SECONDS = 5
STOP_TIMEOUT_SECONDS = 2
# The network provider answers as fast as it is polled; more than twice a second is wasted traffic.
NETWORK_MIN_INTERVAL_MS = 500
# sysmontap sends heartbeats between samples, so this much silence means the transport is stuck.
STALL_SECONDS = 10


class LiveSampler:
    """Runs sysmontap, the graphics channel and per-process network statistics on one DVT connection."""

    def __init__(self, connection: Connection, interval_ms: int = 1000, network: bool = True) -> None:
        self.connection = connection
        self.interval_ms = interval_ms
        self.network = network
        self._fps: int | None = None
        self._gpu: int | None = None
        self._battery: Battery | None = None
        self._net = NetworkRates()
        self._pids: set[int] = set()
        self._names: dict[int, tuple[str, bool]] = {}
        self._unknown: set[int] = set()
        self._unlisted: set[int] = set()

    async def stream(self) -> AsyncGenerator[Event, None]:
        async with contextlib.AsyncExitStack() as stack:
            try:
                dvt = await stack.enter_async_context(DvtProvider(self.connection.dvt))
            except Exception as e:
                if is_dvt_start_failure(e):
                    raise UserFacingError(await diagnose_dvt_failure(self.connection.lockdown)) from e
                raise
            info = await stack.enter_async_context(DeviceInfo(dvt))
            process_attributes = await _supported(info.sysmon_process_attributes(), PROCESS_ATTRIBUTES)
            system_attributes = await _supported(info.sysmon_system_attributes(), SYSTEM_ATTRIBUTES)
            ticks_to_seconds = await _timebase(info)
            processes = await info.proclist()
            self._remember_names(processes)
            self._pids = {p["pid"] for p in processes}

            yield self._header(processes)

            tasks = [
                asyncio.create_task(self._graphics_loop(dvt)),
                asyncio.create_task(self._battery_loop()),
                asyncio.create_task(self._names_loop(info)),
            ]
            if self.network:
                tasks.append(asyncio.create_task(self._network_loop(dvt)))
            try:
                sysmon = Sysmontap(dvt, process_attributes, system_attributes, interval_ms=self.interval_ms)
                # Closed explicitly so sysmontap's stop goes out before the DVT connection closes.
                async with contextlib.aclosing(self._sysmon_samples(sysmon, ticks_to_seconds)) as samples:
                    async for sample in samples:
                        yield sample
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _header(self, processes: Sequence[dict[str, Any]]) -> DeviceHeader:
        values = self.connection.lockdown.all_values or {}
        product_type = str(values.get("ProductType") or getattr(self.connection.dvt, "product_type", "") or "")
        boot_time = None
        launchd = next((p for p in processes if p.get("pid") == 1), None)
        start = launchd.get("startDate") if launchd else None
        if isinstance(start, datetime.datetime):
            boot_time = start.timestamp()
        return DeviceHeader(
            name=str(values.get("DeviceName") or "iPhone"),
            model=model_name(product_type) if product_type else "iPhone",
            product_type=product_type,
            ios_version=self.connection.lockdown.product_version,
            build=str(values.get("BuildVersion") or ""),
            boot_time=boot_time,
        )

    async def _sysmon_samples(self, sysmon: Sysmontap, ticks_to_seconds: float | None) -> AsyncGenerator[Sample, None]:
        # sysmontap pushes two rows per interval: system-wide counters, then the process list.
        process_attributes: list[str] = list(sysmon.process_attributes_cls.__dataclass_fields__)
        system_attributes: list[str] = list(sysmon.system_attributes_cls.__dataclass_fields__)
        rates = {key: CounterRate() for key in ("diskBytesRead", "diskBytesWritten")}
        system_rows = process_rows = 0
        cpu = memory = None
        current: dict[str, float | None] = dict.fromkeys(rates)
        stall = max(STALL_SECONDS, 10 * self.interval_ms / 1000)
        async with sysmon, contextlib.aclosing(sysmon.__aiter__()) as rows:
            while True:
                try:
                    row = await asyncio.wait_for(rows.__anext__(), stall)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise ConnectionTerminatedError(f"no data from the phone for {stall:.0f} seconds") from None
                if not isinstance(row, dict):
                    continue  # heartbeats
                process_attributes = row.get("ProcessesAttributes", process_attributes)
                system_attributes = row.get("SystemAttributes", system_attributes)
                if "System" in row:
                    system_rows += 1
                    values = row_values(row["System"], system_attributes)
                    end = row.get("EndMachAbsTime")
                    when = end * ticks_to_seconds if ticks_to_seconds and isinstance(end, int) else time.monotonic()
                    for key, rate in rates.items():
                        current[key] = rate.update(values.get(key), when)
                    memory = parse_memory(values)
                    # The first CPU reading covers no full interval and is junk.
                    cpu = parse_cpu(row) if system_rows > 1 else None
                if "Processes" in row:
                    process_rows += 1
                    if process_rows == 1:
                        continue  # cpuUsage is None for every process in the first snapshot
                    processes = parse_processes(row, process_attributes)
                    self._enrich(processes)
                    yield Sample(
                        time=time.time(),
                        cpu=cpu,
                        memory=memory,
                        disk_read_rate=current["diskBytesRead"],
                        disk_write_rate=current["diskBytesWritten"],
                        fps=self._fps,
                        gpu=self._gpu,
                        battery=self._battery,
                        processes=processes,
                    )
        raise ConnectionTerminatedError("sysmontap stream ended")

    def _enrich(self, processes: list[Any]) -> None:
        pids = set()
        for process in processes:
            pids.add(process.pid)
            known = self._names.get(process.pid)
            if known is None:
                # DTServiceHub never shows up in the process list, so only ask about a pid once.
                if process.pid not in self._unlisted:
                    self._unknown.add(process.pid)
            else:
                full_name, is_app = known
                # sysmontap truncates names at 31 characters; the process list has them in full.
                if full_name.startswith(process.name):
                    process.name = full_name
                process.app = is_app
            rate = self._net.rates.get(process.pid)
            if rate is not None:
                process.rx_rate, process.tx_rate = rate
        self._pids = pids

    def _remember_names(self, processes: Sequence[dict[str, Any]]) -> None:
        self._names = {
            int(p["pid"]): (str(p.get("name") or ""), bool(p.get("isApplication"))) for p in processes if "pid" in p
        }

    async def _names_loop(self, info: DeviceInfo) -> None:
        while True:
            await asyncio.sleep(NAMES_REFRESH_SECONDS)
            # Short-lived processes come and go between refreshes; asking about those is wasted.
            self._unknown.intersection_update(self._pids)
            if self._unknown:
                await self._refresh_names(info)

    async def _refresh_names(self, info: DeviceInfo) -> None:
        # Only the pids unknown before the request can be judged by its answer; one that appears
        # while it is in flight waits for the next refresh.
        asked = set(self._unknown)
        try:
            processes = await info.proclist()
        except Exception:
            logger.debug("process list refresh failed", exc_info=True)
            return
        self._remember_names(processes)
        self._unknown.difference_update(asked)
        self._unlisted.update(pid for pid in asked if pid not in self._names)

    async def _graphics_loop(self, dvt: DvtProvider) -> None:
        try:
            async with Graphics(dvt) as graphics:
                first = True
                async for event in graphics:
                    parsed = parse_graphics(event)
                    if parsed is None:
                        continue
                    if first:  # covers less than a second
                        first = False
                        continue
                    self._fps, self._gpu = parsed
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("graphics channel failed", exc_info=True)
            self._fps = self._gpu = None

    async def _network_loop(self, dvt: DvtProvider) -> None:
        # Driven through the service rather than `async with NetworkStatistics`, whose exit waits for
        # the stop reply without a time limit; and the pid list changes as processes come and go.
        interval = max(self.interval_ms, NETWORK_MIN_INTERVAL_MS) / 1000
        tracked = sorted(self._pids)
        network = NetworkStatistics(dvt, tracked)
        started = False
        try:
            await network.connect()
            await network.service.start_sampling_for_pids_(tracked)
            started = True
            # Each reply carries the sample taken at the previous call; the first is a leftover.
            await network.service.sample_attributes_for_pids_({}, tracked)
            while True:
                await asyncio.sleep(interval)
                current = set(self._pids)
                new = sorted(current.difference(tracked))
                gone = sorted(set(tracked).difference(current))
                if new:
                    await network.service.start_sampling_for_pids_(new)
                if gone:
                    await network.service.stop_sampling_for_pids_(gone)
                tracked = sorted(current)
                sample = await network.service.sample_attributes_for_pids_({}, tracked)
                if sample:
                    self._net.update(sample, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("network statistics failed", exc_info=True)
            self._net = NetworkRates()  # stale rates would keep showing as if they were live
        finally:
            if started:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(network.service.stop_sampling_for_pids_(tracked), STOP_TIMEOUT_SECONDS)

    async def _battery_loop(self) -> None:
        while True:
            self._battery = await self._read_battery()
            await asyncio.sleep(BATTERY_POLL_SECONDS)

    async def _read_battery(self) -> Battery | None:
        lockdown = self.connection.lockdown
        values = io_power_source = None
        with contextlib.suppress(Exception):
            values = await lockdown.get_value(domain="com.apple.mobile.battery")
        with contextlib.suppress(Exception):
            async with DiagnosticsService(lockdown) as diagnostics:
                io_power_source = await diagnostics.get_battery()
        if not values and not io_power_source:
            return None
        return battery_from_lockdown(values, io_power_source)


async def _supported(query: Any, wanted: Sequence[str]) -> list[str]:
    """The wanted attributes the device supports, in the wanted order."""
    supported = set(await query)
    missing = [name for name in ("pid", "name") if name in wanted and name not in supported]
    if missing:
        raise UserFacingError(f"This iOS version's sysmontap doesn't report {', '.join(missing)}.")
    return [name for name in wanted if name in supported]


async def _timebase(info: DeviceInfo) -> float | None:
    """Seconds per mach tick, from machTimeInfo's numer/denom (125/3 ns on an iPhone X)."""
    try:
        values = await info.mach_time_info()
        return values[1] / values[2] / 1e9
    except Exception:
        logger.debug("machTimeInfo failed", exc_info=True)
        return None


async def replay_stream(path: Path, speed: float = 1.0) -> AsyncGenerator[Event, None]:
    """Play back an ``iphone-top --json`` recording at its recorded pace."""
    header: DeviceHeader | None = None
    previous: float | None = None
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            if not line.strip():
                continue
            data = json.loads(line)
            if data.get("type") == "device":
                header = header_from_dict(data)
                yield header
                continue
            if data.get("type") != "sample":
                continue
            if header is None:
                header = DeviceHeader(name="iPhone", model="replay", product_type="", ios_version="?", build="")
                yield header
            sample = sample_from_dict(data)
            if previous is not None:
                await asyncio.sleep(min(max(sample.time - previous, 0.0), 5.0) / speed)
            previous = sample.time
            yield sample
