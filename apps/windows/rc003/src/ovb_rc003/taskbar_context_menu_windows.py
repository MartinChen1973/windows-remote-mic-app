"""Taskbar context menu parity with the notification-area tray menu.

Right-click on the taskbar window control (``WM_SYSCOMMAND`` / ``SC_MOUSEMENU`` with
``lParam == -1``) and shift-right-click on the title bar (``SC_CONTEXTMENU``) show the
same popup as the tray: **打开无线麦** and **完全退出**.

Windows draws its own items (for example **Close window**) on a normal taskbar
right-click. Custom tray actions are added here for the application-owned window menu
path, which is what most users expect when closing from the taskbar after minimize.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
from typing import Callable, Optional

from . import product_identity

WM_SYSCOMMAND = 0x0112
SC_KEYMENU = 0xF100
SC_MOUSEMENU = 0xF090
SC_CONTEXTMENU = 0xF093

MENU_OPEN_SETTINGS = 10001
MENU_REQUEST_FULL_EXIT = 10002

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x00000800


def _open_settings_label() -> str:
    return f"打开{product_identity.DISPLAY_NAME}"


def register_taskbar_jump_list_tasks(
    app_user_model_id: str,
    *,
    open_label: Optional[str] = None,
    exit_label: str = "完全退出",
) -> bool:
    """Best-effort hook reserved for a future jump-list implementation."""

    _ = (app_user_model_id, open_label, exit_label)
    return False


def dispatch_context_menu_command(
    command_id: int,
    *,
    on_open_settings: Callable[[], None],
    on_request_full_exit: Callable[[], None],
) -> bool:
    if command_id == MENU_OPEN_SETTINGS:
        on_open_settings()
        return True
    if command_id == MENU_REQUEST_FULL_EXIT:
        on_request_full_exit()
        return True
    return False


def show_settings_context_menu(
    hwnd: int,
    *,
    open_label: Optional[str] = None,
    exit_label: str = "完全退出",
    on_open_settings: Callable[[], None],
    on_request_full_exit: Callable[[], None],
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> bool:
    """Popup the tray-equivalent menu at the cursor or at ``(x, y)``."""

    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreatePopupMenu.restype = wintypes.HMENU
    menu = user32.CreatePopupMenu()
    if not menu:
        return False
    open_text = open_label or _open_settings_label()
    try:
        user32.AppendMenuW(
            menu,
            MF_STRING,
            MENU_OPEN_SETTINGS,
            open_text,
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(
            menu,
            MF_STRING,
            MENU_REQUEST_FULL_EXIT,
            exit_label,
        )
        point = wintypes.POINT()
        if x is None or y is None:
            user32.GetCursorPos(ctypes.byref(point))
        else:
            point.x = int(x)
            point.y = int(y)
        user32.SetForegroundWindow(hwnd)
        command_id = user32.TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD,
            point.x,
            point.y,
            0,
            hwnd,
            None,
        )
        if not command_id:
            return False
        return dispatch_context_menu_command(
            int(command_id),
            on_open_settings=on_open_settings,
            on_request_full_exit=on_request_full_exit,
        )
    finally:
        user32.DestroyMenu(menu)


def _syscmd_command(wparam: int) -> int:
    return int(wparam) & 0xFFF0


def handle_syscmd_context_request(
    message: int,
    wparam: int,
    lparam: int,
    hwnd: int,
    *,
    on_open_settings: Callable[[], None],
    on_request_full_exit: Callable[[], None],
) -> bool:
    """Show the tray-equivalent menu for taskbar window-menu requests."""

    if int(message) != WM_SYSCOMMAND:
        return False
    command = _syscmd_command(int(wparam))
    if command in {SC_MOUSEMENU, SC_KEYMENU}:
        if int(lparam) != -1:
            return False
        return show_settings_context_menu(
            hwnd,
            on_open_settings=on_open_settings,
            on_request_full_exit=on_request_full_exit,
        )
    if command == SC_CONTEXTMENU:
        x = int(lparam) & 0xFFFF
        y = (int(lparam) >> 16) & 0xFFFF
        return show_settings_context_menu(
            hwnd,
            x=x,
            y=y,
            on_open_settings=on_open_settings,
            on_request_full_exit=on_request_full_exit,
        )
    return False


def install_settings_taskbar_context_menu(
    app: object,
    *,
    hwnd: int,
    app_user_model_id: str,
    on_open_settings: Callable[[], None],
    on_request_full_exit: Callable[[], None],
) -> object | None:
    """Install native ``WM_SYSCOMMAND`` handling; returns the event filter."""

    _ = app_user_model_id
    if not hwnd:
        return None
    try:
        from PySide6.QtCore import QAbstractNativeEventFilter
    except ImportError:
        return None

    class _SettingsTaskbarContextMenuFilter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):  # type: ignore[no-untyped-def]
            if event_type != b"windows_generic_MSG":
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if int(msg.hWnd) != hwnd:
                return False, 0
            handled = handle_syscmd_context_request(
                int(msg.message),
                int(msg.wParam),
                int(msg.lParam),
                hwnd,
                on_open_settings=on_open_settings,
                on_request_full_exit=on_request_full_exit,
            )
            if handled:
                return True, 0
            return False, 0

    event_filter = _SettingsTaskbarContextMenuFilter()
    install = getattr(app, "installNativeEventFilter", None)
    if not callable(install):
        return event_filter
    install(event_filter)
    return event_filter
