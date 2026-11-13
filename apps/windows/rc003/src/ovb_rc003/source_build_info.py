"""Privacy-safe build fingerprint for settings and bridge startup logs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import dev_session, suspend_clock
from .bridge_runtime_status import BridgeRuntimeIdentity, current_runtime_identity


def _repo_root(module_file: Path) -> Path:
    current = module_file.resolve().parent
    for _ in range(8):
        if (current / ".git").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return module_file.resolve().parents[2]


def git_describe(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def describe(
    app_version: str,
    runtime_identity: Optional[BridgeRuntimeIdentity] = None,
) -> Dict[str, Any]:
    """Return a fixed-field summary suitable for structured app.log lines."""

    module_file = Path(__file__)
    identity = runtime_identity or current_runtime_identity(app_version)
    return {
        "app_version": app_version,
        "git_describe": git_describe(_repo_root(module_file)),
        "python_executable": os.path.normcase(sys.executable),
        "package_dir": os.path.normcase(str(module_file.resolve().parent)),
        "runtime_kind": identity.runtime_kind,
        "runtime_package": identity.package_name,
        "runtime_id": identity.runtime_id,
        "dev_session": dev_session.is_isolated(),
        "suspend_exclusive_clock": suspend_clock.available(),
    }
