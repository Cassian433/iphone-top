"""Drive the Textual app headless with a recorded session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from iphone_top.app import IPhoneTop
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
