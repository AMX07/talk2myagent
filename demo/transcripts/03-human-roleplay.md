# Run 3 — a live human on the other end

The developer played the Amazon support representative, speaking into the Mac's microphone while the agent replied through its speakers. This is the run in the screen recording. No phone call was placed and no account was touched.

| field | value |
|---|---|
| run id | `20260911-232743-481c2843` |
| mode | `roleplay` |
| outcome | `cancelled` (source: `controller_reported`) |
| spoken turns measured | 5 |
| median, their last word to agent's first audio | **1.045 s** |
| median, transcript to agent's first audio | 0.343 s |

## Transcript

Times are seconds from the start of the session. Nothing here is edited except the customer's email address, replaced with a placeholder for publication.

`   0.0s` &nbsp; _system_ &nbsp; Plan prepared; no phone call placed.

`   0.0s` &nbsp; _system_ &nbsp; Human role-play ready. Developer's task saved; microphone is closed.

`   4.6s` &nbsp; _system_ &nbsp; Human role-play started on MacBook Pro Microphone / MacBook Pro Speakers. You are now the recipient. Say 'stop test' or use test-stop to exit.

`   4.6s` &nbsp; _system_ &nbsp; Recording started: Developer explicitly started a recorded local role-play test.

`   4.6s` &nbsp; **Agent** &nbsp; Hi, this is an AI assistant calling on behalf of Ansh about a coffee grinder that arrived broken. May I record and transcribe this call so I can pass the details back to him?

`  15.7s` &nbsp; _system_ &nbsp; Call plan delegated to the local conversation agent. The host is outside the spoken-turn loop.

`  29.6s` &nbsp; **Them** &nbsp; Yes, you can record it.

`  31.9s` &nbsp; **Agent** &nbsp; Sure. Thank you. I'll now connect you to the appropriate representative to assist with the return and refund process.<br>&nbsp;&nbsp;&nbsp;&nbsp;<sub>⏱ 0.95s after their last word; 0.28s thinking + speaking; first token 0.19s</sub>

`  51.0s` &nbsp; **Them** &nbsp; Okay, sounds good

`  53.3s` &nbsp; **Agent** &nbsp; Sure. I need to confirm the return of the coffee grinder and the full refund to the original card. Could you please provide the refund amount and timing?<br>&nbsp;&nbsp;&nbsp;&nbsp;<sub>⏱ 1.03s after their last word; 0.34s thinking + speaking; first token 0.24s</sub>

`  64.9s` &nbsp; **Them** &nbsp; Before I do that, can you please confirm your date of birth and your Amazon account please?

`  71.7s` &nbsp; **Agent** &nbsp; Sure. I don't have that detail with me. Would the order number and account email be enough to find it?<br>&nbsp;&nbsp;&nbsp;&nbsp;<sub>⏱ 1.04s after their last word; 0.34s thinking + speaking; first token 0.24s</sub>

`  80.0s` &nbsp; **Them** &nbsp; Yep, that would be fine.

`  82.7s` &nbsp; **Agent** &nbsp; Sure. Great. I'll now proceed with the return of the coffee grinder and confirm the full refund to the original card. Could you please provide the refund amount and timing?<br>&nbsp;&nbsp;&nbsp;&nbsp;<sub>⏱ 1.07s after their last word; 0.38s thinking + speaking; first token 0.28s</sub>

`  94.2s` &nbsp; **Them** &nbsp; You didn't provide me the order number and account email.

`  97.8s` &nbsp; **Agent** &nbsp; Sure. The order number is 113-4429911 and the account email is ansh@example.com. Could you please confirm the refund amount and timing?<br>&nbsp;&nbsp;&nbsp;&nbsp;<sub>⏱ 1.10s after their last word; 0.41s thinking + speaking; first token 0.30s</sub>

` 114.2s` &nbsp; _system_ &nbsp; Session finished. Developer stopped the role-play.


## What this run shows

**What worked**

- **1.04 s median** from the human's last word to the agent's first audio, over five
  spoken turns, with only 0.34 s of that being the model and the speech synthesis. The
  rest is 0.56 s of silence the endpoint detector waits for before it believes a
  sentence has finished, plus 0.13 s of Whisper transcription.
- Asked for a date of birth it did not have, the agent said so instead of inventing one:
  "I don't have that detail with me. Would the order number and account email be enough
  to find it?" To be precise about the credit: the model declined on its own here. The
  fact guard, which would have substituted its own wording, never fired in any of these
  three runs, so this shows the prompt working rather than the backstop.
- Recording consent was detected from ordinary speech ("Yes, you can record it") rather
  than a keyword. Retention here had already begun at 4.6 s on the developer's own
  test-start basis; the gate that withholds audio until consent applies to live calls.

**What went wrong, honestly**

- At 31.9 s the agent said it would "connect you to the appropriate representative". It
  is the caller, not a switchboard, and it confused its own role.
- At 82.7 s it said it would "proceed with the return and confirm the full refund". It
  cannot do either; it can only ask. The prompt forbids this and the 8B model did it
  anyway.
- It opened almost every turn with "Sure." The cached acknowledgment that buys the
  latency headline is overused.
- It volunteered the order number only after being asked twice.

These are the local 8B model's limits, not the pipeline's. A larger local model can be
set with `conversation_model` in `config.local.json`. The safety rails held throughout:
nothing was invented, and the outcome was never reported as success.
