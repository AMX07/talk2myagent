# The four models, and how to swap the one that matters

Nothing here is a single "voice model". Four specialists run locally, and only
one of them decides anything:

| Role | Default | Runtime | Swappable by |
|---|---|---|---|
| Hears the other side | `mlx-community/whisper-large-v3-turbo` | MLX | `stt_model` |
| Decides every word | `lmstudio-community/gemma-4-26B-A4B-it-QAT-MLX-4bit` | mlx-lm | `conversation_model` |
| Speaks | Kokoro 82M, voice `af_heart` | ONNX Runtime | `voice` |
| Detects end of turn | Silero VAD | ONNX Runtime | `vad_backend` |

Kokoro has no language ability whatsoever. It is handed an exact string and
pronounces it, so changing the voice changes only how the agent sounds. The
conversation model is the one that chooses what to say, when to press a keypad
digit, whether recording consent was given, and when to stop and ask you.

## Swapping the conversation model

Put the model in `config.local.json` (copy `config.example.json`):

```json
{ "conversation_model": "lmstudio-community/gemma-4-26B-A4B-it-QAT-MLX-4bit" }
```

The value is either a Hugging Face repo id or a path to a local folder, so a
model LM Studio already downloaded can be used in place:

```json
{ "conversation_model": "/Users/you/.lmstudio/models/mlx-community/gemma-4-e4b-it-8bit" }
```

Download a repo id ahead of time with `uv run t2ma conversation-model`, then
restart the background service so it picks up the new setting:

```bash
kill -TERM $(lsof -U | rg 'talk2myagent/.runtime/service.sock' | awk '{print $2}' | head -1)
```

Measure before committing to it. This runs the same four call turns, including
the ones that exposed role confusion in a live role-play, through any set of
models and prints both the timings and the replies:

```bash
uv run python scripts/compare_models.py Qwen/Qwen3-8B-MLX-4bit lmstudio-community/gemma-4-26B-A4B-it-QAT-MLX-4bit
```

The number that matters is **time to first sentence**, not total generation.
The agent starts speaking as soon as the first sentence is parseable, so the
rest is generated while it talks.

## What a replacement has to satisfy

- **MLX format.** The runtime is `mlx-lm`, which loads MLX safetensors, not
  GGUF. LM Studio's GGUF downloads cannot be used directly.
- **A supported architecture.** Check for a matching file under
  `.venv/lib/python3.12/site-packages/mlx_lm/models/`. Gemma 4 is supported
  (`gemma4.py`); a brand new architecture such as Muse Glimmer is not.
- **No visible thinking.** Each turn must begin emitting `{"ack": …, "say": …`
  immediately, because the sentence streamer starts speech from the first
  complete sentence. A model that writes a reasoning preamble first spends the
  entire latency budget before saying anything. `enable_thinking=False` is
  passed to any chat template that accepts it; a model that thinks regardless
  is a poor fit no matter how good its answers are.
- **Reliable small JSON.** The reply is a strict object. A model that wraps it
  in prose or markdown fences costs a retry.

## Choosing a size

Bigger is not automatically better here, because the call is waiting. A
mixture-of-experts model is usually the best trade: `gemma-4-26B-A4B` holds 26B
parameters but activates about 4B per token, so it decodes at small-model speed
with large-model knowledge.

Weights all sit in unified memory, so a 128 GB Mac fits any of these. The cost
of a larger model is latency, not capacity.

## GGUF models, including Muse Glimmer

For anything mlx-lm cannot load, the route is LM Studio's OpenAI-compatible
server on port 1234. The generation path already streams token by token, so a
backend implementing `ready`, `prepare`, `respond` and `persona_reply` against
that API would drop in, and the sentence streamer needs no changes because it
only consumes growing text. The explicit prompt-prefix cache would be lost,
though LM Studio does its own prompt caching. This is not implemented.


## Measured on identical call turns

Three models through the same six turns from `examples/roleplay-amazon-return.json`,
including two that only memory can answer. Run it yourself:

```sh
uv run python scripts/compare_models.py \
  Qwen/Qwen3-8B-MLX-4bit \
  lmstudio-community/gemma-4-26B-A4B-it-QAT-MLX-4bit \
  lmstudio-community/gemma-4-31B-it-MLX-4bit
```

| Model | First sentence | Prefill per call | Invented details |
|---|---|---|---|
| Qwen3 8B | 0.29 s | 0.57 s | 0 |
| **Gemma 4 26B A4B (MoE)** | **0.43 s** | **0.54 s** | 0 |
| Gemma 4 31B | 1.18 s | 2.68 s | 0 |

First sentence is what the caller waits on. The agent starts speaking as soon as
one sentence can be parsed out of the streaming JSON and generates the rest
while its own voice plays, so total generation time is not what you feel.

The mixture-of-experts model is the default because it is the only one that is
both fast and sound. It holds 26B parameters but activates about 4B per token,
which is why it costs roughly Qwen's latency while behaving like the 31B.

Speed is not what separates them. On the same turns:

- **Identity.** Qwen has opened with "This is Ansh", impersonating the customer.
  Both Gemma models say they are calling on behalf of him.
- **Reciting the plan.** Qwen read the success criteria aloud to the
  representative as its closing line. Neither Gemma model does this.
- **Knowing when it is done.** Qwen declared the call resolved before getting a
  confirmation number. Both Gemma models stayed open and asked for the return
  instructions, the deadline and a confirmation number.
- **Using what memory returned.** All three requested the lookup. The MoE model
  answered with "The customer's records show it was delivered on 3 September
  2026", which is the behaviour the whole feature exists for.

Prompting did not fix the first two. Explicit prohibitions against reciting the
plan and against claiming to be the customer were added, and the 8B model
violated both again identically. Some failures are capability, not instruction.
