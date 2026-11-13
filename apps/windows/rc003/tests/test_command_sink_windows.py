"""Unit tests for command sink lock, paste, and tray notice state."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import command_sink_windows, voice_interaction_diagnostics_windows


class CommandSinkStateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_toggle_unlock_clears_locked_flag(self) -> None:
        path = command_sink_windows.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "locked": True, "focus_hwnd": 1}),
            encoding="utf-8",
        )
        result = command_sink_windows.toggle_lock(root=self.root)
        self.assertFalse(result.locked)
        self.assertTrue(result.changed)
        self.assertFalse(command_sink_windows.read_public_state(self.root).locked)

    def test_paste_without_lock_raises(self) -> None:
        with self.assertRaises(command_sink_windows.CommandSinkError) as ctx:
            command_sink_windows.paste_clipboard_to_sink(root=self.root)
        self.assertEqual(ctx.exception.code, command_sink_windows.NOT_LOCKED)

    def test_consume_tray_notice_clears_payload(self) -> None:
        path = command_sink_windows.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked": False,
                    "tray_notice": {
                        "kind": command_sink_windows.NOTICE_STALE_LOCK,
                        "message": "stale",
                    },
                }
            ),
            encoding="utf-8",
        )
        kind, message = command_sink_windows.consume_tray_notice(self.root)
        self.assertEqual(kind, command_sink_windows.NOTICE_STALE_LOCK)
        self.assertEqual(message, "stale")
        second_kind, second_message = command_sink_windows.consume_tray_notice(
            self.root
        )
        self.assertEqual(second_kind, "")
        self.assertEqual(second_message, "")


class CommandSinkLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_lock_requires_editable_focus(self) -> None:
        snapshot = voice_interaction_diagnostics_windows.FocusSnapshot(
            True,
            focus_editable=False,
            focus_handle=100,
            foreground_handle=200,
        )
        with mock.patch.object(
            voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            return_value=snapshot,
        ):
            with self.assertRaises(command_sink_windows.CommandSinkError) as ctx:
                command_sink_windows.toggle_lock(root=self.root)
        self.assertEqual(
            ctx.exception.code, command_sink_windows.FOCUS_NOT_EDITABLE
        )

    def test_deliver_invalidates_stale_sink(self) -> None:
        path = command_sink_windows.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked": True,
                    "top_level_hwnd": 10,
                    "focus_hwnd": 20,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            command_sink_windows, "_is_window", return_value=False
        ):
            with self.assertRaises(command_sink_windows.CommandSinkError) as ctx:
                command_sink_windows.paste_clipboard_to_sink(root=self.root)
        self.assertEqual(ctx.exception.code, command_sink_windows.SINK_INVALID)
        self.assertFalse(command_sink_windows.read_public_state(self.root).locked)


if __name__ == "__main__":
    unittest.main()
