"""Register a chat input as the paste target while working in other windows.

While a sink is locked, the OK button uses a two-step clipboard workflow:
first OK captures a clipboard baseline, second OK waits for WeChat (or any IME)
to update the clipboard then pastes into the locked control. Double OK requests
submit; if paste is still pending, submit runs after paste completes.
"""

from __future__ import annotations

import json
import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config, voice_interaction_diagnostics_windows, win32_input

logger = logging.getLogger(__name__)

COMMAND_SINK_STATE_VERSION = 1
COMMAND_SINK_STATE_FILENAME = "command_sink_state.json"

NOT_LOCKED = "not_locked"
FOCUS_NOT_EDITABLE = "focus_not_editable"
SINK_INVALID = "sink_invalid"
DELIVERY_FAILED = "delivery_failed"
CLIPBOARD_WAIT_TIMEOUT = "clipboard_wait_timeout"

NOTICE_STALE_LOCK = "stale_lock"
NOTICE_LOCKED = "locked"
NOTICE_UNLOCKED = "unlocked"

_STALE_NOTICE_COOLDOWN_SECONDS = 60.0
_CLIPBOARD_POLL_INTERVAL_SECONDS = 0.15
_CLIPBOARD_WAIT_TIMEOUT_SECONDS = 120.0

_CF_UNICODETEXT = 13

_state_lock = threading.RLock()
_session_lock = threading.RLock()
_last_stale_notice_at = 0.0


@dataclass
class _UtteranceSession:
    phase: str = "idle"
    baseline_sequence: int = 0
    baseline_text: str = ""
    pending_submit: bool = False
    worker_generation: int = 0
    worker: Optional[threading.Thread] = field(default=None, repr=False)


_session = _UtteranceSession()


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


def is_sink_locked(*, root: Optional[Path] = None) -> bool:
    return read_public_state(root).locked


def should_hijack_ok_button(*, root: Optional[Path] = None) -> bool:
    return is_sink_locked(root)


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
            _reset_session(cancel_worker=True)
            message = (
                "指挥目标已解锁（橙点）。未锁定前请勿对 Cursor 说话，"
                "以免文字无法粘贴。"
            )
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
                "message": (
                    "指挥目标已锁定（绿点）。语音结束后按一次 OK 记录剪贴板，"
                    "再按 OK 等待识别并粘贴；双击 OK 发送。"
                ),
            },
        }
        _save_payload(payload, root)
        _reset_session(cancel_worker=True)
        return CommandSinkActionResult(
            locked=True,
            changed=True,
            notice_kind=NOTICE_LOCKED,
            notice_message=str(payload["tray_notice"]["message"]),
        )


def on_ok_single_tap(*, root: Optional[Path] = None) -> None:
    if not is_sink_locked(root=root):
        raise CommandSinkError(NOT_LOCKED)
    with _session_lock:
        if _session.phase in ("idle", "waiting_clipboard", "pasting"):
            if _session.phase == "waiting_clipboard":
                _cancel_worker_locked()
            sequence, text = _clipboard_snapshot()
            _session.baseline_sequence = sequence
            _session.baseline_text = text
            _session.phase = "baseline_set"
            logger.info(
                "command_sink ok step: baseline captured sequence=%s",
                sequence,
            )
            return
        if _session.phase == "baseline_set":
            _session.phase = "waiting_clipboard"
            _start_clipboard_wait_worker(root)
            logger.info("command_sink ok step: waiting for clipboard update")
            return


def on_ok_double_tap(*, root: Optional[Path] = None) -> None:
    if not is_sink_locked(root=root):
        raise CommandSinkError(NOT_LOCKED)
    with _session_lock:
        if _session.phase in ("waiting_clipboard", "pasting"):
            _session.pending_submit = True
            logger.info("command_sink submit deferred until paste completes")
            return
    submit_sink(root=root)


def paste_clipboard_to_sink(*, root: Optional[Path] = None) -> None:
    """Legacy manual paste; starts the same wait using the current clipboard as baseline."""
    if not is_sink_locked(root=root):
        raise CommandSinkError(NOT_LOCKED)
    with _session_lock:
        sequence, text = _clipboard_snapshot()
        _session.baseline_sequence = sequence
        _session.baseline_text = text
        _session.phase = "waiting_clipboard"
        _start_clipboard_wait_worker(root)


def submit_sink(*, root: Optional[Path] = None) -> None:
    _deliver_to_sink(submit=True, root=root)


def _start_clipboard_wait_worker(root: Optional[Path]) -> None:
    _session.worker_generation += 1
    generation = _session.worker_generation
    baseline_sequence = _session.baseline_sequence
    baseline_text = _session.baseline_text

    def _worker() -> None:
        deadline = time.monotonic() + _CLIPBOARD_WAIT_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                with _session_lock:
                    if _session.worker_generation != generation:
                        return
                sequence, text = _clipboard_snapshot()
                if sequence != baseline_sequence or text != baseline_text:
                    _paste_then_optional_submit(root=root, generation=generation)
                    return
                time.sleep(_CLIPBOARD_POLL_INTERVAL_SECONDS)
            logger.warning("command_sink clipboard wait timed out")
            with _session_lock:
                if _session.worker_generation == generation:
                    _session.phase = "idle"
                    _session.pending_submit = False
        except Exception:
            logger.exception("command_sink clipboard wait failed")
            with _session_lock:
                if _session.worker_generation == generation:
                    _session.phase = "idle"
                    _session.pending_submit = False

    thread = threading.Thread(
        target=_worker,
        name="command-sink-clipboard-wait",
        daemon=True,
    )
    _session.worker = thread
    thread.start()


def _paste_then_optional_submit(*, root: Optional[Path], generation: int) -> None:
    submit_after = False
    with _session_lock:
        if _session.worker_generation != generation:
            return
        _session.phase = "pasting"
        submit_after = _session.pending_submit
        _session.pending_submit = False
    try:
        _deliver_to_sink(submit=False, root=root)
    except CommandSinkError:
        logger.exception("command_sink paste to sink failed")
    else:
        if submit_after:
            try:
                _deliver_to_sink(submit=True, root=root)
            except CommandSinkError:
                logger.exception("command_sink deferred submit failed")
    with _session_lock:
        if _session.worker_generation == generation:
            _session.phase = "idle"


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


def _reset_session(*, cancel_worker: bool) -> None:
    with _session_lock:
        if cancel_worker:
            _cancel_worker_locked()
        _session.phase = "idle"
        _session.baseline_sequence = 0
        _session.baseline_text = ""
        _session.pending_submit = False


def _cancel_worker_locked() -> None:
    _session.worker_generation += 1
    _session.worker = None


def _invalidate_locked(payload: dict[str, Any], root: Optional[Path]) -> None:
    global _last_stale_notice_at
    now = time.monotonic()
    notice_message = "指挥目标已失效，请重新聚焦聊天输入并锁定。"
    _reset_session(cancel_worker=True)
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


def _clipboard_snapshot() -> tuple[int, str]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    sequence = int(user32.GetClipboardSequenceNumber())
    if not user32.OpenClipboard(None):
        return sequence, ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return sequence, ""
        kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return sequence, ""
        try:
            return sequence, ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


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
