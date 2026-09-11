"""Control the macOS Phone app and audio routing without third-party tools.

Dialing uses the Phone app's own keypad popover: type the number into its
"Phone number" field, read it back, and press that popover's Call button (the
Recents list has a Call button per row, so scoping matters). ``tel:`` links are
silently ignored on this macOS build when opened from a script, so they are not
used.

All UI reading and pressing goes through the in-process Accessibility binding in
:mod:`axapi`, which needs the app that launched this service (Terminal, Codex,
OpenCode, Claude) to be allowed under System Settings > Privacy & Security >
Accessibility. Audio is routed through Phone's own Audio menu (Output and
Microphone sections), falling back to the CoreAudio system defaults. Every
mutation is reversible and the previous state is saved under ``.runtime``.
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

from . import axapi
from .config import runtime_dir

PHONE_BUNDLE = "com.apple.mobilephone"
PHONE_PROCESS = "Phone"
END_PATTERN = re.compile(r"(?i)^(end( call)?|hang ?up|end and accept)$")
CONFIRM_PATTERN = re.compile(r"(?i)^(call|dial)$")
KEYPAD_PATTERN = re.compile(r"(?i)^(keypad|show keypad|dial pad)$")
PROGRESS_PATTERN = re.compile(r"(?i)^(calling|connecting|ringing|dialing)")
TIMER_PATTERN = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
ENDED_PATTERN = re.compile(r"(?i)^(call ended|call failed|busy|declined)")
CONTAINER_ROLES = {"AXWindow", "AXSheet", "AXPopover", "AXDialog"}
INTERESTING_ROLES = {"AXButton", "AXMenuButton", "AXStaticText", "AXTextField", "AXRadioButton"}
LEAF_ROLES = {"AXButton", "AXMenuButton", "AXStaticText", "AXRadioButton"}
NUMBER_FIELD = "Phone number"


class PhoneControlError(RuntimeError):
    pass


class AccessibilityError(PhoneControlError):
    pass


def accessibility_enabled() -> bool:
    return axapi.trusted()


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
    """Route Phone's audio through the two virtual buses, and restore afterwards.

    Preferred: Phone's own Audio menu (Output -> incoming bus, Microphone ->
    outgoing bus), which leaves the rest of the Mac alone. Fallback per
    direction: the system default device, which Phone follows under
    "Use System Setting".
    """

    def __init__(self, incoming_bus: str, outgoing_bus: str, phone: PhoneApp | None = None):
        self.incoming_bus, self.outgoing_bus = incoming_bus, outgoing_bus
        self.core = CoreAudio()
        self.phone = phone
        self.saved: dict | None = None
        self.menu: dict[str, bool] = {}
        self.state_file = runtime_dir() / "audio-defaults.json"

    def apply(self) -> dict:
        self.saved = {}
        self.menu = {}
        plan = {"Output": ("output", self.incoming_bus), "Microphone": ("input", self.outgoing_bus)}
        for section, (kind, device) in plan.items():
            done = False
            if self.phone is not None:
                try:
                    done = self.phone.set_audio(section, device)
                except PhoneControlError:
                    done = False
            self.menu[section] = done
            if not done:
                self.saved[kind] = self.core.default(kind)
                self.core.set_default(kind, device)
        self.state_file.write_text(json.dumps({"defaults": self.saved, "menu": self.menu}))
        return {
            "output": self.incoming_bus,
            "input": self.outgoing_bus,
            "phone_menu": dict(self.menu),
            "system_defaults_changed": dict(self.saved),
        }

    def restore(self) -> dict:
        saved, menu = self.saved, self.menu
        if saved is None and self.state_file.exists():
            state = json.loads(self.state_file.read_text())
            saved, menu = state.get("defaults", {}), state.get("menu", {})
        if saved is None:
            return {"restored": False}
        for kind, name in saved.items():
            try:
                self.core.set_default(kind, name)
            except PhoneControlError:
                continue
        if self.phone is not None:
            for section, used in (menu or {}).items():
                if used:
                    try:
                        self.phone.set_audio(section, "Use System Setting")
                    except PhoneControlError:
                        pass
        self.state_file.unlink(missing_ok=True)
        self.saved = None
        return {"restored": True, "defaults": saved, "menu": menu}


# ---------------------------------------------------------------- Phone.app ---


class Node:
    """One UI element, flattened out of the accessibility tree."""

    __slots__ = ("container", "description", "element", "name", "role", "value")

    def __init__(self, role, name="", description="", value="", container="AXWindow", element=None):
        self.role, self.name, self.description = role, name, description
        self.value, self.container, self.element = value, container, element

    @property
    def labels(self) -> list[str]:
        return [text for text in (self.name, self.description) if text]

    def matches(self, label: str) -> bool:
        # Recents rows spell a row as "+1 (888) 280-4331, Outgoing …, Call"; match the head.
        return any(
            text == label or text.split(",")[0].strip() == label or text.split(" ")[0] == label
            for text in self.labels
        )

    def press(self) -> bool:
        return bool(self.element and self.element.press())

    def set_text(self, value: str) -> bool:
        return bool(self.element and self.element.set_text(value))

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "name": self.name,
            "description": self.description,
            "value": self.value,
            "container": self.container,
        }


class MenuItem:
    __slots__ = ("element", "enabled", "mark", "section", "title")

    def __init__(self, title, enabled=True, mark="", section="", element=None):
        self.title, self.enabled, self.mark = title, enabled, mark
        self.section, self.element = section, element

    def press(self) -> bool:
        return bool(self.element and self.element.press())


class AXPhoneBackend:
    """Reads and drives the real Phone app through the Accessibility API."""

    def __init__(self, process_name: str = PHONE_PROCESS):
        self.process_name = process_name
        self._pid: int | None = None
        self._app: axapi.AXElement | None = None

    def _application(self) -> axapi.AXElement | None:
        pid = axapi.pid_of(self.process_name)
        if pid is None:
            self._pid, self._app = None, None
            return None
        if pid != self._pid or self._app is None:
            self._pid, self._app = pid, axapi.application(pid)
        return self._app

    def running(self) -> bool:
        return self._application() is not None

    def launch(self, background: bool = False) -> None:
        command = ["open", "-b", PHONE_BUNDLE] + (["-g"] if background else [])
        subprocess.run(command, check=True, capture_output=True, timeout=15)

    def quit(self) -> None:
        subprocess.run(
            ["osascript", "-e", 'tell application "Phone" to quit'],
            capture_output=True,
            timeout=15,
            check=False,
        )

    def windows(self) -> list[axapi.AXElement]:
        app = self._application()
        return app.elements("AXWindows") if app else []

    def nodes(self) -> list[Node]:
        if not axapi.trusted():
            raise AccessibilityError(
                "macOS Accessibility permission is missing for the app running talk2myagent. "
                "Enable it in System Settings > Privacy & Security > Accessibility for the host "
                "app (Terminal, Codex, OpenCode, or Claude), then retry."
            )
        found: list[Node] = []

        def walk(element: axapi.AXElement, container: str, depth: int) -> None:
            if depth > 30:
                return
            role = element.role
            if role in CONTAINER_ROLES:
                container = role
            elif role in INTERESTING_ROLES:
                name, description = element.labels
                value = element.text("AXValue")
                if name or description or value:
                    found.append(Node(role, name, description, value, container, element))
                if role in LEAF_ROLES:
                    return  # a Recents row is a button whose children add nothing
            for child in element.elements():
                walk(child, container, depth + 1)

        for window in self.windows():
            walk(window, "AXWindow", 0)
        return found

    def menu(self, title: str) -> list[MenuItem]:
        app = self._application()
        if app is None:
            return []
        bar = app.element("AXMenuBar")
        if bar is None:
            return []
        for item in bar.elements():
            if item.text("AXTitle") != title:
                continue
            menus = item.elements()
            if not menus:
                return []
            items: list[MenuItem] = []
            section = ""
            for entry in menus[0].elements():
                name = entry.text("AXTitle")
                enabled = entry.flag("AXEnabled")
                # Phone labels its sections with disabled items ("Microphone", "Output").
                if not enabled and name in {"Microphone", "Output"}:
                    section = name
                    continue
                items.append(
                    MenuItem(name, enabled, entry.text("AXMenuItemMarkChar"), section, entry)
                )
            return items
        return []


class PhoneApp:
    def __init__(self, backend: AXPhoneBackend | None = None):
        self.backend = backend or AXPhoneBackend()

    # ------------------------------------------------------------ reading ---

    def snapshot(self) -> list[Node]:
        return self.backend.nodes()

    def mute_enabled(self) -> bool:
        """An enabled Mute item is the most reliable "a call is up" signal."""
        return any(item.title == "Mute" and item.enabled for item in self.backend.menu("Audio"))

    def audio_selection(self) -> dict[str, str]:
        return {
            item.section: item.title
            for item in self.backend.menu("Audio")
            if item.section and item.mark
        }

    @staticmethod
    def interpret(nodes: list[Node], mute_enabled: bool = False) -> dict:
        buttons, dialog_buttons, texts = [], [], []
        number_field = None
        keypad_open = False
        for node in nodes:
            if node.container in {"AXSheet", "AXPopover", "AXDialog"}:
                keypad_open = keypad_open or node.container == "AXPopover"
            if node.role == "AXTextField" and NUMBER_FIELD in node.labels:
                number_field = node.value
            if node.role in {"AXButton", "AXMenuButton"}:
                buttons.extend(node.labels)
                if node.container in {"AXSheet", "AXPopover", "AXDialog"}:
                    dialog_buttons.extend(node.labels)
            elif node.role == "AXStaticText":
                texts.extend(node.labels + ([node.value] if node.value else []))
        end_button = next((b for b in buttons if END_PATTERN.match(b)), None)
        confirm_button = next((b for b in dialog_buttons if CONFIRM_PATTERN.match(b)), None)
        keypad_button = next((b for b in buttons if KEYPAD_PATTERN.match(b)), None)
        timer = next((t for t in texts if TIMER_PATTERN.match(t)), None)
        progress = next((t for t in texts if PROGRESS_PATTERN.match(t)), None)
        ended = next((t for t in texts if ENDED_PATTERN.match(t)), None)
        in_call = end_button is not None or mute_enabled
        return {
            "running": bool(nodes),
            "in_call": in_call,
            "connected": in_call and (timer is not None or progress is None),
            "mute_enabled": mute_enabled,
            "progress": progress,
            "timer": timer,
            "ended_notice": ended,
            "end_button": end_button,
            "confirm_button": confirm_button,
            "keypad_button": keypad_button,
            "keypad_open": keypad_open,
            "number_field": number_field,
            "buttons": buttons[:40],
            "texts": texts[:40],
        }

    def state(self) -> dict:
        nodes = self.snapshot()
        return self.interpret(nodes, self.mute_enabled() if nodes else False)

    # ------------------------------------------------------------ acting ----

    def find(
        self,
        label: str,
        role: str = "AXButton",
        scope: str = "any",
        nodes: list[Node] | None = None,
    ) -> Node | None:
        for node in nodes if nodes is not None else self.snapshot():
            if node.role != role or (scope != "any" and node.container != scope):
                continue
            if node.matches(label):
                return node
        return None

    def click(self, label: str, role: str = "AXButton", scope: str = "any") -> bool:
        node = self.find(label, role, scope)
        return bool(node and node.press())

    def set_field(self, label: str, value: str) -> str:
        node = self.find(label, role="AXTextField")
        if node is None:
            raise PhoneControlError(f"Text field {label!r} not found.")
        node.set_text(value)
        time.sleep(0.15)
        refreshed = self.find(label, role="AXTextField")
        return refreshed.value if refreshed else ""

    def set_audio(self, section: str, device: str) -> bool:
        """Select a device in Phone's Audio menu; section is 'Output' or 'Microphone'."""
        for item in self.backend.menu("Audio"):
            if item.section == section and item.title == device and item.enabled:
                return item.press()
        return False

    def set_microphone(self, name: str) -> str:
        if not self.set_audio("Microphone", name):
            raise PhoneControlError(f"Microphone {name!r} is not in Phone's Audio menu.")
        return name

    # ------------------------------------------------------------ lifecycle -

    def ensure_window(self, timeout: float = 15) -> int:
        """Phone launched hidden can have no window at all; relaunching restores it."""
        self.backend.launch()
        deadline = time.monotonic() + timeout
        relaunched = False
        while time.monotonic() < deadline:
            windows = len(self.backend.windows())
            if windows:
                return windows
            if not relaunched:
                self.backend.quit()
                time.sleep(2)
                self.backend.launch()
                relaunched = True
            time.sleep(0.5)
        raise PhoneControlError("The Phone app did not show a window; open it manually.")

    def dial(
        self, number: str, confirm_timeout: float = 15, poll: float = 0.5, dry_run: bool = False
    ) -> dict:
        """Dial through the keypad popover and report the first call state.

        dry_run stops after the number is typed and verified; nothing is called.
        """
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
            raise PhoneControlError("Dial requires an E.164 number.")
        self.ensure_window()
        state = self.state()
        if state["in_call"]:
            raise PhoneControlError("A call is already in progress in the Phone app.")
        if not state["keypad_open"]:
            if not self.click("keypad"):
                raise PhoneControlError("Keypad button not found in the Phone app.")
            time.sleep(0.8)
        shown = self.set_field(NUMBER_FIELD, number)
        digits = re.sub(r"\D", "", number)
        if not re.sub(r"\D", "", shown).endswith(digits):
            raise PhoneControlError(f"Keypad shows {shown!r}; expected {number}. Not dialing.")
        if dry_run:
            return {"number": number, "typed": shown, "dialed": False, **self.state()}
        if not self.click("Call", scope="AXPopover"):
            raise PhoneControlError("Keypad Call button not found; nothing dialed.")
        deadline = time.monotonic() + confirm_timeout
        state = self.state()
        while time.monotonic() < deadline and not state["in_call"]:
            if state["confirm_button"] and not state["keypad_open"]:
                self.click(state["confirm_button"], scope="AXSheet")
            time.sleep(poll)
            state = self.state()
        return {"number": number, "typed": shown, "dialed": True, "confirmed": True, **state}

    def clear_keypad(self) -> None:
        try:
            self.set_field(NUMBER_FIELD, "")
        except PhoneControlError:
            pass

    def wait_connected(self, timeout: float = 45, poll: float = 1.0, settle: float = 6) -> dict:
        """Wait for the call to connect; a ringing tone alone is not connection."""
        deadline = time.monotonic() + timeout
        in_call_since = None
        state: dict = {}
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
        clicked = False
        for candidate in ([state["end_button"]] if state["end_button"] else []) + [
            "End Call",
            "End",
            "Hang Up",
        ]:
            if candidate and self.click(candidate):
                clicked = True
                break
        time.sleep(0.8)
        after = self.state()
        return {"hung_up": clicked and not after["in_call"], "was_in_call": True, **after}

    def keypad(self, digits: str) -> dict:
        if not re.fullmatch(r"[0-9*#]{1,16}", digits):
            raise PhoneControlError("Keypad accepts 0-9, * and #.")
        state = self.state()
        if not state["in_call"]:
            raise PhoneControlError("No active call for keypad input.")
        if state["keypad_button"] and not state["keypad_open"]:
            self.click(state["keypad_button"])
            time.sleep(0.4)
        pressed = ""
        for digit in digits:
            if not self.click(digit):
                break
            pressed += digit
            time.sleep(0.2)
        return {"requested": digits, "pressed": pressed, "complete": pressed == digits}


def phone_ui_dump(path: Path | None = None) -> dict:
    """Diagnostic for the exact labels this Phone version exposes."""
    app = PhoneApp()
    app.ensure_window()
    nodes = app.snapshot()
    report = {
        "accessibility": accessibility_enabled(),
        "elements": [node.as_dict() for node in nodes],
        "audio_menu": [
            {"section": i.section, "title": i.title, "enabled": i.enabled, "selected": bool(i.mark)}
            for i in app.backend.menu("Audio")
        ],
        "state": app.interpret(nodes, app.mute_enabled()),
    }
    if path:
        path.write_text(json.dumps(report, indent=2))
    return report
