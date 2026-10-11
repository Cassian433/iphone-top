"""The Textual UI: device line, CPU/memory/GPU meters with history, and a sortable process table."""

from __future__ import annotations

import contextlib
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Digits, Footer, Input

from iphone_top import render
from iphone_top.model import SORT_KEYS, DeviceHeader, Sample, filter_processes, sort_processes

HISTORY = 120
COLUMNS = (("PID", "pid"), ("CPU%", "cpu"), ("MEM", "memory"), ("THR", None), ("RX/s", "rx"), ("TX/s", "tx"))
SORT_LABELS = {"cpu": "CPU", "memory": "memory", "rx": "RX", "tx": "TX", "pid": "PID", "name": "name"}


class Panel(Widget):
    """A widget whose content is a Rich renderable rebuilt for the current width."""

    DEFAULT_CSS = "Panel { height: auto; }"

    def __init__(self, draw, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._draw = draw

    def render(self) -> Text:
        return self._draw(self.size.width)


class IPhoneTop(App[None]):
    TITLE = "iphone-top"
    CSS = """
    Screen { layout: vertical; }
    #device { height: 1; padding: 0 1; background: $boost; }
    #meters { height: auto; padding: 1 1 0 1; }
    #cpu-column { width: 1fr; height: auto; }
    #memory-column { width: 1fr; height: auto; margin: 0 2; }
    #graphics-column { width: 28; height: auto; }
    #fps { width: auto; height: 3; }
    #fps.fps-smooth { color: $success; }
    #fps.fps-uneven { color: $warning; }
    #fps.fps-janky { color: $error; }
    #fps.fps-idle { color: $text-muted; }
    #cpu-history, #fps-history { margin-top: 1; }
    #processes { height: 1fr; margin-top: 1; }
    #status { height: 1; padding: 0 1; background: $boost; }
    #filter { dock: bottom; display: none; height: 3; }
    #filter.visible { display: block; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("slash", "show_filter", "Filter"),
        Binding("a", "toggle_apps", "Apps only"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    def __init__(self, events: AsyncGenerator[object, None], transport: str) -> None:
        super().__init__()
        self.events = events
        self.transport = transport
        self.header: DeviceHeader | None = None
        self.sample: Sample | None = None
        self.shown: Sample | None = None
        self.error: BaseException | None = None
        self.sort_key = "cpu"
        self.filter_text = ""
        self.apps_only = False
        self.paused = False
        self.finished = False
        self.cpu_history: deque[float] = deque(maxlen=HISTORY)
        self.fps_history: deque[float] = deque(maxlen=HISTORY)

    # -- layout ---------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Panel(self._device_line, id="device")
        with Horizontal(id="meters"):
            with Vertical(id="cpu-column"):
                yield Panel(lambda width: render.cpu_meters(self._shown_cpu(), width), id="cpu")
                yield Panel(self._cpu_history, id="cpu-history")
            with Vertical(id="memory-column"):
                yield Panel(self._memory_meters, id="memory")
            with Vertical(id="graphics-column"):
                yield Panel(lambda width: render.fps_caption(self.shown.fps if self.shown else None), id="fps-caption")
                yield Digits("--", id="fps")
                yield Panel(lambda width: render.gpu_meter(self.shown.gpu if self.shown else None, width), id="gpu")
                yield Panel(self._fps_history, id="fps-history")
        table: DataTable[Text] = DataTable(id="processes", cursor_type="row", zebra_stripes=False)
        yield table
        yield Panel(self._status_line, id="status")
        yield Input(placeholder="filter by name or pid, Enter to keep, Esc to clear", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        self._set_columns()
        self.query_one("#processes", DataTable).focus()
        self.run_worker(self._consume(), exclusive=True, exit_on_error=False)

    # -- data -----------------------------------------------------------------------------------

    async def _consume(self) -> None:
        try:
            async with contextlib.aclosing(self.events) as events:
                async for event in events:
                    if isinstance(event, DeviceHeader):
                        self.header = event
                        self.query_one("#device", Panel).refresh()
                    elif isinstance(event, Sample):
                        self.sample = event
                        self.cpu_history.append(event.cpu.total if event.cpu else 0.0)
                        self.fps_history.append(float(event.fps or 0))
                        if not self.paused:
                            self._show(event)
        except Exception as e:  # shown by the CLI after the screen is restored
            self.error = e
            self.exit(return_code=1)
            return
        self.finished = True
        self.query_one("#status", Panel).refresh()

    def _show(self, sample: Sample) -> None:
        self.shown = sample
        for panel in self.query(Panel):
            panel.refresh(layout=True)
        digits = self.query_one("#fps", Digits)
        digits.update("--" if sample.fps is None else str(sample.fps))
        digits.set_classes(f"fps-{render.fps_grade(sample.fps)}")
        self._fill_table()

    def _shown_cpu(self):
        return self.shown.cpu if self.shown else None

    def _selected_pid(self) -> str | None:
        """The row the user moved the cursor to, or None while it sits on the top row.

        The highlight stays on the top row until the user moves it; then it follows that process
        through re-sorts, so a row you picked doesn't slide away under the cursor.
        """
        table = self.query_one("#processes", DataTable)
        if not table.row_count or table.cursor_row <= 0:
            return None
        with contextlib.suppress(Exception):
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return None

    def _fill_table(self, selected: str | None = None) -> None:
        table = self.query_one("#processes", DataTable)
        if self.shown is None:
            return
        selected = selected or self._selected_pid()
        processes = filter_processes(self.shown.processes, self.filter_text, self.apps_only)
        processes = sort_processes(processes, self.sort_key)
        table.clear()
        for process in processes:
            table.add_row(*render.process_cells(process), key=str(process.pid))
        if selected is not None:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(selected), animate=False)

    def _set_columns(self) -> None:
        table = self.query_one("#processes", DataTable)
        selected = self._selected_pid()
        table.clear(columns=True)
        for label, key in COLUMNS:
            marker = "▼" if key == self.sort_key else ""
            table.add_column(Text(f"{label}{marker}", justify="right"), key=label)
        table.add_column(f"NAME{'▲' if self.sort_key == 'name' else ''}", key="NAME")
        self._fill_table(selected)

    # -- text -----------------------------------------------------------------------------------

    def _device_line(self, width: int) -> Text:
        return render.device_line(self.header, self.shown, self.transport, time.time())

    def _cpu_history(self, width: int) -> Text:
        return render.history(self.cpu_history, width, 100, "100%", render.load_style)

    def _fps_history(self, width: int) -> Text:
        top = render.fps_scale(self.fps_history)
        return render.history(self.fps_history, width, top, str(top), render.fps_color)

    def _memory_meters(self, width: int) -> Text:
        return render.memory_meters(self.shown.memory if self.shown else None, self.shown, width)

    def _status_line(self, width: int) -> Text:
        text = Text.assemble(("sort ", render.DIM), (SORT_LABELS[self.sort_key], "bold"))
        if self.filter_text:
            text.append("  filter ", style=render.DIM)
            text.append(repr(self.filter_text), style="bold yellow")
        if self.apps_only:
            text.append("  apps only", style="bold bright_cyan")
        if self.shown is not None:
            shown = len(filter_processes(self.shown.processes, self.filter_text, self.apps_only))
            text.append(f"  {shown}/{len(self.shown.processes)} processes", style=render.DIM)
        if self.paused:
            text.append("  PAUSED", style="bold reverse yellow")
        if self.finished:
            text.append("  end of recording" if self.transport == "replay" else "  stream ended", style="bold")
        return text

    # -- actions --------------------------------------------------------------------------------

    def action_cycle_sort(self) -> None:
        self.sort_key = SORT_KEYS[(SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)]
        self._set_columns()
        self.query_one("#status", Panel).refresh()

    def action_toggle_apps(self) -> None:
        self.apps_only = not self.apps_only
        self._fill_table()
        self.query_one("#status", Panel).refresh()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        if not self.paused and self.sample is not None:
            self._show(self.sample)
        self.query_one("#status", Panel).refresh()

    def action_show_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.add_class("visible")
        field.value = self.filter_text
        field.focus()

    def action_clear_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.value = ""
        self._apply_filter("")
        field.remove_class("visible")
        self.query_one("#processes", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.remove_class("visible")
        self.query_one("#processes", DataTable).focus()

    def _apply_filter(self, text: str) -> None:
        self.filter_text = text
        self._fill_table()
        self.query_one("#status", Panel).refresh()
