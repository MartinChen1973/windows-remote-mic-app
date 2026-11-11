"""Elapsed-time source that excludes Windows suspend and hibernate time.

``time.monotonic()`` on Windows is derived from interrupt time and therefore
keeps counting while the machine is asleep, hibernated, or in modern standby.
Every deadline in the Google receiver path is a *liveness* budget - parent
heartbeat lease, capture readiness, startup timeouts, report freshness - and
those budgets must measure time the machine was actually running. A wall-clock
jump of the suspend duration silently expires all of them at the instant of
resume, before either process has had a chance to run a single loop iteration.

``QueryUnbiasedInterruptTime`` reports the same monotonic base with the bias
(suspend) time removed. Both processes of one run resolve it identically, so a
report stamp produced by the elevated worker stays comparable with the owner's
freshness window across a resume.

On non-Windows hosts, and on a Windows host that cannot resolve the entry
point, this falls back to ``time.monotonic()``: the original behaviour is
preserved rather than inventing an unverified clock.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Callable, Optional

_FILETIME_UNITS_PER_SECOND = 10_000_000

_unbiased_now: Optional[Callable[[], float]] = None
_resolution_attempted = False


def _resolve_unbiased_now() -> Optional[Callable[[], float]]:
    """Bind ``QueryUnbiasedInterruptTime`` once; return None when unavailable."""

    if sys.platform != "win32":
        return None
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel.QueryUnbiasedInterruptTime
        query.argtypes = (ctypes.POINTER(ctypes.c_ulonglong),)
        query.restype = ctypes.c_int
        counter = ctypes.c_ulonglong()
        if not query(ctypes.byref(counter)):
            return None
    except (AttributeError, OSError):
        return None

    def unbiased() -> float:
        value = ctypes.c_ulonglong()
        if not query(ctypes.byref(value)):
            # Losing the reading must not fabricate a deadline. Returning the
            # ordinary monotonic base keeps the caller's budget conventional.
            return time.monotonic()
        return value.value / _FILETIME_UNITS_PER_SECOND

    return unbiased


def now() -> float:
    """Return elapsed running time in seconds, excluding suspend intervals."""

    global _unbiased_now, _resolution_attempted

    source = _unbiased_now
    if source is None and not _resolution_attempted:
        _resolution_attempted = True
        _unbiased_now = source = _resolve_unbiased_now()
    return time.monotonic() if source is None else source()


def available() -> bool:
    """Report whether suspend time is excluded on this host."""

    global _unbiased_now, _resolution_attempted

    if _unbiased_now is None and not _resolution_attempted:
        _resolution_attempted = True
        _unbiased_now = _resolve_unbiased_now()
    return _unbiased_now is not None


# A suspend shows up as the biased (monotonic) clock advancing while the
# unbiased clock does not. Ordinary scheduling skew is milliseconds, so this
# threshold cannot fire on a busy machine; anything longer is a real suspend.
RESUME_BIAS_THRESHOLD_SECONDS = 2.0


class ResumeDetector:
    """Report when this process has resumed from suspend or hibernate.

    Compares the suspend-inclusive clock against the suspend-exclusive one.
    The difference grows only while the machine is not running, so a single
    growing sample identifies a resume without any power-management callback.
    """

    def __init__(self) -> None:
        self._last_bias = time.monotonic() - now()

    def resumed(self) -> bool:
        """Return True once per detected resume."""

        bias = time.monotonic() - now()
        grew = bias - self._last_bias
        self._last_bias = bias
        return grew >= RESUME_BIAS_THRESHOLD_SECONDS
