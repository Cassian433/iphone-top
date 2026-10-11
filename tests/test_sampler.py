from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pymobiledevice3.exceptions import ConnectionTerminatedError

from iphone_top.connect import Connection
from iphone_top.model import DeviceHeader, Sample
from iphone_top.sampler import LiveSampler, replay_stream


class FakeSysmontap:
    """Plays recorded sysmontap rows the way Sysmontap's async iteration delivers them."""

    def __init__(self, recorded: dict[str, Any]) -> None:
        self.rows = recorded["rows"]
        self.process_attributes_cls = dataclasses.make_dataclass("P", recorded["process_attributes"])
        self.system_attributes_cls = dataclasses.make_dataclass("S", recorded["system_attributes"])

    async def __aenter__(self) -> FakeSysmontap:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[Any]:
        for row in self.rows:
            yield "heart"  # heartbeats arrive between rows
            yield row


def make_sampler(device_info: dict[str, Any] | None = None) -> LiveSampler:
    sampler = LiveSampler(Connection(lockdown=None, dvt=None, transport="test"))  # type: ignore[arg-type]
    if device_info is not None:
        sampler._remember_names(device_info["proclist"])
    return sampler


async def collect(sampler: LiveSampler, recorded: dict[str, Any]) -> list[Sample]:
    numer, denom = recorded["mach_time_info"][1:3]
    samples: list[Sample] = []
    with pytest.raises(ConnectionTerminatedError):
        async for sample in sampler._sysmon_samples(FakeSysmontap(recorded), numer / denom / 1e9):  # type: ignore[arg-type]
            samples.append(sample)
    return samples


async def test_sysmon_rows_become_samples(sysmontap: dict[str, Any], device_info: dict[str, Any]) -> None:
    recorded = {**sysmontap, "mach_time_info": device_info["mach_time_info"]}
    samples = await collect(make_sampler(device_info), recorded)
    # Four intervals were recorded; the first process snapshot has no CPU values and is dropped.
    assert len(samples) == 3
    first = samples[0]
    assert first.cpu is not None
    assert first.cpu.total == pytest.approx(37.09, abs=0.01)
    assert first.memory is not None and first.memory.total == 2961506304
    assert len(first.processes) == 389
    assert all(p.cpu is not None for p in first.processes if p.name == "kernel_task")


async def test_disk_rate_uses_device_mach_time(sysmontap: dict[str, Any], device_info: dict[str, Any]) -> None:
    recorded = {**sysmontap, "mach_time_info": device_info["mach_time_info"]}
    samples = await collect(make_sampler(device_info), recorded)
    system = [row for row in sysmontap["rows"] if "System" in row]
    attributes = system[0].get("SystemAttributes", sysmontap["system_attributes"])
    written = attributes.index("diskBytesWritten")
    numer, denom = device_info["mach_time_info"][1:3]
    elapsed = (system[1]["EndMachAbsTime"] - system[0]["EndMachAbsTime"]) * numer / denom / 1e9
    expected = (system[1]["System"][written] - system[0]["System"][written]) / elapsed
    assert samples[0].disk_write_rate == pytest.approx(expected)
    assert 0.9 < elapsed < 1.1  # one 1000 ms interval


async def test_full_names_and_app_flags_come_from_the_process_list(
    sysmontap: dict[str, Any], device_info: dict[str, Any]
) -> None:
    recorded = {**sysmontap, "mach_time_info": device_info["mach_time_info"]}
    samples = await collect(make_sampler(device_info), recorded)
    by_pid = {p.pid: p for p in samples[-1].processes}
    listed = {p["pid"]: p for p in device_info["proclist"]}
    long_names = [p for p in listed.values() if len(p["name"]) > 31 and p["pid"] in by_pid]
    assert long_names, "fixture should contain a name sysmontap truncates"
    for entry in long_names:
        assert by_pid[entry["pid"]].name == entry["name"]
    apps = [p for p in samples[-1].processes if p.app]
    assert apps and all(listed[p.pid]["isApplication"] for p in apps)


class FakeDeviceInfo:
    def __init__(self, proclist: list[dict[str, Any]], during: Any = None) -> None:
        self.proclist_result = proclist
        self.during = during
        self.calls = 0

    async def proclist(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.during is not None:
            self.during()
        return self.proclist_result


async def test_unlisted_pids_are_asked_about_once(sysmontap: dict[str, Any], device_info: dict[str, Any]) -> None:
    recorded = {**sysmontap, "mach_time_info": device_info["mach_time_info"]}
    sampler = make_sampler(device_info)
    await collect(sampler, recorded)
    # DTServiceHub is in the sysmontap rows but never in runningProcesses.
    hub = {p.pid for p in parse_hub(recorded)}
    assert hub and hub <= sampler._unknown
    info = FakeDeviceInfo(device_info["proclist"])
    await sampler._refresh_names(info)  # type: ignore[arg-type]
    assert info.calls == 1
    assert hub <= sampler._unlisted
    assert not sampler._unknown
    await collect(sampler, recorded)
    assert not sampler._unknown  # nothing left to ask about, so the names loop stays quiet


async def test_a_pid_that_appears_during_a_refresh_is_not_written_off(device_info: dict[str, Any]) -> None:
    sampler = make_sampler(device_info)
    sampler._unknown = {999_001}
    info = FakeDeviceInfo(device_info["proclist"], during=lambda: sampler._unknown.add(999_002))
    await sampler._refresh_names(info)  # type: ignore[arg-type]
    assert sampler._unlisted == {999_001}
    assert sampler._unknown == {999_002}


def parse_hub(recorded: dict[str, Any]) -> list[Any]:
    from iphone_top.model import parse_processes

    rows = [row for row in recorded["rows"] if "Processes" in row]
    return [p for p in parse_processes(rows[-1], recorded["process_attributes"]) if p.name == "DTServiceHub"]


async def test_network_rates_are_attached(sysmontap: dict[str, Any], device_info: dict[str, Any]) -> None:
    recorded = {**sysmontap, "mach_time_info": device_info["mach_time_info"]}
    sampler = make_sampler(device_info)
    sampler._net.rates = {0: (1000.0, 20.0)}
    samples = await collect(sampler, recorded)
    kernel = next(p for p in samples[0].processes if p.pid == 0)
    assert (kernel.rx_rate, kernel.tx_rate) == (1000.0, 20.0)
    assert next(p for p in samples[0].processes if p.pid == 1).rx_rate is None


def test_header_uses_launchd_start_for_boot_time(device_info: dict[str, Any]) -> None:
    import datetime

    class Lockdown:
        all_values: ClassVar[dict[str, str]] = {
            "DeviceName": "Test",
            "ProductType": "iPhone10,6",
            "BuildVersion": "20H392",
        }
        product_version = "16.7.16"

    sampler = LiveSampler(Connection(lockdown=Lockdown(), dvt=Lockdown(), transport="test"))  # type: ignore[arg-type]
    start = datetime.datetime(2026, 9, 3, 3, 14, 12, tzinfo=datetime.timezone.utc)
    header = sampler._header([{"pid": 1, "name": "launchd", "startDate": start}])
    assert header == DeviceHeader(
        name="Test",
        model="iPhone X",
        product_type="iPhone10,6",
        ios_version="16.7.16",
        build="20H392",
        boot_time=start.timestamp(),
    )


async def test_replay_stream(recording_path: Path) -> None:
    events = [event async for event in replay_stream(recording_path, speed=1000)]
    assert isinstance(events[0], DeviceHeader)
    assert events[0].model == "iPhone X"
    samples = [e for e in events[1:] if isinstance(e, Sample)]
    assert len(samples) == len(events) - 1 == 6
    assert samples[0].processes and samples[0].cpu is not None


def two_samples_without_header(tmp_path: Path, recording_path: Path) -> Path:
    lines = recording_path.read_text().splitlines()
    path = tmp_path / "samples.ndjson"
    path.write_text("\n".join(lines[1:3]) + "\n\n")
    return path


async def test_replay_without_a_header_line(tmp_path: Path, recording_path: Path) -> None:
    path = two_samples_without_header(tmp_path, recording_path)
    events = [event async for event in replay_stream(path, speed=1000)]
    assert isinstance(events[0], DeviceHeader) and events[0].model == "replay"
    assert len(events) == 3
