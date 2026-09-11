# Can this live inside ChatGPT / Codex?

Yes, as a local Codex plugin with a skill, MCP tools, and a local audio process.
A separate full macOS app is not needed for the first demo. The local process is
necessary because instructions alone cannot acquire audio devices, run models,
or record continuously between tool calls. Codex supports local stdio MCP
servers and plugin-provided tools. This project successfully exercised that
transport and installed the plugin. [Official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)

## Built-in Voice

Current ChatGPT Voice documentation describes live user conversations, task
coordination, interruption, and voice in Codex tasks. It does not establish a
plugin API for connecting a Phone call as its audio source or exporting a
complete independently recorded phone conversation. That is a documentation
boundary, not proof that a manual audio-routing experiment is impossible.
[Official Voice documentation](https://learn.chatgpt.com/docs/features/voice)

A manual experiment could route the remote party into ChatGPT's selected mic and
ChatGPT's output into Phone's selected mic. It would still need two independent
audio routes, manual voice-session control, independent recording, and validation
that the conversation treats the support representative as the remote party
rather than the task's owner. There is no verified supported end-to-end path for
that experiment in this build. A paid Realtime API would be a separate integration,
not a way to programmatically reuse a ChatGPT subscription's built-in voice.

## What runs locally

| Component | Choice | Why |
|---|---|---|
| Intent, plan, replies, summary | This Codex task | Preserves one decision-making agent and task context |
| Telephone network | iPhone cellular calling via Mac Phone | Uses the user's existing number/carrier |
| UI controls | Codex computer use | Dial, inspect connection, navigate keypad, hang up |
| Speech recognition | Whisper small.en through MLX | Local Apple Silicon inference; no speech API charge |
| Speech synthesis | Kokoro 82M through ONNX Runtime | Small open model; speaks the exact text selected by Codex |
| Audio routing | Two BlackHole virtual devices | Separate incoming and outgoing sound |
| Tool interface | Python MCP SDK over stdio | Fits a local plugin |
| Continuous recording | Python sounddevice/soundfile process | Survives individual tool calls and writes independent WAV files |
| Persistence | JSON/JSONL + WAV | Easy to inspect and resume without a hosted database |

Kokoro's model uses Apache 2.0; its ONNX wrapper is MIT. MLX Whisper is provided
in Apple's MLX examples. These models recognize and speak; they do not themselves
decide what to negotiate. [Kokoro upstream](https://github.com/thewh1teagle/kokoro-onnx),
[MLX Whisper upstream](https://github.com/ml-explore/mlx-examples/tree/main/whisper)

Apple's calling feature requires an appropriately configured iPhone/Mac pairing.
BlackHole provides app-to-app loopback audio; having a device installed does not
prove Phone is using it. This host's actual Phone routing must be tested.
[Apple setup](https://support.apple.com/en-us/102405),
[BlackHole upstream](https://github.com/ExistentialAudio/BlackHole)

## The single-agent nuance

The strict interpretation of your workflow is implemented:

```text
User request → Codex writes exact plan
             → computer use dials and verifies connection
             → local tool listens → Codex decides → local tool speaks
             → repeat for IVR / support conversation
             → computer use hangs up
             → finish tool returns transcript + recording
             → same Codex task answers the user
```

There are multiple speech models, but only one agent choosing actions. No
subagent, second chat, or hidden dialogue LLM is launched.

One blocking `make_call(plan)` tool that returns only after a free-form call ends
cannot rely on the suspended caller for every reply. It would need an internal
LLM or state-machine script. An internal LLM could be treated as the voice
execution stage of one product agent, but it would introduce another inference
context, duplicated constraints, and handoff rules. We avoid that for now by
using short tool turns and one final transcript-returning tool.

The tradeoff is latency and Codex allowance use. The one measured cold local
round-trip was 1.6 seconds for synthesis and 1.8 seconds for transcription,
excluding speaking time, endpoint detection, Codex reasoning, and tool transport.
This is suitable for an MVP experiment, not evidence of natural human-speed
conversation. The recorder stays active while Codex reasons. Basic energy-based
barge-in is implemented but has not been verified on a telephone call.

## ChatGPT app and standalone alternatives

- **Codex desktop plugin:** built and installed; best fit for local device access.
- **ChatGPT web app:** a cloud-hosted MCP server cannot directly open this Mac's
  audio devices. It would need an authenticated bridge to a companion process.
  That is extra infrastructure and is not implemented here.
- **Standalone macOS app:** reuse this Python service and put a small native UI
  around it. If eliminating Codex credits is required, add one local instruction
  model as the sole dialogue controller. That mode is not implemented. The current
  CLI rehearsal is deterministic and works without Codex credits.

## What is proven versus remaining

The return and cancel examples pass through actual speech synthesis and ASR and
export a transcript, two-sided recording, and HTML report. The MCP integration
also passes end to end. The representative is synthetic in those tests.

An actual Amazon call remains unverified. Driver installation requires local
administrator interaction. The demo still needs live routing, real user-supplied
order details, the verified support destination, and the account holder for any
required identity checks. Confirmation, refunds, fees, and deadlines must come
from the actual representative. Support refusing an AI caller is a user-handoff
outcome, not something the software can promise to bypass.

The earlier hackathon sponsor stack is intentionally deferred for this requested
minimal build. The code does not claim to satisfy those sponsor requirements.
