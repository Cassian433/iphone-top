"""The Textual UI: device line, CPU/memory/GPU meters with history, and a sortable process table."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Digits, Footer, Input
from textual.worker import Worker, WorkerCancelled, WorkerError, WorkerFailed

from iphone_top import render
from iphone_top.model import SORT_KEYS, DeviceHeader, Sample, filter_processes, sort_processes

HISTORY = 120
# (column key, sort key it shows a marker for)
COLUMNS = (("PID", "pid"), ("CPU%", "cpu"), ("MEM", "memory"), ("THR", None), ("RX/s", "rx"), ("TX/s", "tx"))
SORT_LABELS = {"cpu": "CPU", "memory": "memory", "rx": "RX", "tx": "TX", "pid": "PID", "name": "name"}
CLOSE_TIMEOUT_SECONDS = 10


class Panel(Widget):
    """A widget whose content is a Rich renderable rebuilt for the current width."""

    DEFAULT_CSS = "Panel { height: auto; }"

    def __init__(self, draw, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._draw = draw

    def render(self) -> Text:
        return self._draw(self.size.width)


class ProcessTable(DataTable[Text]):
    """The process list, updated in place so the cursor and scroll position survive each sample.

    ``clear()`` would reset both every second. The highlight stays on the top row until the user
    moves it; then it follows the process they picked through re-sorts until it exits. Moving back
    to the top row hands the highlight back to whatever is busiest.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, **kwargs)
        self.picked: str | None = None
        self._cells: dict[str, tuple[Text, ...]] = {}
        self._updating = False

    def set_columns(self, sort_key: str) -> None:
        if not self.columns:
            # Widths are measured from the first label, so it already holds the marker slot.
            for label, _ in COLUMNS:
                self.add_column(Text(f"{label} ", justify="right"), key=label)
            self.add_column("NAME ", key="NAME")
        # The marker slot is always there, so the columns don't shift when the sort changes.
        for label, key in COLUMNS:
            self.columns[self._column_key(label)].label = Text(
                f"{label}{'▼' if key == sort_key else ' '}", justify="right"
            )
        self.columns[self._column_key("NAME")].label = Text(f"NAME{'▲' if sort_key == 'name' else ' '}")

    def _column_key(self, name: str) -> Any:
        return next(key for key in self.columns if key.value == name)

    def watch_cursor_coordinate(self, old_coordinate: Coordinate, new_coordinate: Coordinate) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        if self._updating or not self.row_count:
            return
        if new_coordinate.row == 0:
            self.picked = None
            return
        with contextlib.suppress(Exception):
            self.picked = self.coordinate_to_cell_key(new_coordinate).row_key.value

    def show(self, rows: Sequence[tuple[str, tuple[Text, ...]]], alive: set[str]) -> None:
        """Make the table show ``rows`` in this order; ``alive`` is every pid in the sample."""
        self._updating = True
        try:
            wanted = dict(rows)
            for key in [key for key in self._cells if key not in wanted]:
                self.remove_row(key)
                del self._cells[key]
            column_keys = list(self.columns)
            for key, cells in rows:
                before = self._cells.get(key)
                if before is None:
                    self.add_row(*cells, key=key)
                elif before != cells:
                    for column, old, new in zip(column_keys, before, cells, strict=True):
                        if old != new:
                            self.update_cell(key, column, new, update_width=True)
                self._cells[key] = cells
            rank = {key: index for index, (key, _) in enumerate(rows)}
            self.sort(self._column_key("PID"), key=lambda pid: rank[pid.plain])
            if self.picked is not None and self.picked not in alive:
                self.picked = None  # the process exited
            if self.picked is not None and self.picked in wanted:
                self.move_cursor(row=self.get_row_index(self.picked), animate=False)
            elif self.picked is None and self.row_count:
                self.move_cursor(row=0, animate=False, scroll=False)
        finally:
            self._updating = False


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
        self.consumer: Worker[None] | None = None

    async def run_until_closed(self, headless: bool = False) -> None:
        """Run the UI, then let the sample stream finish closing before the caller drops the connection.

        Textual cancels workers on exit without waiting for them, so the stream's cleanup (stopping
        the DVT channels) would otherwise race the transport being closed underneath it.
        """
        await self.run_async(headless=headless)
        if self.consumer is not None:
            with contextlib.suppress(WorkerCancelled, WorkerFailed, WorkerError, asyncio.TimeoutError):
                await asyncio.wait_for(self.consumer.wait(), CLOSE_TIMEOUT_SECONDS)

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
        yield ProcessTable(id="processes")
        yield Panel(self._status_line, id="status")
        yield Input(placeholder="filter by name or pid, Enter to keep, Esc to clear", id="filter")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(ProcessTable)
        table.set_columns(self.sort_key)
        table.focus()
        self.consumer = self.run_worker(self._consume(), exclusive=True, exit_on_error=False)

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

    def _fill_table(self) -> None:
        if self.shown is None:
            return
        processes = filter_processes(self.shown.processes, self.filter_text, self.apps_only)
        processes = sort_processes(processes, self.sort_key)
        rows = [(str(p.pid), render.process_cells(p)) for p in processes]
        self.query_one(ProcessTable).show(rows, alive={str(p.pid) for p in self.shown.processes})

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
        self.query_one(ProcessTable).set_columns(self.sort_key)
        self._fill_table()
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
        self.query_one(ProcessTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.remove_class("visible")
        self.query_one(ProcessTable).focus()

    def _apply_filter(self, text: str) -> None:
        self.filter_text = text
        self._fill_table()
        self.query_one("#status", Panel).refresh()
