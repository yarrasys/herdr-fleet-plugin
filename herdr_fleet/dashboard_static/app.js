/* Signal Box: live observations and deliberate owner actions. No terminal input path. */
"use strict";
(function () {
  let capability = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const $ = function (id) { return document.getElementById(id); };
  const state = {csrf: null, data: null, fleet: null, seat: null, targetFleet: null, tab: "terminal",
    question: null, reply: null, drafts: new Map(), intent: null, watch: null, streamKey: null,
    first: true, seen: new Set(), motion: !matchMedia("(prefers-reduced-motion: reduce)").matches,
    expanded: false, collapsed: false, messageMailbox: "owner", tick: null,
    motionChosen: false, unseen: new Map(), ownerSeen: new Set(), tally: null, flights: 0, authEpoch: 0};
  const term = new Terminal({disableStdin: true, cursorBlink: false, convertEol: false, scrollback: 1000,
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', fontSize: 12,
    theme: {background: "#02050a", foreground: "#dcebf4", cursor: "#5fd8ff"},
    allowProposedApi: false, rightClickSelectsWord: true,
    windowOptions: {}, linkHandler: {activate: function () {}},
    clipboardProvider: {readText: function () { return Promise.resolve(""); }, writeText: function () { return Promise.resolve(); }}});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("terminal-screen"));
  const helper = $("terminal-screen").querySelector(".xterm-helper-textarea");
  if (helper) { helper.readOnly = true; helper.tabIndex = -1; helper.setAttribute("aria-hidden", "true"); }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function notice(text) { $("notice").textContent = text; }
  function status(text) {
    const demo = state.data && state.data.demo;
    $("terminal-status").textContent = (demo && !/^DEMO/.test(text) ? "DEMO · " : "") + text;
  }
  function fail(error) { notice(error.message || "Connection unavailable. Reopen the local launch address."); }
  async function api(path, body, signal) {
    const headers = {"X-Fleet-CSRF": state.csrf};
    const options = {headers: headers, cache: "no-store", credentials: "same-origin", signal: signal};
    if (body !== undefined) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options);
    if (!response.ok) {
      const data = await response.json().catch(function () { return {}; });
      throw new Error(data.error || "Request failed. Refresh before a deliberate new attempt.");
    }
    return response;
  }
  function currentFleet() { return state.data && state.data.fleets.find(function (f) { return f.id === state.fleet; }); }
  function targetFleet() { return state.data && state.data.fleets.find(function (f) { return f.id === state.targetFleet; }); }
  function currentSeat() { const f = targetFleet(); return f && f.seats.find(function (s) { return s.name === state.seat; }); }
  function draftKey() { return [state.fleet, state.targetFleet, state.seat, state.reply || ""].join("|"); }
  function saveDraft() { if (state.fleet) state.drafts.set(draftKey(), {text: $("draft").value, intent: state.intent}); }
  function restoreDraft() {
    const draft = state.drafts.get(draftKey());
    $("draft").value = draft ? draft.text : ""; state.intent = draft ? draft.intent : null;
    $("new-attempt").hidden = !state.intent;
    $("action-result").textContent = state.intent && state.intent.description ? state.intent.description : "A confirmed delivery is not acceptance of work.";
  }
  function selectedChanged(fleetId, name) {
    const moved = fleetId !== state.targetFleet || name !== state.seat;
    saveDraft();
    if (moved) { disconnect("Watch disconnected: selected target changed. Screen cleared."); term.reset(); }
    state.targetFleet = fleetId; state.seat = name; state.reply = null;
    restoreDraft(); render();
  }
  function lamp(status) {
    const item = node("span", "", "lamp " + (["working", "blocked", "idle", "done", "absent", "question"].includes(status) ? status : "unknown"));
    item.setAttribute("aria-label", status); return item;
  }
  function fleetCode(label) {
    const words = String(label).trim().split(/\s+/).filter(Boolean);
    return (words.length > 1 ? words.map(function (w) { return w[0]; }).join("").slice(0, 3) : String(label).slice(0, 3)).toUpperCase();
  }
  function questionsOf(fleet) { return fleet.owner.filter(function (m) { return !m.fyi; }); }
  function selectFleet(id) {
    if (state.fleet === id) return;
    const fleet = state.data.fleets.find(function (f) { return f.id === id; }); if (!fleet) return;
    saveDraft(); disconnect("Watch disconnected: fleet changed. Screen cleared."); term.reset(); $("travel").replaceChildren();
    state.fleet = fleet.id; state.targetFleet = fleet.id; state.seat = fleet.seats[0] ? fleet.seats[0].name : null;
    state.question = null; state.reply = null; $("owner-detail").hidden = true;
    restoreDraft(); render(); refreshTab().catch(fail);
  }
  /* Where a name sits along the shown fleet's line: a seat's lamp, the owner at the column edge, anyone else at the far end. */
  function anchor(name) {
    if (name === "owner") return 0;
    const seat = Array.from($("seats").children).find(function (b) { return b.dataset.seat === name; });
    const wrap = $("travel").getBoundingClientRect();
    if (!seat || !wrap.width) return 1;
    const lampBox = seat.querySelector(".lamp").getBoundingClientRect();
    return Math.max(0, Math.min(1, (lampBox.left + lampBox.width / 2 - wrap.left) / wrap.width));
  }
  function ring(name, kind) {
    if (!state.motion) return;
    const item = node("span", "", "ring " + kind); item.style.left = anchor(name) * 100 + "%";
    $("travel").append(item);
    const run = item.animate([{transform: "scale(1)", opacity: .95}, {transform: "scale(5)", opacity: 0}], {duration: 900, easing: "ease-out"});
    const done = function () { item.remove(); }; run.finished.then(done, done);
  }
  /* A logged send crosses the line carrying its own id. Cosmetic only: the receipt text is already on screen. */
  function travel(event, landed) {
    if (!state.motion || state.flights >= 6) { if (landed) ring(event.to, landed); return; }
    const capsule = node("span", "", "capsule" + (event.from === "owner" || event.to === "owner" ? " owner" : ""));
    capsule.append(node("i"), node("b", "#" + event.id.slice(-6)));
    const from = anchor(event.from) * 100 + "%", to = anchor(event.to) * 100 + "%";
    capsule.style.left = to; $("travel").append(capsule); state.flights++;
    const run = capsule.animate([{left: from}, {left: to}], {duration: 1300, easing: "cubic-bezier(.45,.05,.3,1)"});
    const done = function () { state.flights = Math.max(0, state.flights - 1); capsule.remove(); if (landed) ring(event.to, landed); };
    run.finished.then(done, done);
  }
  function bump(item) {
    if (state.motion && item) item.animate([{transform: "scale(1)"}, {transform: "scale(1.28)", filter: "brightness(1.7)"}, {transform: "scale(1)"}], {duration: 700, easing: "ease-out"});
  }
  function logBadge() {
    const count = state.unseen.get(state.fleet) || 0;
    $("log-count").hidden = !count || state.tab === "log"; $("log-count").textContent = String(count);
  }
  function button(text, action) { const b = node("button", text); b.type = "button"; b.addEventListener("click", action); return b; }
  function ago(value) {
    const stamp = Date.parse(value); if (!Number.isFinite(stamp)) return "time unknown";
    const seconds = Math.max(0, Math.floor((Date.now() - stamp) / 1000));
    return seconds < 60 ? seconds + "s ago" : Math.floor(seconds / 60) + "m ago";
  }
  function render() {
    const focusedKey = document.activeElement && document.activeElement.dataset.focusKey;
    const data = state.data; if (!data) return;
    const fleets = data.fleets;
    $("mode").textContent = data.demo ? "DEMO · Authored synthetic run · No live actions" : "LIVE · Authenticated local owner";
    $("persistent-mode").textContent = $("mode").textContent;
    $("replay").hidden = !data.demo;
    const tally = fleets.reduce(function (n, f) { return n + questionsOf(f).length; }, 0);
    $("attention-count").textContent = String(tally);
    if (state.tally !== null && state.tally !== tally) bump($("attention-count"));
    state.tally = tally;
    const navigation = fleets.map(function (fleet, index) {
      const asks = questionsOf(fleet).length, fresh = state.unseen.get(fleet.id) || 0;
      const b = button("", function () { selectFleet(fleet.id); });
      b.className = "fleet-button" + (fleet.stale ? " stale" : "") + (asks ? " hold" : "");
      b.dataset.focusKey = "fleet:" + fleet.id;
      b.setAttribute("aria-current", String(state.fleet === fleet.id));
      b.setAttribute("aria-label", fleet.label + " · " + (fleet.stale ? "stale" : "observed " + ago(fleet.observed_at)) +
        " · " + asks + " owner questions · " + fleet.seats.map(function (s) { return s.name + " " + s.state; }).join(", "));
      b.title = fleet.label;
      const top = node("span", "", "fleet-top");
      top.append(node("span", String(index + 1).padStart(2, "0"), "fleet-idx"), node("strong", fleet.label, "fleet-name"), node("span", fleetCode(fleet.label), "fleet-code"));
      const signals = node("span", "", "fleet-signals");
      fleet.seats.slice(0, 7).forEach(function (s) { signals.append(lamp(s.state)); });
      if (asks) signals.append(lamp("question"));
      const meta = node("span", (fleet.stale ? "Stale · " : "") + ago(fleet.observed_at) + " · " + asks + " owner", "fleet-meta" + (asks ? " hot" : ""));
      b.append(top, signals, meta);
      if (fresh && state.fleet !== fleet.id) b.append(node("span", fresh + " new", "fleet-new"));
      return b;
    });
    $("fleets").replaceChildren.apply($("fleets"), navigation);
    const fleet = currentFleet();
    if (!fleet) { $("fleet-title").textContent = "No registered fleets"; document.title = (data.demo ? "DEMO" : "LIVE") + " · Signal Box"; return; }
    $("fleet-title").textContent = fleet.label;
    document.title = (data.demo ? "DEMO · " : "LIVE · ") + fleet.label + " · Signal Box";
    $("freshness").textContent = (fleet.stale ? "STALE · " : "Observed ") + (fleet.observed_at ? new Date(fleet.observed_at).toLocaleTimeString() + " · " + ago(fleet.observed_at) : "unknown");
    $("watcher").textContent = fleet.watcher && fleet.watcher.running === true ? "Watcher process running" :
      fleet.watcher && fleet.watcher.running === false ? "Watcher stopped · no automatic startup" : "Watcher liveness unknown · no automatic startup";
    if (fleet.warning) notice(fleet.warning);
    const questions = fleet.owner.map(function (message) {
      const row = node("div", "", "question-row" + (message.fyi ? " report" : "")); const label = node("span");
      const words = node("span", message.sender + (message.fyi ? " reports" : " asks you"));
      words.append(node("small", "#" + message.id.slice(-6) + " · " + ago(message.time) + (message.fyi ? " · FYI: the seat's own report, not a checked result" : " · plain message, no preset choices")));
      label.append(lamp(message.fyi ? "idle" : "question"), words);
      const inspect = button(message.fyi ? "Inspect report" : "Inspect question", function () { inspectQuestion(message).catch(fail); });
      if (!message.fyi) inspect.className = "attention";
      inspect.dataset.focusKey = "question:" + message.id;
      row.append(label, inspect);
      if (!state.first && !state.ownerSeen.has(message.id) && state.motion) row.animate([{opacity: 0, transform: "translateX(-22px)"}, {opacity: 1, transform: "none"}], {duration: 700, easing: "cubic-bezier(.2,.7,.2,1)"});
      state.ownerSeen.add(message.id);
      return row;
    });
    $("questions").replaceChildren.apply($("questions"), questions.length ? questions : [node("p", "No queued owner messages in this observation.", "empty")]);
    $("questions").parentNode.classList.toggle("is-empty", !questions.length && $("owner-detail").hidden);
    const asking = questionsOf(fleet).length > 0;
    $("seat-line").classList.toggle("hold", asking && !fleet.stale);
    $("seat-line").classList.toggle("stale", !!fleet.stale);
    const seats = fleet.seats.map(function (seat) {
      const b = button("", function () { selectedChanged(fleet.id, seat.name); });
      b.className = "seat " + (seat.state === "working" && seat.waiting ? "waiting" : seat.state);
      b.setAttribute("aria-pressed", String(state.seat === seat.name && state.targetFleet === fleet.id));
      b.dataset.focusKey = "seat:" + fleet.id + ":" + seat.name; b.dataset.seat = seat.name;
      const card = node("span", "", "card");
      card.append(node("strong", seat.name), node("small", seat.kind + " · " + seat.state + (seat.waiting ? " · waiting on its inbox" + (data.demo ? " (authored)" : "") : ""), "state"),
        node("small", seat.queued + " queued · " + seat.fyi + " FYI"));
      if (seat.context) card.append(node("small", String(seat.context.tokens) + (seat.context.window ? "/" + seat.context.window : "") + " context tokens"));
      if (seat.limited) card.append(node("small", "Recorded provider limit · delivery may be held"));
      b.append(lamp(seat.state), card);
      return b;
    });
    $("seats").replaceChildren.apply($("seats"), seats);
    const target = currentSeat();
    const demoHold = !!(data.demo && data.hold && state.reply);
    $("target-label").textContent = target ? target.name + " · " + target.state : "Choose a seat";
    $("watch").disabled = !target || !target.view || fleet.stale;
    $("send").disabled = !target || !target.view || fleet.stale || (targetFleet() && targetFleet().stale) || (data.demo && !demoHold) ||
      !!(state.intent && (state.intent.pending || (state.intent.result && state.intent.result.certainty === "confirmed")));
    $("new-attempt").disabled = !!(state.intent && state.intent.pending);
    $("send").textContent = demoHold ? "Record sample reply · Demo" : "Send owner message";
    $("read-question").disabled = data.demo || !state.question;
    $("compose-target").textContent = (state.reply ? "Reply #" + state.reply.slice(-6) + " → " : "To ") + (target ? target.name : "no selected seat") +
      (demoHold ? " · DEMO: recorded in this browser only. Nothing is sent, and the authored run continues as scripted."
        : data.demo ? " · Demo does not send live messages." : " · owner attribution · ordinary guarded delivery");
    $("clear-reply").hidden = !state.reply; $("read-with-reply-label").hidden = !state.reply || data.demo;
    $("composer-title").parentNode.parentNode.classList.toggle("replying", !!state.reply);
    $("demo-stages").hidden = !data.demo;
    $("demo-answer").hidden = !(data.demo && data.hold);
    if (data.demo) {
      const evidence = data.stage_evidence || [];
      $("demo-stages").replaceChildren.apply($("demo-stages"), data.stages.map(function (name, index) {
        const kind = index < data.stage ? "done" : index > data.stage ? "todo" : data.hold ? "hold" : "current";
        const item = node("li", "", "demo-stage " + kind);
        item.append(node("span", "", "node"), node("span", "DEMO " + (index + 1) + " · " + name, "k"),
          node("span", kind === "done" ? (evidence[index] ? "closed on #" + evidence[index].slice(-6) : "closed") : kind === "hold" ? "waiting for you" : kind === "current" ? "under way" : "", "sub"));
        return item;
      }));
    }
    logBadge();
    if (state.streamKey && (!target || state.streamKey !== target.view)) disconnect("Seat binding changed; refresh and reconnect explicitly.");
    if (focusedKey) {
      const replacement = Array.from(document.querySelectorAll("[data-focus-key]")).find(function (item) { return item.dataset.focusKey === focusedKey; });
      if (replacement) replacement.focus({preventScroll: true});
    }
  }
  async function refresh() {
    if (!state.csrf || document.hidden) return;
    const epoch = state.authEpoch;
    try {
      const previous = state.data; const data = await (await api("/api/snapshot")).json();
      if (epoch !== state.authEpoch) return;
      state.data = data;
      if (!data.fleets.some(function (f) { return f.id === state.fleet; })) {
        saveDraft(); if (state.fleet) disconnect("Registered fleet unavailable.");
        state.fleet = data.fleets[0] ? data.fleets[0].id : null; state.targetFleet = state.fleet;
        state.seat = data.fleets[0] && data.fleets[0].seats[0] ? data.fleets[0].seats[0].name : null;
        restoreDraft();
      }
      render();
      const events = [];
      data.fleets.forEach(function (f) { (f.events || []).forEach(function (e) {
        const key = [e.id, e.kind, e.time, e.from, e.to].join("|");
        if (!state.first && !state.seen.has(key)) events.push([f.id, e]);
        state.seen.add(key);
      }); });
      while (state.seen.size > 1024) state.seen.delete(state.seen.values().next().value);
      if (events.length) {
        const last = events[events.length - 1][1];
        notice(last.from + " → " + last.to + " · " + (last.delivery || last.kind) + " · #" + last.id.slice(-6));
        const sent = new Set();
        events.forEach(function (pair) {
          const fleetId = pair[0], e = pair[1];
          state.unseen.set(fleetId, (state.unseen.get(fleetId) || 0) + 1);
          if (fleetId !== state.fleet) return;
          if (e.kind === "send") { sent.add(e.id); travel(e, e.delivery === "queued" ? "queued" : null); }
          else if (e.kind === "delivered") {
            if (sent.has(e.id) && state.motion) setTimeout(function () { ring(e.to, "delivered"); }, 1300); else ring(e.to, "delivered");
          }
        });
        bump($("log-count")); render();
        if (state.tab === "log") refreshTab().catch(fail);
      }
      if (!state.first && previous) {
        const before = new Map();
        previous.fleets.forEach(function (f) { f.seats.forEach(function (x) { before.set(f.id + "|" + x.name, x.state); }); });
        const shown = currentFleet();
        if (shown) shown.seats.forEach(function (x) {
          const old = before.get(shown.id + "|" + x.name);
          if (old !== undefined && old !== x.state) ring(x.name, x.state === "working" ? "working" : x.state === "blocked" ? "blocked" : "state");
        });
      }
      state.first = false;
    } catch (error) { if (epoch === state.authEpoch) { notice("Disconnected · last observation retained. " + error.message); $("freshness").textContent = "DISCONNECTED · last observation retained"; } }
  }
  async function list(mode, name, id) {
    let query = "?fleet=" + encodeURIComponent(state.fleet) + "&mode=" + mode + "&limit=50";
    if (name) query += "&name=" + encodeURIComponent(name);
    if (id) query += "&id=" + encodeURIComponent(id);
    return (await api("/api/list" + query)).json();
  }
  async function inspectQuestion(message) {
    const result = await list("messages", "owner", message.id);
    const item = result.items[0]; if (!item) throw new Error("Owner message unavailable; refresh.");
    state.question = item; $("question-sender").textContent = item.sender + " · #" + item.id.slice(-6) + " · " + item.box + "/";
    $("question-body").textContent = item.body + (item.body_truncated ? "\n[Body truncated]" : "");
    $("owner-detail").hidden = false; $("read-question").disabled = state.data.demo;
    $("reply-question").focus();
  }
  async function refreshTab() {
    if (state.tab === "log") {
      state.unseen.set(state.fleet, 0); logBadge();
      const result = await list("log");
      const lines = result.items.map(function (e) { return [e.time, e.from + " → " + e.to, e.kind, e.delivery || e.via || "", e.id ? "#" + e.id.slice(-6) : "", e.first_line || ""].join("  "); });
      $("log-output").textContent = lines.join("\n") + (result.truncated ? "\n[Recent page truncated; inspect the CLI log for older entries.]" : "");
    } else if (state.tab === "messages") {
      const result = await list("messages", state.messageMailbox === "owner" ? "owner" : state.seat);
      const children = result.items.map(function (m) {
        const item = node("article", "", "message-item");
        item.append(node("strong", m.sender + " → " + m.to + " · #" + m.id.slice(-6) + " · " + m.box + "/"),
          node("pre", m.body + (m.body_truncated ? "\n[Body truncated]" : ""), "message-body"));
        return item;
      });
      if (!children.length) children.push(node("p", "No stored messages in this page.", "empty"));
      if (result.truncated) children.push(node("p", "Recent page truncated; older history is available through the CLI.", "empty"));
      $("message-output").replaceChildren.apply($("message-output"), children);
    }
  }
  function tab(name) {
    state.tab = name;
    ["terminal", "log", "messages"].forEach(function (n) { $("tab-" + n).setAttribute("aria-selected", String(n === name)); $(n + "-panel").hidden = n !== name; });
    logBadge();
    if (name !== "terminal") disconnect("Watch disconnected while the terminal is hidden.");
    else scheduleViewport();
    refreshTab().catch(fail);
  }
  function disconnect(message) {
    if (state.watch) state.watch.abort(); state.watch = null; state.streamKey = null;
    status(message || "Watch disconnected. Output retained.");
  }
  async function watch() {
    const target = currentSeat(); if (!target || !target.view) return;
    disconnect(); term.reset();
    fit.fit();
    const dims = fit.proposeDimensions() || {cols: 80, rows: 24};
    const controller = new AbortController(); state.watch = controller; state.streamKey = target.view;
    state.viewport = {cols: Math.max(20, Math.min(300, dims.cols)), rows: Math.max(5, Math.min(120, dims.rows))};
    status("Connecting Watch · read-only viewport · handle native dialogs in Herdr");
    const key = target.view;
    try {
      const response = await api("/api/watch", {fleet: state.targetFleet, seat: target.name, view: target.view,
        cols: state.viewport.cols, rows: state.viewport.rows}, controller.signal);
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let pending = "";
      while (true) {
        const next = await reader.read(); if (next.done) break;
        pending += decoder.decode(next.value, {stream: true});
        if (pending.length > 1048576) throw new Error("Watch buffer limit exceeded.");
        let index;
        while ((index = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, index); pending = pending.slice(index + 1);
          if (!line) continue;
          const frame = JSON.parse(line);
          if (state.streamKey !== key || state.watch !== controller) return;
          if (frame.type === "frame") {
            if (frame.full) { term.reset(); }
            if (term.cols !== frame.cols || term.rows !== frame.rows) term.resize(frame.cols, frame.rows);
            const raw = atob(frame.bytes); const bytes = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            term.write(bytes);
            $("terminal-status").textContent = (state.data.demo ? "DEMO · synthetic ANSI" : "WATCH CONNECTED · read only") +
              " · " + target.name + " · frame " + frame.seq + (target.state === "blocked" ? " · Handle this native dialog in Herdr" : "");
          } else {
            status(frame.message || "Watch stream changed; reconnect explicitly.");
          }
        }
      }
      if (state.watch === controller) { state.watch = null; state.streamKey = null; status($("terminal-status").textContent + (state.data.demo ? " · Demo stream closed" : " · stream ended; agent exit is not inferred")); }
    } catch (error) {
      if (state.watch === controller && error.name !== "AbortError") { disconnect("Watch unavailable or disconnected · " + error.message); }
    }
  }
  function uniqueIntent() { return crypto.randomUUID(); }
  let resizeTimer = null;
  function scheduleViewport() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (state.tab !== "terminal") return;
      const dims = fit.proposeDimensions();
      if (!dims) return;
      const cols = Math.max(20, Math.min(300, dims.cols)), rows = Math.max(5, Math.min(120, dims.rows));
      if (state.watch && state.viewport && (cols !== state.viewport.cols || rows !== state.viewport.rows)) {
        status("Watch reconnecting for a new read-only viewport; fresh full frame required.");
        watch();
      } else if (!state.watch) fit.fit();
    }, 200);
  }
  new ResizeObserver(scheduleViewport).observe($("terminal-screen"));
  async function send() {
    const target = currentSeat(); if (!target) return;
    if (state.data.demo) { demoReply(); return; }
    const text = $("draft").value;
    if (!text.trim()) { notice("Enter a plain-text message first."); return; }
    const body = {action: "send", source: state.fleet, fleet: state.targetFleet, seat: target.name,
      view: target.view, text: text};
    if (state.reply) { body.reply_to = state.reply; body.read = $("read-with-reply").checked; }
    const fingerprint = JSON.stringify(body);
    if (state.intent && state.intent.fingerprint !== fingerprint) {
      notice("This draft has a pending or uncertain intent. Inspect receipts, then prepare a deliberate new attempt."); return;
    }
    if (!state.intent) state.intent = {id: uniqueIntent(), fingerprint: fingerprint};
    if (state.intent.pending || (state.intent.result && state.intent.result.certainty === "confirmed")) return;
    // The retained object belongs to this exact draft, even when the owner changes
    // fleet/seat/reply or edits text before the HTTP response arrives.
    const origin = draftKey(), intent = state.intent;
    intent.pending = true;
    body.intent = intent.id; saveDraft(); $("send").disabled = true; $("new-attempt").disabled = true;
    try {
      const result = await (await api("/api/action", body)).json();
      intent.result = result;
      intent.description = target.name + " · " + result.status + (result.id ? " · #" + result.id.slice(-6) : "") +
        " · " + result.certainty + (result.replayed ? " · existing intent result" : "") + " · delivery is not work acceptance";
      await refresh();
    } catch (error) {
      intent.description = target.name + " · Result uncertain or rejected · " + error.message + " Draft and intent retained.";
    } finally {
      intent.pending = false;
      if (draftKey() === origin && state.intent === intent) {
        $("action-result").textContent = intent.description;
        $("new-attempt").hidden = false;
      }
      render();
    }
  }
  async function demoReply() {
    if (!state.data.demo || !state.data.hold || !state.reply) return;
    const typed = $("draft").value.trim().length;
    if (!typed) { notice("Type a sample reply first. It stays in this browser."); return; }
    $("send").disabled = true;
    try {
      await (await api("/api/demo/reply", {})).json();
      $("action-result").textContent = "DEMO · sample reply recorded in this browser only (" + typed + " characters). Nothing was sent. The authored run continues.";
      $("draft").value = ""; saveDraft(); state.reply = null; state.question = null; $("owner-detail").hidden = true; restoreDraft();
      await refresh();
    } catch (error) { $("action-result").textContent = "DEMO · the Demo server did not accept the reply action · " + error.message; render(); }
  }
  $("demo-answer").addEventListener("click", function () {
    const fleet = state.data.fleets.find(function (f) { return questionsOf(f).length; }); if (!fleet) return;
    selectFleet(fleet.id);
    inspectQuestion(questionsOf(fleet)[0]).then(function () {
      $("reply-question").click();
      if (!$("draft").value) { $("draft").value = "Fix the two tests first, then merge."; saveDraft(); }
      render(); $("draft").focus();
    }).catch(fail);
  });
  $("collapse").addEventListener("click", function () {
    state.collapsed = !state.collapsed; $("app").classList.toggle("collapsed", state.collapsed);
    $("collapse").textContent = state.collapsed ? "Expand fleets" : "Collapse fleets";
    $("collapse").setAttribute("aria-expanded", String(!state.collapsed));
  });
  $("expand").addEventListener("click", function () {
    state.expanded = !state.expanded; $("app").classList.toggle("expanded", state.expanded);
    $("expand").textContent = state.expanded ? "Restore terminal" : "Expand terminal";
    $("expand").setAttribute("aria-expanded", String(state.expanded));
    $("esc-hint").hidden = !state.expanded;
    $("grip").setAttribute("aria-disabled", String(state.expanded)); $("grip").tabIndex = state.expanded ? -1 : 0;
    scheduleViewport();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !state.expanded) return;
    const tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    $("expand").click(); $("expand").focus();
  });
  function motion() {
    document.body.classList.toggle("motion-off", !state.motion);
    $("motion").setAttribute("aria-checked", String(state.motion));
    $("motion-state").textContent = state.motion ? "on" : "off";
    if (!state.motion) $("travel").replaceChildren();
  }
  $("motion").addEventListener("click", function () { state.motion = !state.motion; state.motionChosen = true; motion(); }); motion();
  matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", function (event) {
    if (!state.motionChosen) { state.motion = !event.matches; motion(); }
  });
  $("replay").addEventListener("click", function () {
    $("travel").replaceChildren(); state.ownerSeen.clear();
    $("action-result").textContent = "A confirmed delivery is not acceptance of work.";
    api("/api/demo/replay", {}).then(function (response) { return response.json(); }).then(refresh).catch(fail);
  });
  ["terminal", "log", "messages"].forEach(function (name) { $("tab-" + name).addEventListener("click", function () { tab(name); }); });
  ["terminal", "log", "messages"].forEach(function (name, index, names) {
    $("tab-" + name).addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const next = names[(index + (event.key === "ArrowRight" ? 1 : 2)) % names.length];
      tab(next); $("tab-" + next).focus();
    });
  });
  $("watch").addEventListener("click", watch);
  $("disconnect").addEventListener("click", function () { disconnect(); });
  $("refit").addEventListener("click", function () { if (state.watch) watch(); else notice("Open Watch before refitting its read-only viewport."); });
  $("copy").addEventListener("click", function () {
    const lines = []; for (let i = 0; i < term.buffer.active.length; i++) lines.push(term.buffer.active.getLine(i).translateToString(true));
    navigator.clipboard.writeText(lines.join("\n")).then(function () { notice("Copied terminal screen text."); }).catch(fail);
  });
  $("refresh-log").addEventListener("click", function () { refreshTab().catch(fail); });
  $("watch-log").addEventListener("click", function () { list("watch-log").then(function (r) { $("log-output").textContent = r.items.join("\n"); }).catch(fail); });
  $("owner-history").addEventListener("click", function () { state.messageMailbox = "owner"; refreshTab().catch(fail); });
  $("seat-history").addEventListener("click", function () { state.messageMailbox = "seat"; refreshTab().catch(fail); });
  $("refresh-messages").addEventListener("click", function () { refreshTab().catch(fail); });
  $("close-question").addEventListener("click", function () { $("owner-detail").hidden = true; state.question = null; render(); });
  $("reply-question").addEventListener("click", function () {
    if (!state.question) return;
    const matches = []; state.data.fleets.forEach(function (f) { f.seats.forEach(function (s) { if (s.name === state.question.sender) matches.push([f.id, s.name]); }); });
    if (matches.length !== 1) { notice("The question sender is not a unique registered seat."); return; }
    selectedChanged(matches[0][0], matches[0][1]); saveDraft(); state.reply = state.question.id; restoreDraft(); render(); $("draft").focus();
  });
  $("read-question").addEventListener("click", async function () {
    if (!state.question || state.data.demo) return;
    const button = $("read-question"); button.disabled = true;
    try {
      const result = await (await api("/api/action", {action: "read", source: state.fleet,
        message: state.question.id, intent: uniqueIntent()})).json();
      notice("Selected owner message: " + result.status + " · reading does not acknowledge it."); await refresh();
    } catch (error) { fail(error); } finally { button.disabled = state.data.demo; }
  });
  $("clear-reply").addEventListener("click", function () { saveDraft(); state.reply = null; restoreDraft(); render(); });
  $("draft").addEventListener("input", saveDraft);
  $("send").addEventListener("click", send);
  $("new-attempt").addEventListener("click", function () {
    if (state.intent && state.intent.pending) return;
    state.intent = null; saveDraft(); $("new-attempt").hidden = true;
    notice("New attempt prepared. Verify previous receipts before pressing Send."); render();
  });
  let drag = null;
  function height(value) {
    const pixels = Math.max(200, Math.min(900, value));
    $("terminal-screen").style.setProperty("--terminal-height", pixels + "px");
    $("grip").setAttribute("aria-valuenow", String(pixels));
  }
  $("grip").addEventListener("pointerdown", function (event) { if (state.expanded) return; drag = {y: event.clientY, height: $("terminal-screen").clientHeight}; $("grip").setPointerCapture(event.pointerId); });
  $("grip").addEventListener("pointermove", function (event) { if (drag && !state.expanded) height(drag.height + event.clientY - drag.y); });
  $("grip").addEventListener("pointerup", function () { drag = null; });
  $("grip").addEventListener("keydown", function (event) { if (state.expanded) return; if (event.key === "ArrowUp" || event.key === "ArrowDown") { event.preventDefault(); height($("terminal-screen").clientHeight + (event.key === "ArrowUp" ? -24 : 24)); } });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { state.first = true; refresh(); } });
  window.addEventListener("pagehide", function () { disconnect(); clearInterval(state.tick); clearTimeout(resizeTimer); });
  async function bootstrap(supplied) {
    const launch = supplied || capability;
    capability = null;
    if (!launch) { notice("Owner session required. Reopen the complete launch URL printed by this dashboard process."); return; }
    const epoch = ++state.authEpoch;
    clearInterval(state.tick); state.tick = null; state.csrf = null;
    disconnect("Watch disconnected: reauthenticating owner session.");
    const response = await fetch("/api/session", {method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({capability: launch})});
    const result = await response.json(); if (!response.ok) throw new Error(result.error || "Session exchange failed.");
    if (epoch !== state.authEpoch) return;
    state.csrf = result.csrf; notice(result.demo ? "DEMO · Synthetic authored content. No live APIs or owner sends." : "Owner session established. Live observations do not mark messages read.");
    await refresh(); if (epoch === state.authEpoch) state.tick = setInterval(refresh, 2000);
  }
  window.addEventListener("hashchange", function () {
    const pasted = location.hash.slice(1);
    history.replaceState(null, "", location.pathname);
    if (!pasted) return;
    return bootstrap(pasted).catch(fail);
  });
  bootstrap().catch(function (error) { capability = null; fail(error); });
}());
