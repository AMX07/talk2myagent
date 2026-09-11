"""Phone control tests against a fake accessibility backend."""

import json

import pytest

from talk2myagent import macphone
from talk2myagent.macphone import AudioRouting, MenuItem, Node, PhoneApp, PhoneControlError


class FakeElement:
    """Stands in for an AXUIElement; records presses and value writes."""

    def __init__(self, backend, label):
        self.backend, self.label = backend, label
        self.text = ""

    def press(self):
        self.backend.presses.append(self.label)
        self.backend.advance()
        return True

    def set_text(self, value):
        self.backend.writes.append((self.label, value))
        self.backend.typed = value
        return True


def node(backend, role, name="", description="", value="", container="AXWindow"):
    item = Node(role, name, description, value, container)
    item.element = FakeElement(backend, name or description)
    return item


def recents(backend):
    """The Recents list: each row is a button, plus a per-row Call button."""
    return [
        node(backend, "AXButton", description="+1 (888) 280-4331, Outgoing  unknown, Call"),
        node(backend, "AXButton", description="Call"),
        node(backend, "AXStaticText", description="+1 (888) 280-4331"),
    ]


def toolbar(backend):
    return [
        node(backend, "AXMenuButton", name="Edit", description="Edit"),
        node(backend, "AXButton", description="keypad"),
        node(backend, "AXTextField", description="Search"),
    ]


def keypad_popover(backend):
    return [
        node(
            backend,
            "AXTextField",
            description="Phone number",
            value=backend.typed,
            container="AXPopover",
        ),
        node(backend, "AXButton", description="Call", container="AXPopover"),
    ]


def in_call(backend, timer="0:07", progress=None):
    items = [
        node(backend, "AXButton", description="Mute"),
        node(backend, "AXButton", description="Keypad"),
        node(backend, "AXButton", description="End"),
    ]
    items += [node(backend, "AXButton", description=d) for d in "0123456789*#"]
    if timer:
        items.append(node(backend, "AXStaticText", description=timer))
    if progress:
        items.append(node(backend, "AXStaticText", description=progress))
    return items


class FakeBackend:
    """Serves a scripted sequence of UI states and an Audio menu."""

    IN_CALL_SCREENS = ("ringing", "connected")

    def __init__(self, screens=("idle",), mute=False):
        self.screens = list(screens)
        self.forced_mute = mute
        self.presses, self.writes, self.menu_presses = [], [], []
        self.typed = ""
        self.launched = self.quits = 0
        self.selection = {"Output": "Use System Setting", "Microphone": "Use System Setting"}
        self.window_count = 1

    @property
    def mute(self):
        """Phone enables Mute only while a call is up, so follow the screen."""
        return self.forced_mute or self.screens[0] in self.IN_CALL_SCREENS

    def advance(self):
        if len(self.screens) > 1:
            self.screens.pop(0)

    def running(self):
        return True

    def launch(self, background=False):
        self.launched += 1

    def quit(self):
        self.quits += 1

    def windows(self):
        return [object()] * self.window_count

    def nodes(self):
        screen = self.screens[0]
        if screen == "idle":
            return recents(self) + toolbar(self)
        if screen == "keypad":
            return recents(self) + toolbar(self) + keypad_popover(self)
        if screen == "ringing":
            return in_call(self, timer=None, progress="calling…")
        if screen == "connected":
            return in_call(self)
        raise AssertionError(f"unknown screen {screen}")

    def menu(self, title):
        devices = {
            "Microphone": ["Use System Setting", "BlackHole 16ch", "BlackHole 2ch"],
            "Output": ["Use System Setting", "BlackHole 16ch", "BlackHole 2ch"],
        }
        items = [MenuItem("Mute", enabled=self.mute)]
        for section, names in devices.items():
            for name in names:
                item = MenuItem(
                    name,
                    enabled=True,
                    mark="✓" if self.selection[section] == name else "",
                    section=section,
                )
                item.element = FakeMenuElement(self, section, name)
                items.append(item)
        return items


class FakeMenuElement:
    def __init__(self, backend, section, name):
        self.backend, self.section, self.name = backend, section, name

    def press(self):
        self.backend.menu_presses.append((self.section, self.name))
        self.backend.selection[self.section] = self.name
        return True


def phone_for(*screens, mute=False):
    backend = FakeBackend(screens or ("idle",), mute=mute)
    return PhoneApp(backend=backend), backend


def test_recents_rows_never_look_like_a_dial_confirmation():
    app, _ = phone_for("idle")
    state = app.state()
    assert not state["in_call"]
    # The Recents list has its own "Call" buttons; only a dialog's counts.
    assert "Call" in state["buttons"] and state["confirm_button"] is None
    assert state["keypad_button"] == "keypad" and not state["keypad_open"]


def test_keypad_popover_and_call_states_are_recognized():
    app, backend = phone_for("keypad")
    backend.typed = "+1 (202) 555-0123"
    state = app.state()
    assert state["keypad_open"] and state["confirm_button"] == "Call"
    assert state["number_field"] == "+1 (202) 555-0123"
    app, _ = phone_for("ringing")
    ringing = app.state()
    assert ringing["in_call"] and not ringing["connected"] and ringing["progress"]
    app, _ = phone_for("connected")
    connected = app.state()
    assert connected["connected"] and connected["timer"] == "0:07"
    assert connected["end_button"] == "End"


def test_an_enabled_mute_item_alone_proves_a_call_is_up():
    app, _ = phone_for("idle", mute=True)
    assert app.state()["in_call"] is True


def test_dial_types_the_number_then_presses_the_popover_call_button():
    app, backend = phone_for("idle", "keypad")
    dry = app.dial("+12025550123", dry_run=True)
    assert backend.presses == ["keypad"] and backend.writes == [("Phone number", "+12025550123")]
    assert not dry["dialed"] and dry["number_field"] == "+12025550123"

    app, backend = phone_for("idle", "keypad", "connected")
    result = app.dial("+12025550123", confirm_timeout=2, poll=0.01)
    assert result["dialed"] and result["in_call"]
    assert backend.presses == ["keypad", "Call"]


def test_dial_refuses_a_mismatched_number_and_a_non_e164_number():
    app, backend = phone_for("keypad")

    class Wrong(FakeElement):
        def set_text(self, value):
            backend.typed = "+1 (415) 555-0000"
            return True

    original = backend.nodes

    def wrong_nodes():
        items = original()
        for item in items:
            if item.role == "AXTextField" and "Phone number" in item.labels:
                item.element = Wrong(backend, "Phone number")
        return items

    backend.nodes = wrong_nodes
    with pytest.raises(PhoneControlError, match="Not dialing"):
        app.dial("+12025550123")
    with pytest.raises(PhoneControlError, match="E.164"):
        app.dial("8882804331")


def test_dial_refuses_while_another_call_is_up():
    app, _ = phone_for("connected")
    with pytest.raises(PhoneControlError, match="already in progress"):
        app.dial("+12025550123")


def test_hangup_presses_end_and_confirms_the_call_dropped():
    app, backend = phone_for("connected", "idle")
    result = app.hangup()
    assert backend.presses == ["End"] and result["was_in_call"]
    app, backend = phone_for("idle")
    assert app.hangup() == {"hung_up": False, "was_in_call": False, **app.state()}


def test_keypad_presses_each_digit_during_a_call():
    app, backend = phone_for("connected")
    result = app.keypad("1#")
    assert result["complete"] and backend.presses == ["Keypad", "1", "#"]
    app, _ = phone_for("idle")
    with pytest.raises(PhoneControlError, match="No active call"):
        app.keypad("1")


def test_ensure_window_relaunches_a_windowless_phone():
    app, backend = phone_for("idle")
    backend.window_count = 0

    original = backend.launch

    def launch_then_appear(background=False):
        original(background)
        if backend.launched >= 2:
            backend.window_count = 1

    backend.launch = launch_then_appear
    assert app.ensure_window(timeout=10) == 1
    assert backend.quits == 1 and backend.launched >= 2


def test_missing_accessibility_permission_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(macphone.axapi, "trusted", lambda: False)
    assert macphone.accessibility_enabled() is False
    backend = macphone.AXPhoneBackend()
    with pytest.raises(macphone.AccessibilityError, match="Accessibility"):
        backend.nodes()


class FakeCore:
    def __init__(self):
        self.defaults = {"output": "MacBook Pro Speakers", "input": "MacBook Pro Microphone"}

    def default(self, kind):
        return self.defaults[kind]

    def set_default(self, kind, name):
        self.defaults[kind] = name


def test_audio_routing_prefers_the_phone_menu_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr(macphone, "CoreAudio", FakeCore)
    monkeypatch.setattr(macphone, "runtime_dir", lambda: tmp_path)
    app, backend = phone_for("idle")
    routing = AudioRouting("BlackHole 16ch", "BlackHole 2ch", app)
    applied = routing.apply()
    assert backend.menu_presses == [("Output", "BlackHole 16ch"), ("Microphone", "BlackHole 2ch")]
    assert applied["phone_menu"] == {"Output": True, "Microphone": True}
    assert app.audio_selection() == {"Output": "BlackHole 16ch", "Microphone": "BlackHole 2ch"}
    assert routing.core.defaults["output"] == "MacBook Pro Speakers"  # system left alone
    assert json.loads((tmp_path / "audio-defaults.json").read_text())["menu"]["Output"]
    assert routing.restore()["restored"]
    assert app.audio_selection() == {
        "Output": "Use System Setting",
        "Microphone": "Use System Setting",
    }
    assert not (tmp_path / "audio-defaults.json").exists()


def test_audio_routing_falls_back_to_system_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(macphone, "CoreAudio", FakeCore)
    monkeypatch.setattr(macphone, "runtime_dir", lambda: tmp_path)
    app, backend = phone_for("idle")
    backend.menu = lambda title: []  # an older Phone without device sections
    routing = AudioRouting("BlackHole 16ch", "BlackHole 2ch", app)
    applied = routing.apply()
    assert applied["phone_menu"] == {"Output": False, "Microphone": False}
    assert routing.core.defaults == {"output": "BlackHole 16ch", "input": "BlackHole 2ch"}
    routing.restore()
    assert routing.core.defaults["output"] == "MacBook Pro Speakers"


def test_restore_after_a_crash_reads_saved_state_from_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(macphone, "CoreAudio", FakeCore)
    monkeypatch.setattr(macphone, "runtime_dir", lambda: tmp_path)
    (tmp_path / "audio-defaults.json").write_text(
        json.dumps({"defaults": {"output": "MacBook Pro Speakers"}, "menu": {"Microphone": True}})
    )
    app, backend = phone_for("idle")
    backend.selection["Microphone"] = "BlackHole 2ch"
    routing = AudioRouting("BlackHole 16ch", "BlackHole 2ch", app)
    assert routing.restore()["restored"]
    assert backend.selection["Microphone"] == "Use System Setting"
    assert routing.core.defaults["output"] == "MacBook Pro Speakers"
