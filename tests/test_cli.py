from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from pymobiledevice3.exceptions import NoDeviceConnectedError

from iphone_top import cli


def test_json_replay_emits_ndjson(recording_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--replay", str(recording_path), "--speed", "1000", "--json", "--count", "2"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["type"] for line in lines] == ["device", "sample", "sample"]
    cpus = [p["cpu"] or 0 for p in lines[1]["processes"]]
    assert cpus == sorted(cpus, reverse=True)


def test_no_device_prints_a_plain_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    @contextlib.asynccontextmanager
    async def no_device(**kwargs: Any):
        raise NoDeviceConnectedError()
        yield

    monkeypatch.setattr("iphone_top.connect.connect", no_device)
    assert cli.main(["--json"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("iphone-top: No iPhone found over USB")
    assert "Traceback" not in err


def test_transport_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def record(**kwargs: Any):
        seen.update(kwargs)
        raise NoDeviceConnectedError()
        yield

    monkeypatch.setattr("iphone_top.connect.connect", record)
    for name in ("PYMOBILEDEVICE3_UDID", "PYMOBILEDEVICE3_USBMUX", "USBMUXD_SOCKET_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    cli.main(["--json", "--rsd", "fd00::1", "58783"])
    assert seen == {"udid": None, "rsd": ("fd00::1", 58783), "tunnel": None, "usbmux": None}
    cli.main(["--json", "--tunnel"])
    assert seen["tunnel"] == "" and seen["rsd"] is None
    monkeypatch.setenv("PYMOBILEDEVICE3_UDID", "abc")
    cli.main(["--json", "--tunnel", "abc"])
    assert seen["tunnel"] == "abc" and seen["udid"] == "abc"
    monkeypatch.setenv("PYMOBILEDEVICE3_USBMUX", "/tmp/mux.sock")
    cli.main(["--json"])
    assert seen["usbmux"] == "/tmp/mux.sock"


def test_rejects_conflicting_transports_and_tiny_intervals(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--rsd", "h", "1", "--tunnel"])
    assert cli.main(["--interval", "10"]) == 2
    assert "at least 100 ms" in capsys.readouterr().err
