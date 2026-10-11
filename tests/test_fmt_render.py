from __future__ import annotations

from typing import Any

import pytest

from iphone_top import render
from iphone_top.fmt import human_bytes, human_percent, human_rate, human_uptime, meter
from iphone_top.model import Battery, CpuStats, DeviceHeader, MemoryStats, ProcessStats, Sample, sample_from_dict


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "-"), (0, "0B"), (1023, "1023B"), (1536, "1.50K"), (52_428_800, "50.0M"), (2961506304, "2.76G")],
)
def test_human_bytes(value: float | None, expected: str) -> None:
    assert human_bytes(value) == expected


def test_human_rate() -> None:
    assert human_rate(None) == "-"
    assert human_rate(0.4) == "·"
    assert human_rate(1536) == "1.50K/s"


@pytest.mark.parametrize(
    ("seconds", "expected"), [(None, "?"), (-5, "?"), (42, "0m"), (5400, "1h 30m"), (3_317_500, "38d 9h")]
)
def test_human_uptime(seconds: float | None, expected: str) -> None:
    assert human_uptime(seconds) == expected


def test_human_percent_and_meter() -> None:
    assert human_percent(None) == "-"
    assert human_percent(37.089) == "37.1"
    assert meter(0.5, 10) == (5, 5)
    assert meter(2.0, 10) == (10, 0)
    assert meter(-1, 10) == (0, 10)


def test_meter_fills_its_width_and_overlays_the_value() -> None:
    text = render.meter("CPU", [(0.5, "green")], "50.0%", 30)
    assert len(text.plain) == 30
    assert text.plain.startswith("CPU[|||")
    assert text.plain.endswith("50.0%]")
    # 25 inner cells; half is 12.5, which rounds to even.
    assert text.plain.count("|") == 12


def test_meter_segments_stack_in_order() -> None:
    text = render.meter("Mem", [(0.2, "green"), (0.2, "red")], "x", 15)
    inner = text.plain[4:-1]
    assert inner.startswith("||||")
    assert len(text.plain) == 15
    styles = [span.style for span in text.spans]
    assert styles.index("green") < styles.index("red")


def test_meter_never_hides_the_value_on_tiny_widths() -> None:
    assert "37.1%" in render.meter("CPU", [(1.0, "red")], "37.1%", 4).plain


def test_cpu_meters_lay_cores_out_in_two_columns() -> None:
    text = render.cpu_meters(CpuStats(total=37.0, cores=[10, 20, 30, 40, 50, 60]), 60)
    lines = text.plain.split("\n")
    assert len(lines) == 4
    assert lines[1].startswith("1[") and " 4[" in lines[1]
    assert lines[3].rstrip().endswith("60%]")
    assert "waiting" in render.cpu_meters(None, 40).plain


def test_memory_meters(recording: list[dict[str, Any]]) -> None:
    sample = sample_from_dict(recording[1])
    assert sample.memory is not None
    text = render.memory_meters(sample.memory, sample, 50).plain
    assert "/2.76G]" in text
    assert "app " in text and "wired " in text and "compressed " in text and "cached " in text
    assert f"Tasks {len(sample.processes)}," in text
    memory = MemoryStats(total=100, used=50, app=30, wired=10, compressed=10, cached=20, free=30)
    assert render.memory_meters(memory, None, 40).plain.count("\n") == 2


def test_history_uses_a_fixed_scale() -> None:
    text = render.history([0, 50, 100, 250], width=10, top=100, top_label="100%", style_for=render.load_style)
    upper, lower = text.plain.split("\n")
    assert len(upper) == len(lower) == 10
    assert upper.startswith("100% ") and lower.startswith("   0 ")
    assert lower[-4:] == "▁███"  # 0 draws a baseline; 50, 100 and the clamped 250 fill the lower row
    assert upper[-4:] == "  ██"
    many = render.history(list(range(500)), width=12, top=500, top_label="x", style_for=render.load_style)
    assert all(len(row) == 12 for row in many.plain.split("\n"))


def test_fps_helpers() -> None:
    assert render.fps_scale([0, 59, 60]) == 60
    assert render.fps_scale([0, 118]) == 120
    assert [render.fps_grade(v) for v in (None, 0, 20, 45, 60, 120)] == [
        "idle",
        "idle",
        "janky",
        "uneven",
        "smooth",
        "smooth",
    ]
    assert "static" in render.fps_caption(0).plain
    assert "unavailable" in render.fps_caption(None).plain
    assert render.gpu_meter(None, 20).plain.endswith("n/a]")


def test_battery_text() -> None:
    assert render.battery_text(None).plain == "battery ?"
    text = render.battery_text(Battery(level=97, charging=False, external_power=True, temperature=29.29)).plain
    assert text == "battery 97% on power 29.3°C"
    assert "charging" in render.battery_text(Battery(level=15, charging=True)).plain


def test_device_line() -> None:
    header = DeviceHeader(
        name="iPhone", model="iPhone X", product_type="iPhone10,6", ios_version="16.7.16", build="20H392", boot_time=0
    )
    sample = Sample(time=3_317_500, battery=Battery(level=97))
    text = render.device_line(header, sample, "usbmux", now=0).plain
    assert text.startswith("iPhone  iPhone X  iOS 16.7.16 (20H392)  up 38d 9h  battery 97%")
    assert text.endswith("via usbmux")
    assert render.device_line(None, None, "usbmux", 0).plain == "connecting..."


def test_process_cells() -> None:
    cells = render.process_cells(ProcessStats(pid=82349, name="SpringBoard", cpu=2.5, memory=83412568, threads=22))
    assert [cell.plain for cell in cells] == ["82349", "2.5", "79.5M", "22", "-", "-", "SpringBoard"]
    hub = render.process_cells(ProcessStats(pid=746, name="DTServiceHub", cpu=66.0, tx_rate=694_000.0))
    assert hub[-1].plain == "DTServiceHub  (sampling)"
    assert hub[5].plain == "678K/s"
    app = render.process_cells(ProcessStats(pid=5, name="Preferences", app=True))
    assert "bold" in str(app[-1].style)
