"""Open a device connection the way pymobiledevice3's own CLI does, and explain failures in plain words.

* iOS 16 and older: lockdown over usbmux; DVT runs on the same connection (needs the Developer Disk
  Image mounted and, on iOS 16, Developer Mode on).
* iOS 17+: DVT moved behind RemoteXPC, so it needs an RSD tunnel. ``--rsd HOST PORT`` and
  ``--tunnel [UDID]`` use one you started; with neither, a no-root in-process tunnel is opened
  with ``PreferredRsdTunnel`` (iOS 17.4+; 17.0-17.3 on Linux/Windows needs tunneld).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError,
    DeveloperModeIsNotEnabledError,
    DeviceNotFoundError,
    InvalidServiceError,
    NoDeviceConnectedError,
    NotPairedError,
    PairingDialogResponsePendingError,
    PasswordRequiredError,
    StartServiceError,
    TunneldConnectionError,
    UserDeniedPairingError,
    UserspaceTunnelUnavailableError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

PMD3 = "pymobiledevice3"


class UserFacingError(Exception):
    """An error whose message is already written for the person at the terminal."""


@dataclass
class Connection:
    lockdown: LockdownServiceProvider
    """For lockdown queries: device name, battery, developer-mode status."""
    dvt: LockdownServiceProvider
    """For DVT instruments. The same object on iOS 16; an RSD tunnel on iOS 17+."""
    transport: str


def ios_major(version: str | None) -> int:
    try:
        return int((version or "0").split(".", 1)[0])
    except ValueError:
        return 0


@contextlib.asynccontextmanager
async def connect(
    udid: str | None = None,
    rsd: tuple[str, int] | None = None,
    tunnel: str | None = None,
) -> AsyncIterator[Connection]:
    """Yield an open :class:`Connection`; ``tunnel`` is a UDID, or "" for the only tunneld device."""
    if rsd is not None:
        service = RemoteServiceDiscoveryService(rsd)
        await service.connect()
        try:
            yield Connection(service, service, f"rsd {rsd[0]}:{rsd[1]}")
        finally:
            await service.close()
        return

    if tunnel is not None:
        service = await _tunneld_device(tunnel or udid)
        try:
            yield Connection(service, service, "tunneld")
        finally:
            await service.close()
        return

    lockdown = await create_using_usbmux(serial=udid or await _only_usb_device())
    try:
        if ios_major(lockdown.product_version) >= 17:
            # Imported lazily: it pulls in the userspace network stack, which iOS 16 never needs.
            from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel

            try:
                async with PreferredRsdTunnel(serial=lockdown.udid) as service:
                    yield Connection(lockdown, service, "userspace tunnel")
            except UserspaceTunnelUnavailableError as e:
                raise UserFacingError(
                    f"iOS {lockdown.product_version} needs a tunnel iphone-top can't open on this host by itself.\n"
                    f"Start one in another terminal:  sudo {PMD3} remote tunneld\n"
                    "then run:                       iphone-top --tunnel ''"
                ) from e
        else:
            yield Connection(lockdown, lockdown, "usbmux")
    finally:
        await lockdown.close()


async def _only_usb_device() -> str | None:
    """The UDID of the single USB device, None if there is none (create_using_usbmux then explains)."""
    devices = await usbmux.select_devices_by_connection_type(connection_type="USB")
    if len(devices) <= 1:
        return devices[0].serial if devices else None
    listing = "\n".join(f"  {device.serial}" for device in devices)
    raise UserFacingError(
        f"{len(devices)} devices are connected over USB. Pick one with --udid (or PYMOBILEDEVICE3_UDID):\n{listing}"
    )


async def _tunneld_device(udid: str | None) -> RemoteServiceDiscoveryService:
    from pymobiledevice3.tunneld.api import get_tunneld_devices

    services = await get_tunneld_devices()
    chosen: RemoteServiceDiscoveryService | None = None
    if udid:
        chosen = next((s for s in services if s.udid == udid), None)
    elif len(services) == 1:
        chosen = services[0]
    for service in services:
        if service is not chosen:
            await service.close()
    if chosen is not None:
        return chosen
    if not services:
        raise UserFacingError("tunneld is running but has no device tunnels. Is the phone plugged in and unlocked?")
    if udid:
        raise UserFacingError(f"tunneld has no tunnel for {udid}.")
    listing = "\n".join(f"  {s.udid}" for s in services)
    raise UserFacingError(f"tunneld serves {len(services)} devices; pick one with --tunnel UDID:\n{listing}")


async def diagnose_dvt_failure(lockdown: LockdownServiceProvider) -> str:
    """Why the instruments service didn't start, using read-only lockdown queries only."""
    version = lockdown.product_version
    major = ios_major(version)
    if major >= 16:
        with contextlib.suppress(Exception):
            if not await lockdown.get_developer_mode_status():
                return developer_mode_message()
    with contextlib.suppress(Exception):
        from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService

        image_type = "Personalized" if major >= 17 else "Developer"
        async with MobileImageMounterService(lockdown) as mounter:
            if not await mounter.is_image_mounted(image_type):
                return ddi_message(version)
    return (
        f"The instruments service on the phone (iOS {version}) refused the connection.\n"
        f"Check that Developer Mode is on and the Developer Disk Image is mounted:  {PMD3} mounter auto-mount"
    )


def developer_mode_message() -> str:
    return (
        "Developer Mode is off on the phone.\n"
        "Turn it on in Settings > Privacy & Security > Developer Mode (the phone restarts).\n"
        f"If the switch isn't there, run  {PMD3} amfi reveal-developer-mode  and look again."
    )


def ddi_message(version: str | None) -> str:
    return (
        f"The Developer Disk Image isn't mounted (iOS {version}). Mount it with:\n"
        f"  {PMD3} mounter auto-mount\n"
        "It stays mounted until the phone restarts."
    )


def explain(error: BaseException) -> str | None:
    """A plain-language message for errors with a known fix, or None to let the traceback through."""
    if isinstance(error, UserFacingError):
        return str(error)
    if isinstance(error, ConnectionFailedToUsbmuxdError):
        return (
            "Can't reach usbmuxd, the service that talks to iPhones over USB.\n"
            "Linux: install usbmuxd and start it (sudo systemctl start usbmuxd). "
            "Windows: install iTunes or Apple Devices. macOS: it is always running; check the cable."
        )
    if isinstance(error, NoDeviceConnectedError):
        return "No iPhone found over USB. Plug it in, unlock it, and tap Trust if asked."
    if isinstance(error, DeviceNotFoundError):
        return f"Device not found: {error}. Check --udid / PYMOBILEDEVICE3_UDID."
    if isinstance(error, (NotPairedError, PairingDialogResponsePendingError)):
        return "The phone doesn't trust this computer yet. Unlock it, tap Trust, and run iphone-top again."
    if isinstance(error, UserDeniedPairingError):
        return "The phone refused to trust this computer. Unplug, plug back in, and tap Trust."
    if isinstance(error, PasswordRequiredError):
        return "The phone is locked. Unlock it and run iphone-top again."
    if isinstance(error, DeveloperModeIsNotEnabledError):
        return developer_mode_message()
    if isinstance(error, TunneldConnectionError):
        return f"No tunneld is running. Start one with:  sudo {PMD3} remote tunneld"
    if isinstance(error, ConnectionTerminatedError):
        return "Lost the connection to the phone (unplugged, or it went to sleep on a flaky cable)."
    if isinstance(error, ConnectionRefusedError):
        return f"Connection refused: {error}. Is the --rsd address from a running tunnel?"
    return None


def is_dvt_start_failure(error: BaseException) -> bool:
    """The instruments service itself didn't start (no DDI, Developer Mode off, iOS 17 without tunnel)."""
    return isinstance(error, (InvalidServiceError, StartServiceError))
