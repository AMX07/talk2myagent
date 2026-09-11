# Using talk2myagent from Codex, OpenCode, Claude Code, or the terminal

One local MCP server (`python -m talk2myagent mcp`) serves every host. It talks
to a persistent background service on a private Unix socket, so models stay
loaded between calls and every host sees the same sessions under `runs/`.

```sh
cd talk2myagent
./scripts/setup.sh          # venv, models, doctor
uv run t2ma install         # registers the server with OpenCode, Claude Code, and Codex
```

`install` writes absolute paths to this checkout's `.venv`, so rerun it if the
folder moves.

## OpenCode

`install` writes `opencode.json` at the repository root with an `mcp.phone`
entry (`type: local`). OpenCode also scans `.claude/skills/`, where the
`phone-call` skill is copied. Open this folder in OpenCode (desktop app or
`opencode` CLI); the tools appear as `phone_*` and the skill as `phone-call`.
Say, for example:

> Call Amazon US support at +1 888 280 4331 (from amazon.com/contact-us) and
> return my coffee grinder, order 112-3344, because it arrived damaged. You may
> share my name, Alan Weismann, and that order number.

The agent builds the plan, calls `phone_call_start`, polls `phone_call_wait`,
and judges the result with `phone_call_review`.

## Claude Code

`install` writes `.mcp.json` at the repository root (`mcpServers.phone`) and the
same `.claude/skills/phone-call/SKILL.md`. Start `claude` in this folder and
approve the project MCP server when prompted. The tool names are identical.

## Codex

The plugin under `plugins/talk2myagent` is registered in the personal
marketplace by `install` (it runs `codex plugin add talk2myagent@personal`).
Start a new Codex task; the `phone-call` skill and `phone_*` tools load
automatically. The plugin's `.mcp.json` points at this checkout.

## Terminal only (no agent credits)

The local model can draft the plan and run the whole call:

```sh
uv run t2ma plan --task "Return my coffee grinder because it arrived damaged" \
  --company "Amazon US" --number +18882804331 --source "amazon.com/contact-us" \
  --name "Alan Weismann" --fact order_id=112-3344 --fact item="coffee grinder" \
  --out .runtime/plan.json
uv run t2ma call --plan @.runtime/plan.json        # asks for confirmation, then dials
uv run t2ma call --plan @.runtime/plan.json --demo # same pipeline, no dialing
uv run t2ma demo --play                            # built-in rehearsal, audible
```

`t2ma call` prints each transcript line as it happens and the response latency
per turn. Ctrl-C hangs up and restores the audio devices. Review the printed
result and `report.html` yourself; in this mode nobody judges the success
criteria for you.

## Tool summary

| Tool | Purpose |
|---|---|
| `phone_doctor` | Devices, models, Accessibility, live-call readiness |
| `phone_plan_draft` | Local-model draft of a `CallPlan` from a task |
| `phone_call_start` | Route audio, dial, converse, hang up (returns immediately) |
| `phone_call_wait` | Stream events; `done` carries the result |
| `phone_call_status` / `phone_hangup` / `phone_result` | Progress, early stop, recovery |
| `phone_call_review` | Host judges success criteria with recipient evidence |
| `phone_test_*` | Developer-as-recipient role-play through the Mac microphone |
| `phone_prepare` … `phone_finish` | Manual, slower, hand-dialed debugging flow |

All hosts share `config.local.json` (copy `config.example.json`) for devices,
models, endpointing, and the conversation model.
