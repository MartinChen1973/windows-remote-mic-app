"""Windows taskbar button overlay icon (orange / green status dot)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from . import resources

HRESULT = ctypes.c_long
_POINTER = ctypes.POINTER(ctypes.c_void_p)
_IID_ITASKBAR_LIST3 = (
    0xEA1AFB91,
    0x9E28,
    0x4B86,
    (0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x51, 0x1E, 0x29),
)
_CLSID_TASKBAR_LIST = (
    0x56FDF344,
    0xFD6D,
    0x11D0,
    (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90),
)

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_LR_DEFAULTSIZE = 0x0040
_CLSCTX_INPROC_SERVER = 0x1

_icon_cache: dict[str, int] = {}


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    )


def _make_guid(parts: tuple) -> _GUID:
    return _GUID(parts[0], parts[1], parts[2], (wintypes.BYTE * 8)(*parts[3]))


def apply_sink_lamp_overlay(
    hwnd: int,
    *,
    lamp: str,
) -> None:
    """Attach orange, green, or red status dot to the taskbar button."""
    if not hwnd:
        return
    if lamp not in ("open", "locked", "rejected"):
        clear_overlay(hwnd)
        return
    dot_names = {
        "open": "taskbar-dot-orange",
        "locked": "taskbar-dot-green",
        "rejected": "taskbar-dot-red",
    }
    descriptions = {
        "open": "指挥目标未锁定",
        "locked": "指挥目标已锁定",
        "rejected": "指挥目标锁定失败",
    }
    path = resources.find_taskbar_dot_icon(dot_names[lamp])
    if path is None:
        clear_overlay(hwnd)
        return
    icon = _load_icon(path)
    if icon:
        _set_overlay(hwnd, icon, descriptions[lamp])
    else:
        clear_overlay(hwnd)


def clear_overlay(hwnd: int) -> None:
    if hwnd:
        _set_overlay(hwnd, 0, "")


def _set_overlay(hwnd: int, icon: int, description: str) -> None:
    try:
        taskbar = _create_taskbar_list3()
        if not taskbar:
            return
        hr_init = _vtable_call(taskbar, 3)
        if hr_init not in (0, 0x00000001):  # S_OK or S_FALSE
            return
        _vtable_call(taskbar, 15, hwnd, icon, description)
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _create_taskbar_list3() -> int:
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx.argtypes = (wintypes.LPVOID, wintypes.DWORD)
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        _POINTER,
    )
    ole32.CoCreateInstance.restype = HRESULT
    ole32.CoInitializeEx(None, 0)
    clsid = _make_guid(_CLSID_TASKBAR_LIST)
    iid = _make_guid(_IID_ITASKBAR_LIST3)
    obj = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(clsid),
        None,
        _CLSCTX_INPROC_SERVER,
        ctypes.byref(iid),
        ctypes.byref(obj),
    )
    if hr != 0 or not obj:
        return 0
    return int(obj.value)


def _vtable_call(taskbar_ptr: int, index: int, *args) -> int:
    """Call ``ITaskbarList3`` vtable slot ``index`` (IUnknown offset + method)."""
    vtable = ctypes.cast(
        ctypes.c_void_p(taskbar_ptr),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents[0]
    method = ctypes.cast(
        ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index],
        ctypes.c_void_p,
    ).value
    if not method:
        return -1
    if index == 3:
        prototype = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
        return int(prototype(method)(taskbar_ptr))
    if index == 15:
        prototype = ctypes.WINFUNCTYPE(
            HRESULT,
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.HANDLE,
            wintypes.LPCWSTR,
        )
        hwnd = wintypes.HWND(args[0]) if args else wintypes.HWND(0)
        icon = wintypes.HANDLE(args[1]) if len(args) > 1 else wintypes.HANDLE(0)
        desc = args[2] if len(args) > 2 else ""
        return int(prototype(method)(taskbar_ptr, hwnd, icon, desc))
    return -1


def _load_icon(path: Path) -> int:
    key = str(path.resolve())
    cached = _icon_cache.get(key)
    if cached:
        return cached
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.LoadImageW.argtypes = (
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.LoadImageW.restype = wintypes.HANDLE
    if path.suffix.lower() != ".ico":
        ico_path = path.with_suffix(".ico")
        if ico_path.is_file():
            path = ico_path
    handle = int(
        user32.LoadImageW(
            None,
            str(path),
            _IMAGE_ICON,
            0,
            0,
            _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
        )
        or 0
    )
    if handle:
        _icon_cache[key] = handle
    return handle
