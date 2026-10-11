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


def test_rejects_a_zero_replay_speed(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--replay", "x.ndjson", "--speed", "0"]) == 2
    assert "--speed must be greater than 0" in capsys.readouterr().err


def test_replay_file_problems_are_explained(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.ndjson"
    assert cli.main(["--replay", str(missing), "--json"]) == cli.EXIT_ERROR
    assert f"No such file: {missing}" in capsys.readouterr().err
    garbage = tmp_path / "garbage.ndjson"
    garbage.write_text("this is not json\n")
    assert cli.main(["--replay", str(garbage), "--json"]) == cli.EXIT_ERROR
    assert "isn't an iphone-top --json recording (line 1" in capsys.readouterr().err


def test_replay_ignores_fields_from_newer_versions(
    tmp_path: Path, recording_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    header, sample = (json.loads(line) for line in recording_path.read_text().splitlines()[:2])
    header["serial_port"] = "x"
    sample["thermal"] = "nominal"
    sample["cpu"]["efficiency_cores"] = 2
    sample["processes"][0]["ppid"] = 1
    newer = tmp_path / "newer.ndjson"
    newer.write_text(json.dumps(header) + "\n" + json.dumps(sample) + "\n")
    assert cli.main(["--replay", str(newer), "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["type"] for line in lines] == ["device", "sample"]
    assert "thermal" not in lines[1] and "ppid" not in lines[1]["processes"][0]


def test_debug_log_goes_to_the_named_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("pymobiledevice3").level)
    log_file = tmp_path / "debug.log"
    monkeypatch.setenv("IPHONE_TOP_DEBUG", str(log_file))
    try:
        cli._quiet_logging()
        logging.getLogger("pymobiledevice3.dtx").debug("handshake done")
        logging.getLogger("iphone_top.sampler").debug("graphics channel failed")
        for handler in root.handlers:
            handler.flush()
        text = log_file.read_text()
        assert "handshake done" in text and "graphics channel failed" in text
    finally:
        for handler in root.handlers:
            if handler not in saved[0]:
                handler.close()
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        logging.getLogger("pymobiledevice3").setLevel(saved[2])
