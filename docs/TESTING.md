# Human voice playground

This is a sandbox integration test: a real developer replaces the telephone
recipient, while the same Codex agent, speech models, recording, and transcript
pipeline are used. It accepts an arbitrary task supplied at test-time. No default
Amazon script or simulated recipient replies are used in this mode.

## Use it in Codex

After installing the updated plugin, open a new Codex task in this project:

1. Say **Enter test mode**. The agent checks the Mac's audio devices and waits.
   The microphone is still closed.
2. Describe what you want the caller to accomplish, with relevant facts and
   constraints. This instruction is the task owner's request, not recipient speech.
3. Codex shows the plan, announces the role switch, and starts. You now play the
   person receiving the call. The agent greets you, explains its purpose, answers
   your questions, and continues using your actual spoken replies.
4. Once the outcome is established, the agent closes the conversation and returns
   the result to you as the task owner. Results include criterion checks with
   transcript evidence, the full available transcript, and the recording.

If you want to inspect or edit a plan before any audio starts, include **prepare
only; wait for me to start**. Otherwise, supplying the task in test mode starts
the interaction automatically after planning.

The UI is the Codex conversation. The local service is the audio transport; it
does not contain a second reasoning agent. Keep the Codex task running throughout
the conversation. ChatGPT's built-in Voice mode is not needed and should be off
to avoid two listeners/responders competing for the conversation.

## Audio and control

- **Speakers (default):** Mac microphone and speakers. Mic input is suppressed
  during the agent's playback and for 450 ms after it ends. Reply after it stops
  speaking. This prevents self-transcription without a driver or echo canceller,
  but does not support interrupting by voice while the agent is speaking.
- **Headphones:** tell Codex you are wearing headphones and identify the audio
  devices if needed. Microphone remains live during playback and 300 ms of
  recipient speech requests an interruption. Noise can trigger the basic VAD.
- If macOS requests microphone access for the local process, allow it to run the
  test. No iPhone, BlackHole, phone number, or administrator installation is needed.
- Recording is on by default for an explicitly started test. Say **do not retain
  audio** before starting to use `record: false`. Recognized text and generated
  agent speech are still saved for the transcript, as in the existing call tools.

Say **stop test**, **end test**, or **exit test** on its own to exit. This command
is detected by the local speech pipeline. In speakers mode, say it while the
agent is listening. For an immediate keyboard-based stop, including during
playback, type **stop test** in Codex or run:

```sh
cd talk2myagent
uv run t2ma test-stop
```

`uv run t2ma test` prints the available devices and entry instructions. It does
not independently interpret a task or launch another AI; enter the task in Codex.

## Lifecycle and tool contract

| Phase | Tool | Behavior |
|---|---|---|
| Awaiting task | `phone_test_mode` | Device inventory; no microphone or task chosen |
| Ready | `phone_test_prepare` | Saves developer request, recipient role, facts, dialogue, success criteria |
| Talking | `phone_test_start` | Warms models, opens physical audio, starts requested recording, speaks greeting |
| Conversation | `phone_listen` / `phone_say` | Same tools as telephone mode; Codex decides each reply |
| Results | `phone_test_finish` | Closes audio, evaluates criteria, returns evidence/transcript/recording |
| Abort | `phone_test_stop` | Stops playback and microphone; preserves partial results |

The engine rejects dialing, telephone keypad/tones, ordinary Phone connection,
and synthetic-recipient injection for role-play sessions. Only one hardware audio
session can run at once, whether role-play or telephone. Test scenarios are stored
as demo plans so they cannot accidentally be reused as live call plans.

For a successful test, each criterion must be evaluated exactly once as `met`,
with at least one actual recipient event sequence ID. `not_met` and `unknown`
remain valid outcomes but cannot be reported as `completed`. The criterion judge
is the same Codex agent, not an independent evaluator; the engine verifies the
presence/provenance of evidence, not its semantic truth.

Artifacts are stored in `runs/<call-id>/`, outside Git. Results explicitly mark
`test_mode: true` and `real_world_actions: false`. Ending a role-play closes its
microphone and never requires a Phone hangup. The existing inactivity and
maximum-duration watchdog also covers this mode.

## Verification

`uv run pytest -q` covers arbitrary tasks, no mic before start, first greeting,
device routing, role-play/telephone isolation, optional recording, headphone
behavior, evidence checks, duplicate start, voice-stop, and partial results.
The existing scripted speech regression and MCP checks remain available.

These tests do not establish natural conversation quality or completion of a
developer's yet-to-be-supplied task. Run that test interactively in Codex.
