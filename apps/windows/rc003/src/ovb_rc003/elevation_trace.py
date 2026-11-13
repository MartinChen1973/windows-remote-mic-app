"""Structured logs around UAC / elevation prompts (no secrets in fields)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

_LOG = logging.getLogger("ovb_rc003")

# Windows ERROR_CANCELLED when the user declines UAC (ShellExecute/runas).
_WINERROR_UAC_CANCELLED = 1223


def default_log_file_hint() -> str:
    try:
        from . import config
        from . import logging_setup

        root = config.config_root()
        return str(logging_setup.log_dir(root) / "app.log")
    except Exception:
        return "%LOCALAPPDATA%\\RemoteMic\\RC003\\logs\\app.log"


def _payload(**fields: Any) -> str:
    safe = {key: value for key, value in fields.items() if value is not None}
    return json.dumps(safe, ensure_ascii=True, sort_keys=True)


def log_process_token(*, context: str, elevated: Optional[bool]) -> None:
    """Record whether the current process token is elevated."""

    _LOG.info(
        "elevation_status: context=%s elevated=%s",
        context,
        "unknown" if elevated is None else int(bool(elevated)),
    )


def log_uac_requested(
    kind: str,
    *,
    executable: Optional[Path | str] = None,
    detail: str = "",
    **fields: Any,
) -> None:
    """Immediately before calling ShellExecute/os.startfile with verb runas."""

    exe = ""
    if executable is not None:
        try:
            exe = Path(executable).name
        except (TypeError, ValueError):
            exe = "invalid"
    _LOG.info(
        "elevation_prompt: requesting kind=%s executable=%s detail=%s %s",
        kind,
        exe,
        detail or "-",
        _payload(**fields),
    )


def log_uac_outcome(
    kind: str,
    outcome: str,
    *,
    winerror: Optional[int] = None,
    **fields: Any,
) -> None:
    """Record acceptance, cancellation, or launch failure after a prompt."""

    if winerror == _WINERROR_UAC_CANCELLED and outcome != "cancelled":
        outcome = "cancelled"
    _LOG.info(
        "elevation_prompt: outcome kind=%s result=%s winerror=%s %s",
        kind,
        outcome,
        winerror if winerror is not None else -1,
        _payload(**fields),
    )


def log_uac_note(message: str) -> None:
    """Human-oriented hint tied to the same grep target as prompts."""

    _LOG.info("elevation_prompt: note=%s", message)
