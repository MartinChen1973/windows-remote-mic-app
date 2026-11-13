"""Unit tests for command sink lock, OK gestures, and tray notice state."""

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

    def test_on_ok_single_raises_when_unlocked(self) -> None:
        with self.assertRaises(command_sink_windows.CommandSinkError) as ctx:
            command_sink_windows.on_ok_single_tap(root=self.root)
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

    def test_ok_single_advances_baseline_then_wait(self) -> None:
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
            command_sink_windows,
            "_clipboard_snapshot",
            side_effect=[(1, "old"), (2, "new")],
        ), mock.patch.object(
            command_sink_windows, "_start_clipboard_wait_worker"
        ) as start_worker:
            command_sink_windows.on_ok_single_tap(root=self.root)
            command_sink_windows.on_ok_single_tap(root=self.root)
            start_worker.assert_called_once()

    def test_double_ok_defers_submit_while_waiting(self) -> None:
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
        command_sink_windows._session.phase = "waiting_clipboard"
        with mock.patch.object(command_sink_windows, "submit_sink") as submit:
            command_sink_windows.on_ok_double_tap(root=self.root)
            submit.assert_not_called()
        self.assertTrue(command_sink_windows._session.pending_submit)


if __name__ == "__main__":
    unittest.main()
