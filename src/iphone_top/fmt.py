"""Short human-readable strings for the TUI. Pure functions, unit-tested."""

from __future__ import annotations

_UNITS = ("B", "K", "M", "G", "T")


def human_bytes(value: float | None) -> str:
    """1536 -> "1.5K", 2961506304 -> "2.76G". Binary units, at most four significant characters."""
    if value is None:
        return "-"
    size = float(value)
    for unit in _UNITS:
        if abs(size) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)}B"
            if abs(size) < 10:
                return f"{size:.2f}{unit}"
            if abs(size) < 100:
                return f"{size:.1f}{unit}"
            return f"{size:.0f}{unit}"
        size /= 1024
    raise AssertionError("unreachable")


def human_rate(value: float | None) -> str:
    """Bytes per second; a dash for unknown, a dot for idle, so busy rows stand out."""
    if value is None:
        return "-"
    if value < 1:
        return "·"
    return f"{human_bytes(value)}/s"


def human_uptime(seconds: float | None) -> str:
    """3317500 -> "38d 9h", 5400 -> "1h 30m", 42 -> "0m"."""
    if seconds is None or seconds < 0:
        return "?"
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def human_percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}"


def meter(fraction: float, width: int) -> tuple[int, int]:
    """Split ``width`` cells into (filled, empty) for a 0-1 fraction, htop style."""
    width = max(width, 0)
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return filled, width - filled
