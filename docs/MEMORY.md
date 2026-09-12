# Memory: answering what the plan does not carry

A call plan holds only what someone typed into it. When the other side asks for
a delivery date, an account email, or what was agreed last time, a plan-only
agent has to say it does not know. Memory is where that answer comes from
instead.

## What happens on the call

1. They ask for something the plan's `facts` do not contain.
2. The agent says a short holding line and names what it needs, for example
   `lookup: "delivery date for the coffee grinder"`.
3. The store is searched locally, in well under a millisecond.
4. The result enters the conversation as a `memory_result` message, and the
   agent answers from it on its next turn, without waiting for them to speak
   again.
5. If the store has nothing, the agent is told exactly that and says it does
   not have the detail. It gets one lookup per question, so a miss cannot loop.

The lookup shows up in the transcript and in the live view as its own line, so
you can always see what was asked and what came back.

Smaller models sometimes say "let me check that" and forget to name what they
are checking. When that happens the recipient's own question is used as the
query, rather than leaving them waiting on a promise the agent never kept.

## What the agent may then say

The fact guard blocks any email, number or date that appears in neither the
plan nor the transcript. A retrieved record counts as part of the transcript,
so a date that came from memory is speakable and an invented one still is not.

## How retrieval works

BM25 over the stored sentences, entirely local. Three things make it usable for
a voice call rather than a search box:

- **Stop words are dropped.** On a small store, function words look
  statistically rare and would otherwise let "which card was it paid on" match
  a delivery date on the strength of the word "on".
- **Words are stemmed**, so "delivery", "delivered" and "deliver" agree.
- **A short alias list** covers the handful of equivalences a support call
  turns on, because lexical matching cannot otherwise connect "card" to "Visa".
  Aliases expand the query only; nothing is rewritten on the way in.

A record is returned only if it covers at least half of what was asked. An
absolute score threshold does not work here, because BM25's inverse document
frequency collapses when a term appears in most of a small store: a perfect
one-record match can score below a weak match in a large one. Coverage is
stable at every size, and it is what stops an unrelated row being served as an
answer.

## What goes in, and what never does

Written automatically:

- **Plan facts**, when a real call or role-play is prepared.
- **Call outcomes**, when one finishes, plus each criterion the other side
  actually confirmed, stored as a promise you can follow up on.

Demo plans never write to memory, so rehearsal fictions cannot leak into a real
call.

Written by you:

```sh
uv run t2ma request memory_add --json '{"text": "The Amazon account email is alex@example.com."}'
uv run t2ma request memory_search --json '{"query": "account email"}'
```

Write one self-contained sentence naming the thing and its value. Retrieval is
lexical, so "The Amazon account email is alex@example.com." is found and
"email: alex@example.com" often is not.

In the live view, type the sentence and press **Remember**. It works whether or
not a call is running.

From Codex, OpenCode or Claude Code, the MCP server exposes `phone_memory_add`,
`phone_memory_search`, `phone_memory_recent` and `phone_memory_forget`. Search
before writing a `CallPlan`: past calls can fill facts the user would otherwise
have to repeat.

## Where it lives

`memory/records.jsonl`, one JSON object per line, mode 600, and listed in
`.gitignore`. It is the customer's own information and never enters the
repository. Nothing in this path reaches the network.

Remove a single record with `phone_memory_forget` or:

```sh
uv run t2ma request memory_forget --json '{"record_id": "..."}'
```

Turn the automatic learning off with `"memory_learn": false` in
`config.local.json`.
