"""Control the macOS Phone app and CoreAudio defaults without third-party tools.

Dialing uses the system ``tel:`` handler (Phone.app). Confirming, observing,
hanging up, and pressing keypad digits use accessibility scripting through
``osascript``, which needs the host application (Terminal, Codex, OpenCode,
Claude) to be allowed under System Settings > Privacy & Security > Accessibility.
Audio routing uses CoreAudio through ctypes. Every mutation is reversible and the
previous defaults are saved to ``.runtime`` for recovery.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import ClassVar

from .config import runtime_dir

PHONE_BUNDLE = "com.apple.mobilephone"
END_PATTERN = re.compile(r"(?i)^(end( call)?|hang ?up|end and accept)$")
CONFIRM_PATTERN = re.compile(r"(?i)^(call|dial)$")
KEYPAD_PATTERN = re.compile(r"(?i)^(keypad|show keypad|dial pad)$")
PROGRESS_PATTERN = re.compile(r"(?i)^(calling|connecting|ringing|dialing)")
TIMER_PATTERN = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
ENDED_PATTERN = re.compile(r"(?i)^(call ended|call failed|busy|declined)")


class PhoneControlError(RuntimeError):
    pass


class AccessibilityError(PhoneControlError):
    pass


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def osascript(script: str, timeout: float = 20) -> str:
    try:
        completed = subprocess.run(
            ["osascript", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PhoneControlError("Phone app scripting timed out.") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        if "-1719" in message or "assistive access" in message or "-25211" in message:
            raise AccessibilityError(
                "macOS Accessibility permission is missing for the app running talk2myagent. "
                "Enable it in System Settings > Privacy & Security > Accessibility for the host "
                "app (Terminal, Codex, OpenCode, or Claude), then retry."
            )
        raise PhoneControlError(message or "osascript failed")
    return completed.stdout


def accessibility_enabled() -> bool:
    try:
        return (
            osascript('tell application "System Events" to UI elements enabled').strip() == "true"
        )
    except PhoneControlError:
        return False


# ---------------------------------------------------------------- CoreAudio ---


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode())[0]


class _Address(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


class CoreAudio:
    """Default-device inspection and switching; names match sounddevice's names."""

    SYSTEM_OBJECT = 1
    SELECTORS: ClassVar[dict[str, str]] = {
        "output": "dOut",
        "input": "dIn ",
        "system_output": "sOut",
    }

    def __init__(self):
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _address(self, selector: str) -> _Address:
        return _Address(_fourcc(selector), _fourcc("glob"), 0)

    def _get(self, obj: int, selector: str, ctype):
        address = self._address(selector)
        size = ctypes.c_uint32(ctypes.sizeof(ctype))
        out = ctype()
        status = self.ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(out)
        )
        if status != 0:
            raise PhoneControlError(f"CoreAudio read failed ({selector}: {status}).")
        return out

    def devices(self) -> dict[str, int]:
        address = self._address("dev#")
        size = ctypes.c_uint32()
        if self.ca.AudioObjectGetPropertyDataSize(
            self.SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size)
        ):
            raise PhoneControlError("CoreAudio device enumeration failed.")
        count = size.value // 4
        ids = (ctypes.c_uint32 * count)()
        if self.ca.AudioObjectGetPropertyData(
            self.SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size), ids
        ):
            raise PhoneControlError("CoreAudio device enumeration failed.")
        return {self._name(device): device for device in ids}

    def _name(self, device: int) -> str:
        ref = self._get(device, "lnam", ctypes.c_void_p)
        buffer = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(ref, buffer, 512, 0x08000100)
        self.cf.CFRelease(ref)
        return buffer.value.decode()

    def default(self, kind: str) -> str:
        return self._name(
            self._get(self.SYSTEM_OBJECT, self.SELECTORS[kind], ctypes.c_uint32).value
        )

    def set_default(self, kind: str, name: str) -> None:
        devices = self.devices()
        if name not in devices:
            raise PhoneControlError(f"Audio device {name!r} not found.")
        address = self._address(self.SELECTORS[kind])
        value = ctypes.c_uint32(devices[name])
        status = self.ca.AudioObjectSetPropertyData(
            self.SYSTEM_OBJECT, ctypes.byref(address), 0, None, 4, ctypes.byref(value)
        )
        if status != 0:
            raise PhoneControlError(f"CoreAudio could not set default {kind} ({status}).")


class AudioRouting:
    """Route Phone's audio through the two virtual buses, and restore afterwards."""

    def __init__(self, incoming_bus: str, outgoing_bus: str, phone: PhoneApp | None = None):
        self.incoming_bus, self.outgoing_bus = incoming_bus, outgoing_bus
        self.core = CoreAudio()
        self.phone = phone
        self.saved: dict | None = None
        self.state_file = runtime_dir() / "audio-defaults.json"

    def apply(self) -> dict:
        self.saved = {kind: self.core.default(kind) for kind in ("output", "input")}
        self.state_file.write_text(json.dumps(self.saved))
        # Phone plays the far end on the default output; our recorder reads that bus.
        self.core.set_default("output", self.incoming_bus)
        # Phone's "Use System Setting" microphone follows the default input; we speak into it.
        self.core.set_default("input", self.outgoing_bus)
        microphone = None
        if self.phone is not None:
            try:
                microphone = self.phone.set_microphone(self.outgoing_bus)
            except PhoneControlError as exc:
                microphone = f"menu unchanged: {exc}"
        return {
            "previous": dict(self.saved),
            "output": self.incoming_bus,
            "input": self.outgoing_bus,
            "phone_microphone": microphone,
        }

    def restore(self) -> dict:
        saved = self.saved
        if saved is None and self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
        if not saved:
            return {"restored": False}
        for kind, name in saved.items():
            try:
                self.core.set_default(kind, name)
            except PhoneControlError:
                continue
        if self.phone is not None:
            try:
                self.phone.set_microphone("Use System Setting")
            except PhoneControlError:
                pass
        self.state_file.unlink(missing_ok=True)
        self.saved = None
        return {"restored": True, **saved}


# ---------------------------------------------------------------- Phone.app ---

_SNAPSHOT = """
tell application "System Events"
  if not (exists process "Phone") then return "NOPROCESS"
  tell process "Phone"
    set out to ""
    repeat with w in windows
      set out to out & "W|" & (name of w) & "|" & (subrole of w) & linefeed
      repeat with el in entire contents of w
        try
          set r to role of el
          if r is in {"AXButton", "AXStaticText", "AXTextField", "AXMenuButton", "AXSheet", "AXPopUpButton", "AXGroup", "AXRadioButton"} then
            set n to ""
            try
              set n to name of el
            end try
            set d to ""
            try
              set d to description of el
            end try
            set v to ""
            try
              set v to value of el
            end try
            if (n is not "") or (d is not "") or (v is not "") then
              set out to out & r & "|" & n & "|" & d & "|" & v & linefeed
            end if
          end if
        end try
      end repeat
    end repeat
    return out
  end tell
end tell
"""

_CLICK = """
tell application "System Events"
  tell process "Phone"
    repeat with w in windows
      repeat with el in entire contents of w
        try
          if role of el is "{role}" then
            set n to ""
            try
              set n to name of el
            end try
            set d to ""
            try
              set d to description of el
            end try
            if n is "{label}" or d is "{label}" then
              click el
              return "clicked"
            end if
          end if
        end try
      end repeat
    end repeat
    return "missing"
  end tell
end tell
"""

_MENU = """
tell application "System Events"
  tell process "Phone"
    click menu item "{item}" of menu 1 of menu bar item "Audio" of menu bar 1
    return "clicked"
  end tell
end tell
"""


class PhoneApp:
    def __init__(self, runner=None):
        self.run = runner or osascript

    def open(self, url: str | None = None) -> None:
        command = ["open", "-g", "-b", PHONE_BUNDLE]
        if url:
            command.append(url)
        subprocess.run(command, check=True, capture_output=True, timeout=15)

    def snapshot(self) -> list[dict]:
        raw = self.run(_SNAPSHOT)
        if raw.strip() == "NOPROCESS":
            return []
        elements = []
        for line in raw.splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                if len(parts) == 3 and parts[0] == "W":
                    elements.append({"role": "AXWindow", "name": parts[1], "subrole": parts[2]})
                continue
            role, name, description, value = parts
            elements.append(
                {"role": role, "name": name, "description": description, "value": value}
            )
        return elements

    @staticmethod
    def interpret(elements: list[dict]) -> dict:
        labels = []
        for element in elements:
            for key in ("name", "description", "value"):
                text = str(element.get(key, "")).strip()
                if text:
                    labels.append((element["role"], text))
        buttons = [text for role, text in labels if role in {"AXButton", "AXMenuButton"}]
        texts = [text for role, text in labels if role == "AXStaticText"]
        end_button = next((b for b in buttons if END_PATTERN.match(b)), None)
        confirm_button = next((b for b in buttons if CONFIRM_PATTERN.match(b)), None)
        keypad_button = next((b for b in buttons if KEYPAD_PATTERN.match(b)), None)
        timer = next((t for t in texts if TIMER_PATTERN.match(t)), None)
        progress = next((t for t in texts if PROGRESS_PATTERN.match(t)), None)
        ended = next((t for t in texts if ENDED_PATTERN.match(t)), None)
        in_call = end_button is not None
        return {
            "running": bool(elements),
            "in_call": in_call,
            "connected": in_call and (timer is not None or progress is None),
            "progress": progress,
            "timer": timer,
            "ended_notice": ended,
            "end_button": end_button,
            "confirm_button": confirm_button,
            "keypad_button": keypad_button,
            "buttons": buttons[:40],
            "texts": texts[:40],
        }

    def state(self) -> dict:
        return self.interpret(self.snapshot())

    def click(self, label: str, role: str = "AXButton") -> bool:
        result = self.run(_CLICK.format(role=role, label=_escape(label))).strip()
        return result == "clicked"

    def set_microphone(self, name: str) -> str:
        self.run(_MENU.format(item=_escape(name)))
        return name

    def dial(self, number: str, confirm_timeout: float = 12, poll: float = 0.5) -> dict:
        """Open the tel: URL, accept Phone's confirmation, and report the first call state."""
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
            raise PhoneControlError("Dial requires an E.164 number.")
        self.open(f"tel:{number}")
        deadline = time.monotonic() + confirm_timeout
        confirmed = False
        state = {}
        while time.monotonic() < deadline:
            state = self.state()
            if state["in_call"]:
                break
            if state["confirm_button"] and not confirmed:
                confirmed = self.click(state["confirm_button"])
            time.sleep(poll)
        return {"number": number, "confirmed": confirmed, **state}

    def wait_connected(self, timeout: float = 45, poll: float = 1.0, settle: float = 6) -> dict:
        """Wait for the call to connect; ringing tone alone is not connection."""
        deadline = time.monotonic() + timeout
        in_call_since = None
        state = {}
        while time.monotonic() < deadline:
            state = self.state()
            if not state["in_call"]:
                if in_call_since is not None:
                    return {"connected": False, "reason": "call_ended", **state}
            else:
                in_call_since = in_call_since or time.monotonic()
                if state["timer"] or (
                    state["progress"] is None and time.monotonic() - in_call_since >= settle
                ):
                    return {"connected": True, **state}
            time.sleep(poll)
        return {"connected": False, "reason": "timeout", **state}

    def hangup(self) -> dict:
        state = self.state()
        if not state["in_call"]:
            return {"hung_up": False, "was_in_call": False, **state}
        clicked = self.click(state["end_button"])
        time.sleep(0.8)
        after = self.state()
        return {"hung_up": clicked and not after["in_call"], "was_in_call": True, **after}

    def keypad(self, digits: str) -> dict:
        if not re.fullmatch(r"[0-9*#]{1,16}", digits):
            raise PhoneControlError("Keypad accepts 0-9, * and #.")
        state = self.state()
        if not state["in_call"]:
            raise PhoneControlError("No active call for keypad input.")
        if state["keypad_button"]:
            self.click(state["keypad_button"])
            time.sleep(0.4)
        pressed = ""
        for digit in digits:
            if not self.click(digit):
                break
            pressed += digit
            time.sleep(0.25)
        return {"requested": digits, "pressed": pressed, "complete": pressed == digits}


def phone_ui_dump(path: Path | None = None) -> dict:
    """Diagnostic for the exact labels this Phone version exposes."""
    app = PhoneApp()
    app.open()
    time.sleep(1.5)
    elements = app.snapshot()
    report = {"accessibility": True, "elements": elements, "state": app.interpret(elements)}
    if path:
        path.write_text(json.dumps(report, indent=2))
    return report
