"""Command-sink focus fallbacks for Electron/Cursor paste delivery."""

from __future__ import annotations

import unittest
from unittest import mock

from ovb_rc003 import command_sink_windows, voice_interaction_diagnostics_windows


class CommandSinkFocusFallbackTests(unittest.TestCase):
    def test_focus_outcome_allows_chromium_foreground_paste(self) -> None:
        self.assertTrue(
            voice_interaction_diagnostics_windows.focus_outcome_allows_paste(
                "chromium_foreground_paste"
            )
        )
        self.assertFalse(
            voice_interaction_diagnostics_windows.focus_outcome_allows_paste(
                "focus_failed"
            )
        )

    def test_runtime_miss_still_tries_editable_pick(self) -> None:
        editable = mock.Mock()
        root = mock.Mock()
        auto = mock.Mock()
        auto.UIAutomationInitializerInThread.return_value.__enter__ = mock.Mock(
            return_value=None
        )
        auto.UIAutomationInitializerInThread.return_value.__exit__ = mock.Mock(
            return_value=None
        )
        auto.ControlFromHandle.return_value = root
        auto.GetFocusedControl.return_value = None
        with mock.patch.dict(
            "sys.modules",
            {"uiautomation": auto},
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_find_uia_by_runtime_id",
            return_value=None,
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_uia_pick_editable_control",
            return_value=editable,
        ) as pick, mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_uia_set_focus",
            return_value=True,
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_hwnd_focus_is",
            return_value=False,
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_win32_set_focus_attached",
            return_value=False,
        ), mock.patch.object(
            voice_interaction_diagnostics_windows,
            "_is_chromium_renderer_hwnd",
            return_value=False,
        ), mock.patch(
            "ctypes.WinDLL",
            return_value=mock.Mock(
                SetFocus=mock.Mock(return_value=0),
                GetForegroundWindow=mock.Mock(return_value=0),
            ),
        ):
            outcome = voice_interaction_diagnostics_windows.focus_command_sink_target(
                top_level_hwnd=100,
                focus_hwnd=200,
                runtime_id=[1, 2, 3],
            )
        self.assertEqual(outcome, "uia_editable")
        pick.assert_called()

    def test_delivery_proceeds_on_chromium_foreground_paste(self) -> None:
        path = command_sink_windows.state_path(None)
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            path = command_sink_windows.state_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "locked": True,
                        "top_level_hwnd": 594302,
                        "focus_hwnd": 265970,
                        "foreground_executable": "Cursor.exe",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                command_sink_windows, "_is_window", return_value=True
            ), mock.patch.object(
                command_sink_windows, "_activate_window"
            ), mock.patch.object(
                voice_interaction_diagnostics_windows,
                "focus_command_sink_target",
                return_value="chromium_foreground_paste",
            ), mock.patch.object(
                command_sink_windows.win32_input, "send_key_combo_tap"
            ) as paste:
                command_sink_windows._deliver_to_sink(submit=False, root=root)
                paste.assert_called_once_with(("ctrl", "v"))


if __name__ == "__main__":
    unittest.main()
