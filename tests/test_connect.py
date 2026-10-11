from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pymobiledevice3.exceptions import (
    ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError,
    DeveloperModeIsNotEnabledError,
    InvalidServiceError,
    NoDeviceConnectedError,
    NotPairedError,
    PasswordRequiredError,
    StartServiceError,
    TunneldConnectionError,
)

from iphone_top import connect
from iphone_top.connect import UserFacingError, diagnose_dvt_failure, explain, ios_major, is_dvt_start_failure


def test_ios_major() -> None:
    assert ios_major("16.7.16") == 16
    assert ios_major("17.4") == 17
    assert ios_major("") == 0
    assert ios_major(None) == 0
    assert ios_major("garbage") == 0


@pytest.mark.parametrize(
    ("error", "needle"),
    [
        (NoDeviceConnectedError(), "No iPhone found over USB"),
        (ConnectionFailedToUsbmuxdError(), "usbmuxd"),
        (NotPairedError(), "tap Trust"),
        (PasswordRequiredError(), "Unlock it"),
        (DeveloperModeIsNotEnabledError(), "Developer Mode is off"),
        (TunneldConnectionError(), "sudo pymobiledevice3 remote tunneld"),
        (ConnectionTerminatedError("closed"), "Lost the connection"),
        (UserFacingError("as written"), "as written"),
    ],
)
def test_explain_known_errors(error: BaseException, needle: str) -> None:
    message = explain(error)
    assert message is not None
    assert needle in message


def test_explain_lets_unknown_errors_through() -> None:
    assert explain(ValueError("bug")) is None


def test_dvt_start_failures() -> None:
    assert is_dvt_start_failure(StartServiceError("com.apple.instruments.remoteserver", "InvalidService"))
    assert is_dvt_start_failure(InvalidServiceError("No such service", "udid", "17.5"))
    assert not is_dvt_start_failure(NotPairedError())


class FakeLockdown:
    def __init__(self, version: str, developer_mode: bool) -> None:
        self.product_version = version
        self.developer_mode = developer_mode

    async def get_developer_mode_status(self) -> bool:
        return self.developer_mode


class FakeMounter:
    mounted = True
    asked: ClassVar[list[str]] = []

    def __init__(self, lockdown: Any) -> None:
        pass

    async def __aenter__(self) -> FakeMounter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def is_image_mounted(self, image_type: str) -> bool:
        FakeMounter.asked.append(image_type)
        return FakeMounter.mounted


@pytest.fixture
def mounter(monkeypatch: pytest.MonkeyPatch) -> type[FakeMounter]:
    import pymobiledevice3.services.mobile_image_mounter as module

    FakeMounter.mounted = True
    FakeMounter.asked = []
    monkeypatch.setattr(module, "MobileImageMounterService", FakeMounter)
    return FakeMounter


async def test_diagnose_developer_mode_off(mounter: type[FakeMounter]) -> None:
    message = await diagnose_dvt_failure(FakeLockdown("16.7.16", developer_mode=False))  # type: ignore[arg-type]
    assert message == connect.developer_mode_message()
    assert mounter.asked == []


async def test_diagnose_missing_ddi(mounter: type[FakeMounter]) -> None:
    mounter.mounted = False
    message = await diagnose_dvt_failure(FakeLockdown("16.7.16", developer_mode=True))  # type: ignore[arg-type]
    assert "pymobiledevice3 mounter auto-mount" in message
    assert "16.7.16" in message
    assert mounter.asked == ["Developer"]


async def test_diagnose_ios17_checks_the_personalized_image(mounter: type[FakeMounter]) -> None:
    mounter.mounted = False
    await diagnose_dvt_failure(FakeLockdown("17.5", developer_mode=True))  # type: ignore[arg-type]
    assert mounter.asked == ["Personalized"]


async def test_diagnose_falls_back_to_a_generic_hint(mounter: type[FakeMounter]) -> None:
    message = await diagnose_dvt_failure(FakeLockdown("15.8", developer_mode=True))  # type: ignore[arg-type]
    assert "refused" in message
    assert "mounter auto-mount" in message
