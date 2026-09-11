# Setup and demo

All source lives in this repository. `.venv`, `models`, `.runtime`, and `runs` are
local and excluded from Git. Whisper uses the standard Hugging Face model cache.
No cloud speech API or API key is required. Codex reasoning still consumes your
normal Codex allowance; speech inference does not.

## Run the rehearsal now

```sh
cd /Users/anshmittal/Documents/talk2myagent
./scripts/setup.sh
uv run t2ma demo --action return
# or: uv run t2ma demo --action cancel
```

The console prints recognized support speech and paths to `report.html`,
`recording.wav`, `transcript.json`, and `result.json`. Open the report to play the
recording. It uses fictional facts and never places a call. Support audio is
generated with a second Kokoro voice and passed through Whisper; the responses
are a fixed test script, not evidence of autonomous negotiation.

Once models are downloaded, `HF_HUB_OFFLINE=1 uv run t2ma demo` works offline.
For machine-readable tool calls without installing the plugin:

```sh
uv run t2ma request doctor
uv run t2ma request prepare --json @examples/demo-request.json
uv run t2ma request listen --json '{"call_id":"ID_FROM_PREPARE","after_seq":0,"timeout_seconds":15}'
uv run t2ma request result --json '{"call_id":"ID_FROM_PREPARE"}'
```

`request` starts a persistent service on a private Unix socket. No TCP port is
opened. This keeps continuous capture alive between MCP or CLI requests.

## Install the plugin on this Mac

```sh
uv run python scripts/install_plugin.py
```

The installer generates `.mcp.json` with absolute paths to this checkout and
creates a symlink from the personal marketplace to the plugin source. Start a
new Codex task so it picks up the tools. Keep this checkout in place. Rerun the
installer after moving it. Plugin code is local; the Share link alone does not
package the Python service or model weights for another person's Mac.

If the personal marketplace entry does not exist on a fresh machine, first run
the bundled plugin-creator scaffold (with `--with-marketplace`) into a temporary
location, then point the generated `plugins/talk2myagent` symlink at this repo's
plugin and run the installer. On the development Mac the entry already exists.

## Set up live audio

The iPhone and Mac must have cellular calling configured through the same Apple
Account. See [Apple's setup](https://support.apple.com/en-us/102405).

1. Run in your Terminal: `brew install --cask blackhole-2ch blackhole-16ch`.
   Installation needs your administrator password. Homebrew advises a reboot.
   Do not enter your password into the chat.
2. Verify both buses with `uv run t2ma doctor` and `uv run t2ma loopback-test`.
   Restart the local service if it was started before installing devices.
3. In Phone's **Audio > Microphone** menu, select **BlackHole 2ch**. This receives
   the agent's outgoing voice.
4. Route Phone's speaker output to **BlackHole 16ch**. If Phone offers no output
   selector, choose BlackHole 16ch as macOS's system output. An Audio MIDI Setup
   Multi-Output device containing BlackHole 16ch and headphones lets you monitor
   the conversation. Never put BlackHole 2ch into this Multi-Output device.
5. Keep both buses at 48 kHz. Mute other audio-producing apps while the system
   output is routed into the call recorder; it will otherwise capture them too.
6. Permit microphone/audio capture to the Python/terminal process if macOS asks.
   Validate bidirectional routing with a consenting test recipient. Loopback-test
   proves the buses work; it does not prove Phone uses them.
7. After the call, restore system output to your usual speakers/headphones and
   Phone's microphone to its previous setting.

The service never changes system devices silently. Custom bus names can be set
in `config.local.json`, using `config.example.json` as a starting point. Audio
device names must match exactly. Two separate buses are required to avoid echo.

## Conduct an Amazon call

In a new Codex task with this plugin, give the exact item/order, return or cancel
request, reason, and what information may be shared. Supply or verify the support
number using Amazon's official support flow. The sample number in demo plans is
fictional and blocked from live use.

Suggested request:

> Use phone-call to help return my [item], order [number], because [reason].
> You may disclose my name and this order information to Amazon US support at
> [verified number]. Show the exact call plan. Request a fee-free return to the
> original payment method; involve me for verification or different terms.

Codex prepares the plan, uses computer use to dial, observes connection, starts
the local audio session, and alternates speech/listening tools. Recording starts
only after consent. It handles phone menu digits with computer use. It hangs up
using Phone's UI and calls `phone_finish`, which returns the transcript to Codex.

**A real return/cancellation has not been validated by the rehearsal.** Amazon
may require the account holder, authentication, a different support route, or
an online action. A support IVR can change; this code has no hardcoded Amazon menu.

## Recover if credits or the process stop

Call events are appended to `runs/<call-id>/events.jsonl`. Plans live in
`session.json`. The recorder flushes remote audio continuously. `result` returns
saved output after the service restarts. A hard kill can leave the WAV header
incomplete even though raw samples have been written; graceful shutdown is best.

The watchdog stops live audio after 120 seconds without a tool request or 15
minutes total (configurable). **It cannot hang up the iPhone call. End the Phone
call yourself if the agent stops.** Remote disconnect also needs UI observation;
silence is not reliable evidence of hangup. The terminal rehearsal remains usable
without Codex credits, but free-form reasoning requires an active Codex task.

The service log is `.runtime/service.log`. To stop it gracefully, find its PID
with `lsof .runtime/service.sock` and use `kill -TERM <PID>`. Do not kill unrelated
Python processes. Never commit private transcripts or order details.
