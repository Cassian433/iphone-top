from __future__ import annotations

import datetime
from typing import Any

import pytest

from iphone_top.model import (
    PAGE_SIZE,
    CounterRate,
    DeviceHeader,
    NetworkRates,
    ProcessStats,
    battery_from_lockdown,
    filter_processes,
    header_from_dict,
    header_to_dict,
    model_name,
    parse_cpu,
    parse_graphics,
    parse_memory,
    parse_processes,
    row_values,
    sample_from_dict,
    sample_to_dict,
    sort_processes,
)


def system_rows(sysmontap: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in sysmontap["rows"] if "System" in row]


def process_rows(sysmontap: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in sysmontap["rows"] if "Processes" in row]


def test_cpu_total_is_the_core_sum_divided_by_core_count(sysmontap: dict[str, Any]) -> None:
    row = system_rows(sysmontap)[1]
    cpu = parse_cpu(row)
    assert cpu is not None
    assert row["CPUCount"] == 6
    assert cpu.total == pytest.approx(row["SystemCPUUsage"]["CPU_TotalLoad"] / 6)
    assert cpu.total == pytest.approx(37.09, abs=0.01)
    assert len(cpu.cores) == 6
    assert cpu.cores[0] == pytest.approx(10.0)
    assert all(0 <= core <= 100 for core in cpu.cores)


def test_cpu_is_clamped_and_tolerates_missing_fields() -> None:
    row = {"SystemCPUUsage": {"CPU_TotalLoad": 900.0}, "CPUCount": 6, "PerCPUUsage": [{"CPU_TotalLoad": -1.0}]}
    cpu = parse_cpu(row)
    assert cpu is not None
    assert cpu.total == 100.0
    assert cpu.cores == [0.0]
    assert parse_cpu({}) is None
    assert parse_cpu({"SystemCPUUsage": {"CPU_TotalLoad": 10.0}, "CPUCount": 0}) is None


def test_memory_uses_16k_pages(sysmontap: dict[str, Any]) -> None:
    row = system_rows(sysmontap)[1]
    values = row_values(row["System"], row.get("SystemAttributes", sysmontap["system_attributes"]))
    memory = parse_memory(values)
    assert memory is not None
    # iPhone X: 180756 pages of 16 KiB = 2.76 GiB, the RAM iOS reports for a 3 GB phone.
    assert memory.total == 180756 * PAGE_SIZE == 2961506304
    assert memory.used == memory.app + memory.wired + memory.compressed
    assert memory.app == (values["vmIntPageCount"] - values["vmPurgeableCount"]) * PAGE_SIZE
    assert memory.cached == (values["vmExtPageCount"] + values["vmPurgeableCount"]) * PAGE_SIZE
    assert memory.used + memory.cached + memory.free == memory.total
    assert parse_memory({"physMemSize": 1}) is None
    assert parse_memory({**values, "physMemSize": 0}) is None


def test_processes_first_snapshot_has_no_cpu(sysmontap: dict[str, Any]) -> None:
    first, second = process_rows(sysmontap)[:2]
    attributes = sysmontap["process_attributes"]
    assert all(p.cpu is None for p in parse_processes(first, attributes))
    processes = parse_processes(second, attributes)
    assert len(processes) == len(second["Processes"]) == 389
    kernel = next(p for p in processes if p.pid == 0)
    assert kernel.name == "kernel_task"
    assert kernel.cpu == pytest.approx(21.84, abs=0.01)
    assert kernel.memory == 152502272
    assert kernel.threads == 355


def test_counter_rate() -> None:
    rate = CounterRate()
    assert rate.update(1000, 10.0) is None
    assert rate.update(3000, 12.0) == 1000.0
    assert rate.update(2000, 13.0) is None  # counter went backwards
    assert rate.update(2500, 14.0) == 500.0
    assert rate.update(None, 15.0) is None


def test_network_rates_diff_lifetime_totals(network_samples: list[dict[int, dict[str, Any]]]) -> None:
    rates = NetworkRates()
    rates.update(network_samples[0], host_time=0)
    assert rates.rates == {}  # one observation is not a rate
    rates.update(network_samples[1], host_time=0)
    first, second = network_samples[0][746], network_samples[1][746]
    elapsed = second["time"] - first["time"]
    rx, tx = rates.rates[746]
    assert rx == pytest.approx((second["net.rx.bytes"] - first["net.rx.bytes"]) / elapsed)
    assert tx == pytest.approx((second["net.tx.bytes"] - first["net.tx.bytes"]) / elapsed)
    assert tx > 100_000  # DTServiceHub streaming the samples to the host
    assert rates.rates[39] == (0.0, 0.0)


def test_network_rates_ignore_deltas_and_counter_resets() -> None:
    def record(rx: int, tx: int, when: float) -> dict[str, Any]:
        # .delta equal to the total, as in the first reply after sampling starts
        return {"net.rx.bytes": rx, "net.tx.bytes": tx, "net.rx.bytes.delta": rx, "time": when}

    rates = NetworkRates()
    rates.update({7: record(1_000_000, 50, 100.0)}, host_time=0)
    rates.update({7: record(1_000_500, 150, 101.0)}, host_time=0)
    assert rates.rates[7] == (500.0, 100.0)
    rates.update({7: record(10, 10, 102.0)}, host_time=0)
    assert 7 not in rates.rates
    rates.update({7: record(110, 10, 103.0)}, host_time=0)
    assert rates.rates[7] == (100.0, 0.0)


def test_network_rates_accept_datetimes_and_fall_back_to_host_time() -> None:
    start = datetime.datetime(2026, 10, 11, tzinfo=datetime.timezone.utc)
    rates = NetworkRates()
    rates.update({1: {"net.rx.bytes": 0, "net.tx.bytes": 0, "time": start}}, host_time=0)
    later = start + datetime.timedelta(seconds=2)
    rates.update({1: {"net.rx.bytes": 400, "net.tx.bytes": 0, "time": later}}, host_time=0)
    assert rates.rates[1] == (200.0, 0.0)
    rates = NetworkRates()
    rates.update({1: {"net.rx.bytes": 0, "net.tx.bytes": 0}}, host_time=10.0)
    rates.update({1: {"net.rx.bytes": 100, "net.tx.bytes": 0}}, host_time=11.0)
    assert rates.rates[1] == (100.0, 0.0)


def test_graphics_event(graphics_events: list[Any]) -> None:
    assert parse_graphics(graphics_events[1]) == (0, 0)
    assert parse_graphics({"CoreAnimationFramesPerSecond": 59, "Device Utilization %": 31}) == (59, 31)
    assert parse_graphics("heartbeat") is None
    assert parse_graphics({"unrelated": 1}) is None


def test_model_name() -> None:
    assert model_name("iPhone10,6") == "iPhone X"
    assert model_name("iPhone10,3") == "iPhone X"
    assert model_name("Unknown1,1") == "Unknown1,1"


def test_battery(battery: dict[str, Any]) -> None:
    state = battery_from_lockdown(battery["lockdown"], battery["io_power_source"])
    assert state.level == 97
    assert state.charging is False
    assert state.external_power is True
    assert state.temperature == pytest.approx(29.29)
    only_lockdown = battery_from_lockdown(battery["lockdown"], None)
    assert only_lockdown.temperature is None
    assert only_lockdown.level == 97


def test_json_round_trip(recording: list[dict[str, Any]]) -> None:
    header, *samples = recording
    assert header["type"] == "device"
    assert header["name"] == "iPhone"
    assert header_to_dict(header_from_dict(header)) == header
    for data in samples:
        assert sample_to_dict(sample_from_dict(data)) == data


def test_header_from_dict_ignores_unknown_keys() -> None:
    header = header_from_dict(
        {
            "type": "device",
            "name": "x",
            "model": "m",
            "product_type": "p",
            "ios_version": "16.0",
            "build": "b",
            "something_new": 1,
        }
    )
    assert header == DeviceHeader(name="x", model="m", product_type="p", ios_version="16.0", build="b")


def test_sort_and_filter() -> None:
    processes = [
        ProcessStats(pid=1, name="launchd", cpu=0.5, memory=20, rx_rate=None),
        ProcessStats(pid=100, name="SpringBoard", cpu=3.0, memory=80, rx_rate=60.0, app=False),
        ProcessStats(pid=101, name="MobileSafari", cpu=None, memory=300, rx_rate=5000.0, app=True),
        ProcessStats(pid=10, name="backboardd", cpu=3.0, memory=30, tx_rate=1.0),
    ]
    assert [p.pid for p in sort_processes(processes, "cpu")] == [10, 100, 1, 101]
    assert [p.pid for p in sort_processes(processes, "memory")] == [101, 100, 10, 1]
    assert [p.pid for p in sort_processes(processes, "rx")] == [101, 100, 1, 10]
    assert sort_processes(processes, "tx")[0].pid == 10
    assert [p.pid for p in sort_processes(processes, "pid")] == [1, 10, 100, 101]
    assert [p.name for p in sort_processes(processes, "name")] == [
        "backboardd",
        "launchd",
        "MobileSafari",
        "SpringBoard",
    ]
    assert [p.pid for p in filter_processes(processes, "SPRING")] == [100]
    assert [p.pid for p in filter_processes(processes, "1")] == [1]
    assert [p.pid for p in filter_processes(processes, "", apps_only=True)] == [101]
    assert len(filter_processes(processes, "  ")) == 4
