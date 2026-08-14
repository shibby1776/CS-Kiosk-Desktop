"""Camera-light brightness mapping shared by normal and diagnostic controls."""
from __future__ import annotations

DEFAULT_RAW_MAX = 80
DEFAULT_GAMMA = 2.4


def percent_to_raw(percent: float, *, raw_max: int = DEFAULT_RAW_MAX, gamma: float = DEFAULT_GAMMA) -> int:
    """Map operator-facing brightness percent to a protected raw PWM level.

    A gamma greater than one spreads the low raw levels across much more of the
    slider, which makes low-light photographic adjustments easier.
    """
    p = max(0.0, min(100.0, float(percent))) / 100.0
    return max(0, min(int(raw_max), int(round(int(raw_max) * (p ** float(gamma))))))


def raw_to_percent(raw: int, *, raw_max: int = DEFAULT_RAW_MAX, gamma: float = DEFAULT_GAMMA) -> float:
    """Return the approximate UI percent corresponding to a raw PWM level."""
    raw = max(0, min(int(raw_max), int(raw)))
    if raw_max <= 0 or raw <= 0:
        return 0.0
    return 100.0 * ((raw / float(raw_max)) ** (1.0 / float(gamma)))
