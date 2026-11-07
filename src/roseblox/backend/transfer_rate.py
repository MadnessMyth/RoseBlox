"""Download speed and time-left estimates for progress bars."""

from __future__ import annotations

import time
from collections import deque

SIZE_UNITS = ("B", "KiB", "MiB", "GiB")


class TransferRate:
    """Average speed over the last few seconds, so the estimate does not jump around.

    Only bytes received while it is watching count, so a resumed download is not
    reported as impossibly fast.
    """

    def __init__(self, window: float = 5.0, *, minimum_span: float = 1.0, clock=time.monotonic) -> None:
        self.window = window
        self.minimum_span = minimum_span
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def add(self, downloaded: int) -> None:
        now = self._clock()
        if self._samples and downloaded < self._samples[-1][1]:
            # The transfer started over (for example a server without resume support).
            self._samples.clear()
        self._samples.append((now, downloaded))
        cutoff = now - self.window
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()

    def speed(self) -> float | None:
        """Bytes per second, or None until there is enough data to say."""
        if len(self._samples) < 2:
            return None
        (start, first), (end, last) = self._samples[0], self._samples[-1]
        span = end - start
        if span < self.minimum_span:
            return None
        return max(last - first, 0) / span

    def remaining(self, downloaded: int, total: int | None) -> float | None:
        speed = self.speed()
        if not speed or total is None or total <= 0:
            return None
        return max(total - downloaded, 0) / speed


def format_size(size: float) -> str:
    value = float(size)
    for unit in SIZE_UNITS:
        if value < 1024 or unit == SIZE_UNITS[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def format_speed(bytes_per_second: float) -> str:
    return f"{format_size(bytes_per_second)}/s"


def format_duration(seconds: float) -> str:
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 10:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def progress_text(fraction: float | None, rate: TransferRate, downloaded: int, total: int | None) -> str:
    """``"37% · 12.4 MiB/s · 1 min 20 s left"``, leaving out what is not known yet."""
    parts = [f"{fraction:.0%}"] if fraction is not None else [format_size(downloaded)]
    speed = rate.speed()
    if speed is not None:
        parts.append(format_speed(speed))
        remaining = rate.remaining(downloaded, total)
        if remaining is not None:
            parts.append(f"{format_duration(remaining)} left")
    return " · ".join(parts)
