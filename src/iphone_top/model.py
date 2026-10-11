"""Plain records for one sample of device telemetry, and the parsing that builds them.

Nothing here talks to a device: the functions take the raw payloads that pymobiledevice3's DVT
services yield (sysmontap rows, graphics events, network-statistics replies) and return plain
dataclasses that the TUI renders and ``--json`` serializes. Tests feed them recorded payloads.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

# arm64 iOS uses 16 KiB VM pages; sysmontap reports memory as page counts. An iPhone X reports
# physMemSize=180756, which is 2.76 GiB at 16 KiB and only 706 MiB at 4 KiB.
PAGE_SIZE = 16384

# Process attributes requested from sysmontap. Sysmontap.create() asks for all ~74, which makes
# every row several times larger than what the table shows.
# procAge changes every interval; without an attribute that always changes, sysmontap leaves idle
# processes out of the snapshot, so they would look like they had exited.
PROCESS_ATTRIBUTES = ("pid", "name", "cpuUsage", "physFootprint", "threadCount", "procAge")
SYSTEM_ATTRIBUTES = (
    "physMemSize",
    "vmFreeCount",
    "vmSpeculativeCount",
    "vmWireCount",
    "vmIntPageCount",
    "vmExtPageCount",
    "vmPurgeableCount",
    "vmCompressorPageCount",
    # netBytesIn/netBytesOut are left out on purpose: they include loopback, and the device side of
    # the USB connection runs over loopback, so in and out move by the same byte count every interval
    # and mostly measure iphone-top's own traffic.
    "diskBytesRead",
    "diskBytesWritten",
)


@dataclass
class CpuStats:
    total: float
    """Busy percent of the whole device, 0-100."""
    cores: list[float] = field(default_factory=list)
    """Busy percent of each core, 0-100."""


@dataclass
class MemoryStats:
    """Bytes. ``used`` is app + wired + compressed, the same split Activity Monitor uses."""

    total: int
    used: int
    app: int
    wired: int
    compressed: int
    cached: int
    free: int


@dataclass
class ProcessStats:
    pid: int
    name: str
    cpu: float | None = None
    """Percent of one core, so a busy multithreaded process can go past 100."""
    memory: int | None = None
    """Physical footprint in bytes (what Xcode's memory gauge and jetsam count)."""
    threads: int | None = None
    rx_rate: float | None = None
    """Bytes per second received; None until two network samples have been seen."""
    tx_rate: float | None = None
    app: bool = False
    """iOS lists it as an application (apps, plus some of their extensions), not a daemon."""


@dataclass
class Battery:
    level: int | None = None
    charging: bool | None = None
    external_power: bool | None = None
    temperature: float | None = None
    """Battery temperature in Celsius."""


@dataclass
class DeviceHeader:
    name: str
    model: str
    product_type: str
    ios_version: str
    build: str
    boot_time: float | None = None
    """Unix time of boot (launchd's start date), for uptime."""


@dataclass
class Sample:
    time: float
    """Unix time the sample was assembled on the host."""
    cpu: CpuStats | None = None
    memory: MemoryStats | None = None
    disk_read_rate: float | None = None
    disk_write_rate: float | None = None
    fps: int | None = None
    gpu: int | None = None
    """GPU device utilization percent."""
    battery: Battery | None = None
    processes: list[ProcessStats] = field(default_factory=list)


_Record = TypeVar("_Record", CpuStats, MemoryStats, ProcessStats, Battery, DeviceHeader)


# ---------------------------------------------------------------------------------------------
# sysmontap
# ---------------------------------------------------------------------------------------------


def row_values(values: Sequence[Any], attributes: Sequence[str]) -> dict[str, Any]:
    """Map one positional sysmontap value list onto its attribute names."""
    return dict(zip(attributes, values, strict=False))


def parse_cpu(row: Mapping[str, Any]) -> CpuStats | None:
    """CPU load from a sysmontap system row.

    ``CPU_TotalLoad`` is the sum over cores (600 means six busy cores), so it is divided by
    ``CPUCount``. The first row after starting is bogus (it reported 200 with three of six cores
    enabled on an iPhone X), which the sampler skips.
    """
    usage = row.get("SystemCPUUsage")
    count = row.get("CPUCount")
    if not isinstance(usage, Mapping) or not isinstance(count, int) or count <= 0:
        return None
    total = usage.get("CPU_TotalLoad")
    if not isinstance(total, (int, float)):
        return None
    cores: list[float] = []
    for core in row.get("PerCPUUsage") or []:
        load = core.get("CPU_TotalLoad") if isinstance(core, Mapping) else None
        cores.append(_clamp_percent(load if isinstance(load, (int, float)) else 0.0))
    return CpuStats(total=_clamp_percent(total / count), cores=cores)


def parse_memory(system: Mapping[str, Any], page_size: int = PAGE_SIZE) -> MemoryStats | None:
    """Memory split from sysmontap system values (page counts)."""
    try:
        total = int(system["physMemSize"]) * page_size
        internal = int(system["vmIntPageCount"])
        purgeable = int(system["vmPurgeableCount"])
        wired = int(system["vmWireCount"]) * page_size
        compressed = int(system["vmCompressorPageCount"]) * page_size
        external = int(system["vmExtPageCount"])
    except (KeyError, TypeError, ValueError):
        return None
    if total <= 0:
        return None
    app = max(internal - purgeable, 0) * page_size
    cached = (external + purgeable) * page_size
    used = app + wired + compressed
    free = max(total - used - cached, 0)
    return MemoryStats(total=total, used=used, app=app, wired=wired, compressed=compressed, cached=cached, free=free)


def parse_processes(row: Mapping[str, Any], attributes: Sequence[str]) -> list[ProcessStats]:
    """Process list from a sysmontap ``Processes`` row (``{pid: [values...]}``)."""
    processes: list[ProcessStats] = []
    for pid, values in (row.get("Processes") or {}).items():
        entry = row_values(values, attributes)
        name = entry.get("name") or entry.get("comm") or "?"
        processes.append(
            ProcessStats(
                pid=int(entry.get("pid", pid)),
                name=str(name),
                cpu=_float_or_none(entry.get("cpuUsage")),
                memory=_int_or_none(entry.get("physFootprint")),
                threads=_int_or_none(entry.get("threadCount")),
            )
        )
    return processes


class CounterRate:
    """Turns a monotonically increasing counter into a per-second rate."""

    def __init__(self) -> None:
        self._last: tuple[float, float] | None = None

    def update(self, value: float | None, when: float) -> float | None:
        if value is None:
            return None
        last, self._last = self._last, (value, when)
        if last is None:
            return None
        previous, then = last
        elapsed = when - then
        if elapsed <= 0 or value < previous:
            # Clock went backwards or the counter reset (process restarted, interface reset).
            return None
        return (value - previous) / elapsed


# ---------------------------------------------------------------------------------------------
# graphics
# ---------------------------------------------------------------------------------------------


def parse_graphics(event: Any) -> tuple[int | None, int | None] | None:
    """(fps, gpu utilization percent) from a graphics.opengl event, or None if it isn't a sample."""
    if not isinstance(event, Mapping):
        return None
    fps = _int_or_none(event.get("CoreAnimationFramesPerSecond"))
    gpu = _int_or_none(event.get("Device Utilization %"))
    if fps is None and gpu is None:
        return None
    return fps, gpu


# ---------------------------------------------------------------------------------------------
# per-process network statistics
# ---------------------------------------------------------------------------------------------


class NetworkRates:
    """Per-PID rx/tx rates from NetworkStatistics replies.

    Each record carries lifetime byte totals (``net.rx.bytes``/``net.tx.bytes``) and the device's
    sample ``time``. Rates come from consecutive totals. The ``.delta`` fields don't track them:
    on an iPhone X they were often 0 while the totals grew, and in the first reply after sampling
    started they equalled the lifetime total.
    """

    def __init__(self) -> None:
        self._last: dict[int, tuple[float, int, int]] = {}
        self.rates: dict[int, tuple[float, float]] = {}

    def update(self, sample: Mapping[int, Mapping[str, Any]], host_time: float) -> None:
        rates: dict[int, tuple[float, float]] = {}
        seen: dict[int, tuple[float, int, int]] = {}
        for pid, record in sample.items():
            rx = _int_or_none(record.get("net.rx.bytes"))
            tx = _int_or_none(record.get("net.tx.bytes"))
            if rx is None or tx is None:
                continue
            when = _timestamp(record.get("time"), host_time)
            pid = int(pid)
            seen[pid] = (when, rx, tx)
            last = self._last.get(pid)
            if last is None:
                continue
            then, last_rx, last_tx = last
            elapsed = when - then
            if elapsed <= 0 or rx < last_rx or tx < last_tx:
                continue
            rates[pid] = ((rx - last_rx) / elapsed, (tx - last_tx) / elapsed)
        self._last = seen
        self.rates = rates


# ---------------------------------------------------------------------------------------------
# header helpers
# ---------------------------------------------------------------------------------------------


def model_name(product_type: str) -> str:
    """Marketing name for a ProductType ("iPhone10,6" -> "iPhone X"), or the ProductType itself."""
    try:
        from pymobiledevice3.irecv_devices import IRECV_DEVICES
    except ImportError:  # the table moved; the raw identifier is still useful
        return product_type
    for device in IRECV_DEVICES:
        if device.product_type == product_type:
            # "iPhone X (GSM)" -> "iPhone X": the radio variant is noise in a header.
            return device.display_name.split(" (", 1)[0]
    return product_type


def battery_from_lockdown(values: Mapping[str, Any] | None, io_power_source: Mapping[str, Any] | None) -> Battery:
    """Battery state from the ``com.apple.mobile.battery`` lockdown domain plus IOPMPowerSource."""
    values = values or {}
    io_power_source = io_power_source or {}
    level = _int_or_none(values.get("BatteryCurrentCapacity", io_power_source.get("CurrentCapacity")))
    charging = values.get("BatteryIsCharging", io_power_source.get("IsCharging"))
    external = values.get("ExternalConnected", io_power_source.get("ExternalConnected"))
    raw_temperature = _float_or_none(io_power_source.get("Temperature"))
    return Battery(
        level=level,
        charging=bool(charging) if charging is not None else None,
        external_power=bool(external) if external is not None else None,
        # IOPMPowerSource reports hundredths of a degree Celsius (2909 -> 29.09).
        temperature=raw_temperature / 100 if raw_temperature is not None else None,
    )


# ---------------------------------------------------------------------------------------------
# JSON round-trip (NDJSON output and --replay input)
# ---------------------------------------------------------------------------------------------


def sample_to_dict(sample: Sample) -> dict[str, Any]:
    data = _rounded(dataclasses.asdict(sample))
    data["type"] = "sample"
    return data


def header_to_dict(header: DeviceHeader) -> dict[str, Any]:
    data = dataclasses.asdict(header)
    data["type"] = "device"
    return data


def sample_from_dict(data: Mapping[str, Any]) -> Sample:
    """Rebuild a sample; keys this version doesn't know (from a newer recording) are ignored."""
    cpu = data.get("cpu")
    memory = data.get("memory")
    battery = data.get("battery")
    return Sample(
        time=float(data["time"]),
        cpu=_known(CpuStats, cpu) if cpu else None,
        memory=_known(MemoryStats, memory) if memory else None,
        disk_read_rate=data.get("disk_read_rate"),
        disk_write_rate=data.get("disk_write_rate"),
        fps=data.get("fps"),
        gpu=data.get("gpu"),
        battery=_known(Battery, battery) if battery else None,
        processes=[_known(ProcessStats, p) for p in data.get("processes") or []],
    )


def header_from_dict(data: Mapping[str, Any]) -> DeviceHeader:
    return _known(DeviceHeader, data)


def _known(cls: type[_Record], data: Mapping[str, Any]) -> _Record:
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


# ---------------------------------------------------------------------------------------------
# sorting and filtering for the process table
# ---------------------------------------------------------------------------------------------

SORT_KEYS = ("cpu", "memory", "rx", "tx", "pid", "name")


def sort_processes(processes: Iterable[ProcessStats], key: str) -> list[ProcessStats]:
    """Busiest first for numeric columns, ascending for pid and name."""
    if key == "pid":
        return sorted(processes, key=lambda p: p.pid)
    if key == "name":
        return sorted(processes, key=lambda p: (p.name.lower(), p.pid))
    getters = {
        "cpu": lambda p: p.cpu,
        "memory": lambda p: p.memory,
        "rx": lambda p: p.rx_rate,
        "tx": lambda p: p.tx_rate,
    }
    getter = getters[key]
    return sorted(processes, key=lambda p: (-(getter(p) or 0), p.pid))


def filter_processes(processes: Iterable[ProcessStats], text: str, apps_only: bool = False) -> list[ProcessStats]:
    """Case-insensitive substring match on the name, or an exact pid when the text is a number."""
    needle = text.strip().lower()
    result: list[ProcessStats] = []
    for process in processes:
        if apps_only and not process.app:
            continue
        if needle and needle not in process.name.lower() and needle != str(process.pid):
            continue
        result.append(process)
    return result


# ---------------------------------------------------------------------------------------------


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: Any, fallback: float) -> float:
    # NetworkStatistics converts the device's NSDate to a datetime; the raw service returns the
    # NSDate itself, whose .utc is a datetime.
    value = getattr(value, "utc", value)
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return fallback


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value
