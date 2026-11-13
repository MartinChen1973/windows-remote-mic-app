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
from typing import Any, Callable, Optional

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
NOTICE_LOCK_REJECTED = "lock_rejected"
NOTICE_OK_BASELINE = "ok_baseline"
NOTICE_OK_WAIT_CLIPBOARD = "ok_wait_clipboard"
NOTICE_OK_FORCE_PASTE = "ok_force_paste"
NOTICE_OK_SUBMIT = "ok_submit"
NOTICE_OK_PASTE_FAILED = "ok_paste_failed"

LAMP_OPEN = "open"
LAMP_LOCKED = "locked"
LAMP_REJECTED = "rejected"

_STALE_NOTICE_COOLDOWN_SECONDS = 60.0
_TRAY_NOTICE_KIND_COOLDOWN_SECONDS = 30.0
_TRAY_NOTICE_QUEUE_MAX = 20
_TRAY_NOTICE_KINDS_NO_COOLDOWN = frozenset(
    {
        NOTICE_OK_BASELINE,
        NOTICE_OK_WAIT_CLIPBOARD,
        NOTICE_OK_FORCE_PASTE,
        NOTICE_OK_SUBMIT,
        NOTICE_OK_PASTE_FAILED,
    }
)
_CLIPBOARD_POLL_INTERVAL_SECONDS = 0.15
_CLIPBOARD_WAIT_TIMEOUT_SECONDS = 120.0
_CLIPBOARD_POLL_LOG_INTERVAL_SECONDS = 2.0
_CLIPBOARD_TEXT_POPULATE_WAIT_SECONDS = 8.0
_CLIPBOARD_DELAYED_RENDER_ATTEMPTS_QUICK = 6
_CLIPBOARD_DELAYED_RENDER_ATTEMPTS_PATIENT = 48
_CLIPBOARD_MIN_WAIT_BEFORE_PASTE_SECONDS = 1.2
_CLIPBOARD_GRACE_PASTE_SECONDS = 8.0
_CLIPBOARD_SEQUENCE_SETTLE_SECONDS = 0.35

_CF_UNICODETEXT = 13
_CF_TEXT = 1
_CF_OEMTEXT = 7
_CLIPBOARD_TEXT_CF_ORDER = (_CF_UNICODETEXT, _CF_TEXT, _CF_OEMTEXT)

_state_lock = threading.RLock()
_session_lock = threading.RLock()
_last_stale_notice_at = 0.0
_last_tray_notice_queued_at: dict[str, float] = {}
_tray_notice_seq = 0


@dataclass
class _UtteranceSession:
    phase: str = "idle"
    baseline_sequence: int = 0
    baseline_text: str = ""
    pending_submit: bool = False
    worker_generation: int = 0
    worker: Optional[threading.Thread] = field(default=None, repr=False)
    foreground_hwnd_before_voice: int = 0


_session = _UtteranceSession()


class CommandSinkError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CommandSinkPublicState:
    locked: bool
    lock_lamp: str = LAMP_OPEN
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
    return is_sink_locked(root=root)


def ok_recognizes_double_click(*, root: Optional[Path] = None) -> bool:
    """While locked, double-click is submit (or submit-after-paste when waiting).

    Double recognition stays off during ``recording`` so a quick second press
    counts as the second single-click (end voice and wait for clipboard).
    It stays on during ``idle`` and ``waiting_clipboard`` so a deliberate
    double-click can submit without the first release being treated as a lone
    single that cancels the clipboard wait.
    """

    if not is_sink_locked(root=root):
        return True
    with _session_lock:
        return _session.phase in ("idle", "waiting_clipboard")


def state_path(root: Optional[Path] = None) -> Path:
    return (root or config.config_root()) / COMMAND_SINK_STATE_FILENAME


def read_public_state(root: Optional[Path] = None) -> CommandSinkPublicState:
    payload = _load_payload(root)
    notice = payload.get("tray_notice") if isinstance(payload.get("tray_notice"), dict) else {}
    locked = bool(payload.get("locked"))
    return CommandSinkPublicState(
        locked=locked,
        lock_lamp=_resolve_lock_lamp(payload),
        target_executable=str(payload.get("foreground_executable") or ""),
        tray_notice_kind=str(notice.get("kind") or ""),
        tray_notice_message=str(notice.get("message") or ""),
    )


def connected_sink_icon_state(*, root: Optional[Path] = None) -> str:
    """Tray / taskbar icon id: orange open, green locked, red rejected."""

    public = read_public_state(root)
    if public.locked:
        return "connected_sink_locked"
    if public.lock_lamp == LAMP_REJECTED:
        return "connected_sink_rejected"
    return "connected_sink_open"


def taskbar_lamp_mode(*, root: Optional[Path] = None) -> str:
    public = read_public_state(root)
    if public.locked:
        return LAMP_LOCKED
    if public.lock_lamp == LAMP_REJECTED:
        return LAMP_REJECTED
    return LAMP_OPEN


def record_lock_rejection(
    *,
    root: Optional[Path] = None,
    snapshot: Optional[voice_interaction_diagnostics_windows.FocusSnapshot] = None,
) -> None:
    with _state_lock:
        payload = _load_payload(root)
        if bool(payload.get("locked")):
            return
        _write_unlocked(payload)
        payload["lock_lamp"] = LAMP_REJECTED
        payload["tray_notice"] = {
            "kind": NOTICE_LOCK_REJECTED,
            "message": (
                "锁定失败（红点）：请把焦点放在可编辑的聊天输入框上，"
                "再按指挥目标锁定键。"
            ),
        }
        if snapshot is not None:
            payload["last_lock_rejection"] = {
                "foreground_executable": snapshot.foreground_executable,
                "foreground_class": snapshot.foreground_class,
                "focus_class": snapshot.focus_class,
                "focus_input_status": snapshot.focus_input_status,
                "focus_editable": snapshot.focus_editable,
            }
        _save_payload(payload, root)


def _resolve_lock_lamp(payload: dict[str, Any]) -> str:
    if bool(payload.get("locked")):
        return LAMP_LOCKED
    lamp = str(payload.get("lock_lamp") or LAMP_OPEN)
    if lamp == LAMP_REJECTED:
        return LAMP_REJECTED
    return LAMP_OPEN


def _queue_tray_notice(
    *,
    kind: str,
    message: str,
    root: Optional[Path] = None,
) -> None:
    if not kind and not message:
        return
    global _last_tray_notice_queued_at, _tray_notice_seq
    now = time.monotonic()
    notice_kind = str(kind)
    if notice_kind not in _TRAY_NOTICE_KINDS_NO_COOLDOWN:
        last_at = _last_tray_notice_queued_at.get(notice_kind, 0.0)
        if now - last_at < _TRAY_NOTICE_KIND_COOLDOWN_SECONDS:
            logger.info(
                "command_sink tray notice skipped: kind=%s reason=cooldown",
                notice_kind,
            )
            return
    _tray_notice_seq += 1
    seq = _tray_notice_seq
    entry = {"kind": notice_kind, "message": str(message), "seq": seq}
    with _state_lock:
        payload = _load_payload(root)
        queue = payload.get("tray_notice_queue")
        if not isinstance(queue, list):
            queue = []
        queue.append(entry)
        if len(queue) > _TRAY_NOTICE_QUEUE_MAX:
            queue = queue[-_TRAY_NOTICE_QUEUE_MAX :]
        payload["tray_notice_queue"] = queue
        payload["tray_notice"] = entry
        _save_payload(payload, root)
    _last_tray_notice_queued_at[notice_kind] = now
    logger.info(
        "command_sink tray notice queued: kind=%s seq=%s queue_len=%s",
        notice_kind,
        seq,
        len(queue),
    )
    if notice_kind == NOTICE_OK_PASTE_FAILED:
        logger.warning(
            "command_sink paste failed (user-visible): %s",
            entry["message"],
        )


def consume_tray_notice(root: Optional[Path] = None) -> tuple[str, str, int]:
    with _state_lock:
        payload = _load_payload(root)
        queue = payload.get("tray_notice_queue")
        if isinstance(queue, list) and queue:
            entry = queue.pop(0)
            payload["tray_notice_queue"] = queue
            payload["tray_notice"] = queue[-1] if queue else {"kind": "", "message": ""}
            _save_payload(payload, root)
            if isinstance(entry, dict):
                kind = str(entry.get("kind") or "")
                message = str(entry.get("message") or "")
                seq = int(entry.get("seq") or 0)
                if kind or message:
                    logger.info(
                        "command_sink tray notice consumed: kind=%s seq=%s",
                        kind,
                        seq,
                    )
                    return kind, message, seq
            return "", "", 0
        notice = payload.get("tray_notice")
        if not isinstance(notice, dict):
            return "", "", 0
        kind = str(notice.get("kind") or "")
        message = str(notice.get("message") or "")
        if not kind and not message:
            return "", "", 0
        payload["tray_notice"] = {"kind": "", "message": ""}
        _save_payload(payload, root)
        seq = int(notice.get("seq") or 0)
        return kind, message, seq


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
        focus_hwnd = (
            voice_interaction_diagnostics_windows.resolve_command_sink_focus_hwnd(
                snapshot
            )
        )
        lock_rejected = (
            not snapshot.supported
            or snapshot.focus_editable is not True
            or not focus_hwnd
            or not snapshot.foreground_handle
            or not _is_window(focus_hwnd)
            or not _is_window(snapshot.foreground_handle)
        )
        if lock_rejected:
            record_lock_rejection(root=root, snapshot=snapshot)
            raise CommandSinkError(FOCUS_NOT_EDITABLE)
        uia_runtime_id = voice_interaction_diagnostics_windows.focused_control_runtime_id()
        payload = {
            "version": COMMAND_SINK_STATE_VERSION,
            "locked": True,
            "lock_lamp": LAMP_LOCKED,
            "top_level_hwnd": int(snapshot.foreground_handle),
            "focus_hwnd": int(focus_hwnd),
            "foreground_executable": str(snapshot.foreground_executable or ""),
            **(
                {"focus_uia_runtime_id": uia_runtime_id}
                if uia_runtime_id
                else {}
            ),
            "tray_notice": {
                "kind": NOTICE_LOCKED,
                "message": (
                    "指挥目标已锁定（绿点）。第一次 OK 启动微信输入法，"
                    "第二次 OK 结束说话并等待剪贴板粘贴；双击 OK 发送。"
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


def on_ok_single_tap(
    *,
    root: Optional[Path] = None,
    emit_ok_single: Optional[Callable[[], None]] = None,
) -> None:
    if not is_sink_locked(root=root):
        raise CommandSinkError(NOT_LOCKED)
    emit_after = False
    notice: tuple[str, str] = ("", "")
    force_paste_generation: Optional[int] = None
    force_paste_root = root
    with _session_lock:
        if _session.phase == "pasting":
            logger.info(
                "command_sink ok single ignored during phase=%s",
                _session.phase,
            )
            return
        if _session.phase == "waiting_clipboard":
            _cancel_worker_locked()
            force_paste_generation = _session.worker_generation
            _session.phase = "pasting"
            notice = (
                NOTICE_OK_FORCE_PASTE,
                (
                    "等待剪贴板期间再按 OK：不再重新录音，"
                    "立即向锁定的输入框粘贴剪贴板内容。"
                ),
            )
            logger.info(
                "command_sink ok single during clipboard wait: paste now "
                "generation=%s",
                force_paste_generation,
            )
        elif _session.phase == "idle":
            _session.foreground_hwnd_before_voice = _capture_voice_foreground_hwnd(
                root
            )
            sequence, text = _clipboard_snapshot()
            _session.baseline_sequence = sequence
            _session.baseline_text = text
            _session.phase = "recording"
            emit_after = True
            notice = (
                NOTICE_OK_BASELINE,
                (
                    "第一次 OK：已发送你的「确定·单击」映射（启动微信输入法），"
                    "并记录剪贴板基线。说完后再按一次 OK。"
                ),
            )
            logger.info(
                "command_sink ok step: recording started sequence=%s "
                "baseline_text_len=%s voice_foreground_hwnd=%s",
                sequence,
                len(text),
                _session.foreground_hwnd_before_voice,
            )
        elif _session.phase == "recording":
            _session.phase = "waiting_clipboard"
            _start_clipboard_wait_worker(root)
            emit_after = True
            notice = (
                NOTICE_OK_WAIT_CLIPBOARD,
                (
                    "第二次 OK：已再次发送「确定·单击」映射（结束微信输入法），"
                    "正在等待剪贴板更新并自动粘贴到锁定的输入框。"
                ),
            )
            logger.info(
                "command_sink ok step: waiting for clipboard update "
                "baseline_sequence=%s baseline_text_len=%s",
                _session.baseline_sequence,
                len(_session.baseline_text),
            )
        else:
            logger.info(
                "command_sink ok single ignored during phase=%s",
                _session.phase,
            )
            return
    if force_paste_generation is not None:
        generation = force_paste_generation

        def _immediate_paste() -> None:
            seq, text = _clipboard_snapshot(patient=True)
            _paste_then_optional_submit(
                root=force_paste_root,
                generation=generation,
                clipboard_sequence=seq,
                clipboard_text=text,
            )

        threading.Thread(
            target=_immediate_paste,
            name="command-sink-force-paste",
            daemon=True,
        ).start()
    if emit_after and emit_ok_single is not None:
        emit_ok_single()
    if notice[0]:
        _queue_tray_notice(kind=notice[0], message=notice[1], root=root)


def on_ok_double_tap(*, root: Optional[Path] = None) -> None:
    if not is_sink_locked(root=root):
        raise CommandSinkError(NOT_LOCKED)
    with _session_lock:
        phase = _session.phase
        if phase == "waiting_clipboard":
            _session.pending_submit = True
            logger.info(
                "command_sink ok double while waiting: submit after paste completes"
            )
            _queue_tray_notice(
                kind=NOTICE_OK_SUBMIT,
                message=(
                    "已记录双击提交：粘贴完成后将向锁定的聊天输入框发送回车。"
                ),
                root=root,
            )
            return
        if phase in ("pasting", "recording"):
            logger.info(
                "command_sink ok double ignored during phase=%s "
                "(only single-click advances voice/paste)",
                phase,
            )
            return
    try:
        submit_sink(root=root)
    except CommandSinkError:
        raise
    else:
        _queue_tray_notice(
            kind=NOTICE_OK_SUBMIT,
            message="已向锁定的聊天输入框发送回车（提交）。",
            root=root,
        )


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
        started = time.monotonic()
        last_poll_log = started
        anchor_sequence = _clipboard_sequence_number()
        last_sequence = anchor_sequence
        last_text_len = len(baseline_text)
        try:
            logger.info(
                "command_sink clipboard worker started: generation=%s "
                "baseline_sequence=%s baseline_text_len=%s "
                "anchor_sequence=%s",
                generation,
                baseline_sequence,
                len(baseline_text),
                anchor_sequence,
            )
            while time.monotonic() < deadline:
                with _session_lock:
                    if _session.worker_generation != generation:
                        logger.info(
                            "command_sink clipboard worker cancelled: "
                            "generation=%s current=%s",
                            generation,
                            _session.worker_generation,
                        )
                        return
                sequence = _clipboard_sequence_number()
                last_sequence = sequence
                elapsed = time.monotonic() - started
                now = time.monotonic()
                poll_text = ""
                if now - last_poll_log >= _CLIPBOARD_POLL_LOG_INTERVAL_SECONDS:
                    _, poll_text = _clipboard_snapshot()
                    last_text_len = len(poll_text)
                    probe = _clipboard_access_probe()
                    logger.info(
                        "command_sink clipboard poll: elapsed=%.1fs "
                        "sequence=%s text_len=%s baseline_sequence=%s "
                        "anchor_sequence=%s open_ok=%s unicode=%s formats=%s",
                        elapsed,
                        sequence,
                        len(poll_text),
                        baseline_sequence,
                        anchor_sequence,
                        probe.get("open_ok"),
                        probe.get("unicode_available"),
                        probe.get("formats"),
                    )
                    last_poll_log = now
                ready_reason = _clipboard_paste_trigger_reason(
                    baseline_sequence=baseline_sequence,
                    baseline_text=baseline_text,
                    anchor_sequence=anchor_sequence,
                    sequence=sequence,
                    elapsed_seconds=elapsed,
                    text=poll_text,
                )
                if ready_reason:
                    if sequence != baseline_sequence or sequence != anchor_sequence:
                        time.sleep(_CLIPBOARD_SEQUENCE_SETTLE_SECONDS)
                    sequence, text = _clipboard_snapshot(patient=True)
                    last_sequence = sequence
                    last_text_len = len(text)
                    _log_clipboard_change_detail(
                        baseline_sequence=baseline_sequence,
                        baseline_text=baseline_text,
                        sequence=sequence,
                        text=text,
                    )
                    logger.info(
                        "command_sink clipboard ready: reason=%s "
                        "sequence=%s baseline_sequence=%s anchor_sequence=%s "
                        "text_len=%s baseline_text_len=%s preview=%r",
                        ready_reason,
                        sequence,
                        baseline_sequence,
                        anchor_sequence,
                        len(text),
                        len(baseline_text),
                        _clipboard_text_log_preview(text),
                    )
                    _paste_then_optional_submit(
                        root=root,
                        generation=generation,
                        clipboard_sequence=sequence,
                        clipboard_text=text,
                    )
                    return
                time.sleep(_CLIPBOARD_POLL_INTERVAL_SECONDS)
            timeout_message = (
                "等待剪贴板超时（约 120 秒），未粘贴。"
                "请查看 app.log 中 command_sink paste failure report。"
            )
            logger.warning(
                "command_sink clipboard wait timed out: last_sequence=%s "
                "last_text_len=%s baseline_sequence=%s",
                last_sequence,
                last_text_len,
                baseline_sequence,
            )
            _log_paste_delivery_failure(
                root=root,
                reason=CLIPBOARD_WAIT_TIMEOUT,
                user_message=timeout_message,
                clipboard_sequence=last_sequence,
                clipboard_text_len=last_text_len,
                detail=(
                    f"clipboard_wait_timeout baseline_sequence={baseline_sequence} "
                    f"last_sequence={last_sequence} baseline_text_len="
                    f"{len(baseline_text)}"
                ),
            )
            with _session_lock:
                if _session.worker_generation == generation:
                    _session.phase = "idle"
                    _session.pending_submit = False
                    _queue_tray_notice(
                        kind=NOTICE_OK_PASTE_FAILED,
                        message=timeout_message,
                        root=root,
                    )
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


def _paste_then_optional_submit(
    *,
    root: Optional[Path],
    generation: int,
    clipboard_sequence: int = 0,
    clipboard_text: str = "",
) -> None:
    submit_after = False
    with _session_lock:
        if _session.worker_generation != generation:
            logger.info(
                "command_sink paste skipped: stale generation=%s current=%s",
                generation,
                _session.worker_generation,
            )
            return
        _session.phase = "pasting"
        submit_after = _session.pending_submit
        _session.pending_submit = False
    logger.info(
        "command_sink paste starting: generation=%s submit_after=%s",
        generation,
        submit_after,
    )
    try:
        _deliver_to_sink(submit=False, root=root)
    except CommandSinkError as exc:
        user_message = (
            f"粘贴到指挥目标失败（{exc.code}）。"
            "请查看 app.log 中 command_sink paste failure report。"
        )
        detail = str(exc.args[0]) if exc.args else ""
        focus_outcome = ""
        if detail.startswith("focus_outcome="):
            focus_outcome = detail.split(" ", 1)[0].split("=", 1)[-1]
        _log_paste_delivery_failure(
            root=root,
            reason=exc.code,
            user_message=user_message,
            focus_outcome=focus_outcome,
            clipboard_sequence=clipboard_sequence,
            clipboard_text_len=len(clipboard_text),
            clipboard_preview=_clipboard_text_log_preview(clipboard_text),
            detail=detail,
        )
        logger.warning(
            "command_sink paste to sink failed: code=%s detail=%s",
            exc.code,
            detail,
            exc_info=True,
        )
        _queue_tray_notice(
            kind=NOTICE_OK_PASTE_FAILED,
            message=user_message,
            root=root,
        )
    else:
        logger.info("command_sink paste completed")
        if submit_after:
            try:
                _deliver_to_sink(submit=True, root=root)
            except CommandSinkError as exc:
                logger.warning(
                    "command_sink deferred submit failed: code=%s",
                    exc.code,
                    exc_info=True,
                )
            else:
                logger.info("command_sink deferred submit completed")
    with _session_lock:
        if _session.worker_generation == generation:
            _session.phase = "idle"
            logger.info("command_sink paste worker finished: phase=idle")


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
    runtime_id = payload.get("focus_uia_runtime_id")
    if not isinstance(runtime_id, list):
        runtime_id = None
    previous_hwnd = _foreground_hwnd()
    runtime_id_len = len(runtime_id) if runtime_id else 0
    logger.info(
        "command_sink delivery begin: submit=%s top_level_hwnd=%s "
        "focus_hwnd=%s runtime_id_len=%s previous_fg_hwnd=%s",
        submit,
        top_level_hwnd,
        focus_hwnd,
        runtime_id_len,
        previous_hwnd,
    )
    try:
        _activate_window(top_level_hwnd)
        target_exe = str(payload.get("foreground_executable") or "")
        focus_outcome = voice_interaction_diagnostics_windows.focus_command_sink_target(
            top_level_hwnd=top_level_hwnd,
            focus_hwnd=focus_hwnd,
            runtime_id=runtime_id,
            foreground_executable=target_exe,
        )
        if not voice_interaction_diagnostics_windows.focus_outcome_allows_paste(
            focus_outcome
        ):
            detail = (
                f"focus_outcome={focus_outcome} top_level_hwnd={top_level_hwnd} "
                f"focus_hwnd={focus_hwnd} runtime_id_len={runtime_id_len} "
                f"foreground_executable={target_exe} previous_fg_hwnd={previous_hwnd}"
            )
            logger.warning(
                "command_sink delivery focus failed: %s",
                detail,
            )
            raise CommandSinkError(DELIVERY_FAILED, detail)
        logger.info(
            "command_sink delivery focus: outcome=%s submit=%s",
            focus_outcome,
            submit,
        )
        if submit:
            win32_input.send_return()
            logger.info("command_sink delivery sent: Enter")
        else:
            win32_input.send_key_combo_tap(("ctrl", "v"))
            logger.info("command_sink delivery sent: Ctrl+V")
    except (OSError, win32_input.Win32InputUnavailableError) as exc:
        detail = f"win32_delivery_error type={type(exc).__name__} message={exc}"
        logger.warning("command_sink delivery win32 failed: %s", detail)
        raise CommandSinkError(DELIVERY_FAILED, detail) from exc
    finally:
        if submit:
            if previous_hwnd and _is_window(previous_hwnd):
                _activate_window(previous_hwnd)
        else:
            _restore_foreground_after_paste(root=root)


def _capture_voice_foreground_hwnd(root: Optional[Path]) -> int:
    hwnd = _foreground_hwnd()
    if not hwnd:
        return 0
    payload = _read_disk_payload(root)
    top = int(payload.get("top_level_hwnd") or 0)
    focus = int(payload.get("focus_hwnd") or 0)
    if hwnd in (top, focus):
        return 0
    return hwnd


def _restore_foreground_after_paste(*, root: Optional[Path] = None) -> None:
    restore = 0
    with _session_lock:
        restore = int(_session.foreground_hwnd_before_voice or 0)
        _session.foreground_hwnd_before_voice = 0
    if not restore or not _is_window(restore):
        return
    top = int(_read_disk_payload(root).get("top_level_hwnd") or 0)
    if top and restore == top:
        logger.info(
            "command_sink skipped restore hwnd=%s (same as locked top-level)",
            restore,
        )
        return
    _activate_window(restore)
    logger.info("command_sink restored foreground hwnd=%s after paste", restore)


def _reset_session(*, cancel_worker: bool) -> None:
    with _session_lock:
        if cancel_worker:
            _cancel_worker_locked()
        _session.phase = "idle"
        _session.baseline_sequence = 0
        _session.baseline_text = ""
        _session.pending_submit = False
        _session.foreground_hwnd_before_voice = 0


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


_CLIPBOARD_TEXT_MAX_CHARS = 1_000_000


def _clipboard_text_log_preview(text: str, *, max_chars: int = 120) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1]}…"


def _clipboard_sequence_number() -> int:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        return int(user32.GetClipboardSequenceNumber())
    except (OSError, AttributeError, ctypes.ArgumentError):
        return 0


def _clipboard_paste_trigger_reason(
    *,
    baseline_sequence: int,
    baseline_text: str,
    anchor_sequence: int,
    sequence: int,
    elapsed_seconds: float,
    text: str = "",
) -> str:
    """Decide paste from sequence / time; do not require readable clipboard text."""

    if elapsed_seconds < _CLIPBOARD_MIN_WAIT_BEFORE_PASTE_SECONDS:
        return ""
    if text and (sequence != baseline_sequence or text != baseline_text):
        return "readable_text"
    if sequence != baseline_sequence:
        return "sequence_beyond_first_ok_baseline"
    if sequence != anchor_sequence:
        return "sequence_since_wait_started"
    if elapsed_seconds >= _CLIPBOARD_GRACE_PASTE_SECONDS:
        return "grace_after_voice"
    return ""


def _clipboard_access_probe() -> dict[str, Any]:
    """Best-effort clipboard open probe for poll logs (no text read)."""

    probe: dict[str, Any] = {
        "open_ok": False,
        "unicode_available": False,
        "formats": [],
        "winerror": 0,
    }
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _ = kernel32  # reserved for future size probes
        user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
        user32.EnumClipboardFormats.restype = wintypes.UINT
        if not user32.OpenClipboard(None):
            probe["winerror"] = ctypes.get_last_error()
            return probe
        probe["open_ok"] = True
        try:
            probe["unicode_available"] = bool(
                user32.IsClipboardFormatAvailable(_CF_UNICODETEXT)
            )
            fmt = 0
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if fmt == 0:
                    break
                probe["formats"].append(fmt)
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError, ctypes.ArgumentError):
        pass
    return probe


def _clipboard_ready_for_paste(
    *,
    baseline_sequence: int,
    baseline_text: str,
    sequence: int,
    text: str,
    populate_outcome: str = "",
) -> bool:
    _ = populate_outcome
    if text:
        return sequence != baseline_sequence or text != baseline_text
    return bool(
        _clipboard_paste_trigger_reason(
            baseline_sequence=baseline_sequence,
            baseline_text=baseline_text,
            anchor_sequence=baseline_sequence,
            sequence=sequence,
            elapsed_seconds=_CLIPBOARD_MIN_WAIT_BEFORE_PASTE_SECONDS,
        )
    )


def _log_paste_delivery_failure(
    *,
    root: Optional[Path],
    reason: str,
    user_message: str,
    focus_outcome: str = "",
    clipboard_sequence: int = 0,
    clipboard_text_len: int = 0,
    clipboard_preview: str = "",
    detail: str = "",
) -> None:
    """One consolidated line for app.log matching the on-screen paste-failed notice."""

    payload = _read_disk_payload(root)
    runtime_id = payload.get("focus_uia_runtime_id")
    runtime_id_len = len(runtime_id) if isinstance(runtime_id, list) else 0
    logger.error(
        "command_sink paste failure report: reason=%s user_message=%s "
        "focus_outcome=%s foreground_executable=%s top_level_hwnd=%s "
        "focus_hwnd=%s runtime_id_len=%s clipboard_sequence=%s "
        "clipboard_text_len=%s clipboard_preview=%r detail=%s",
        reason,
        user_message,
        focus_outcome or "n/a",
        str(payload.get("foreground_executable") or ""),
        int(payload.get("top_level_hwnd") or 0),
        int(payload.get("focus_hwnd") or 0),
        runtime_id_len,
        clipboard_sequence,
        clipboard_text_len,
        clipboard_preview,
        detail,
    )


def _wait_clipboard_text_after_change(
    *,
    baseline_sequence: int,
    initial_sequence: int,
) -> tuple[str, str]:
    """After sequence bumps, some IMEs publish CF_UNICODETEXT slightly later."""

    deadline = time.monotonic() + _CLIPBOARD_TEXT_POPULATE_WAIT_SECONDS
    while time.monotonic() < deadline:
        sequence, text = _clipboard_snapshot(patient=True)
        if sequence != baseline_sequence and text:
            return text, "ready"
        time.sleep(_CLIPBOARD_POLL_INTERVAL_SECONDS)
    sequence, text = _clipboard_snapshot(patient=True)
    if text:
        return text, "ready_late"
    if sequence != initial_sequence:
        return "", "sequence_only_empty"
    return "", "still_empty"


def _log_clipboard_change_detail(
    *,
    baseline_sequence: int,
    baseline_text: str,
    sequence: int,
    text: str,
) -> None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.OpenClipboard(None):
            logger.info(
                "command_sink clipboard detail: open_failed sequence=%s->%s",
                baseline_sequence,
                sequence,
            )
            return
        try:
            formats: list[int] = []
            fmt = 0
            user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
            user32.EnumClipboardFormats.restype = wintypes.UINT
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if fmt == 0:
                    break
                formats.append(fmt)
            logger.info(
                "command_sink clipboard detail: sequence=%s->%s "
                "text_len=%s baseline_text_len=%s formats=%s",
                baseline_sequence,
                sequence,
                len(text),
                len(baseline_text),
                formats,
            )
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError, ctypes.ArgumentError):
        logger.info(
            "command_sink clipboard detail: enumerate_failed sequence=%s->%s",
            baseline_sequence,
            sequence,
        )


def _read_clipboard_text_from_handle(
    user32: ctypes.WinDLL,
    kernel32: ctypes.WinDLL,
    handle: int,
    *,
    cf_format: int,
) -> str:
    if not handle:
        return ""
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return ""
    try:
        if cf_format == _CF_UNICODETEXT:
            return _read_locked_clipboard_text(int(pointer))
        if cf_format in (_CF_TEXT, _CF_OEMTEXT):
            raw = ctypes.string_at(
                pointer,
                min(_CLIPBOARD_TEXT_MAX_CHARS, 8_000_000),
            )
            return raw.decode("mbcs", errors="replace").split("\0", 1)[0]
    except (OSError, ValueError, OverflowError, MemoryError, UnicodeError):
        return ""
    finally:
        kernel32.GlobalUnlock(handle)
    return ""


def _read_locked_clipboard_text(pointer: int) -> str:
    if not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer, _CLIPBOARD_TEXT_MAX_CHARS)
    except (OSError, ValueError, OverflowError, MemoryError):
        try:
            raw = ctypes.string_at(
                pointer,
                min(_CLIPBOARD_TEXT_MAX_CHARS * 2, 8_000_000),
            )
            return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]
        except (OSError, ValueError, UnicodeError, MemoryError):
            return ""


def _clipboard_advertises_readable_text() -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        if not user32.OpenClipboard(None):
            return False
        try:
            for cf_format in _CLIPBOARD_TEXT_CF_ORDER:
                if user32.IsClipboardFormatAvailable(cf_format):
                    return True
            return False
        finally:
            user32.CloseClipboard()
    except (OSError, AttributeError, ctypes.ArgumentError):
        return False


def _read_clipboard_text_from_open_clipboard(
    user32: ctypes.WinDLL,
    kernel32: ctypes.WinDLL,
    *,
    delayed_render_attempts: int,
) -> str:
    user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    for cf_format in _CLIPBOARD_TEXT_CF_ORDER:
        if not user32.IsClipboardFormatAvailable(cf_format):
            continue
        attempts = (
            delayed_render_attempts
            if cf_format == _CF_UNICODETEXT
            else 1
        )
        for attempt in range(attempts):
            handle = int(user32.GetClipboardData(cf_format) or 0)
            if handle:
                text = _read_clipboard_text_from_handle(
                    user32,
                    kernel32,
                    handle,
                    cf_format=cf_format,
                )
                if text:
                    return text
            if attempt + 1 < attempts:
                time.sleep(_CLIPBOARD_POLL_INTERVAL_SECONDS)
    return ""


def _clipboard_snapshot(*, patient: bool = False) -> tuple[int, str]:
    delayed_attempts = (
        _CLIPBOARD_DELAYED_RENDER_ATTEMPTS_PATIENT
        if patient
        else _CLIPBOARD_DELAYED_RENDER_ATTEMPTS_QUICK
    )
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        sequence = int(user32.GetClipboardSequenceNumber())
        opened = False
        for _ in range(8):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return sequence, ""
        try:
            text = _read_clipboard_text_from_open_clipboard(
                user32,
                kernel32,
                delayed_render_attempts=delayed_attempts,
            )
            return sequence, text
        finally:
            user32.CloseClipboard()
    except (OSError, ctypes.ArgumentError, AttributeError, MemoryError, RuntimeError):
        logger.warning("command_sink clipboard snapshot failed", exc_info=True)
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
            return int(user32.GetClipboardSequenceNumber()), ""
        except (OSError, AttributeError):
            return 0, ""


def _write_unlocked(payload: dict[str, Any]) -> None:
    payload["version"] = COMMAND_SINK_STATE_VERSION
    payload["locked"] = False
    payload["lock_lamp"] = LAMP_OPEN
    payload.pop("top_level_hwnd", None)
    payload.pop("focus_hwnd", None)
    payload.pop("foreground_executable", None)
    payload.pop("focus_uia_runtime_id", None)


def _default_payload() -> dict[str, Any]:
    return {
        "version": COMMAND_SINK_STATE_VERSION,
        "locked": False,
        "lock_lamp": LAMP_OPEN,
    }


def _read_disk_payload(root: Optional[Path]) -> dict[str, Any]:
    path = state_path(root)
    if not path.is_file():
        return _default_payload()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_payload()
    if not isinstance(data, dict):
        return _default_payload()
    return data


def drop_persisted_lock_on_process_start(*, root: Optional[Path] = None) -> bool:
    """Clear a persisted lock when settings or bridge starts (not a live session)."""

    with _state_lock:
        payload = _read_disk_payload(root)
        if not bool(payload.get("locked")):
            return False
        _reset_session(cancel_worker=True)
        _write_unlocked(payload)
        payload["tray_notice"] = {"kind": "", "message": ""}
        _save_payload(payload, root)
        logger.info("command_sink persisted lock cleared on process start")
        return True


def reconcile_persisted_lock(*, root: Optional[Path] = None) -> bool:
    """Clear a lock when its HWNDs are gone (reboot, target app closed).

    Returns True when persisted state was changed.
    """

    global _last_stale_notice_at
    with _state_lock:
        payload = _read_disk_payload(root)
        if not bool(payload.get("locked")):
            return False
        focus_hwnd = int(payload.get("focus_hwnd") or 0)
        top_level_hwnd = int(payload.get("top_level_hwnd") or 0)
        if _sink_handles_valid(focus_hwnd, top_level_hwnd):
            return False
        now = time.monotonic()
        _reset_session(cancel_worker=True)
        _write_unlocked(payload)
        if now - _last_stale_notice_at >= _STALE_NOTICE_COOLDOWN_SECONDS:
            payload["tray_notice"] = {
                "kind": NOTICE_STALE_LOCK,
                "message": "指挥目标已失效，请重新聚焦聊天输入并锁定。",
            }
            _last_stale_notice_at = now
        else:
            payload["tray_notice"] = {"kind": "", "message": ""}
        _save_payload(payload, root)
        logger.info(
            "command_sink persisted lock dropped: focus_hwnd=%s top_level_hwnd=%s",
            focus_hwnd,
            top_level_hwnd,
        )
        return True


def _load_payload(root: Optional[Path]) -> dict[str, Any]:
    reconcile_persisted_lock(root=root)
    return _read_disk_payload(root)


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
