"""LiveSampler.stream() end to end against fake DVT services: startup, teardown order, failures."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pymobiledevice3.exceptions import ConnectionTerminatedError

from iphone_top import sampler as sampler_module
from iphone_top.connect import Connection, explain
from iphone_top.model import DeviceHeader, Sample
from iphone_top.sampler import LiveSampler


class World:
    """Recorded payloads plus a log of what the fakes were asked to do."""

    def __init__(self, sysmontap: dict[str, Any], device_info: dict[str, Any], network: list[Any], battery: Any):
        self.sysmontap = sysmontap
        self.device_info = device_info
        self.network = network
        self.battery = battery
        self.log: list[str] = []
        self.hang_after_rows = False
        self.network_fails_after: int | None = None
        self.graphics_events: list[Any] = [
            {"CoreAnimationFramesPerSecond": 3, "Device Utilization %": 1},  # partial first second, dropped
            {"CoreAnimationFramesPerSecond": 59, "Device Utilization %": 31},
        ]


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    sysmontap: dict[str, Any],
    device_info: dict[str, Any],
    network_samples: list[Any],
    battery: dict[str, Any],
) -> World:
    w = World(sysmontap, device_info, network_samples, battery)

    class FakeDvt:
        def __init__(self, provider: Any) -> None:
            pass

        async def __aenter__(self) -> FakeDvt:
            w.log.append("dvt open")
            return self

        async def __aexit__(self, *exc: object) -> None:
            w.log.append("dvt close")

    class FakeDeviceInfo:
        def __init__(self, dvt: Any) -> None:
            pass

        async def __aenter__(self) -> FakeDeviceInfo:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def sysmon_process_attributes(self) -> list[str]:
            return w.device_info["process_attributes_supported"]

        async def sysmon_system_attributes(self) -> list[str]:
            return w.device_info["system_attributes_supported"]

        async def mach_time_info(self) -> list[Any]:
            return w.device_info["mach_time_info"]

        async def proclist(self) -> list[dict[str, Any]]:
            processes = []
            for entry in w.device_info["proclist"]:
                entry = dict(entry)
                if entry.get("startDate") is not None:
                    entry["startDate"] = datetime.datetime.fromtimestamp(entry["startDate"], datetime.timezone.utc)
                processes.append(entry)
            return processes

    class FakeSysmontap:
        def __init__(self, dvt: Any, process_attributes: list[str], system_attributes: list[str], **_: Any) -> None:
            assert process_attributes == w.sysmontap["process_attributes"]
            self.process_attributes_cls = dataclasses.make_dataclass("P", process_attributes)
            self.system_attributes_cls = dataclasses.make_dataclass("S", system_attributes)

        async def __aenter__(self) -> FakeSysmontap:
            w.log.append("sysmon start")
            return self

        async def __aexit__(self, *exc: object) -> None:
            w.log.append("sysmon stop")

        async def __aiter__(self) -> AsyncIterator[Any]:
            for row in w.sysmontap["rows"]:
                await asyncio.sleep(0.02)
                yield row
            if w.hang_after_rows:
                await asyncio.Event().wait()

    class FakeGraphics:
        def __init__(self, dvt: Any) -> None:
            pass

        async def __aenter__(self) -> FakeGraphics:
            w.log.append("graphics start")
            return self

        async def __aexit__(self, *exc: object) -> None:
            w.log.append("graphics stop")

        async def __aiter__(self) -> AsyncIterator[Any]:
            for event in w.graphics_events:
                yield event
            await asyncio.Event().wait()

    class FakeNetworkService:
        def __init__(self) -> None:
            self.calls = 0

        async def start_sampling_for_pids_(self, pids: list[int]) -> None:
            w.log.append("net start")

        async def stop_sampling_for_pids_(self, pids: list[int]) -> None:
            w.log.append("net stop")

        async def sample_attributes_for_pids_(self, attributes: Any, pids: list[int]) -> Any:
            self.calls += 1
            if w.network_fails_after is not None and self.calls > w.network_fails_after:
                raise ConnectionTerminatedError("channel dropped")
            return w.network[(self.calls - 1) % len(w.network)]

    class FakeNetworkStatistics:
        def __init__(self, dvt: Any, pids: list[int]) -> None:
            self.service = FakeNetworkService()

        async def connect(self) -> None:
            return None

    class FakeDiagnostics:
        def __init__(self, lockdown: Any) -> None:
            pass

        async def __aenter__(self) -> FakeDiagnostics:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get_battery(self) -> Any:
            return w.battery["io_power_source"]

    for name, fake in (
        ("DvtProvider", FakeDvt),
        ("DeviceInfo", FakeDeviceInfo),
        ("Sysmontap", FakeSysmontap),
        ("Graphics", FakeGraphics),
        ("NetworkStatistics", FakeNetworkStatistics),
        ("DiagnosticsService", FakeDiagnostics),
    ):
        monkeypatch.setattr(sampler_module, name, fake)
    monkeypatch.setattr(sampler_module, "NETWORK_MIN_INTERVAL_MS", 10)
    return w


class FakeLockdown:
    def __init__(self, battery: dict[str, Any]) -> None:
        self.all_values = {"DeviceName": "iPhone", "ProductType": "iPhone10,6", "BuildVersion": "20H392"}
        self.product_version = "16.7.16"
        self.battery = battery

    async def get_value(self, domain: str | None = None, key: str | None = None) -> Any:
        assert domain == "com.apple.mobile.battery"
        return self.battery["lockdown"]


def live(world: World, interval_ms: int = 50) -> LiveSampler:
    lockdown = FakeLockdown(world.battery)
    return LiveSampler(Connection(lockdown, lockdown, "test"), interval_ms=interval_ms)  # type: ignore[arg-type]


async def test_stream_yields_a_header_then_samples_and_tears_down_in_order(world: World) -> None:
    events: list[Any] = []
    async with contextlib.aclosing(live(world).stream()) as stream:
        async for event in stream:
            events.append(event)
            if len(events) == 3:
                break
    header, first, second = events
    assert isinstance(header, DeviceHeader)
    assert header.model == "iPhone X"
    assert header.boot_time is not None and header.boot_time > 1.7e9
    assert isinstance(first, Sample) and isinstance(second, Sample)
    assert second.battery is not None and second.battery.level == 97
    assert (second.fps, second.gpu) == (59, 31)
    log = world.log
    assert log[0] == "dvt open" and log[-1] == "dvt close"
    # Every channel is stopped before the DVT connection under it is closed.
    for stop in ("sysmon stop", "graphics stop", "net stop"):
        assert stop in log
        assert log.index(stop) < log.index("dvt close")


async def test_a_dropped_stream_reads_as_a_lost_connection(world: World) -> None:
    with pytest.raises(ConnectionTerminatedError) as raised:
        async for _ in live(world).stream():
            pass
    assert "Lost the connection" in (explain(raised.value) or "")
    assert world.log[-1] == "dvt close"
    assert "sysmon stop" in world.log


async def test_a_stalled_stream_times_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.hang_after_rows = True
    monkeypatch.setattr(sampler_module, "STALL_SECONDS", 0.3)
    with pytest.raises(ConnectionTerminatedError, match="no data from the phone"):
        async for _ in live(world, interval_ms=10).stream():
            pass
    assert world.log[-1] == "dvt close"


async def test_network_rates_are_dropped_when_the_channel_fails(world: World) -> None:
    world.network_fails_after = 3  # the leftover reply, then two real samples, then an error
    sampler = live(world)
    sampler._pids = {0, 746}
    await sampler._network_loop(object())  # type: ignore[arg-type]
    assert sampler._net.rates == {}
    assert world.log.count("net start") == 1 and world.log[-1] == "net stop"


async def test_network_rates_flow_while_the_channel_works(world: World) -> None:
    world.network_fails_after = None
    sampler = live(world)
    sampler._pids = {0, 746}
    task = asyncio.create_task(sampler._network_loop(object()))  # type: ignore[arg-type]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if 746 in sampler._net.rates:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # The first reply is discarded as a leftover, so the first rate spans recorded samples 2 and 3.
    before, after = world.network[1][746], world.network[2][746]
    expected = (after["net.tx.bytes"] - before["net.tx.bytes"]) / (after["time"] - before["time"])
    assert sampler._net.rates[746][1] == pytest.approx(expected)
    assert world.log[-1] == "net stop"
