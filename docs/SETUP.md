# Setup and live calling

Everything runs on this Mac: Whisper (hearing), Kokoro (speaking), and Qwen
(deciding) through MLX and ONNX Runtime. No cloud speech, telephony, or LLM API
is used. `.venv`, `models`, `.runtime`, and `runs` stay out of Git.

```sh
cd /Users/anshmittal/Documents/talk2myagent
./scripts/setup.sh        # venv + models + doctor
uv run t2ma install       # register with OpenCode, Claude Code, Codex
uv run t2ma demo --play   # hear an autonomous rehearsal; no call placed
```

## One-time macOS setup for real calls

1. **Cellular calls from the Mac.** The iPhone and Mac must share an Apple
   Account with "Calls on Other Devices" enabled
   ([Apple's guide](https://support.apple.com/en-us/102405)). Place one manual
   call from the Phone app once to confirm it works.
2. **Virtual audio buses.** `brew install --cask blackhole-2ch blackhole-16ch`
   (needs your administrator password; Homebrew may ask for a reboot). Both are
   installed on this Mac already.
3. **Accessibility permission.** The service confirms the dial sheet, hangs up,
   and presses keypad digits by scripting the Phone app. macOS attributes that
   to the app that launched the service, so enable **System Settings › Privacy &
   Security › Accessibility** for each host you will use: Terminal (or iTerm)
   for the CLI, and the Codex, OpenCode, or Claude desktop app for agent use.
   `uv run t2ma doctor` reports `accessibility_enabled`; `uv run t2ma phone-ui`
   dumps the labels the Phone app exposes if a control is not found.
4. **Nothing else to configure.** For each call the service saves your current
   default output/input devices, points the system output at BlackHole 16ch
   (what Phone plays), points the system input and Phone's microphone menu at
   BlackHole 2ch (what the agent speaks into), and restores both when the call
   ends. If a crash leaves them switched, run `uv run t2ma audio-restore`.

Optional: `config.local.json` (copy `config.example.json`) to change devices,
models, endpointing, or `monitor_device` (the speakers used to let the room hear
both sides; the built-in speakers are used by default).

## Place a call

From an agent (Codex, OpenCode, Claude Code): describe the call; the agent
prepares the plan and uses `phone_call_start`. See [INTERFACES.md](INTERFACES.md).

From the terminal:

```sh
cp examples/amazon-return-plan.json .runtime/plan.json   # fill in the REPLACE fields
uv run t2ma call --plan @.runtime/plan.json
```

The command shows the plan, asks for confirmation, then prints each transcript
line and the response latency per turn. Phases: routing → dialing → ringing →
talking → ended. Ctrl-C hangs up. Results go to `runs/<call-id>/` with
`recording.wav` (their side left, agent right), `transcript.json`,
`transcript.txt`, `result.json`, and `report.html`.

What the agent does on the line:

- Waits for the greeting, then delivers the plan's opening (identity, purpose,
  request to record). If a phone menu answers, it replies to the menu instead
  and presses digits in the Phone app when told to.
- Uses only plan facts. Any email, digit string, or date that is not in the
  plan or the transcript is blocked before it is spoken and replaced with
  "I don't have that detail on hand."
- Starts retaining audio only after the other side agrees to recording.
- Ends with a proposal (`resolved`, `needs_user`) that the host agent must
  review against the success criteria; silence never counts as success.

## Amazon specifics

Amazon US customer service: +1 888 280 4331 (from amazon.com/contact-us;
confirm it yourself before the call). Their system may ask for the phone number
on the account, send a one-time code, or require the account holder. Those are
stop conditions: the agent says it cannot provide them and ends with
`needs_user`, and you take over. A return or cancellation is only reported as
`completed` when the representative confirms it on the call.

## Speed

Measured on this Mac (M-series, 128 GB) in the autonomous rehearsal:

| Stage | Typical |
|---|---|
| End-of-speech detection (Silero VAD) | 0.55 s after the last word |
| Whisper large-v3-turbo on a 10 s clip | 0.1–0.3 s |
| First reply token (8B model, cached prefix) | 0.2–0.3 s |
| First audio (cached acknowledgment, then streamed sentences) | ≈0.3 s after the first token |

The greeting and stock phrases are pre-synthesized, so the opening plays with no
generation at all. `t2ma call` prints "from their last word to first reply audio"
per turn; expect roughly 1–1.5 s on a live call.

## Recover

Events append to `runs/<id>/events.jsonl` as they happen. `uv run t2ma request
result --json '{"call_id":"ID"}'` recovers a session after a restart. The
watchdog stops audio after 120 s without activity or 15 minutes total and the
runner hangs up. To stop the background service: find its PID with
`lsof -U | rg 'talk2myagent/.runtime/service.sock'` and `kill -TERM <PID>`.
