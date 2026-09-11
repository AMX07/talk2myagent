import json

import pytest

from talk2myagent import macphone
from talk2myagent.macphone import AccessibilityError, AudioRouting, PhoneApp, PhoneControlError

IDLE = """W|Phone|AXStandardWindow
AXButton|||Message
AXButton|keypad|keypad|
AXStaticText||+1 (888) 280-4331|
"""
CONFIRM = (
    IDLE
    + """W||AXDialog
AXStaticText||Call +1 (888) 280-4331?|
AXButton|Cancel|Cancel|
AXButton|Call|Call|
"""
)
RINGING = """W|Phone|AXStandardWindow
AXStaticText||calling…|
AXButton|mute|Mute|
AXButton|keypad|Keypad|
AXButton|End|End Call|
"""
CONNECTED = """W|Phone|AXStandardWindow
AXStaticText||0:07|
AXButton|mute|Mute|
AXButton|keypad|Keypad|
AXButton|End|End Call|
"""


class ScriptedPhone:
    """Feeds canned accessibility dumps and records clicks."""

    def __init__(self, states):
        self.states = list(states)
        self.clicks = []
        self.opened = []

    def __call__(self, script):
        if "click el" in script:
            label = script.split('n is "', 1)[1].split('"', 1)[0]
            self.clicks.append(label)
            if len(self.states) > 1:
                self.states.pop(0)
            return "clicked\n"
        if "menu item" in script:
            self.clicks.append("menu:" + script.split('menu item "', 1)[1].split('"', 1)[0])
            return "clicked\n"
        return self.states[0]


def test_interpret_recognizes_confirmation_ringing_and_connected_states():
    idle = PhoneApp.interpret(PhoneApp(runner=lambda s: IDLE).snapshot())
    assert not idle["in_call"] and idle["confirm_button"] is None
    confirm = PhoneApp.interpret(PhoneApp(runner=lambda s: CONFIRM).snapshot())
    assert confirm["confirm_button"] == "Call" and not confirm["in_call"]
    ringing = PhoneApp.interpret(PhoneApp(runner=lambda s: RINGING).snapshot())
    assert ringing["in_call"] and not ringing["connected"] and ringing["progress"]
    connected = PhoneApp.interpret(PhoneApp(runner=lambda s: CONNECTED).snapshot())
    assert connected["connected"] and connected["timer"] == "0:07"
    assert connected["end_button"] == "End" and connected["keypad_button"] == "keypad"


def test_dial_confirms_and_hangs_up(monkeypatch):
    script = ScriptedPhone([CONFIRM, RINGING])
    phone = PhoneApp(runner=script)
    monkeypatch.setattr(phone, "open", lambda url=None: script.opened.append(url))
    result = phone.dial("+18882804331", confirm_timeout=2, poll=0.01)
    assert script.opened == ["tel:+18882804331"]
    assert result["confirmed"] and result["in_call"] and script.clicks == ["Call"]
    script.states = [CONNECTED, IDLE]
    hung = phone.hangup()
    assert hung["hung_up"] and hung["was_in_call"] and script.clicks[-1] == "End"


def test_dial_rejects_bad_numbers_and_keypad_needs_a_call():
    phone = PhoneApp(runner=lambda s: IDLE)
    with pytest.raises(PhoneControlError):
        phone.dial("8882804331")
    with pytest.raises(PhoneControlError):
        phone.keypad("1")


def test_keypad_presses_each_digit():
    script = ScriptedPhone([CONNECTED])
    phone = PhoneApp(runner=script)
    result = phone.keypad("1#")
    assert result["complete"] and script.clicks == ["keypad", "1", "#"]


def test_missing_accessibility_is_reported_clearly(monkeypatch):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "execution error: System Events got an error: osascript is not allowed assistive access. (-1719)"

    monkeypatch.setattr(macphone.subprocess, "run", lambda *a, **k: Completed())
    with pytest.raises(AccessibilityError, match="Accessibility"):
        macphone.osascript('tell application "System Events" to UI elements enabled')
    assert macphone.accessibility_enabled() is False


def test_audio_routing_applies_and_restores(monkeypatch, tmp_path):
    class FakeCore:
        def __init__(self):
            self.defaults = {"output": "MacBook Pro Speakers", "input": "MacBook Pro Microphone"}

        def default(self, kind):
            return self.defaults[kind]

        def set_default(self, kind, name):
            if name not in {
                "BlackHole 16ch",
                "BlackHole 2ch",
                "MacBook Pro Speakers",
                "MacBook Pro Microphone",
            }:
                raise PhoneControlError("missing")
            self.defaults[kind] = name

    monkeypatch.setattr(macphone, "CoreAudio", FakeCore)
    monkeypatch.setattr(macphone, "runtime_dir", lambda: tmp_path)
    script = ScriptedPhone([IDLE])
    routing = AudioRouting("BlackHole 16ch", "BlackHole 2ch", PhoneApp(runner=script))
    applied = routing.apply()
    assert applied["output"] == "BlackHole 16ch" and applied["input"] == "BlackHole 2ch"
    assert routing.core.defaults == {"output": "BlackHole 16ch", "input": "BlackHole 2ch"}
    assert script.clicks == ["menu:BlackHole 2ch"]
    assert (
        json.loads((tmp_path / "audio-defaults.json").read_text())["output"]
        == "MacBook Pro Speakers"
    )
    restored = routing.restore()
    assert restored["restored"] and routing.core.defaults["output"] == "MacBook Pro Speakers"
    assert not (tmp_path / "audio-defaults.json").exists()
    assert script.clicks[-1] == "menu:Use System Setting"
