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

    <div class="composer">
      <input id="reply" placeholder="Type to tell the agent what to do…" autocomplete="off">
      <button id="send">Send</button>
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
  if (event.speaker === "system") {
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

async function post(path, payload) {
  if (!callId) return;
  await fetch(path, { method: "POST", headers: {"Content-Type": "application/json"},
                      body: JSON.stringify({ call_id: callId, ...payload }) });
  tick();
}

function renderAsk(decision) {
  const box = $("ask");
  if (!decision) { box.hidden = true; askedFor = null; return; }
  if (askedFor === decision.question) return;
  askedFor = decision.question;
  $("question").textContent = decision.question;
  const opts = $("opts");
  opts.replaceChildren();
  (decision.options || []).forEach((choice) => {
    const button = document.createElement("button");
    button.textContent = choice;
    button.onclick = () => { box.hidden = true; post("/api/decide", { answer: choice }); };
    opts.append(button);
  });
  box.hidden = false;
  $("reply").placeholder = "Or type your own answer…";
  $("reply").focus();
}

function send() {
  const field = $("reply");
  const text = field.value.trim();
  if (!text || !callId) return;
  field.value = "";
  post(askedFor ? "/api/decide" : "/api/guidance", askedFor ? { answer: text } : { text });
  $("ask").hidden = true;
}
$("send").onclick = send;
$("reply").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
$("end").onclick = () => { $("end").disabled = true; post("/api/hangup", {}); };

async function tick() {
  let state;
  try { state = await (await fetch("/api/state?after=" + cursor)).json(); }
  catch (e) { return; }                       // the service may be restarting
  if (!state.call_id) return;
  if (state.call_id !== callId) { callId = state.call_id; cursor = 0; seen.clear(); feed.replaceChildren(); }

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
  $("reply").disabled = !state.can_control;

  (state.events || []).forEach(bubble);
  cursor = state.cursor;
  renderAsk(state.decision);
}
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
