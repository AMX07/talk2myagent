# Run 1 — the first real dial

A real call, placed through the macOS Phone app to the developer's own iPhone as a self-test. Because this Mac relays calls through that same iPhone, the carrier dropped it after about three and a half seconds. That was the expected outcome and it still exercised every step up to the conversation.

| field | value |
|---|---|
| run id | `20260911-231023-3133f866` |
| mode | `live` |
| outcome | `failed` (source: `controller_reported`) |
| spoken turns measured | 0 |

## Transcript

Times are seconds from the start of the session. Nothing here is edited except the dialed phone number, replaced with a placeholder for publication.

`   0.0s` &nbsp; _system_ &nbsp; Plan prepared; no phone call placed.

`   6.8s` &nbsp; _system_ &nbsp; Models ready for the call.

`   6.9s` &nbsp; _system_ &nbsp; Audio routed: Phone output -> BlackHole 16ch, Phone microphone -> BlackHole 2ch.

`  12.4s` &nbsp; _system_ &nbsp; Dialed +1 (555) 010-0123 through the Phone app.

`  15.9s` &nbsp; _system_ &nbsp; Call stopped: Call did not connect (call_ended).

`  15.9s` &nbsp; _system_ &nbsp; Session finished. Call stopped: Call did not connect (call_ended).

`  16.0s` &nbsp; _system_ &nbsp; Audio devices restored.


## What this run shows

- The keypad dial path works against the real Phone app: the keypad opened, the number
  was typed and read back, Call was pressed, and the app reported an active call.
- Audio routed itself (Phone output to BlackHole 16ch, microphone to BlackHole 2ch) and
  restored afterwards. The Mac's own system defaults were never touched.
- The drop was handled by failing closed: outcome `failed`, no invented conversation,
  devices restored, nothing left hanging.
- Not shown, because the line never connected: a sustained conversation, the in-call
  window's button labels, live two-way audio, and hangup.
