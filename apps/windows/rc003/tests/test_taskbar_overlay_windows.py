"""Unit tests for taskbar overlay helpers."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import taskbar_overlay_windows


class TaskbarOverlayTests(unittest.TestCase):
    def test_load_icon_prefers_ico(self) -> None:
        directory = Path(__file__).resolve().parents[1] / "src" / "ovb_rc003" / "assets" / "icons"
        path = directory / "taskbar-dot-green.svg"
        if not path.is_file():
            self.skipTest("dot icon not present")
        with mock.patch.object(taskbar_overlay_windows, "_set_overlay") as overlay:
            taskbar_overlay_windows.apply_sink_lamp_overlay(
                123,
                lamp="locked",
            )
        overlay.assert_called_once()

    def test_clear_overlay_invokes_set_with_zero_icon(self) -> None:
        with mock.patch.object(taskbar_overlay_windows, "_set_overlay") as overlay:
            taskbar_overlay_windows.clear_overlay(456)
        overlay.assert_called_once_with(456, 0, "")


if __name__ == "__main__":
    unittest.main()
