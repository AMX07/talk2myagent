"""A small ctypes binding for the macOS Accessibility (AXUIElement) API.

Driving another app's UI through ``osascript``/System Events costs one Apple
Event per property read, which made a single Phone window snapshot take about
ten seconds. The same walk through this in-process binding takes under a tenth
of a second, which is what makes polling during a live call practical.

Ownership: ``AXUIElementCopyAttributeValue`` returns +1 references. Every
Core Foundation object this module hands out is wrapped in :class:`AXElement`
or converted to a Python value and released immediately.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
from ctypes import POINTER, byref, c_bool, c_char_p, c_int, c_long, c_ulong, c_void_p

UTF8 = 0x08000100
PRESS = "AXPress"

_services = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

_cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, ctypes.c_uint32]
_cf.CFStringCreateWithCString.restype = c_void_p
_cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = c_bool
_cf.CFArrayGetCount.argtypes = [c_void_p]
_cf.CFArrayGetCount.restype = c_long
_cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
_cf.CFArrayGetValueAtIndex.restype = c_void_p
_cf.CFGetTypeID.argtypes = [c_void_p]
_cf.CFGetTypeID.restype = c_ulong
_cf.CFStringGetTypeID.restype = c_ulong
_cf.CFArrayGetTypeID.restype = c_ulong
_cf.CFBooleanGetTypeID.restype = c_ulong
_cf.CFBooleanGetValue.argtypes = [c_void_p]
_cf.CFBooleanGetValue.restype = c_bool
_cf.CFRetain.argtypes = [c_void_p]
_cf.CFRetain.restype = c_void_p
_cf.CFRelease.argtypes = [c_void_p]

_services.AXUIElementCreateApplication.argtypes = [c_int]
_services.AXUIElementCreateApplication.restype = c_void_p
_services.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
_services.AXUIElementCopyAttributeValue.restype = c_int
_services.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
_services.AXUIElementSetAttributeValue.restype = c_int
_services.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
_services.AXUIElementPerformAction.restype = c_int
_services.AXIsProcessTrusted.restype = c_bool

_STRING_TYPE = _cf.CFStringGetTypeID()
_ARRAY_TYPE = _cf.CFArrayGetTypeID()
_BOOLEAN_TYPE = _cf.CFBooleanGetTypeID()
_names: dict[str, int] = {}


def _cfstring(value: str) -> int:
    return _cf.CFStringCreateWithCString(None, value.encode(), UTF8)


def _name(attribute: str) -> int:
    """Attribute-name strings are constant; create each one once."""
    if attribute not in _names:
        _names[attribute] = _cfstring(attribute)
    return _names[attribute]


def _python_string(ref: int) -> str:
    buffer = ctypes.create_string_buffer(4096)
    if _cf.CFStringGetCString(ref, buffer, 4096, UTF8):
        return buffer.value.decode(errors="replace")
    return ""


def trusted() -> bool:
    """Whether this process may drive other apps (Accessibility permission)."""
    return bool(_services.AXIsProcessTrusted())


def pid_of(process_name: str) -> int | None:
    try:
        output = subprocess.check_output(["pgrep", "-x", process_name], text=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    first = output.split()
    return int(first[0]) if first else None


class AXElement:
    """One accessibility element; releases its Core Foundation reference on drop."""

    __slots__ = ("ref",)

    def __init__(self, ref: int, *, adopt: bool = True):
        self.ref = ref
        if not adopt and ref:
            _cf.CFRetain(ref)

    def __del__(self):
        try:
            if self.ref:
                _cf.CFRelease(self.ref)
                self.ref = 0
        except Exception:  # noqa: BLE001 - interpreter shutdown can clear globals
            pass

    def _copy(self, attribute: str) -> int | None:
        out = c_void_p()
        if _services.AXUIElementCopyAttributeValue(self.ref, _name(attribute), byref(out)) != 0:
            return None
        return out.value

    def text(self, attribute: str) -> str:
        ref = self._copy(attribute)
        if not ref:
            return ""
        try:
            return _python_string(ref) if _cf.CFGetTypeID(ref) == _STRING_TYPE else ""
        finally:
            _cf.CFRelease(ref)

    def flag(self, attribute: str) -> bool:
        ref = self._copy(attribute)
        if not ref:
            return False
        try:
            return (
                bool(_cf.CFBooleanGetValue(ref)) if _cf.CFGetTypeID(ref) == _BOOLEAN_TYPE else False
            )
        finally:
            _cf.CFRelease(ref)

    def element(self, attribute: str) -> AXElement | None:
        ref = self._copy(attribute)
        return AXElement(ref) if ref else None

    def elements(self, attribute: str = "AXChildren") -> list[AXElement]:
        ref = self._copy(attribute)
        if not ref:
            return []
        try:
            if _cf.CFGetTypeID(ref) != _ARRAY_TYPE:
                return []
            # Array members are borrowed; retain each before the array is released.
            return [
                AXElement(_cf.CFArrayGetValueAtIndex(ref, i), adopt=False)
                for i in range(_cf.CFArrayGetCount(ref))
            ]
        finally:
            _cf.CFRelease(ref)

    def press(self) -> bool:
        return _services.AXUIElementPerformAction(self.ref, _name(PRESS)) == 0

    def set_text(self, value: str) -> bool:
        string = _cfstring(value)
        try:
            return _services.AXUIElementSetAttributeValue(self.ref, _name("AXValue"), string) == 0
        finally:
            _cf.CFRelease(string)

    @property
    def role(self) -> str:
        return self.text("AXRole")

    @property
    def labels(self) -> tuple[str, str]:
        return self.text("AXTitle"), self.text("AXDescription")


def application(pid: int) -> AXElement:
    return AXElement(_services.AXUIElementCreateApplication(pid))
