"""Drive the Textual app headless with a recorded session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from iphone_top import render
from iphone_top.app import COLUMNS, IPhoneTop, ProcessTable
from iphone_top.model import DeviceHeader, ProcessStats, Sample
from iphone_top.sampler import replay_stream


async def wait_for(app: IPhoneTop, condition: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting for the app"
        await asyncio.sleep(0.02)


def first_pid(app: IPhoneTop) -> int:
    table = app.query_one("#processes", DataTable)
    key = table.coordinate_to_cell_key(Coordinate(0, 0)).row_key.value
    assert key is not None
    return int(key)


async def test_replayed_session(recording_path: Path, recording: list[dict[str, Any]]) -> None:
    last = recording[-1]
    app = IPhoneTop(replay_stream(recording_path, speed=1000), transport="replay")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(app, lambda: app.finished)
        await pilot.pause()
        table = app.query_one("#processes", DataTable)
        assert table.row_count == len(last["processes"])
        busiest = max(last["processes"], key=lambda p: p["cpu"] or 0)
        assert first_pid(app) == busiest["pid"]

        await pilot.press("s")
        assert app.sort_key == "memory"
        biggest = max(last["processes"], key=lambda p: p["memory"] or 0)
        assert first_pid(app) == biggest["pid"]

        await pilot.press("slash")
        await pilot.press(*"springboard")
        await pilot.pause()
        assert app.filter_text == "springboard"
        matches = [p for p in last["processes"] if "springboard" in p["name"].lower()]
        assert 1 <= len(matches) < 5  # SpringBoard and springboardservicesrelay
        assert table.row_count == len(matches)
        await pilot.press("enter")
        assert app.focused is table

        await pilot.press("escape")
        assert app.filter_text == ""
        assert table.row_count == len(last["processes"])

        await pilot.press("a")
        apps = [p for p in last["processes"] if p["app"]]
        assert apps
        assert table.row_count == len(apps)

        await pilot.press("p")
        assert app.paused
        await pilot.press("q")
    assert app.error is None


async def test_errors_from_the_stream_end_the_app(recording_path: Path) -> None:
    async def failing():
        async for event in replay_stream(recording_path, speed=1000):
            yield event
            raise ConnectionResetError("unplugged")

    app = IPhoneTop(failing(), transport="replay")
    async with app.run_test(size=(100, 30)):
        await wait_for(app, lambda: app.error is not None)
    assert isinstance(app.error, ConnectionResetError)
    assert app.return_code == 1


async def test_cursor_stays_on_top_until_moved_then_follows_the_process(recording_path: Path) -> None:
    app = IPhoneTop(replay_stream(recording_path, speed=1000), transport="replay")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(app, lambda: app.finished)
        table = app.query_one("#processes", DataTable)
        await pilot.press("s")
        assert table.cursor_row == 0

        await pilot.press("down", "down")
        picked = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        await pilot.press("s")  # re-sort by RX: the picked process moves, the cursor goes with it
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value == picked


def process(pid: int, cpu: float) -> ProcessStats:
    return ProcessStats(pid=pid, name=f"proc{pid}", cpu=cpu, memory=pid * 1024)


async def queued(queue: asyncio.Queue[Any]) -> AsyncGenerator[object, None]:
    yield DeviceHeader(name="iPhone", model="iPhone X", product_type="iPhone10,6", ios_version="16.7", build="")
    while (item := await queue.get()) is not None:
        yield item


def cursor_pid(table: ProcessTable) -> str | None:
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


async def show(app: IPhoneTop, queue: asyncio.Queue[Any], sample: Sample) -> None:
    await queue.put(sample)
    await wait_for(app, lambda: app.shown is sample)


async def test_the_highlight_follows_a_picked_process_to_the_top_and_back() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    app = IPhoneTop(queued(queue), transport="test")
    async with app.run_test(size=(100, 30)) as pilot:
        table = app.query_one(ProcessTable)
        await show(app, queue, Sample(time=1, processes=[process(1, 50), process(2, 40), process(3, 30)]))
        assert cursor_pid(table) == "1"
        await pilot.press("down", "down")
        assert table.picked == "3"

        await show(app, queue, Sample(time=2, processes=[process(3, 90), process(1, 50), process(2, 40)]))
        assert (table.cursor_row, cursor_pid(table)) == (0, "3")
        await show(app, queue, Sample(time=3, processes=[process(2, 95), process(3, 90), process(1, 50)]))
        assert (table.cursor_row, cursor_pid(table)) == (1, "3")  # still on the pick, not the new top

        await pilot.press("up")  # back on the top row: the highlight rides the top again
        assert table.picked is None
        await show(app, queue, Sample(time=4, processes=[process(1, 99), process(2, 95), process(3, 90)]))
        assert (table.cursor_row, cursor_pid(table)) == (0, "1")

        await pilot.press("down")
        assert table.picked == "2"
        await show(app, queue, Sample(time=5, processes=[process(1, 99), process(3, 90)]))  # pid 2 exited
        assert table.picked is None
        assert (table.cursor_row, cursor_pid(table)) == (0, "1")
        await queue.put(None)


async def test_a_scrolled_table_stays_put_when_samples_arrive() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    app = IPhoneTop(queued(queue), transport="test")
    async with app.run_test(size=(100, 30)) as pilot:
        table = app.query_one(ProcessTable)
        await show(app, queue, Sample(time=1, processes=[process(pid, 300 - pid) for pid in range(1, 300)]))
        await pilot.pause()
        table.scroll_to(y=50, animate=False)
        await pilot.pause()
        assert table.scroll_y == 50
        await show(app, queue, Sample(time=2, processes=[process(pid, 301 - pid) for pid in range(1, 300)]))
        await pilot.pause()
        assert table.scroll_y == 50
        assert table.picked is None and table.cursor_row == 0
        await queue.put(None)


async def test_quitting_waits_for_the_stream_to_close() -> None:
    closed: list[float] = []

    async def slow_to_close() -> AsyncGenerator[object, None]:
        try:
            yield DeviceHeader(name="iPhone", model="iPhone X", product_type="", ios_version="16.7", build="")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.3)  # stopping DVT channels takes a round trip or two
            closed.append(asyncio.get_running_loop().time())

    app = IPhoneTop(slow_to_close(), transport="test")

    async def quit_once_running() -> None:
        await wait_for(app, lambda: app.header is not None)
        app.exit()

    quitter = asyncio.create_task(quit_once_running())
    await asyncio.wait_for(app.run_until_closed(headless=True), 10)
    await quitter
    assert closed, "run_until_closed returned before the stream finished closing"


def test_column_widths_fit_the_longest_values() -> None:
    # The numeric columns have fixed widths, so a value wider than its column would be cut off.
    widest = ProcessStats(
        pid=99999, name="x", cpu=1000.0, memory=1023 * 1024**2, threads=9999, rx_rate=1023 * 1024, tx_rate=1023 * 1024
    )
    for (label, _, width), cell in zip(COLUMNS, render.process_cells(widest), strict=False):
        assert cell.cell_len <= width, label


async def test_only_the_name_column_sizes_itself() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    app = IPhoneTop(queued(queue), transport="test")
    async with app.run_test(size=(100, 30)):
        table = app.query_one(ProcessTable)
        await show(app, queue, Sample(time=1, processes=[process(1, 50), process(2, 40)]))
        sizing = {key.value: column.auto_width for key, column in table.columns.items()}
        assert sizing == {
            "PID": False,
            "CPU%": False,
            "MEM": False,
            "THR": False,
            "RX/s": False,
            "TX/s": False,
            "NAME": True,
        }
        await queue.put(None)
