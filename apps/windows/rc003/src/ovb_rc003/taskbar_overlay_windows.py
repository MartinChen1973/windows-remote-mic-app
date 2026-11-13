"""Windows taskbar overlay helpers (reserved).

Primary status uses the application / notification icon with an orange
(guarded) or green (locked) badge baked into the SVG. Overlay-icon COM
support remains optional for a later release.
"""

from __future__ import annotations


def apply_sink_lamp_overlay(
    hwnd: int,
    *,
    show_lamp: bool,
    locked: bool,
) -> None:
    """No-op placeholder; lamp is shown via ``resources.find_app_icon``."""

    _ = (hwnd, show_lamp, locked)


def clear_overlay(hwnd: int) -> None:
    _ = hwnd
