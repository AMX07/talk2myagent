# talk2myagent

A local phone-call plugin for Codex on Apple Silicon. Codex writes the call plan,
uses the Mac Phone app to dial through your iPhone, and controls speech with local
tools. Whisper recognizes the other party; Kokoro speaks Codex's exact words.
Recordings and transcripts live on your Mac, independent of Apple's recording.

**Working local speech demo; live telephone integration awaits hardware setup and validation.**
The plugin is installed on this Mac. Return and cancellation rehearsals have
passed using actual local Kokoro synthesis and Whisper transcription. No real
Amazon order or account has been modified. Simulations are explicitly labeled.

```sh
cd /Users/anshmittal/Documents/talk2myagent
uv run t2ma demo --action return
```

See [setup and live calling](docs/SETUP.md), [platform analysis](docs/DECISIONS.md),
and [resume notes](PROGRESS.md). Start a new Codex task to load the installed
`phone-call` skill and its 13 MCP tools. The CLI rehearsal also runs without Codex credits.

Verified: 21 automated tests, real speech round-trip, both Amazon rehearsal paths,
and the MCP transport through the persistent local service. Speech generation of
a 3.2-second utterance took 1.6 seconds and its transcription took 1.8 seconds on
this Mac (one cold-run measurement, not a sustained latency benchmark).

## Architecture decision

Use a Codex plugin + MCP server + small Python audio service. No cloud telephony
provider, separate OpenAI API key, or full native app is required for this design.
The Mac and iPhone must already support cellular calling. Two separate virtual
audio buses are needed for clean input/output routing.

Codex is the single decision-making agent. Each `listen` tool returns the remote
party's words, and each `say` tool speaks text chosen by that same Codex task.
`finish` returns the accumulated transcript and artifact paths. A blocking tool
that conducts an unpredictable entire call cannot ask its caller for every reply;
it would need an internal dialogue model or a fixed script. This demo supports
host-driven conversation and an explicitly simulated scripted smoke test.

Current official Voice docs describe the user-facing voice conversation but do
not document a phone-audio injection/export API for plugins. Routing built-in
Voice through virtual devices might be an experiment, but it is not the supported
integration this project depends on. Cloud Realtime APIs are a separate product.

## Sources checked September 11, 2026

- [ChatGPT Voice](https://learn.chatgpt.com/docs/features/voice)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp)
- [Apple calling from Mac](https://support.apple.com/en-us/102405)
- [BlackHole audio routing](https://github.com/ExistentialAudio/BlackHole)
- [Whisper on MLX](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
- [Kokoro ONNX](https://github.com/thewh1teagle/kokoro-onnx)

This minimal demo deliberately postpones the earlier hackathon sponsor integrations.
