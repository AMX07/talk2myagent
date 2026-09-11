# Resume here

## Current state — September 11, 2026

Working local speech demo and installed Codex plugin. **No live Amazon call has
been made by this project, and no real order has been changed.**

All source is in `/Users/anshmittal/Documents/talk2myagent`. Git is on `main`.
Run `git log --oneline` to see incremental checkpoints. `uv.lock` pins packages.

## Verified

- Host: macOS 27, Apple Silicon, 128 GiB RAM.
- User confirmed US Amazon and iPhone calling already configured on this Mac.
- Phone's Audio menu exposes microphone selection; no output selector was seen.
- Kokoro model + voices downloaded to `models/`; Whisper small.en in the standard
  Hugging Face cache. Local synthesis/transcription round-trip succeeded.
- Return rehearsal: `runs/20260911-202727-ea92a830/`.
- Cancellation rehearsal (HF_HUB_OFFLINE=1): `runs/20260911-202913-8227952d/`.
- Real MCP integration: `runs/20260911-202915-e6444b6a/`; 13 tools exposed, actual
  synthesized request, remote synthesis → Whisper, transcript and recording returned.
- 21 automated tests passed; Ruff passed before final documentation changes.
- Skill and plugin validators passed.
- Plugin installed with `codex plugin add talk2myagent@personal`. Start a **new
  Codex task** for tool discovery. The installer reruns the cachebuster flow.
- The personal marketplace's `./plugins/talk2myagent` resolves from the user's
  home. `~/plugins/talk2myagent` is a symlink into this repo, not a second code copy.
- Browser URL policy blocked previewing the local HTML file. Audio structure and
  report content are validated by code; visual browser inspection was not completed.

## Next required live-call steps

1. User runs `brew install --cask blackhole-2ch blackhole-16ch` in their Terminal.
   Attempted install reached sudo and stopped because it needs their password.
   Homebrew says a reboot may be required. Do not request the password in chat.
2. Run `uv run t2ma doctor` and `uv run t2ma loopback-test`. Configure Phone input
   to 2ch and Phone/system output to 16ch. Verify with a consenting test recipient.
3. Obtain real order/item/reason, allowed disclosure/action, and verified support
   number. Present the concrete call plan. Existing authorization applies within
   its scope; do not re-ask for actions the user already approved.
4. Conduct the call through computer use + tools and return actual artifacts.

## Important boundaries

- Codex is the only reasoning agent; Whisper/Kokoro are speech components.
- `phone_dial_request` and `phone_keypad` return instructions, not executed UI actions.
- Computer use owns dialing, connection observation, keypad, and hangup.
- Watchdog stops audio after inactivity or max duration but **cannot hang up**.
- `phone_finish` returns the complete available transcript and flags missing hangup.
- RMS voice activity detection is simple; hold music/noise can trigger false speech
  or interruption. Telephone routing, interruption, and IVR remain untested live.
- Rehearsal dialogue is scripted. Passing it proves the audio/tool pipeline, not
  autonomous negotiation or Amazon acceptance of an AI caller.
- Raw remote audio is saved only after recording_start. Recognized text and
  generated outgoing speech are persisted even before audio recording starts.

## Commands

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run t2ma demo --action return
uv run python scripts/verify_mcp.py
uv run t2ma request doctor
uv run python scripts/install_plugin.py
```

The persistent service uses `.runtime/service.sock` (private Unix socket) and
`.runtime/service.log`. Stop only its own PID with SIGTERM if restarting it. No
models, credentials, or private call artifacts are committed to Git.
