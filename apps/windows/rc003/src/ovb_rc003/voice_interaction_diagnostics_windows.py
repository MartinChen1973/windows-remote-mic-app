"""Privacy-safe focus/result diagnostics for Windows voice input.

Only handles, process IDs, class names, native input metadata and text lengths are observed. Window
titles and user text are deliberately never read or logged.
"""

from __future__ import annotations

import logging
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import os
from pathlib import PureWindowsPath
import time
from typing import Optional

logger = logging.getLogger(__name__)

WM_GETTEXTLENGTH = 0x000E
SMTO_ABORTIFHUNG = 0x0002
TEXT_QUERY_TIMEOUT_MS = 20

CHROMIUM_RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"
CHROMIUM_SHELL_CLASS = "Chrome_WidgetWin_1"
_ELECTRON_FOCUS_CLASSES = frozenset(
    {
        CHROMIUM_RENDERER_CLASS.casefold(),
        CHROMIUM_SHELL_CLASS.casefold(),
    }
)
_NATIVE_EDIT_CLASSES = frozenset(
    {
        "edit",
        "richedit",
        "richedit20a",
        "richedit20w",
        "richedit50w",
        "richeditd2dpt",
    }
)
_COMMAND_SINK_CHROMIUM_HOST_EXECUTABLES = frozenset(
    {
        "cursor.exe",
        "code.exe",
    }
)
_UIA_EDITABLE_CONTROL_TYPES = frozenset(
    {
        "EditControl",
        "DocumentControl",
    }
)


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    )


@dataclass(frozen=True)
class FocusSnapshot:
    supported: bool
    foreground_pid: int = 0
    foreground_class: str = ""
    focus_handle: int = 0
    focus_class: str = ""
    text_length: Optional[int] = None
    error: str = ""
    foreground_handle: int = 0
    foreground_thread_id: int = 0
    foreground_executable: str = ""
    process_query_status: str = "unavailable"
    keyboard_layout: int = 0
    foreground_stable: bool = False
    focus_editable: Optional[bool] = None
    focus_read_only: Optional[bool] = None
    focus_native_caret_detected: Optional[bool] = None
    focus_input_status: str = "unknown"


@dataclass(frozen=True)
class SubmissionObservation:
    focus_state: str
    text_state: str
    text_delta: Optional[int]


def _window_class_name(user32, hwnd: int) -> str:
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    copied = user32.GetClassNameW(wintypes.HWND(hwnd), buffer, len(buffer))
    return buffer.value if copied else ""


def _window_text_length(user32, hwnd: int) -> Optional[int]:
    if not hwnd:
        return None
    result = ctypes.c_size_t(0)
    delivered = user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd),
        WM_GETTEXTLENGTH,
        0,
        0,
        SMTO_ABORTIFHUNG,
        TEXT_QUERY_TIMEOUT_MS,
        ctypes.byref(result),
    )
    return int(result.value) if delivered else None


def _uia_focused_control_editable() -> Optional[bool]:
    """Privacy-safe UIA check: patterns and read-only flags only, never text."""

    try:
        import uiautomation as auto
    except ImportError:
        return None
    try:
        with auto.UIAutomationInitializerInThread():
            control = auto.GetFocusedControl()
            if control is None:
                return False
            control_type = str(getattr(control, "ControlTypeName", "") or "")
            if control_type in _UIA_EDITABLE_CONTROL_TYPES:
                return not _uia_control_read_only(control)
            if _control_supports_text_input(control):
                return not _uia_control_read_only(control)
            return False
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _control_supports_text_input(control: object) -> bool:
    for method_name in ("IsTextPatternAvailable", "IsValuePatternAvailable"):
        checker = getattr(control, method_name, None)
        if not callable(checker):
            continue
        try:
            if bool(checker()):
                return True
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
    return False


def _uia_focus_native_handle() -> int:
    try:
        import uiautomation as auto
    except ImportError:
        return 0
    try:
        with auto.UIAutomationInitializerInThread():
            control = auto.GetFocusedControl()
            if control is None:
                return 0
            return int(getattr(control, "NativeWindowHandle", 0) or 0)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return 0


def _uia_control_read_only(control: object) -> bool:
    for attribute in ("IsReadOnly", "IsReadonly"):
        reader = getattr(control, attribute, None)
        if callable(reader):
            try:
                return bool(reader())
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                return False
        if reader is not None:
            try:
                return bool(reader)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                return False
    return False


def _find_descendant_window_by_class(root_hwnd: int, class_name: str) -> int:
    if not root_hwnd:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    target = [0]
    target_class = class_name.casefold()

    def visit(hwnd: int) -> bool:
        if _window_class_name(user32, hwnd).casefold() == target_class:
            target[0] = int(hwnd)
            return False
        return True

    def enum_children(parent: int) -> None:
        if target[0]:
            return

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, lparam):
            if not visit(int(hwnd)):
                return False
            enum_children(int(hwnd))
            return True

        user32.EnumChildWindows(wintypes.HWND(parent), callback, 0)

    enum_children(int(root_hwnd))
    return int(target[0])


def focused_control_runtime_id() -> list[int]:
    """Runtime id of the focused UIA control at lock time; safe to persist."""

    try:
        import uiautomation as auto
    except ImportError:
        return []
    try:
        with auto.UIAutomationInitializerInThread():
            control = auto.GetFocusedControl()
            if control is None:
                return []
            runtime_id = control.GetRuntimeId()
            if not runtime_id:
                return []
            return [int(value) for value in runtime_id]
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return []


def _uia_set_focus(control: object) -> bool:
    setter = getattr(control, "SetFocus", None)
    if not callable(setter):
        return False
    try:
        return bool(setter())
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return False


def _uia_runtime_id(control: object) -> tuple[int, ...]:
    try:
        runtime_id = control.GetRuntimeId()
        if not runtime_id:
            return ()
        return tuple(int(value) for value in runtime_id)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return ()


def _find_uia_by_runtime_id(
    root: object,
    runtime_id: tuple[int, ...],
    *,
    max_nodes: int = 512,
) -> Optional[object]:
    if not runtime_id:
        return None
    queue = [root]
    visited = 0
    while queue and visited < max_nodes:
        control = queue.pop(0)
        visited += 1
        if _uia_runtime_id(control) == runtime_id:
            return control
        getter = getattr(control, "GetChildren", None)
        if not callable(getter):
            continue
        try:
            children = getter()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if not children:
            continue
        queue.extend(children)
    return None


def _uia_pick_editable_control(root: object, *, max_nodes: int = 512) -> Optional[object]:
    queue = [root]
    visited = 0
    fallback = None
    while queue and visited < max_nodes:
        control = queue.pop(0)
        visited += 1
        control_type = str(getattr(control, "ControlTypeName", "") or "")
        if control_type in _UIA_EDITABLE_CONTROL_TYPES or _control_supports_text_input(
            control
        ):
            if not _uia_control_read_only(control):
                fallback = control
                if control_type in _UIA_EDITABLE_CONTROL_TYPES:
                    return control
        getter = getattr(control, "GetChildren", None)
        if not callable(getter):
            continue
        try:
            children = getter()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if children:
            queue.extend(children)
    return fallback


_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_MK_LBUTTON = 0x0001


def focus_outcome_allows_paste(outcome: str) -> bool:
    return outcome != "focus_failed"


def _hwnd_focus_is(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetFocus.restype = wintypes.HWND
    return int(user32.GetFocus() or 0) == int(hwnd)


def _win32_set_focus_attached(*, target_hwnd: int, top_level_hwnd: int) -> bool:
    if not target_hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for name in ("GetForegroundWindow", "GetFocus", "SetFocus", "SetForegroundWindow"):
        getattr(user32, name).restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    fg = int(user32.GetForegroundWindow() or 0)
    fg_tid = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), None))
    target_tid = int(
        user32.GetWindowThreadProcessId(wintypes.HWND(int(target_hwnd)), None)
    )
    cur_tid = int(kernel32.GetCurrentThreadId())
    attached_fg = attached_cur = False
    try:
        if fg_tid and fg_tid != target_tid:
            attached_fg = bool(user32.AttachThreadInput(fg_tid, target_tid, True))
        if cur_tid != target_tid:
            attached_cur = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        if top_level_hwnd:
            user32.SetForegroundWindow(wintypes.HWND(int(top_level_hwnd)))
        focused = int(user32.SetFocus(wintypes.HWND(int(target_hwnd))) or 0)
        return bool(focused) or _hwnd_focus_is(target_hwnd)
    except (OSError, AttributeError, ctypes.ArgumentError):
        return False
    finally:
        if attached_cur:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            user32.AttachThreadInput(fg_tid, target_tid, False)


def _click_hwnd_client_center(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        return False
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return False
    x = max(1, width // 2)
    y = max(1, height // 2)
    lparam = (y << 16) | (x & 0xFFFF)
    handle = wintypes.HWND(int(hwnd))
    down = bool(
        user32.PostMessageW(handle, _WM_LBUTTONDOWN, wintypes.WPARAM(_MK_LBUTTON), lparam)
    )
    up = bool(user32.PostMessageW(handle, _WM_LBUTTONUP, 0, lparam))
    return down and up


def _is_chromium_renderer_hwnd(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    return (
        _window_class_name(user32, int(hwnd)).casefold()
        == CHROMIUM_RENDERER_CLASS.casefold()
    )


def focus_command_sink_target(
    *,
    top_level_hwnd: int,
    focus_hwnd: int,
    runtime_id: Optional[list[int]] = None,
    foreground_executable: str = "",
) -> str:
    """Refocus the locked input; returns a short outcome label for logging."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetFocus.argtypes = (wintypes.HWND,)
    user32.SetFocus.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    set_focus_result = 0
    if focus_hwnd:
        set_focus_result = int(user32.SetFocus(wintypes.HWND(int(focus_hwnd))) or 0)
    if set_focus_result or _hwnd_focus_is(focus_hwnd):
        logger.info(
            "command_sink focus: win32_set_focus ok hwnd=%s",
            focus_hwnd,
        )
        return "win32_focus"
    if focus_hwnd:
        logger.info(
            "command_sink focus: win32_set_focus failed hwnd=%s",
            focus_hwnd,
        )

    requested = tuple(int(value) for value in (runtime_id or ()))
    try:
        import uiautomation as auto
    except ImportError:
        auto = None  # type: ignore[assignment]
    if auto is not None:
        try:
            with auto.UIAutomationInitializerInThread():
                for hwnd in (int(focus_hwnd or 0), int(top_level_hwnd or 0)):
                    if not hwnd:
                        continue
                    root = auto.ControlFromHandle(hwnd)
                    if root is None:
                        logger.info(
                            "command_sink focus: uia root missing hwnd=%s",
                            hwnd,
                        )
                        continue
                    if requested:
                        match = _find_uia_by_runtime_id(root, requested)
                        if match is not None and _uia_set_focus(match):
                            logger.info(
                                "command_sink focus: uia_runtime ok hwnd=%s "
                                "runtime_id_len=%s",
                                hwnd,
                                len(requested),
                            )
                            return "uia_runtime"
                        logger.warning(
                            "command_sink focus: uia_runtime miss hwnd=%s "
                            "runtime_id_len=%s",
                            hwnd,
                            len(requested),
                        )
                    editable = _uia_pick_editable_control(root)
                    if editable is not None and _uia_set_focus(editable):
                        logger.info(
                            "command_sink focus: uia_editable ok hwnd=%s",
                            hwnd,
                        )
                        return "uia_editable"
                    logger.info(
                        "command_sink focus: uia_editable miss hwnd=%s",
                        hwnd,
                    )
                focused = auto.GetFocusedControl()
                if (
                    focused is not None
                    and (
                        _control_supports_text_input(focused)
                        or not _uia_control_read_only(focused)
                    )
                    and _uia_set_focus(focused)
                ):
                    logger.info("command_sink focus: uia_focused_control ok")
                    return "uia_focused_control"
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            logger.warning("command_sink focus: uia path failed", exc_info=True)

    if focus_hwnd and _win32_set_focus_attached(
        target_hwnd=focus_hwnd,
        top_level_hwnd=top_level_hwnd,
    ):
        logger.info(
            "command_sink focus: win32_attach ok hwnd=%s top_level=%s",
            focus_hwnd,
            top_level_hwnd,
        )
        return "win32_attach_focus"

    if focus_hwnd and _is_chromium_renderer_hwnd(focus_hwnd):
        if _click_hwnd_client_center(focus_hwnd):
            time.sleep(0.08)
            if _hwnd_focus_is(focus_hwnd):
                logger.info(
                    "command_sink focus: chromium_click ok hwnd=%s",
                    focus_hwnd,
                )
                return "chromium_click_focus"
        logger.info(
            "command_sink focus: chromium_click did not confirm hwnd=%s",
            focus_hwnd,
        )

    executable = str(foreground_executable or "").casefold()
    foreground = int(user32.GetForegroundWindow() or 0)
    if (
        top_level_hwnd
        and foreground == int(top_level_hwnd)
        and executable in _COMMAND_SINK_CHROMIUM_HOST_EXECUTABLES
        and focus_hwnd
    ):
        logger.info(
            "command_sink focus: chromium foreground paste hwnd=%s top_level=%s",
            focus_hwnd,
            top_level_hwnd,
        )
        return "chromium_foreground_paste"

    logger.warning(
        "command_sink focus: all paths failed top_level=%s focus=%s "
        "runtime_id_len=%s foreground=%s",
        top_level_hwnd,
        focus_hwnd,
        len(requested),
        foreground,
    )
    return "focus_failed"


def resolve_command_sink_focus_hwnd(snapshot: FocusSnapshot) -> int:
    """Pick a paste-friendly HWND for Electron chat hosts."""

    focus = int(snapshot.focus_handle or 0)
    if snapshot.focus_class.casefold() == CHROMIUM_RENDERER_CLASS.casefold():
        return focus
    top_level = int(snapshot.foreground_handle or 0)
    if top_level:
        renderer = _find_descendant_window_by_class(
            top_level, CHROMIUM_RENDERER_CLASS
        )
        if renderer:
            return renderer
    native = _uia_focus_native_handle()
    return native or focus


def _electron_chat_input_state(
    focus_class: str,
    foreground_executable: str,
    *,
    gui_info_ok: bool,
    stable: bool,
) -> Optional[dict[str, object]]:
    if not stable or not gui_info_ok:
        return None
    executable = str(foreground_executable or "").casefold()
    if executable not in _COMMAND_SINK_CHROMIUM_HOST_EXECUTABLES:
        return None
    uia_editable = _uia_focused_control_editable()
    if uia_editable is True:
        return {
            "focus_editable": True,
            "focus_read_only": False,
            "focus_native_caret_detected": None,
            "focus_input_status": "electron_uia_editable",
        }
    if focus_class.casefold() not in _ELECTRON_FOCUS_CLASSES:
        return None
    if uia_editable is False:
        return {
            "focus_editable": False,
            "focus_read_only": True,
            "focus_native_caret_detected": None,
            "focus_input_status": "electron_uia_not_editable",
        }
    return {
        "focus_editable": True,
        "focus_read_only": False,
        "focus_native_caret_detected": None,
        "focus_input_status": "electron_chromium_focus",
    }


def _focus_input_state(user32, focus: int, class_name: str, info,
                       gui_info_ok: bool, stable: bool) -> dict:
    """Best-effort native metadata, never a verdict on user error or IME readiness."""
    result = dict(focus_editable=None, focus_read_only=None,
                  focus_native_caret_detected=None, focus_input_status="unknown")
    if not stable:
        result["focus_input_status"] = "foreground_changed"
        return result
    if not gui_info_ok or not focus or int(info.hwndFocus or 0) != focus:
        result["focus_input_status"] = "focus_unavailable"
        return result
    # False means no native caret observed on this focus, not no text cursor:
    # browsers and other custom controls can draw their own caret.
    result["focus_native_caret_detected"] = int(info.hwndCaret or 0) == focus
    if class_name.casefold() not in _NATIVE_EDIT_CLASSES:
        result["focus_input_status"] = "unsupported_control"
        return result
    try:
        ctypes.set_last_error(0)
        style = int(user32.GetWindowLongW(wintypes.HWND(focus), -16))
        if not style and ctypes.get_last_error():
            result["focus_input_status"] = "style_unavailable"
            return result
        read_only = bool(style & 0x0800)  # ES_READONLY, also used by Rich Edit.
        result.update(focus_read_only=read_only,
                      focus_editable=not read_only and not bool(style & 0x08000000),
                      focus_input_status="native_edit_style")  # WS_DISABLED
    except (AttributeError, OSError, ValueError):
        result["focus_input_status"] = "style_unavailable"
    return result


def _process_basename(process_id: int) -> tuple[str, str]:
    """Query this PID afresh; never persist paths or cache identities across PID reuse."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return "", "open_failed"
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return "", "query_failed"
        name = PureWindowsPath(buffer.value).name
        if len(name) > 128 or any(ord(c) < 32 for c in name):
            return "", "name_rejected"
        return name, "captured"
    finally:
        kernel.CloseHandle(handle)


def _capture_windows_focus(*, include_text_length: bool = True) -> FocusSnapshot:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetGUIThreadInfo.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(_GuiThreadInfo),
    )
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetClassNameW.restype = ctypes.c_int
    user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    )
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    user32.GetKeyboardLayout.restype = wintypes.HANDLE
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = wintypes.LONG

    foreground = user32.GetForegroundWindow()
    if not foreground:
        return FocusSnapshot(True, error="no_foreground_window")
    process_id = wintypes.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    focus = int(foreground)
    gui_info_ok = bool(thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)))
    if gui_info_ok:
        focus = int(info.hwndFocus or foreground)
    try:
        executable, process_status = _process_basename(int(process_id.value))
    except (OSError, AttributeError, ValueError):
        executable, process_status = "", "query_failed"
    layout = int(user32.GetKeyboardLayout(thread_id) or 0) if thread_id else 0
    focus_class = _window_class_name(user32, focus)
    text_length = _window_text_length(user32, focus) if include_text_length else None
    input_state = _focus_input_state(user32, focus, focus_class, info, gui_info_ok, True)
    stable = int(user32.GetForegroundWindow() or 0) == int(foreground)
    if not stable:
        input_state = _focus_input_state(user32, focus, focus_class, info, gui_info_ok, False)
    elif input_state.get("focus_editable") is not True:
        electron_state = _electron_chat_input_state(
            focus_class,
            executable,
            gui_info_ok=gui_info_ok,
            stable=True,
        )
        if electron_state is not None:
            input_state.update(electron_state)
    return FocusSnapshot(
        supported=True,
        foreground_pid=int(process_id.value),
        foreground_class=_window_class_name(user32, int(foreground)),
        focus_handle=focus,
        focus_class=focus_class,
        text_length=text_length,
        foreground_handle=int(foreground),
        foreground_thread_id=int(thread_id),
        foreground_executable=executable,
        process_query_status=process_status,
        keyboard_layout=layout,
        foreground_stable=stable,
        **input_state,
    )


def capture_focus_snapshot(*, platform: Optional[str] = None,
                           include_text_length: bool = True) -> FocusSnapshot:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return FocusSnapshot(False, error="unsupported_platform")
    try:
        return _capture_windows_focus() if include_text_length else _capture_windows_focus(include_text_length=False)
    except (AttributeError, OSError, ValueError):
        return FocusSnapshot(True, error="capture_failed")


def context_fields(snapshot: FocusSnapshot) -> dict:
    """Common local observation schema; HKL alone does not identify every TSF IME."""
    return dict(foreground_available=snapshot.supported and bool(snapshot.foreground_pid),
                foreground_pid=snapshot.foreground_pid,
                foreground_thread_id=snapshot.foreground_thread_id,
                foreground_handle=snapshot.foreground_handle,
                foreground_executable=snapshot.foreground_executable,
                process_query_status=snapshot.process_query_status,
                foreground_class=snapshot.foreground_class,
                foreground_stable=snapshot.foreground_stable,
                foreground_is_app=snapshot.foreground_pid == os.getpid(),
                focus_handle=snapshot.focus_handle, focus_class=snapshot.focus_class,
                focus_editable=snapshot.focus_editable,
                focus_read_only=snapshot.focus_read_only,
                focus_native_caret_detected=snapshot.focus_native_caret_detected,
                focus_input_status=snapshot.focus_input_status,
                keyboard_layout=snapshot.keyboard_layout,
                input_method_identity="not_inferred_from_layout",
                observation_status=snapshot.error or "captured")


class ContextTimeline:
    """Writer-owned, limited observation of local changes around an input action."""
    def __init__(self, capture=None, clock=None, environment=None):
        from .input_environment_diagnostics_windows import EnvironmentSampler
        self.capture = capture or (lambda: capture_focus_snapshot(include_text_length=False))
        self.clock = clock or time.monotonic
        self.environment = environment if environment is not None else EnvironmentSampler(clock=self.clock)
        self.deadline = 0.0
        self.next_sample = 0.0
        self.previous = None
        self.last_observation = 0.0
        self.context = {}
        self.voice_active = False

    def accept(self, item: dict) -> None:
        event = item.get("event")
        if event == "mapping_trigger" and self.context.get("attempt_id") and (self.voice_active or self.deadline > self.clock()):
            return  # A concurrent ordinary action must not truncate the voice observation.
        if event in ("attempt_started", "mapping_trigger"):
            self.voice_active = event == 'attempt_started'
            self.context = {k: item[k] for k in ("attempt_id", "gesture_id", "provider") if k in item}
            self.deadline = self.clock() + (30 if event == "attempt_started" else 2)
            self.previous = None
            self.next_sample = 0
        elif event == "attempt_finished" and item.get("attempt_id") == self.context.get("attempt_id"):
            self.voice_active = False
            # Recognition/automatic submission can happen after the held key is released.
            # Reopen a bounded tail even when a long recording exhausted the first window.
            self.deadline = self.clock() + 10
            self.next_sample = 0

    def poll(self) -> Optional[dict]:
        now = self.clock()
        if not self.deadline or now < self.next_sample:
            return None
        if now > self.deadline:
            self.deadline = 0
            return dict(self.context, phase="observation_ended", sampling_interval_ms=250,
                        observation_scope="local_input_environment", target_response="unknown")
        self.next_sample = now + 0.25
        try:
            fields = context_fields(self.capture())
        except Exception:
            fields = {"observation_status": "capture_failed"}
        try:
            fields.update(self.environment.sample(fields))
        except Exception:
            fields['environment_status'] = 'capture_failed'
        fields['observation_stage'] = ('voice_active' if self.voice_active else
                                       'after_voice' if self.context.get('attempt_id') else 'mapping')
        # Sampling timestamps describe freshness, not a change in the environment.
        comparison = {key: value for key, value in fields.items() if key not in {
            "process_sample_monotonic_ms", "response_sample_monotonic_ms"}}
        unchanged = comparison == self.previous
        if unchanged and now - self.last_observation < 5.0:
            return None
        phase = "initial" if self.previous is None else "heartbeat" if unchanged else "changed"
        self.previous = comparison
        self.last_observation = now
        return dict(self.context, **fields, phase=phase, sampling_interval_ms=250,
                    observation_scope="local_input_environment", target_response="unknown")


def compare_submission(
    before: Optional[FocusSnapshot],
    after: Optional[FocusSnapshot],
) -> SubmissionObservation:
    if before is None or after is None or not before.supported or not after.supported:
        return SubmissionObservation("unavailable", "unavailable", None)
    if before.error or after.error:
        return SubmissionObservation("unavailable", "unavailable", None)
    same_focus = bool(
        before.focus_handle
        and before.focus_handle == after.focus_handle
        and before.foreground_pid == after.foreground_pid
    )
    focus_state = "same" if same_focus else "changed"
    if before.text_length is None or after.text_length is None:
        return SubmissionObservation(focus_state, "unavailable", None)
    delta = after.text_length - before.text_length
    text_state = "grew" if delta > 0 else "shrunk" if delta < 0 else "unchanged"
    return SubmissionObservation(focus_state, text_state, delta)
