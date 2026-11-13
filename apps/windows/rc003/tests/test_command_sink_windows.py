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
        with mock.patch.object(command_sink_windows, "_is_window", return_value=True):
            result = command_sink_windows.toggle_lock(root=self.root)
        self.assertFalse(result.locked)
        self.assertTrue(result.changed)
        self.assertFalse(command_sink_windows.read_public_state(self.root).locked)

    def test_record_lock_rejection_sets_red_lamp(self) -> None:
        command_sink_windows.record_lock_rejection(root=self.root)
        public = command_sink_windows.read_public_state(self.root)
        self.assertFalse(public.locked)
        self.assertEqual(
            public.lock_lamp, command_sink_windows.LAMP_REJECTED
        )
        self.assertEqual(
            command_sink_windows.connected_sink_icon_state(root=self.root),
            "connected_sink_rejected",
        )

    def test_on_ok_single_raises_when_unlocked(self) -> None:
        with self.assertRaises(command_sink_windows.CommandSinkError) as ctx:
            command_sink_windows.on_ok_single_tap(root=self.root)
        self.assertEqual(ctx.exception.code, command_sink_windows.NOT_LOCKED)

    def test_drop_persisted_lock_on_process_start_clears_green(self) -> None:
        path = command_sink_windows.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked": True,
                    "lock_lamp": "locked",
                    "top_level_hwnd": 10,
                    "focus_hwnd": 20,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(command_sink_windows, "_is_window", return_value=True):
            changed = command_sink_windows.drop_persisted_lock_on_process_start(
                root=self.root
            )
        self.assertTrue(changed)
        self.assertFalse(command_sink_windows.read_public_state(self.root).locked)

    def test_stale_persisted_lock_cleared_when_hwnds_are_gone(self) -> None:
        path = command_sink_windows.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locked": True,
                    "lock_lamp": "locked",
                    "top_level_hwnd": 999001,
                    "focus_hwnd": 999002,
                    "foreground_executable": "Cursor.exe",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(command_sink_windows, "_is_window", return_value=False):
            changed = command_sink_windows.reconcile_persisted_lock(root=self.root)
        self.assertTrue(changed)
        public = command_sink_windows.read_public_state(self.root)
        self.assertFalse(public.locked)
        self.assertEqual(public.lock_lamp, command_sink_windows.LAMP_OPEN)
        self.assertFalse(command_sink_windows.should_hijack_ok_button(root=self.root))
        self.assertEqual(
            command_sink_windows.connected_sink_icon_state(root=self.root),
            "connected_sink_open",
        )

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
        kind, message, _seq = command_sink_windows.consume_tray_notice(self.root)
        self.assertEqual(kind, command_sink_windows.NOTICE_STALE_LOCK)
        self.assertEqual(message, "stale")
        second_kind, second_message, _second_seq = (
            command_sink_windows.consume_tray_notice(self.root)
        )
        self.assertEqual(second_kind, "")
        self.assertEqual(second_message, "")

    def test_tray_notice_queue_preserves_ok_steps(self) -> None:
        command_sink_windows._queue_tray_notice(
            kind=command_sink_windows.NOTICE_OK_BASELINE,
            message="first",
            root=self.root,
        )
        command_sink_windows._queue_tray_notice(
            kind=command_sink_windows.NOTICE_OK_WAIT_CLIPBOARD,
            message="second",
            root=self.root,
        )
        k1, m1, s1 = command_sink_windows.consume_tray_notice(self.root)
        k2, m2, s2 = command_sink_windows.consume_tray_notice(self.root)
        self.assertEqual((k1, m1), (command_sink_windows.NOTICE_OK_BASELINE, "first"))
        self.assertEqual(
            (k2, m2),
            (command_sink_windows.NOTICE_OK_WAIT_CLIPBOARD, "second"),
        )
        self.assertGreater(s2, s1)
        command_sink_windows._queue_tray_notice(
            kind=command_sink_windows.NOTICE_OK_BASELINE,
            message="first",
            root=self.root,
        )
        command_sink_windows._queue_tray_notice(
            kind=command_sink_windows.NOTICE_OK_BASELINE,
            message="again",
            root=self.root,
        )
        _k, _m, _s = command_sink_windows.consume_tray_notice(self.root)
        k4, m4, _s4 = command_sink_windows.consume_tray_notice(self.root)
        self.assertEqual(m4, "again")


class CommandSinkLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        command_sink_windows._session.phase = "idle"
        command_sink_windows._session.pending_submit = False

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_lock_persists_uia_runtime_id_when_available(self) -> None:
        snapshot = voice_interaction_diagnostics_windows.FocusSnapshot(
            True,
            focus_editable=True,
            focus_handle=100,
            foreground_handle=200,
            focus_class=voice_interaction_diagnostics_windows.CHROMIUM_RENDERER_CLASS,
            foreground_executable="Cursor.exe",
            focus_input_status="chromium_renderer_focus",
        )
        with mock.patch.object(
            voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            return_value=snapshot,
        ), mock.patch.object(
            command_sink_windows, "_is_window", return_value=True
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "focused_control_runtime_id",
            return_value=[7, 42, 9],
        ):
            command_sink_windows.toggle_lock(root=self.root)
        path = command_sink_windows.state_path(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("focus_uia_runtime_id"), [7, 42, 9])

    def test_lock_accepts_cursor_chromium_renderer_focus(self) -> None:
        snapshot = voice_interaction_diagnostics_windows.FocusSnapshot(
            True,
            focus_editable=True,
            focus_handle=100,
            foreground_handle=200,
            focus_class=voice_interaction_diagnostics_windows.CHROMIUM_RENDERER_CLASS,
            foreground_executable="Cursor.exe",
            focus_input_status="chromium_renderer_focus",
        )
        with mock.patch.object(
            voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            return_value=snapshot,
        ), mock.patch.object(
            command_sink_windows, "_is_window", return_value=True
        ):
            result = command_sink_windows.toggle_lock(root=self.root)
        self.assertTrue(result.locked)
        self.assertEqual(result.notice_kind, command_sink_windows.NOTICE_LOCKED)

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
        emit = mock.Mock()
        with mock.patch.object(
            command_sink_windows, "_is_window", return_value=True
        ), mock.patch.object(
            command_sink_windows,
            "_clipboard_snapshot",
            side_effect=[(1, "old"), (2, "new")],
        ), mock.patch.object(
            command_sink_windows, "_start_clipboard_wait_worker"
        ) as start_worker:
            command_sink_windows.on_ok_single_tap(
                root=self.root, emit_ok_single=emit
            )
            command_sink_windows.on_ok_single_tap(
                root=self.root, emit_ok_single=emit
            )
            start_worker.assert_called_once()
        self.assertEqual(emit.call_count, 2)

    def test_double_ok_while_waiting_schedules_submit_after_paste(self) -> None:
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
        with mock.patch.object(
            command_sink_windows, "_is_window", return_value=True
        ), mock.patch.object(command_sink_windows, "submit_sink") as submit:
            command_sink_windows.on_ok_double_tap(root=self.root)
            submit.assert_not_called()
        self.assertTrue(command_sink_windows._session.pending_submit)

    def test_ok_recognizes_double_when_idle_or_waiting_not_recording(self) -> None:
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
        with mock.patch.object(command_sink_windows, "_is_window", return_value=True):
            self.assertTrue(
                command_sink_windows.ok_recognizes_double_click(root=self.root)
            )
            command_sink_windows._session.phase = "waiting_clipboard"
            self.assertTrue(
                command_sink_windows.ok_recognizes_double_click(root=self.root)
            )
            command_sink_windows._session.phase = "recording"
            self.assertFalse(
                command_sink_windows.ok_recognizes_double_click(root=self.root)
            )

    def test_clipboard_paste_trigger_uses_sequence_and_grace_not_readable_text(
        self,
    ) -> None:
        self.assertEqual(
            command_sink_windows._clipboard_paste_trigger_reason(
                baseline_sequence=80,
                baseline_text="",
                anchor_sequence=90,
                sequence=85,
                elapsed_seconds=2.0,
            ),
            "sequence_beyond_first_ok_baseline",
        )
        self.assertEqual(
            command_sink_windows._clipboard_paste_trigger_reason(
                baseline_sequence=90,
                baseline_text="",
                anchor_sequence=90,
                sequence=90,
                elapsed_seconds=9.0,
            ),
            "grace_after_voice",
        )
        self.assertTrue(
            command_sink_windows._clipboard_ready_for_paste(
                baseline_sequence=1,
                baseline_text="",
                sequence=2,
                text="hello",
            )
        )

    def test_ok_single_during_wait_force_pastes_instead_of_restarting(self) -> None:
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
        command_sink_windows._session.worker_generation = 3
        emit = mock.Mock()
        with mock.patch.object(
            command_sink_windows, "_is_window", return_value=True
        ), mock.patch.object(command_sink_windows, "_cancel_worker_locked"), mock.patch(
            "threading.Thread",
        ) as thread_cls, mock.patch.object(
            command_sink_windows, "_paste_then_optional_submit"
        ):
            command_sink_windows.on_ok_single_tap(
                root=self.root, emit_ok_single=emit
            )
            emit.assert_not_called()
            thread_cls.assert_called_once()
        self.assertEqual(command_sink_windows._session.phase, "pasting")

    def test_clipboard_snapshot_survives_wstring_at_failure(self) -> None:
        user32 = mock.Mock()
        kernel32 = mock.Mock()
        user32.GetClipboardSequenceNumber.return_value = 99
        user32.OpenClipboard.return_value = True
        user32.GetClipboardData.return_value = 1
        kernel32.GlobalLock.return_value = 0x1000
        with mock.patch(
            "ovb_rc003.command_sink_windows.ctypes.WinDLL",
            side_effect=[user32, kernel32],
        ), mock.patch(
            "ovb_rc003.command_sink_windows.ctypes.wstring_at",
            side_effect=OSError("invalid wchar"),
        ), mock.patch(
            "ovb_rc003.command_sink_windows.ctypes.string_at",
            return_value=b"hi\x00",
        ):
            sequence, text = command_sink_windows._clipboard_snapshot()
        self.assertEqual(sequence, 99)
        self.assertIsInstance(text, str)


if __name__ == "__main__":
    unittest.main()
