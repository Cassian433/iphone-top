"""Fixtures recorded from an iPhone X on iOS 16.7.16 with scripts/record_fixtures.py.

Only Apple system processes were kept and the device name was replaced, so nothing here identifies
the phone. No test talks to a device.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def sysmontap() -> dict[str, Any]:
    return load("sysmontap_rows.json")


@pytest.fixture
def device_info() -> dict[str, Any]:
    return load("device_info.json")


@pytest.fixture
def network_samples() -> list[dict[int, dict[str, Any]]]:
    return [{int(pid): record for pid, record in sample.items()} for sample in load("network_samples.json")]


@pytest.fixture
def battery() -> dict[str, Any]:
    return load("battery.json")


@pytest.fixture
def graphics_events() -> list[Any]:
    return load("graphics_events.json")


@pytest.fixture
def recording_path() -> Path:
    return FIXTURES / "recording.ndjson"


@pytest.fixture
def recording() -> list[dict[str, Any]]:
    lines = (FIXTURES / "recording.ndjson").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
