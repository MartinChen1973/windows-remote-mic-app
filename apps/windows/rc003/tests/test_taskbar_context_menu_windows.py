"""Unit tests for taskbar jump list and context menu helpers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import unittest
from unittest import mock

from ovb_rc003 import config, product_identity, taskbar_context_menu_windows


class TaskbarContextMenuDispatchTests(unittest.TestCase):
    def test_dispatch_open_and_exit(self) -> None:
        opened = []
        exited = []

        self.assertTrue(
            taskbar_context_menu_windows.dispatch_context_menu_command(
                taskbar_context_menu_windows.MENU_OPEN_SETTINGS,
                on_open_settings=lambda: opened.append(True),
                on_request_full_exit=lambda: exited.append(True),
            )
        )
        self.assertEqual(opened, [True])
        self.assertEqual(exited, [])

        self.assertTrue(
            taskbar_context_menu_windows.dispatch_context_menu_command(
                taskbar_context_menu_windows.MENU_REQUEST_FULL_EXIT,
                on_open_settings=lambda: opened.append(True),
                on_request_full_exit=lambda: exited.append(True),
            )
        )
        self.assertEqual(exited, [True])

    def test_syscmd_mouse_menu_shows_tray_equivalent_actions(self) -> None:
        calls: list[int] = []

        def show_menu(*_args, **_kwargs):
            calls.append(1)
            return True

        with mock.patch.object(
            taskbar_context_menu_windows,
            "show_settings_context_menu",
            side_effect=show_menu,
        ):
            handled = taskbar_context_menu_windows.handle_syscmd_context_request(
                taskbar_context_menu_windows.WM_SYSCOMMAND,
                taskbar_context_menu_windows.SC_MOUSEMENU,
                -1,
                123,
                on_open_settings=lambda: None,
                on_request_full_exit=lambda: None,
            )
        self.assertTrue(handled)
        self.assertEqual(calls, [1])

    def test_syscmd_mouse_menu_leaves_title_bar_alone(self) -> None:
        handled = taskbar_context_menu_windows.handle_syscmd_context_request(
            taskbar_context_menu_windows.WM_SYSCOMMAND,
            taskbar_context_menu_windows.SC_MOUSEMENU,
            0,
            123,
            on_open_settings=lambda: None,
            on_request_full_exit=lambda: None,
        )
        self.assertFalse(handled)


class TaskbarJumpListTests(unittest.TestCase):
    def test_register_jump_list_is_best_effort_stub(self) -> None:
        app_id = f"{config.APP_ID}.{config.PRODUCT_ID}.Settings"
        self.assertFalse(
            taskbar_context_menu_windows.register_taskbar_jump_list_tasks(app_id)
        )

    def test_open_settings_label_uses_product_name(self) -> None:
        self.assertIn(
            product_identity.DISPLAY_NAME,
            taskbar_context_menu_windows._open_settings_label(),
        )


class TaskbarNativeFilterTests(unittest.TestCase):
    def test_filter_handles_matching_hwnd_syscmd(self) -> None:
        class FakeApp:
            def __init__(self) -> None:
                self.filters = []

            def installNativeEventFilter(self, event_filter) -> None:
                self.filters.append(event_filter)

        app = FakeApp()
        handled_flags: list[bool] = []

        with mock.patch.object(
            taskbar_context_menu_windows,
            "register_taskbar_jump_list_tasks",
            return_value=True,
        ):
            with mock.patch.object(
                taskbar_context_menu_windows,
                "handle_syscmd_context_request",
                side_effect=lambda *_a, **_k: handled_flags.append(True) or True,
            ):
                filter_obj = taskbar_context_menu_windows.install_settings_taskbar_context_menu(
                app,
                hwnd=42,
                app_user_model_id="test.app",
                on_open_settings=lambda: None,
                on_request_full_exit=lambda: None,
                )
        self.assertIsNotNone(filter_obj)
        self.assertEqual(len(app.filters), 1)
        self.assertIs(app.filters[0], filter_obj)
