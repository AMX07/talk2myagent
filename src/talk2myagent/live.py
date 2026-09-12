"""The live call view: real-time transcript and the controls for the call.

The service listens on a private Unix socket, which a browser cannot reach, so
this opens a small HTTP server bound to 127.0.0.1 on an ephemeral port for as
long as the viewer runs. It serves one page plus a JSON API that proxies four
engine operations: read the live state, send typed guidance, answer a decision
the agent is holding the line for, and end the call.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .client import request

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>talk2myagent · live call</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --stroke: rgba(255,255,255,.18);
    --text: #f7f9fb;
    --dim: rgba(240,246,252,.66);
    --faint: rgba(240,246,252,.42);
    --them: rgba(255,255,255,.17);
    --agent: rgba(86,178,255,.34);
  }
  html, body { height: 100%; }
  body {
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    color: var(--text); background: #061018;
    display: grid; place-items: center; overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }
  .sky { position: fixed; inset: -25%; z-index: 0; filter: blur(80px); opacity: .9; }
  .blob { position: absolute; border-radius: 50%; mix-blend-mode: screen; }
  .b1 { width: 52vw; height: 52vw; left: 2%;  top: 4%;  background: #1f6feb; animation: d1 21s ease-in-out infinite; }
  .b2 { width: 44vw; height: 44vw; right: 0%; top: 30%; background: #12a594; animation: d2 25s ease-in-out infinite; }
  .b3 { width: 40vw; height: 40vw; left: 28%; bottom: 0%; background: #6d4aff; animation: d3 29s ease-in-out infinite; }
  @keyframes d1 { 50% { transform: translate3d(5vw,6vh,0) scale(1.12) } }
  @keyframes d2 { 50% { transform: translate3d(-6vw,4vh,0) scale(1.09) } }
  @keyframes d3 { 50% { transform: translate3d(4vw,-5vh,0) scale(1.13) } }

  .phone {
    position: relative; z-index: 1;
    width: min(95vw, 480px); height: min(94vh, 860px);
    display: flex; flex-direction: column;
    padding: 22px 18px 18px; border-radius: 40px;
    background: rgba(255,255,255,.10);
    border: 1px solid var(--stroke);
    backdrop-filter: blur(46px) saturate(180%);
    -webkit-backdrop-filter: blur(46px) saturate(180%);
    box-shadow: 0 34px 90px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.22);
  }

  header { flex: none; text-align: center; padding: 4px 0 2px; }
  .tag { font-size: 12.5px; color: var(--faint); display: inline-flex; align-items: center; gap: 6px; }
  .tag b { width: 7px; height: 7px; border-radius: 2px; background: var(--faint); }
  header h1 { font-size: 27px; font-weight: 600; letter-spacing: -.4px; margin-top: 3px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub { font-size: 13px; color: var(--dim); margin-top: 4px;
         display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

  .status { flex: none; display: flex; align-items: center; justify-content: center;
            gap: 8px; margin: 14px 0 2px; font-size: 12.5px; color: var(--dim); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #30d158;
         box-shadow: 0 0 0 0 rgba(48,209,88,.55); animation: pulse 1.9s infinite; }
  .dot.thinking { background: #ffd426 } .dot.speaking { background: #64b5ff }
  .dot.waiting  { background: #ff9f0a } .dot.off { background: rgba(255,255,255,.35); animation: none }
  @keyframes pulse { 70% { box-shadow: 0 0 0 10px rgba(48,209,88,0) } 100% { box-shadow: 0 0 0 0 rgba(48,209,88,0) } }
  .bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 12px }
  .bars i { width: 2.5px; background: #8ccbff; border-radius: 2px; animation: eq .9s ease-in-out infinite }
  .bars i:nth-child(2){animation-delay:.12s} .bars i:nth-child(3){animation-delay:.24s}
  .bars i:nth-child(4){animation-delay:.36s} .bars i:nth-child(5){animation-delay:.48s}
  @keyframes eq { 0%,100% { height:3px } 50% { height:12px } }

  .feed { flex: 1; overflow-y: auto; margin-top: 12px; padding: 4px 4px 4px 2px;
          display: flex; flex-direction: column; gap: 9px; }
  .feed::-webkit-scrollbar { width: 4px }
  .feed::-webkit-scrollbar-thumb { background: rgba(255,255,255,.18); border-radius: 3px }
  .line { animation: rise .32s cubic-bezier(.22,.9,.3,1) both; max-width: 84%; }
  @keyframes rise { from { opacity:0; transform: translateY(8px) } }
  .name { font-size: 11px; color: var(--faint); margin: 0 0 3px 12px }
  .bubble { padding: 10px 14px; border-radius: 20px; font-size: 15px; line-height: 1.42;
            background: var(--them); border: 1px solid rgba(255,255,255,.10);
            border-bottom-left-radius: 7px; }
  .agent { align-self: flex-end }
  .agent .name { text-align: right; margin: 0 12px 3px 0 }
  .agent .bubble { background: var(--agent); border-color: rgba(120,200,255,.34);
                   border-bottom-left-radius: 20px; border-bottom-right-radius: 7px }
  .owner { align-self: flex-end }
  .owner .name { text-align: right; margin: 0 12px 3px 0 }
  .owner .bubble { background: rgba(48,209,88,.28); border-color: rgba(70,220,120,.36);
                   border-bottom-left-radius: 20px; border-bottom-right-radius: 7px }
  .system { align-self: center; max-width: 92%; text-align: center; font-size: 12px;
            color: var(--faint); padding: 1px 0 }
  .memory { align-self: center; max-width: 94%; font-size: 12.5px; line-height: 1.45;
            padding: 8px 13px; border-radius: 14px; color: rgba(215,235,255,.92);
            background: rgba(120,170,255,.13); border: 1px solid rgba(140,190,255,.26) }
  .memory b { color: rgba(170,210,255,.95); font-weight: 600 }
  .memory .miss { color: rgba(255,190,140,.95) }

  .ask { flex: none; margin-top: 10px; padding: 15px 16px; border-radius: 22px;
         background: rgba(255,159,10,.15); border: 1px solid rgba(255,175,60,.42);
         animation: rise .3s both; }
  .ask .q { font-size: 14.5px; line-height: 1.45; margin-bottom: 11px }
  .ask .q span { display: block; font-size: 11px; letter-spacing: .5px; text-transform: uppercase;
                 color: rgba(255,200,120,.95); margin-bottom: 5px }
  .opts { display: flex; flex-wrap: wrap; gap: 7px }
  .opts button { font: inherit; font-size: 13.5px; padding: 8px 14px; border-radius: 999px;
                 color: var(--text); cursor: pointer;
                 background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.24); }
  .opts button:hover { background: rgba(255,255,255,.26) }

  .composer { flex: none; display: flex; gap: 8px; align-items: center; margin-top: 12px }
  .composer input {
    flex: 1; min-width: 0; font: inherit; font-size: 14.5px; color: var(--text);
    padding: 11px 16px; border-radius: 999px; outline: none;
    background: rgba(255,255,255,.12); border: 1px solid var(--stroke);
  }
  .composer input::placeholder { color: var(--faint) }
  .composer input:focus { border-color: rgba(140,200,255,.55); background: rgba(255,255,255,.17) }
  .composer button { font: inherit; font-size: 14px; padding: 11px 16px; border-radius: 999px;
                     cursor: pointer; color: var(--text);
                     background: rgba(255,255,255,.14); border: 1px solid var(--stroke) }
  .composer button:hover { background: rgba(255,255,255,.24) }

  footer { flex: none; display: flex; align-items: center; gap: 10px; margin-top: 12px }
  .rec { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--dim) }
  .rec b { width: 7px; height: 7px; border-radius: 50%; background: #ff453a;
           animation: pulse 1.9s infinite }
  .grow { flex: 1 }
  .lat { font-size: 12px; color: var(--faint) }
  .end { font: inherit; font-size: 15px; font-weight: 600; color: #fff; cursor: pointer;
         padding: 12px 22px; border-radius: 999px; border: none;
         background: linear-gradient(180deg, #ff5f57, #e03a30);
         box-shadow: 0 6px 18px rgba(224,58,48,.38) }
  .end:disabled { opacity: .35; cursor: default; box-shadow: none }
  .task {
    flex: none; display: block; margin-top: 12px; padding: 12px 14px; border-radius: 20px;
    background: rgba(255,255,255,.09); border: 1px solid var(--stroke);
  }
  .task .title { font-size: 13px; color: var(--dim); margin-bottom: 8px; letter-spacing: .2px }
  .task label { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--dim) }
  .task .line1 { display: flex; gap: 8px; margin-top: 8px }
  .task textarea {
    width: 100%; min-height: 96px; resize: vertical; font: inherit; color: var(--text);
    padding: 10px 12px; border-radius: 12px; margin-top: 8px; border: 1px solid var(--stroke);
    background: rgba(255,255,255,.1);
  }
  .task textarea::placeholder, .task input::placeholder { color: var(--faint) }
  .task input {
    width: 100%; font: inherit; font-size: 14px; color: var(--text);
    padding: 10px 12px; border-radius: 12px; border: 1px solid var(--stroke);
    background: rgba(255,255,255,.1);
  }
  .task .line1 input, .task .line2 input { width: 100%; }
  .task .meta {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px;
  }
  .task .row { margin-top: 8px; display: flex; gap: 8px; align-items: center; }
  .task .row button {
    font: inherit; font-size: 14px; padding: 10px 16px; border-radius: 999px; border: 1px solid var(--stroke);
    color: var(--text); cursor: pointer; background: rgba(255,255,255,.12)
  }
  .task .row button:hover { background: rgba(255,255,255,.22) }
  .error { color: #ff7c76; font-size: 12px; margin-top: 6px; min-height: 16px }
  .empty { margin: auto; text-align: center; color: var(--faint); font-size: 14px; padding: 0 20px }
  [hidden] { display: none !important }
</style></head>
<body>
  <div class="sky"><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div></div>
  <main class="phone">
    <header>
      <div class="tag"><b></b><span id="tag">No call</span></div>
      <h1 id="company">Waiting…</h1>
      <div class="sub" id="objective">Nothing is on the air right now.</div>
    </header>

    <div class="status">
      <span class="dot off" id="dot"></span><span id="phase">Idle</span>
      <span class="bars" id="bars" hidden><i></i><i></i><i></i><i></i><i></i></span>
    </div>

    <div class="feed" id="feed">
      <div class="empty" id="empty">The transcript appears here, line by line, as people speak.</div>
    </div>

    <div class="ask" id="ask" hidden>
      <div class="q"><span>Your decision</span><span id="question"></span></div>
      <div class="opts" id="opts"></div>
    </div>

    <div class="task" id="taskCard">
      <div class="title">Start a role-play test</div>
      <textarea id="taskPrompt" placeholder="Describe the task, like: 'Call Silicon Valley clinic and set up my annual check-up appointment for next Wednesday.'"></textarea>
      <div class="line1">
        <input id="recipient" placeholder="Recipient role (e.g. clinic representative)" value="clinic representative">
      </div>
      <div class="line1">
        <input id="customerName" placeholder="Your name (who the agent represents)" value="Ansh">
      </div>
      <textarea id="facts" placeholder="Optional facts to share, one per line: key: value, or paste JSON"></textarea>
      <div class="row">
        <label><input type="checkbox" id="record" checked> Keep local recording</label>
        <button id="startRoleplay">Start role-play</button>
      </div>
      <div class="error" id="taskError"></div>
    </div>

    <div class="composer">
      <input id="reply" placeholder="Type to tell the agent what to do…" autocomplete="off">
      <button id="send">Send</button>
      <button id="remember" title="Save this as a fact the agent can look up on any call">Remember</button>
    </div>

    <footer>
      <span class="rec" id="rec" hidden><b></b> Recording</span>
      <span class="lat" id="latency"></span>
      <span class="grow"></span>
      <button class="end" id="end" disabled>End call</button>
    </footer>
  </main>
<script>
const $ = (id) => document.getElementById(id);
const feed = $("feed");
const PHASES = {
  preparing: ["Preparing", "thinking"], routing: ["Routing audio", "thinking"],
  dialing: ["Dialing", "thinking"], ringing: ["Ringing", "thinking"],
  talking: ["Listening", ""], ended: ["Call ended", "off"], prepared: ["Ready", "off"],
  idle: ["Idle", "off"],
};
let cursor = 0, seen = new Set(), callId = null, askedFor = null, live = false;

function bubble(event) {
  if (seen.has(event.seq)) return;
  seen.add(event.seq);
  $("empty")?.remove();
  const row = document.createElement("div");
  if (event.speaker === "memory") {
    row.className = "line memory";
    const found = (event.hits || 0) > 0;
    const label = document.createElement("b");
    label.textContent = found ? "Looked up " : "Looked up ";
    const query = document.createElement("span");
    query.textContent = "“" + (event.query || "") + "” — ";
    const answer = document.createElement("span");
    answer.className = found ? "" : "miss";
    answer.textContent = event.text;
    row.append(label, query, answer);
  } else if (event.speaker === "system") {
    row.className = "line system";
    row.textContent = event.text;
  } else {
    const who = event.speaker === "agent" ? "Agent"
              : event.speaker === "owner" ? "You" : "Them";
    row.className = "line " + (event.speaker === "agent" ? "agent"
                             : event.speaker === "owner" ? "owner" : "them");
    const name = document.createElement("div");
    name.className = "name"; name.textContent = who;
    const body = document.createElement("div");
    body.className = "bubble"; body.textContent = event.text;
    row.append(name, body);
  }
  feed.append(row);
  feed.scrollTop = feed.scrollHeight;
}

function showTaskError(message) {
  $("taskError").textContent = message || "";
}

function parseFacts(text) {
  const raw = text.trim();
  if (!raw) return {};
  const tryJson = () => {
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("Facts JSON must be an object.");
    }
    const values = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (!key) continue;
      values[key] = String(value);
    }
    return values;
  };

  try {
    return tryJson();
  } catch (e) {
    const values = {};
    for (const line of raw.split("\n")) {
      const row = line.trim();
      if (!row) continue;
      const sep = row.includes(":") ? ":" : row.includes("=") ? "=" : "";
      if (!sep) throw new Error("Each fact line should be `key: value` or `key = value`.");
      const i = row.indexOf(sep);
      const key = row.slice(0, i).trim();
      const value = row.slice(i + 1).trim();
      if (!key) throw new Error("Each fact line needs a key.");
      values[key] = value;
    }
    return values;
  }
}

function setTaskMode(active) {
  $("taskCard").hidden = active;
  $("reply").disabled = !active;
  $("send").disabled = !active;
}

async function post(path, payload, requireCall = true) {
  const body = { ...payload };
  if (requireCall) {
    if (!callId) {
      throw new Error("No active role-play session.");
    }
    body.call_id = callId;
  }
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.error) {
    throw new Error(result.error || `Request failed (${response.status}).`);
  }
  tick();
  return result;
}

function renderNoCall() {
  $("company").textContent = "Waiting…";
  $("objective").textContent = "Nothing is on the air right now.";
  $("tag").textContent = "No call";
  $("phase").textContent = "Idle";
  $("dot").className = "dot off";
  $("bars").hidden = true;
  $("rec").hidden = true;
  $("latency").textContent = "";
  $("end").textContent = "Stop test";
  $("reply").placeholder = "Type to tell the agent what to do…";
  setTaskMode(false);
  const empty = $("empty");
  if (feed.childElementCount === 0 && !empty) {
    const blank = document.createElement("div");
    blank.className = "empty";
    blank.id = "empty";
    blank.textContent = "The transcript appears here, line by line, as people speak.";
    feed.append(blank);
  }
  $("ask").hidden = true;
  $("end").disabled = true;
  askedFor = null;
}

function renderAsk(decision) {
  const box = $("ask");
  if (!decision) {
    box.hidden = true;
    askedFor = null;
    return;
  }
  const question = decision.question;
  if (!question || askedFor === question) return;
  askedFor = question;
  $("question").textContent = question;
  const opts = $("opts");
  opts.replaceChildren();
  (decision.options || []).forEach((choice) => {
    const button = document.createElement("button");
    button.textContent = choice;
    button.onclick = () => { box.hidden = true; post("/api/decide", { answer: choice }).catch((error) => showTaskError(error.message)); };
    opts.append(button);
  });
  box.hidden = false;
  $("reply").placeholder = "Or type your own answer…";
  $("reply").focus();
}

async function startTask() {
  const task = $("taskPrompt").value.trim();
  const recipientRole = $("recipient").value.trim();
  const customerName = $("customerName").value.trim();
  const factText = $("facts").value.trim();
  const start = $("startRoleplay");

  if (!task) {
    showTaskError("Please enter a task.");
    return;
  }
  if (!recipientRole) {
    showTaskError("Please enter a recipient role.");
    return;
  }
  if (!customerName) {
    showTaskError("Please enter your name.");
    return;
  }

  let facts;
  try {
    facts = parseFacts(factText);
  } catch (error) {
    showTaskError(error.message);
    return;
  }

  start.disabled = true;
  start.textContent = "Starting…";
  showTaskError("");

  try {
    const started = await post(
      "/api/start_task",
      {
        task,
        recipient_role: recipientRole,
        customer_name: customerName,
        facts,
        record: $("record").checked,
      },
      false
    );
    callId = started.call_id || null;
    cursor = 0;
    seen.clear();
    feed.replaceChildren();
    await tick();
  } catch (error) {
    showTaskError(error.message);
  }

  start.disabled = false;
  start.textContent = "Start role-play";
}

function send() {
  const field = $("reply");
  const text = field.value.trim();
  if (!text || !callId) return;
  field.value = "";
  const target = askedFor ? "/api/decide" : "/api/guidance";
  const payload = askedFor ? { answer: text } : { text };
  post(target, payload).catch((error) => showTaskError(error.message));
  $("ask").hidden = true;
}

function remember() {
  const field = $("reply");
  const text = field.value.trim();
  if (!text) return;
  field.value = "";
  post("/api/memory", { text }, false)
    .then((saved) => showTaskError("Remembered. " + (saved.records || 0) + " facts stored."))
    .catch((error) => showTaskError(error.message));
}

$("startRoleplay").onclick = startTask;
$("remember").onclick = remember;
$("send").onclick = send;
$("reply").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
$("end").onclick = () => { $("end").disabled = true; post("/api/hangup", {}).catch((error) => showTaskError(error.message)); };

async function tick() {
  let state;
  try { state = await (await fetch("/api/state?after=" + cursor)).json(); }
  catch (e) { return; }                       // the service may be restarting
  if (!state.call_id) {
    callId = null;
    cursor = 0;
    seen.clear();
    renderNoCall();
    return;
  }
  if (!state.live) {
    setTaskMode(false);
  } else {
    setTaskMode(true);
  }
  if (!state.live && callId && callId !== state.call_id) {
    cursor = 0;
    seen.clear();
    feed.replaceChildren();
    $("ask").hidden = true;
    askedFor = null;
  }
  if (state.call_id !== callId && state.live) {
    callId = state.call_id;
    cursor = 0;
    seen.clear();
    feed.replaceChildren();
    $("ask").hidden = true;
    askedFor = null;
  }
  if (state.call_id === callId && state.live) {
    $("taskCard").hidden = true;
  }

  const head = state.header || {};
  $("company").textContent = head.company || "Call";
  $("objective").textContent = head.objective || "";
  $("tag").textContent = head.mode === "roleplay" ? "Role-play · no call placed"
                       : head.mode === "demo" ? "Rehearsal · no call placed"
                       : (head.number || "Live call");

  live = !!state.live;
  let [text, kind] = PHASES[state.phase] || ["Connected", ""];
  const talk = (state.conversation || {}).phase;
  if (state.phase === "talking" && talk === "responding") [text, kind] = ["Thinking", "thinking"];
  if (state.phase === "talking" && talk === "speaking") [text, kind] = ["Speaking", "speaking"];
  if (state.decision) [text, kind] = ["Waiting for you", "waiting"];
  if (!live) [text, kind] = ["Call ended", "off"];
  $("phase").textContent = text;
  $("dot").className = "dot " + kind;
  $("bars").hidden = kind !== "speaking";
  $("rec").hidden = !state.recording;
  $("latency").textContent = state.last_latency != null ? state.last_latency.toFixed(2) + "s reply" : "";
  $("end").disabled = !state.can_control;
  $("end").textContent = head.mode === "roleplay" ? "Stop test" : "End call";
  if (!live) {
    $("taskCard").hidden = false;
    setTaskMode(false);
  }

  (state.events || []).forEach(bubble);
  cursor = state.cursor;
  renderAsk(state.decision);
}
renderNoCall();
tick();
setInterval(tick, 400);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload).encode(), "application/json", status)

    def _call(self, operation: str, **arguments) -> None:
        try:
            self._json(request(operation, **arguments))
        except (RuntimeError, ValueError) as exc:
            self._json({"error": str(exc)[:300]}, 400)

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route.path == "/api/state":
            after = parse_qs(route.query).get("after", ["0"])[0]
            try:
                self._json(request("live_state", after_seq=int(after)))
            except (RuntimeError, ValueError) as exc:
                self._json({"call_id": None, "error": str(exc)[:200]})
        else:
            self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "Malformed request."}, 400)
            return
        if route == "/api/start_task":
            task = payload.get("task", "")
            recipient_role = payload.get("recipient_role", "")
            customer_name = payload.get("customer_name", "")
            facts = payload.get("facts", {})
            if not isinstance(task, str) or not task.strip():
                self._json({"error": "Task is required."}, 400)
                return
            if not isinstance(recipient_role, str) or not recipient_role.strip():
                self._json({"error": "recipient_role is required."}, 400)
                return
            if not isinstance(customer_name, str) or not customer_name.strip():
                self._json({"error": "customer_name is required."}, 400)
                return
            if not isinstance(facts, dict):
                self._json({"error": "facts must be a JSON object."}, 400)
                return
            try:
                plan = request(
                    "roleplay_from_task",
                    task=task.strip(),
                    recipient_role=recipient_role.strip(),
                    customer_name=customer_name.strip(),
                    facts={str(k): str(v) for k, v in facts.items() if k},
                )
            except (RuntimeError, ValueError) as exc:
                self._json({"error": str(exc)[:300]}, 400)
                return
            call_id = plan.get("call_id")
            plan_id = plan.get("plan_id")
            if not isinstance(call_id, str) or not isinstance(plan_id, str):
                self._json({"error": "Could not prepare the scenario."}, 500)
                return
            try:
                started = request(
                    "test_start",
                    call_id=call_id,
                    plan_id=plan_id,
                    record=bool(payload.get("record", True)),
                    audio_mode="speakers",
                    controller="local",
                )
            except (RuntimeError, ValueError) as exc:
                self._json({"error": str(exc)[:300]}, 400)
                return
            self._json(started)
            return

        if route == "/api/memory":
            text = payload.get("text", "")
            if not isinstance(text, str) or not text.strip():
                self._json({"error": "Nothing to remember."}, 400)
                return
            self._call("memory_add", text=text, kind="fact", source="live view")
            return

        call_id = payload.get("call_id")
        if not isinstance(call_id, str):
            self._json({"error": "No call selected."}, 400)
            return
        if route == "/api/guidance":
            self._call("guidance", call_id=call_id, text=str(payload.get("text", "")))
        elif route == "/api/decide":
            self._call("decide", call_id=call_id, answer=str(payload.get("answer", "")))
        elif route == "/api/hangup":
            # Role-play owns no telephone line; stopping the test is its hangup.
            try:
                state = request("live_state", after_seq=0)
                roleplay = (state.get("header") or {}).get("mode") == "roleplay"
            except (RuntimeError, ValueError):
                roleplay = False
            self._call("test_stop" if roleplay else "hangup", call_id=call_id)
        else:
            self.send_error(404)

    def log_message(self, *args):  # keep the terminal clean for the call itself
        pass


def start_viewer(open_browser: bool = True) -> tuple[ThreadingHTTPServer, str]:
    """Serve the live view on localhost and return the server plus its URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        subprocess.run(["open", url], check=False, capture_output=True, timeout=10)
    return server, url
