# Resume here

## Current state — September 11, 2026 (evening)

Demo-ready autonomous caller, multi-host. **No real dialed call has been made
yet by the software; the last blocker is a one-time macOS permission.**

### What changed today (later session)

- `phone_call_start` runs a whole call: route audio (CoreAudio via ctypes),
  dial `tel:` in Phone.app, confirm the dial sheet and detect connection
  (accessibility scripting), converse with the local model, press keypad
  digits, hang up, restore devices, return transcript + recording + latency.
- Speed: Qwen3-8B with a cached prompt prefix, sentence-streamed Kokoro, fixed
  acknowledgments played from cache, Whisper large-v3-turbo, Silero endpoint at
  0.55 s. Rehearsal (`runs/20260911-222218-25609adf`): median 0.36 s from
  transcript to first reply audio; whole 6-turn call in 25 s wall clock.
- Safety: per-sentence fact guard (emails, digit strings, dates not in plan or
  transcript are replaced with a fallback), deterministic opening on a human
  greeting, consent-gated audio retention, request→continue rule, loop breaker.
- Hosts: `uv run t2ma install` writes `opencode.json`, `.mcp.json`,
  `.claude/skills/phone-call/`, and refreshes the Codex plugin manifest.
  Tool names are identical everywhere (`phone_*`).
- CLI without any agent: `t2ma plan`, `t2ma call`, `t2ma demo [--play]`,
  `t2ma hangup`, `t2ma phone-ui`, `t2ma phone-state`, `t2ma audio-restore`.
- 62 tests; `scripts/verify_mcp.py --full` runs a demo call through MCP.

### To make the first real call

1. Grant Accessibility (System Settings › Privacy & Security › Accessibility)
   to the host app: Terminal for `t2ma call`, or the Codex / OpenCode / Claude
   desktop app. `uv run t2ma doctor` must show `accessibility_enabled: true`.
2. `uv run t2ma phone-ui` with the Phone app open: confirm the dump shows the
   labels the matcher expects (a `Call` confirm button after `open tel:`, an
   `End`/`End Call` button and a `m:ss` timer during a call, a `keypad`
   button). Adjust the regexes at the top of `src/talk2myagent/macphone.py` if
   this Phone version uses other words.
3. Copy `examples/amazon-return-plan.json`, fill the REPLACE fields with real
   order details, and run `uv run t2ma call --plan @.runtime/plan.json`.
   Listen through the built-in speakers (monitor). Ctrl-C hangs up.
4. Watch for: the confirm click (if the sheet is not detected in 12 s the run
   fails without dialing), connection detection (timer text), and the
   Phone microphone menu (the service selects BlackHole 2ch; if the menu item
   name differs, the system-default input is still switched).

### Known limits

- The `codex` CLI was not on PATH in this session, so the plugin cachebuster
  was not bumped; the plugin directory is a symlink into this repo, so new
  Codex tasks still load the updated skill and `.mcp.json`.
- Qwen3-8B sometimes over-confirms or asks for something already given; the
  loop breaker closes after two identical turns. A stronger local model can be
  set in `config.local.json` (`conversation_model`) if quality matters more.
- The persona in `t2ma demo` invents its own details; it tests the pipeline,
  not Amazon's policies.
- Barge-in during agent speech is energy-based over BlackHole; hold music
  could trigger it on a live call.

### Commands

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run t2ma doctor
uv run t2ma demo --play
uv run python scripts/verify_mcp.py --full
uv run t2ma install
```

The persistent service listens on `.runtime/service.sock`; stop it with
`kill -TERM` on the PID from `lsof -U | rg 'talk2myagent/.runtime/service.sock'`
after code changes so the next command starts the new code.

## Earlier milestones (still valid)

- Human role-play mode (`phone_test_*`) verified live on September 11 with the
  developer playing Amazon support; `controller` values are now `local`/`host`.
- Scripted rehearsal (`t2ma demo --scripted`) and the original manual tool set
  remain for debugging.
