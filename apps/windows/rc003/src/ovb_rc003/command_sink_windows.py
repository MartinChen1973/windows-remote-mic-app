"""Register a chat input as the paste target while working in other windows.

The user locks the focused editable control once (for example Cursor chat), then
dictates elsewhere with clipboard-based IME output and uses remote actions to
paste or submit into the locked sink without keeping that control focused.
"""

from __future__ import annotations

import json
import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config, voice_interaction_diagnostics_windows, win32_input

COMMAND_SINK_STATE_VERSION = 1
COMMAND_SINK_STATE_FILENAME = "command_sink_state.json"

NOT_LOCKED = "not_locked"
FOCUS_NOT_EDITABLE = "focus_not_editable"
SINK_INVALID = "sink_invalid"
DELIVERY_FAILED = "delivery_failed"

NOTICE_STALE_LOCK = "stale_lock"
NOTICE_LOCKED = "locked"
NOTICE_UNLOCKED = "unlocked"

_STALE_NOTICE_COOLDOWN_SECONDS = 60.0
_state_lock = threading.RLock()
_last_stale_notice_at = 0.0


class CommandSinkError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CommandSinkPublicState:
    locked: bool
    target_executable: str = ""
    tray_notice_kind: str = ""
    tray_notice_message: str = ""


@dataclass(frozen=True)
class CommandSinkActionResult:
    locked: bool
    changed: bool = False
    error: str = ""
    notice_kind: str = ""
    notice_message: str = ""


def state_path(root: Optional[Path] = None) -> Path:
    return (root or config.config_root()) / COMMAND_SINK_STATE_FILENAME


def read_public_state(root: Optional[Path] = None) -> CommandSinkPublicState:
    payload = _load_payload(root)
    notice = payload.get("tray_notice") if isinstance(payload.get("tray_notice"), dict) else {}
    return CommandSinkPublicState(
        locked=bool(payload.get("locked")),
        target_executable=str(payload.get("foreground_executable") or ""),
        tray_notice_kind=str(notice.get("kind") or ""),
        tray_notice_message=str(notice.get("message") or ""),
    )


def consume_tray_notice(root: Optional[Path] = None) -> tuple[str, str]:
    with _state_lock:
        payload = _load_payload(root)
        notice = payload.get("tray_notice")
        if not isinstance(notice, dict):
            return "", ""
        kind = str(notice.get("kind") or "")
        message = str(notice.get("message") or "")
        if not kind and not message:
            return "", ""
        payload["tray_notice"] = {"kind": "", "message": ""}
        _save_payload(payload, root)
        return kind, message


def toggle_lock(*, root: Optional[Path] = None) -> CommandSinkActionResult:
    with _state_lock:
        payload = _load_payload(root)
        if bool(payload.get("locked")):
            _write_unlocked(payload)
            _save_payload(payload, root)
            message = "指挥目标已解锁。未锁定前请勿对 Cursor 说话，以免文字无法粘贴。"
            return CommandSinkActionResult(
                locked=False,
                changed=True,
                notice_kind=NOTICE_UNLOCKED,
                notice_message=message,
            )
        snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot(
            include_text_length=False
        )
        if not snapshot.supported:
            raise CommandSinkError(FOCUS_NOT_EDITABLE)
        if snapshot.focus_editable is not True:
            raise CommandSinkError(FOCUS_NOT_EDITABLE)
        if not snapshot.focus_handle or not snapshot.foreground_handle:
            raise CommandSinkError(FOCUS_NOT_EDITABLE)
        if not _is_window(snapshot.focus_handle) or not _is_window(
            snapshot.foreground_handle
        ):
            raise CommandSinkError(FOCUS_NOT_EDITABLE)
        payload = {
            "version": COMMAND_SINK_STATE_VERSION,
            "locked": True,
            "top_level_hwnd": int(snapshot.foreground_handle),
            "focus_hwnd": int(snapshot.focus_handle),
            "foreground_executable": str(snapshot.foreground_executable or ""),
            "tray_notice": {
                "kind": NOTICE_LOCKED,
                "message": "指挥目标已锁定，可以说话；剪贴板内容将粘贴到该输入框。",
            },
        }
        _save_payload(payload, root)
        return CommandSinkActionResult(
            locked=True,
            changed=True,
            notice_kind=NOTICE_LOCKED,
            notice_message=str(payload["tray_notice"]["message"]),
        )


def paste_clipboard_to_sink(*, root: Optional[Path] = None) -> None:
    _deliver_to_sink(submit=False, root=root)


def submit_sink(*, root: Optional[Path] = None) -> None:
    _deliver_to_sink(submit=True, root=root)


def _deliver_to_sink(*, submit: bool, root: Optional[Path] = None) -> None:
    with _state_lock:
        payload = _load_payload(root)
        if not bool(payload.get("locked")):
            raise CommandSinkError(NOT_LOCKED)
        focus_hwnd = int(payload.get("focus_hwnd") or 0)
        top_level_hwnd = int(payload.get("top_level_hwnd") or 0)
        if not _sink_handles_valid(focus_hwnd, top_level_hwnd):
            _invalidate_locked(payload, root)
            raise CommandSinkError(SINK_INVALID)
    previous_hwnd = _foreground_hwnd()
    try:
        _activate_window(top_level_hwnd)
        if not _set_focus(focus_hwnd):
            raise CommandSinkError(DELIVERY_FAILED)
        if submit:
            win32_input.send_return()
        else:
            win32_input.send_key_combo_tap(("ctrl", "v"))
    except (OSError, win32_input.Win32InputUnavailableError) as exc:
        raise CommandSinkError(DELIVERY_FAILED, str(exc)) from exc
    finally:
        if previous_hwnd and _is_window(previous_hwnd):
            _activate_window(previous_hwnd)


def _invalidate_locked(payload: dict[str, Any], root: Optional[Path]) -> None:
    global _last_stale_notice_at
    now = time.monotonic()
    notice_message = "指挥目标已失效，请重新聚焦聊天输入并锁定。"
    with _state_lock:
        _write_unlocked(payload)
        if now - _last_stale_notice_at >= _STALE_NOTICE_COOLDOWN_SECONDS:
            payload["tray_notice"] = {
                "kind": NOTICE_STALE_LOCK,
                "message": notice_message,
            }
            _last_stale_notice_at = now
        else:
            payload["tray_notice"] = {"kind": "", "message": ""}
        _save_payload(payload, root)


def _write_unlocked(payload: dict[str, Any]) -> None:
    payload["version"] = COMMAND_SINK_STATE_VERSION
    payload["locked"] = False
    payload.pop("top_level_hwnd", None)
    payload.pop("focus_hwnd", None)
    payload.pop("foreground_executable", None)


def _load_payload(root: Optional[Path]) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return {"version": COMMAND_SINK_STATE_VERSION, "locked": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"version": COMMAND_SINK_STATE_VERSION, "locked": False}
    if not isinstance(data, dict):
        return {"version": COMMAND_SINK_STATE_VERSION, "locked": False}
    return data


def _save_payload(payload: dict[str, Any], root: Optional[Path]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sink_handles_valid(focus_hwnd: int, top_level_hwnd: int) -> bool:
    return bool(focus_hwnd and top_level_hwnd) and _is_window(
        focus_hwnd
    ) and _is_window(top_level_hwnd)


def _user32():
    if getattr(_user32, "_dll", None) is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetFocus.argtypes = (wintypes.HWND,)
        user32.SetFocus.restype = wintypes.HWND
        user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        _user32._dll = user32
    return _user32._dll


def _foreground_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def _is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(_user32().IsWindow(wintypes.HWND(hwnd)))


def _activate_window(hwnd: int) -> None:
    user32 = _user32()
    if not hwnd:
        return
    handle = wintypes.HWND(hwnd)
    _SW_RESTORE = 9
    _SW_SHOW = 5
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, _SW_RESTORE)
    else:
        user32.ShowWindow(handle, _SW_SHOW)
    user32.BringWindowToTop(handle)
    user32.SetForegroundWindow(handle)


def _set_focus(hwnd: int) -> bool:
    if not hwnd:
        return False
    return bool(_user32().SetFocus(wintypes.HWND(hwnd)))
