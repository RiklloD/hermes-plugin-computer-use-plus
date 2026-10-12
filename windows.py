"""On-screen window lookup through CoreGraphics (ctypes, no pyobjc): which CGWindowID belongs to an app's window, its
bounds in top-left screen points, and the main display height (to flip Cocoa's bottom-left coordinates)."""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from typing import Any, Dict, List, Optional

_cf = _cg = None


def _load() -> bool:
    global _cf, _cg
    if _cg is not None:
        return True
    if sys.platform != "darwin":
        return False
    try:
        _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        _cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
    except OSError:
        return False
    vp, i64, u32 = ctypes.c_void_p, ctypes.c_long, ctypes.c_uint32
    _cg.CGWindowListCopyWindowInfo.restype, _cg.CGWindowListCopyWindowInfo.argtypes = vp, [u32, u32]
    _cg.CGMainDisplayID.restype = u32
    _cg.CGDisplayBounds.restype, _cg.CGDisplayBounds.argtypes = _CGRect, [u32]
    _cf.CFArrayGetCount.restype, _cf.CFArrayGetCount.argtypes = i64, [vp]
    _cf.CFArrayGetValueAtIndex.restype, _cf.CFArrayGetValueAtIndex.argtypes = vp, [vp, i64]
    _cf.CFDictionaryGetValue.restype, _cf.CFDictionaryGetValue.argtypes = vp, [vp, vp]
    _cf.CFStringCreateWithCString.restype, _cf.CFStringCreateWithCString.argtypes = vp, [vp, ctypes.c_char_p, u32]
    _cf.CFStringGetCString.restype, _cf.CFStringGetCString.argtypes = ctypes.c_bool, [vp, ctypes.c_char_p, i64, u32]
    _cf.CFNumberGetValue.restype, _cf.CFNumberGetValue.argtypes = ctypes.c_bool, [vp, i64, vp]
    _cf.CFRelease.argtypes = [vp]
    _cf.CFGetTypeID.restype, _cf.CFGetTypeID.argtypes = i64, [vp]
    _cf.CFStringGetTypeID.restype = i64
    _cf.CFNumberGetTypeID.restype = i64
    _cf.CFDictionaryGetTypeID.restype = i64
    return True


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


_UTF8 = 0x08000100
_keys: Dict[str, int] = {}


def _key(name: str) -> int:
    if name not in _keys:
        _keys[name] = _cf.CFStringCreateWithCString(None, name.encode(), _UTF8)  # kept for the process lifetime
    return _keys[name]


def _get(d: int, name: str) -> Optional[int]:
    return _cf.CFDictionaryGetValue(d, _key(name)) or None


def _str(ref: Optional[int]) -> str:
    if not ref or _cf.CFGetTypeID(ref) != _cf.CFStringGetTypeID():
        return ""
    buf = ctypes.create_string_buffer(2048)
    return buf.value.decode("utf-8", "replace") if _cf.CFStringGetCString(ref, buf, 2048, _UTF8) else ""


def _num(ref: Optional[int]) -> Optional[float]:
    if not ref or _cf.CFGetTypeID(ref) != _cf.CFNumberGetTypeID():
        return None
    out = ctypes.c_double()
    return out.value if _cf.CFNumberGetValue(ref, 13, ctypes.byref(out)) else None  # kCFNumberDoubleType


def list_windows() -> List[Dict[str, Any]]:
    """On-screen windows, front to back: {window_id, pid, owner, title, layer, bounds: (x, y, w, h)}."""
    if not _load():
        return []
    arr = _cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # kCGWindowListOptionOnScreenOnly | ExcludeDesktopElements
    if not arr:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for i in range(_cf.CFArrayGetCount(arr)):
            d = _cf.CFArrayGetValueAtIndex(arr, i)
            b = _get(d, "kCGWindowBounds")
            rect = None
            if b and _cf.CFGetTypeID(b) == _cf.CFDictionaryGetTypeID():
                vals = [_num(_get(b, k)) for k in ("X", "Y", "Width", "Height")]
                if all(v is not None for v in vals):
                    rect = tuple(vals)
            wid, pid, layer = (_num(_get(d, k)) for k in ("kCGWindowNumber", "kCGWindowOwnerPID", "kCGWindowLayer"))
            if wid is None or pid is None:
                continue
            out.append({"window_id": int(wid), "pid": int(pid), "layer": int(layer or 0),
                        "owner": _str(_get(d, "kCGWindowOwnerName")), "title": _str(_get(d, "kCGWindowName")),
                        "bounds": rect})
    finally:
        _cf.CFRelease(arr)
    return out


def find_window(pid: int, title: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The app's normal (layer 0) window, preferring an exact title match, then the frontmost one."""
    cands = [w for w in list_windows() if w["pid"] == pid and w["layer"] == 0 and w["bounds"]
             and w["bounds"][2] >= 60 and w["bounds"][3] >= 40]
    if not cands:
        return None
    if title:
        exact = [w for w in cands if w["title"] == title]
        if exact:
            return exact[0]
    return cands[0]


def window_by_id(window_id: int) -> Optional[Dict[str, Any]]:
    return next((w for w in list_windows() if w["window_id"] == window_id), None)


def main_display_height() -> Optional[float]:
    if not _load():
        return None
    r = _cg.CGDisplayBounds(_cg.CGMainDisplayID())
    return float(r.size.height) or None
