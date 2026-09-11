# Resume here

## Current state — September 11, 2026 (late evening)

Demo-ready autonomous caller, multi-host. Everything except the final
**press of the Call button on a real number** is verified on this Mac.

### Verified working

- **Conversation pipeline**, real models, `t2ma demo`:
  0.29 s median from transcript to first reply audio, whole 3-turn call in
  21 s wall clock (`runs/20260911-230002-f42f7b52`). Qwen3-8B with a cached
  prompt prefix, sentence-streamed Kokoro, cached acknowledgments, Whisper
  large-v3-turbo, Silero endpointing at 0.55 s.
- **Phone app control** through the Accessibility API (`axapi.py`, ctypes):
  window state reads in **0.04 s** (was 10 s through osascript). Live dry run
  opens the keypad, types `+12025550123`, reads back `+1 (202) 555-0123`, and
  locates the popover's Call button. Nothing was ever dialed.
- **Audio routing** through Phone's own Audio menu in 0.06 s, leaving system
  defaults untouched, and restoring to "Use System Setting" afterwards.
- **Both virtual buses** carry a tone at full amplitude (`t2ma loopback-test`).
  Cross-process check confirmed a listener that opens BlackHole 2ch input
  *first* (as Phone does) still hears audio written later by another process.
- `t2ma doctor` reports `live_call_ready: true`.
- 69 tests; `scripts/verify_mcp.py --full` runs a demo call through MCP.
- Hosts: `uv run t2ma install` writes `opencode.json`, `.mcp.json`,
  `.claude/skills/phone-call/`, and registers the Codex plugin.

### First real dial — September 11, 2026, 23:10

`runs/20260911-231023-3133f866`. Dialed the developer's own iPhone as a
self-test. The Phone app entered a call state, then the carrier dropped it
after ~3.5 s, which is the expected outcome when the Mac relays through the
same iPhone it is dialing.

Proven by that call:

- The keypad dial path works end to end against the real Phone app: keypad
  opened, number typed and read back, Call pressed, and the app reported an
  active call.
- Audio routed automatically (Phone output to BlackHole 16ch, microphone to
  BlackHole 2ch) and restored to "Use System Setting" afterwards, with the
  system defaults never touched.
- The runner failed closed on the drop: outcome `failed`, reason
  `call did not connect (call_ended)`, no invented conversation, devices
  restored, no stale `.runtime/audio-defaults.json`, no call left hanging.

Still unproven, because the call never connected: a sustained conversation,
the in-call window's button labels, two-way audio over a live line, and
hangup. Those need a call to a number that is not this iPhone.

### What is left for a real call

1. **Place one short test call to a number other than this iPhone** (a second
   phone, a friend who is expecting it, or a recorded test line), with the
   monitor on so you hear both sides:
   ```sh
   uv run t2ma call --plan @.runtime/plan.json
   ```
   That single call verifies the three untested things at once: the Call press
   registers, the in-call UI matches, and audio flows both ways.
2. **Watch for in-call label mismatches.** The in-call window has never been
   inspected. `in_call` detection has a reliable fallback (Phone enables its
   Audio ▸ Mute item only during a call), but hangup looks for a button named
   `End`, `End Call`, or `Hang Up`. If none matches, hangup returns
   `hung_up: false` and the result says the call still needs ending by hand.
   Run `uv run t2ma phone-ui` *while the call is up* to capture real labels,
   then adjust `END_PATTERN` / `KEYPAD_PATTERN` in `src/talk2myagent/macphone.py`.
3. **Then the Amazon demo**: fill in `examples/amazon-return-plan.json` with a
   real order and run the same command.

Ctrl-C hangs up and restores audio. `uv run t2ma audio-restore` fixes devices
if a crash leaves them switched.

### Known limits

- Qwen3-8B sometimes over-confirms or repeats a question; a loop breaker ends
  the call after two identical turns. Set `conversation_model` in
  `config.local.json` for a stronger local model.
- The rehearsal persona invents its own order details; it exercises the
  pipeline, not Amazon's policies.
- Barge-in during agent speech is energy-based over BlackHole; hold music
  could trigger it on a live call.
- Each outgoing utterance opens its own output stream on BlackHole 2ch. That
  is proven to work cross-process; a single persistent stream would shave a
  little setup time per turn if it ever matters.

### Commands

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run t2ma doctor
uv run t2ma loopback-test
uv run t2ma demo --play
uv run t2ma phone-ui
uv run python scripts/verify_mcp.py --full
uv run t2ma install
```

The persistent service listens on `.runtime/service.sock`; after code changes
stop it with `kill -TERM` on the PID from
`lsof -U | rg 'talk2myagent/.runtime/service.sock'` so the next command picks
up the new code.

## Earlier milestones (still valid)

- Human role-play mode (`phone_test_*`) verified live on September 11 with the
  developer playing Amazon support; `controller` values are `local`/`host`.
- Scripted rehearsal (`t2ma demo --scripted`) and the manual tool set remain
  for debugging.
