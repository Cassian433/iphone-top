"""Rich text for the TUI: htop-style meters, the device line, table cells. No Textual imports."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text

from iphone_top.fmt import human_bytes, human_percent, human_rate, human_uptime
from iphone_top.model import Battery, CpuStats, DeviceHeader, MemoryStats, ProcessStats, Sample

LABEL = "bold cyan"
DIM = "grey50"
MEMORY_SEGMENTS = (("app", "green"), ("wired", "red"), ("compressed", "yellow"), ("cached", "blue"))
# The instruments server and sysmond do the measuring; their CPU is the price of watching.
OBSERVERS = frozenset({"DTServiceHub", "sysmond"})


def load_style(percent: float) -> str:
    if percent >= 80:
        return "red"
    if percent >= 50:
        return "yellow"
    return "green"


def meter(label: str, segments: Sequence[tuple[float, str]], value: str, width: int) -> Text:
    """``label[||||||      value]`` filling ``width`` cells; segments are (fraction, style) in order."""
    inner = max(width - len(label) - 2, len(value) + 1)
    cells: list[tuple[str, str]] = [(" ", "")] * inner
    position = 0
    filled = 0.0
    for fraction, style in segments:
        filled += max(fraction, 0.0)
        end = min(round(filled * inner), inner)
        for index in range(position, end):
            cells[index] = ("|", style)
        position = max(position, end)
    start = inner - len(value)
    for offset, char in enumerate(value):
        cells[start + offset] = (char, DIM if cells[start + offset][1] == "" else "bold")
    text = Text(label, style=LABEL)
    text.append("[", style="bold")
    for char, style in _runs(cells):
        text.append(char, style=style)
    text.append("]", style="bold")
    return text


def _runs(cells: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    for char, style in cells:
        if runs and runs[-1][1] == style:
            runs[-1] = (runs[-1][0] + char, style)
        else:
            runs.append((char, style))
    return runs


def cpu_meters(cpu: CpuStats | None, width: int) -> Text:
    if cpu is None:
        return Text.assemble(meter("CPU", [], "...", width), "\n", Text("waiting for the second sample", style=DIM))
    lines = [meter("CPU", [(cpu.total / 100, load_style(cpu.total))], f"{cpu.total:.1f}%", width)]
    if cpu.cores:
        # Two columns of per-core meters, numbered from 1 like htop.
        rows = (len(cpu.cores) + 1) // 2
        column = (width - 1) // 2
        label_width = len(str(len(cpu.cores)))
        for row in range(rows):
            line = Text()
            for index in (row, row + rows):
                if index >= len(cpu.cores):
                    continue
                load = cpu.cores[index]
                label = f"{index + 1:>{label_width}}"
                if index != row:
                    line.append(" ")
                line.append_text(meter(label, [(load / 100, load_style(load))], f"{load:.0f}%", column))
            lines.append(line)
    return Text("\n").join(lines)


def memory_meters(memory: MemoryStats | None, sample: Sample | None, width: int) -> Text:
    if memory is None:
        lines = [meter("Mem", [], "...", width), Text(""), Text("")]
    else:
        segments = [(getattr(memory, name) / memory.total, style) for name, style in MEMORY_SEGMENTS]
        lines = [meter("Mem", segments, f"{human_bytes(memory.used)}/{human_bytes(memory.total)}", width)]
        for pair in (MEMORY_SEGMENTS[:2], MEMORY_SEGMENTS[2:]):
            legend = Text()
            for name, style in pair:
                if legend:
                    legend.append("  ")
                legend.append("|", style=style)
                legend.append(f"{name} ", style=DIM)
                legend.append(human_bytes(getattr(memory, name)))
            lines.append(legend)
    if sample is not None:
        disk = Text("Disk ", style=LABEL)
        disk.append("read ", style=DIM)
        disk.append(human_rate(sample.disk_read_rate))
        disk.append("  write ", style=DIM)
        disk.append(human_rate(sample.disk_write_rate))
        lines.append(disk)
        tasks = Text("Tasks ", style=LABEL)
        threads = sum(p.threads or 0 for p in sample.processes)
        apps = sum(1 for p in sample.processes if p.app)
        tasks.append(f"{len(sample.processes)}")
        tasks.append(", ", style=DIM)
        tasks.append(f"{threads}")
        tasks.append(" thr, ", style=DIM)
        tasks.append(f"{apps}")
        tasks.append(" apps", style=DIM)
        lines.append(tasks)
    return Text("\n").join(lines)


BLOCKS = " ▁▂▃▄▅▆▇█"


def history(values: Sequence[float], width: int, top: float, top_label: str, style_for: Callable[[float], str]) -> Text:
    """Two-row bar chart of the newest values on a fixed 0..top scale, with the scale on the left.

    Textual's Sparkline scales to the min and max of its data, which draws a steady 37% CPU as a
    full-height wall; a fixed scale keeps the height meaningful.
    """
    axis = max(len(top_label), 1) + 1
    columns = max(width - axis, 1)
    recent = list(values)[-columns:]
    upper = Text(f"{top_label:>{axis - 1}} ", style=DIM)
    lower = Text(f"{'0':>{axis - 1}} ", style=DIM)
    padding = columns - len(recent)
    upper.append(" " * padding)
    lower.append(" " * padding)
    for value in recent:
        level = round(max(0.0, min(value / top, 1.0)) * 16) if top > 0 else 0
        if level == 0:
            upper.append(" ")
            lower.append("▁", style=DIM)
            continue
        style = style_for(value)
        upper.append(BLOCKS[max(level - 8, 0)], style=style)
        lower.append(BLOCKS[min(level, 8)], style=style)
    return Text.assemble(upper, "\n", lower)


def fps_scale(values: Sequence[float]) -> int:
    """60, or 120 once a ProMotion display has gone past 60."""
    return 120 if any(value > 60 for value in values) else 60


def fps_color(fps: float) -> str:
    return {"smooth": "green", "uneven": "yellow", "janky": "red"}.get(fps_grade(int(fps)), DIM)


def gpu_meter(gpu: int | None, width: int) -> Text:
    if gpu is None:
        return meter("GPU", [], "n/a", width)
    return meter("GPU", [(gpu / 100, load_style(gpu))], f"{gpu}%", width)


def fps_caption(fps: int | None) -> Text:
    if fps is None:
        return Text.assemble(("FPS", LABEL), ("  graphics channel unavailable", DIM))
    if fps == 0:
        return Text.assemble(("FPS", LABEL), ("  screen is static", DIM))
    return Text.assemble(("FPS", LABEL), ("  whole display", DIM))


def fps_grade(fps: int | None) -> str:
    """Grade a frame rate: smooth at 55+, uneven at 30-54, janky below 30, idle at 0."""
    if not fps:
        return "idle"
    return "smooth" if fps >= 55 else "uneven" if fps >= 30 else "janky"


def battery_text(battery: Battery | None) -> Text:
    if battery is None or battery.level is None:
        return Text("battery ?", style=DIM)
    style = "red" if battery.level <= 20 else "yellow" if battery.level <= 50 else "green"
    text = Text.assemble(("battery ", DIM), (f"{battery.level}%", style))
    if battery.charging:
        text.append(" charging", style="green")
    elif battery.external_power:
        text.append(" on power", style=DIM)
    if battery.temperature is not None:
        hot = battery.temperature >= 40
        text.append(f" {battery.temperature:.1f}°C", style="red" if hot else "")
    return text


def device_line(header: DeviceHeader | None, sample: Sample | None, transport: str, now: float) -> Text:
    if header is None:
        return Text("connecting...", style=DIM)
    text = Text.assemble((header.name, "bold"), "  ", (header.model, LABEL))
    text.append(f"  iOS {header.ios_version}")
    if header.build:
        text.append(f" ({header.build})", style=DIM)
    if header.boot_time is not None:
        reference = sample.time if sample is not None else now
        text.append("  up ", style=DIM)
        text.append(human_uptime(reference - header.boot_time))
    text.append("  ")
    text.append_text(battery_text(sample.battery if sample else None))
    text.append(f"  via {transport}", style=DIM)
    return text


def process_cells(process: ProcessStats) -> tuple[Text, ...]:
    observer = process.name in OBSERVERS
    if observer:
        name = Text(process.name, style=DIM)
        name.append("  (sampling)", style=f"{DIM} italic")
    elif process.app:
        name = Text(process.name, style="bold bright_cyan")
    else:
        name = Text(process.name)
    cpu = process.cpu
    cpu_style = DIM if observer or cpu is None else "red" if cpu >= 80 else "yellow" if cpu >= 20 else ""
    return (
        Text(str(process.pid), style=DIM, justify="right"),
        Text(human_percent(cpu), style=cpu_style, justify="right"),
        Text(human_bytes(process.memory), justify="right"),
        Text("-" if process.threads is None else str(process.threads), style=DIM, justify="right"),
        Text(human_rate(process.rx_rate), style=_rate_style(process.rx_rate, "green"), justify="right"),
        Text(human_rate(process.tx_rate), style=_rate_style(process.tx_rate, "magenta"), justify="right"),
        name,
    )


def _rate_style(rate: float | None, busy: str) -> str:
    return busy if rate is not None and rate >= 1 else DIM
