# Stack: where each platform attaches

talk2myagent is deliberately small and local. The interesting question is not
whether these platforms could be bolted on, but **exactly which seam each one
attaches to**, and whether it can attach without breaking the thing that makes
the demo work.

Everything below names a real function in this repository and a real API from
the platform's own documentation. Status is stated plainly: none of the five
data and orchestration integrations are wired up yet, and the document says so
rather than implying otherwise.

| Platform | Fit | Attaches to | Status |
|---|---|---|---|
| [RocketRide](https://docs.rocketride.org/) | High | `phone_*` MCP tools as pipeline nodes | Designed, not wired |
| [Cognee](https://github.com/topoteretes/cognee) | High | `Engine._persist_result` and `plan_from_task` | Designed, not wired |
| [HydraDB](https://docs.hydradb.com/) | High, overlaps Cognee | Same seam as Cognee; pick one | Designed, not wired |
| [Modiqo / Rote](https://www.modiqo.ai/faq) | Conditional | The pre-call and post-call tool path | Designed, not wired |
| [Hotdata](https://www.hotdata.dev/) | Conditional | `CallPlan.facts` and `conversation_review` | Designed, not wired |
| [Snyk](https://docs.snyk.io/scan-with-snyk/snyk-code) | Development tooling | CI, next to pytest and ruff | Designed, CLI not installed |

---

## The constraint that decides everything

A turn on a live call costs 1.03 s, measured in
[run 3](transcripts/03-human-roleplay.md):

| Stage | Time |
|---|---|
| Silence we wait for before believing they finished | 0.56 s |
| Whisper transcribing the turn | 0.13 s |
| Model deciding, then first audio leaving the speaker | 0.34 s |

There is no slack. A single network round trip inside that loop is 100–300 ms
against a 340 ms budget, and a knowledge-graph query is worse. So the rule is:

> **Nothing external goes inside the per-turn loop.** Every integration below
> attaches *before* the call, *around* it, or *after* it.

The one apparent exception, a mid-call decision, already pauses the
conversation on purpose: `Engine.ask_owner` (`talk2myagent/engine.py`)
speaks a holding line, so a slow lookup there costs nothing.

---

## RocketRide — orchestrating the call, not the turn

**What it is.** An open-source runtime for AI pipelines. Pipelines are portable
JSON executed by a multithreaded C++ core, built visually in VS Code, with a
Python and TypeScript SDK. It positions itself "one layer below your
application and your agent framework," and it both consumes MCP tools and
exposes pipelines as MCP tools.

**Where it attaches.** This project already speaks MCP. `uv run t2ma mcp`
(`talk2myagent/mcp_server.py`) exposes 27 tools, and the ones a pipeline
needs are `phone_plan_draft`, `phone_call_start`, `phone_call_wait`,
`phone_hangup` and `phone_call_review`. Registering that server makes the whole
calling capability a set of RocketRide nodes with no adapter code.

**The concrete pipeline.** What is currently prose in
`plugins/talk2myagent/skills/phone-call/SKILL.md` becomes version-controlled
JSON:

```text
[retrieve context] → [build CallPlan] → [phone_call_start authorized=true]
      → [poll phone_call_wait until done]
      → branch on result.review_required
           ├─ needs_user  → [notify the customer] → [phone_hangup]
           └─ otherwise   → [phone_call_review with evidence] → [file outcome]
```

**What it fixes.** Right now the sequence lives in a skill file that a host
agent reads and follows. It is not versioned as an artifact, not replayable,
and not observable. A JSON pipeline is all three.

**Caveat.** The models that produce each spoken turn stay local, because of the
budget above. RocketRide's provider fan-out is useful for the planning and
review nodes, not for the 0.34 s path.

---

## Cognee — what happened last time, and what was promised

**What it is.** An open-source memory platform for agents: an Extract → Cognify
→ Load pipeline that turns documents into a knowledge graph plus vector index,
with a small Python API — `remember()`, `recall()`, `improve()`, `forget()` —
and a `session_id` for short-lived context alongside the durable graph.

**Where it attaches.** Two seams, both already isolated.

1. **After every call.** `Engine._persist_result`
   (`talk2myagent/engine.py`) is the single place where a finished
   call is written to `runs/<id>/`. It is the natural hook:

   ```python
   await cognee.remember(
       json.dumps({
           "company": call.plan.company,
           "objective": call.plan.objective,
           "outcome": result["outcome"],
           "promised": result["summary"],
           "evidence": result["evaluation"],
           "transcript": result["transcript"],
       }),
       session_id=call.id,
   )
   await cognee.improve()      # fold this call into the durable graph
   ```

2. **Before the next one.** `LocalConversation.plan_from_task`
   (`talk2myagent/conversation.py`) currently drafts a plan from a task
   string and hand-typed facts. With memory it starts from history:

   ```python
   prior = await cognee.recall(
       f"previous contact with {company} about {facts['order_id']}"
   )
   ```

   Those recalled facts go into `CallPlan.facts`, and an unkept promise becomes
   a `stop_condition` or an `allowed_action` — which is how
   "follow up on the refund they promised last Tuesday" becomes a plan the
   agent can actually hold someone to.

**What it fixes.** Every `CallPlan` in this repo is written from scratch. The
agent has no idea it called the same company nine days ago. That is the single
largest gap between this demo and something you would use twice.

**Caveat.** Cognify uses an LLM to extract entities, so this runs between
calls, never during one.

---

## HydraDB — the same job, one hosted API

**What it is.** A unified context substrate for AI: semantic search, graph
reasoning and personalised ranking behind one API, with an explicit
`type: "knowledge" | "memory" | "all"` selector on search and an OpenAPI v2
spec for coding agents.

**Where it attaches.** The same two seams as Cognee. Its distinguishing feature
is the knowledge-versus-memory split, which maps cleanly onto the two kinds of
context a support call needs:

| HydraDB type | In this project |
|---|---|
| `knowledge` | Durable account facts: orders, the card a refund must go to, delivery dates |
| `memory` | Episodic call history: who promised what, on which call, and whether it happened |
| `all` | One pre-call query that populates `CallPlan.facts` and `stop_conditions` together |

**Honest position.** Cognee and HydraDB solve the same problem here and you
would pick one. The trade is that Cognee's defaults are file-based and
self-hosted, which matches this project's local-only stance, while HydraDB is a
hosted API you sign up for. If the requirement is "nothing leaves the machine",
Cognee wins by default; if the requirement is ranked retrieval across a lot of
accounts without running infrastructure, HydraDB does.

---

## Modiqo / Rote — crystallising the steps around the conversation

**What it is.** A method layer for tool-using agents. When an agent succeeds,
Rote records the tool calls, responses, ordering and data dependencies (not the
conversation), strips the failed paths, turns fixed values into declared
inputs, and packages the result as a versioned **Play** at a URI like
`https://play.modiqo.ai/owner/name@version`. Plays run local-first in the
runner's own environment; the registry stores versions rather than proxying
calls. A developer hands a Play URI to a supported harness such as Claude Code
or Codex.

**Where it attaches.** Precisely at the boundary this project already draws.
The repository separates the host agent (plans and judges) from the local
conversation worker (talks). Rote fits the first, never the second:

- **A pre-call Play.** Verify the support number on the company's official
  contact page, pull the order, assemble the `CallPlan`, check the facts are
  present. Today this is a human or a host agent doing the same steps each
  time, and getting the number wrong is a real failure mode the code guards
  against with `phone_source`.
- **A post-call Play.** Take `result.json`, file the confirmation number, email
  the recap, update the record, set a reminder for the promised date.

Both already run inside Claude Code or Codex, which is exactly where Rote
expects to execute, and this project's MCP server is already loaded there
(`uv run t2ma install`).

**Why it stops at the conversation.** Rote's own definition of determinism is a
declared tool path with declared checks, not identical results. A support call
is the opposite: the reply depends on words nobody can predict, which is why
`ConversationWorker` (`talk2myagent/conversation.py`) regenerates every
turn. Trying to crystallise the dialogue would break it; crystallising the
scaffolding around it is free.

---

## Hotdata — the evidence the agent is missing

**What it is.** An ultra-high-concurrency execution layer for agent workloads,
built on Rust, Apache DataFusion and Arrow. Each session is isolated and
read-only, work is async by default, and one query can mix SQL, vector search,
full-text and cross-source joins. It accepts PostgreSQL, DuckDB and Snowflake
dialects as well as its own HotSQL, and holds flat latency under concurrency
where a warehouse degrades.

**Where it attaches.** Three places, in descending order of how much they
matter to this demo.

1. **Proving a claim before the call.** "You charged me twice" is only
   actionable with the two transaction IDs. A pre-call query puts them into
   `CallPlan.facts`, and the fact guard then *permits* the agent to say them,
   because `unsupported_details`
   (`talk2myagent/conversation.py`) allows anything present in the
   plan. Without the query the agent cannot state the evidence at all.

2. **Catching a mis-heard number.** This is the most useful fit, because it
   closes a hole the audit of this demo actually found. In
   [run 2](transcripts/02-autonomous-rehearsal.md) Whisper rendered a spoken
   price of one hundred sixty-five dollars as "1.65, zero zero", and the agent
   repeated it as settled fact. The guard cannot catch that: the garbled figure
   *was* in the transcript, so it counted as something the other side said. A
   query against the real billing record at `Engine.conversation_review`
   (`talk2myagent/engine.py`) would contradict the figure and fail the
   criterion instead of confirming it.

3. **Choosing an approach from outcomes.** Every call already writes a
   structured `result.json` with an outcome, per-criterion evidence and
   per-turn latency. Loaded as a table, "which opening gets a refund approved
   most often" becomes a query.

**Honest caveat.** This is the weakest fit today and the table at the top says
so. There are 23 runs on this machine and no billing warehouse behind it. The
concurrency story that Hotdata is built for — thousands of agents exploring in
parallel — is not this demo's problem yet. Use 2 above is worth having at any
scale, though, because it turns a known correctness hole into a check.

---

## Snyk — scanning what this thing actually does

**What it is.** `snyk test` scans dependencies for known vulnerabilities and
returns a pass/fail exit code suitable for a CI gate; `snyk code test` is
static analysis of first-party code.

**Why this repository is worth scanning.** It is a small project with an
unusually broad surface for its size:

| Surface | Where | Why it matters |
|---|---|---|
| Local HTTP server | `talk2myagent/live.py` | Serves the call view and accepts POSTs that send guidance and end calls |
| Unix-socket RPC | `talk2myagent/service.py` | Dispatches 34 operations onto the engine |
| `subprocess` | 5 modules, including `macphone.py` | Launches `open` and `osascript` to drive the Phone app |
| `ctypes` FFI | `axapi.py`, `macphone.py` | Raw pointers into the Accessibility and CoreAudio APIs, with manual retain and release |
| Untrusted input | `conversation.py` | Transcribed speech from whoever answers the phone |
| 119 locked packages | `uv.lock` | Native wheels for MLX, ONNX Runtime, PortAudio |

**The gate.** Alongside the existing checks:

```sh
uv run pytest -q                              # 74 tests
uv run ruff check src tests scripts
snyk test --severity-threshold=high           # dependencies
snyk code test --severity-threshold=high      # first-party code
```

**Status and honesty.** The Snyk CLI is not installed on this machine, so no
scan has been run and no findings are claimed. And as the brief itself notes,
Snyk adds no calling capability; it is development tooling, and listing it as
anything else would be dishonest.

---

## Sources

- [RocketRide documentation](https://docs.rocketride.org/) and [rocketride-server](https://github.com/rocketride-org/rocketride-server)
- [Cognee on GitHub](https://github.com/topoteretes/cognee) and [how Cognee builds AI memory](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory)
- [HydraDB SDK documentation](https://docs.hydradb.com/)
- [Rote FAQ](https://www.modiqo.ai/faq) by [Modiqo](https://www.modiqo.ai/)
- [Hotdata](https://www.hotdata.dev/)
- [Snyk Code](https://docs.snyk.io/scan-with-snyk/snyk-code) and the [Snyk CLI test command](https://docs.snyk.io/developer-tools/snyk-cli/commands/test)
